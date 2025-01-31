# Copyright 2016-2021, Pulumi Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Define gRPC plumbing to expose a custom user-defined `Provider`
instance as a gRPC server so that it can be used as a Pulumi plugin.

"""

from typing import Dict, List, Set, Optional, TypeVar, Any, cast
import argparse
import asyncio
import sys

import grpc
import grpc.aio

from pulumi.provider.provider import Provider, CallResult, ConstructResult
from pulumi.resource import Resource, DependencyResource, DependencyProviderResource
from pulumi.runtime import known_types, proto, rpc
from pulumi.runtime.proto import provider_pb2_grpc, ResourceProviderServicer
from pulumi.runtime.stack import wait_for_rpcs
import pulumi
import pulumi.resource
import pulumi.runtime.config
import pulumi.runtime.settings


# _MAX_RPC_MESSAGE_SIZE raises the gRPC Max Message size from `4194304` (4mb) to `419430400` (400mb)
_MAX_RPC_MESSAGE_SIZE = 1024 * 1024 * 400
_GRPC_CHANNEL_OPTIONS = [('grpc.max_receive_message_length', _MAX_RPC_MESSAGE_SIZE)]


class ProviderServicer(ResourceProviderServicer):
    """Implements a subset of `ResourceProvider` methods to support
    `Construct` and other methods invoked by the engine when the user
    program creates a remote `ComponentResource` (with `remote=true`
    in the constructor).

    See `ResourceProvider` defined in `provider.proto`.

    """

    engine_address: str
    provider: Provider
    args: List[str]
    lock: asyncio.Lock


    async def Construct(self, request: proto.ConstructRequest, context) -> proto.ConstructResponse:  # pylint: disable=invalid-overridden-method
        # Calls to `Construct` and `Call` are serialized because they currently modify globals. When we are able to
        # avoid modifying globals, we can remove the locking.
        await self.lock.acquire()
        try:
            return await self._construct(request, context)
        finally:
            self.lock.release()

    async def _construct(self, request: proto.ConstructRequest, context) -> proto.ConstructResponse:
        # pylint: disable=unused-argument
        assert isinstance(request, proto.ConstructRequest), \
            f'request is not ConstructRequest but is {type(request)} instead'

        pulumi.runtime.settings.reset_options(
            project=_empty_as_none(request.project),
            stack=_empty_as_none(request.stack),
            parallel=_zero_as_none(request.parallel),
            engine_address=self.engine_address,
            monitor_address=_empty_as_none(request.monitorEndpoint),
            preview=request.dryRun)

        pulumi.runtime.config.set_all_config(dict(request.config), request.configSecretKeys)

        inputs = await self._construct_inputs(request)

        result = self.provider.construct(name=request.name,
                                         resource_type=request.type,
                                         inputs=inputs,
                                         options=self._construct_options(request))

        response = await self._construct_response(result)

        # Wait for outstanding RPCs such as more provider Construct
        # calls. This can happen if i.e. provider creates child
        # resources but does not await their URN promises.
        #
        # Do not await all tasks as that starts hanging waiting for
        # indefinite grpc.aio servier tasks.
        await wait_for_rpcs(await_all_outstanding_tasks=False)

        return response

    @staticmethod
    async def _construct_inputs(request: proto.ConstructRequest) -> Dict[str, pulumi.Input[Any]]:

        def deps(key: str) -> Set[str]:
            return set(urn for urn in
                       request.inputDependencies.get(
                           key,
                           proto.ConstructRequest.PropertyDependencies()
                       ).urns)

        return {
            k: await ProviderServicer._create_output(the_input, deps=deps(k))
            for k, the_input in
            rpc.deserialize_properties(request.inputs, keep_unknowns=True).items()
        }

    @staticmethod
    async def _create_output(the_input: Any, deps: Set[str]) -> Any:
        is_secret = rpc.is_rpc_secret(the_input)

        # If it's a resource reference or a prompt value, return it directly without wrapping
        # it as an output.
        if await _is_resource_reference(the_input, deps) or (not is_secret and len(deps) == 0):
            return the_input

        # Otherwise, wrap it as an output so we can handle secrets
        # and/or track dependencies.
        # Note: If the value is or contains an unknown value, the Output will mark its value as
        # unknown automatically, so we just pass true for is_known here.
        return pulumi.Output(
            resources=set(DependencyResource(urn) for urn in deps),
            future=_as_future(rpc.unwrap_rpc_secret(the_input)),
            is_known=_as_future(True),
            is_secret=_as_future(is_secret))

    @staticmethod
    def _construct_options(request: proto.ConstructRequest) -> pulumi.ResourceOptions:
        parent = None
        if not _empty_as_none(request.parent):
            parent = DependencyResource(request.parent)
        return pulumi.ResourceOptions(
            aliases=list(request.aliases),
            depends_on=[DependencyResource(urn)
                        for urn in request.dependencies],
            protect=request.protect,
            providers={pkg: DependencyProviderResource(ref)
                       for pkg, ref in request.providers.items()},
            parent=parent)

    async def _construct_response(self, result: ConstructResult) -> proto.ConstructResponse:
        urn = await pulumi.Output.from_input(result.urn).future()

        # Note: property_deps is populated by rpc.serialize_properties.
        property_deps: Dict[str, List[pulumi.resource.Resource]] = {}
        state = await rpc.serialize_properties(
            inputs={k: v for k, v in result.state.items() if k not in ['id', 'urn']},
            property_deps=property_deps)

        deps: Dict[str, proto.ConstructResponse.PropertyDependencies] = {}
        for k, resources in property_deps.items():
            urns = await asyncio.gather(*(r.urn.future() for r in resources))
            deps[k] = proto.ConstructResponse.PropertyDependencies(urns=urns)

        return proto.ConstructResponse(urn=urn,
                                       state=state,
                                       stateDependencies=deps)


    async def Call(self, request: proto.CallRequest, context):  # pylint: disable=invalid-overridden-method
        # Calls to `Construct` and `Call` are serialized because they currently modify globals. When we are able to
        # avoid modifying globals, we can remove the locking.
        await self.lock.acquire()
        try:
            return await self._call(request, context)
        finally:
            self.lock.release()

    async def _call(self, request: proto.CallRequest, context):
        # pylint: disable=unused-argument
        assert isinstance(request, proto.CallRequest), \
            f'request is not CallRequest but is {type(request)} instead'

        pulumi.runtime.settings.reset_options(
            project=_empty_as_none(request.project),
            stack=_empty_as_none(request.stack),
            parallel=_zero_as_none(request.parallel),
            engine_address=self.engine_address,
            monitor_address=_empty_as_none(request.monitorEndpoint),
            preview=request.dryRun)

        pulumi.runtime.config.set_all_config(dict(request.config), request.configSecretKeys)

        args = await self._call_args(request)

        result = self.provider.call(token=request.tok, args=args)

        response = await self._call_response(result)

        # Wait for outstanding RPCs such as more provider Construct
        # calls. This can happen if i.e. provider creates child
        # resources but does not await their URN promises.
        #
        # Do not await all tasks as that starts hanging waiting for
        # indefinite grpc.aio servier tasks.
        await wait_for_rpcs(await_all_outstanding_tasks=False)

        return response

    @staticmethod
    async def _call_args(request: proto.CallRequest) -> Dict[str, pulumi.Input[Any]]:

        def deps(key: str) -> Set[str]:
            return set(urn for urn in
                       request.argDependencies.get(
                           key,
                           proto.CallRequest.ArgumentDependencies()
                       ).urns)

        return {
            k: await ProviderServicer._create_output(the_input, deps=deps(k))
            for k, the_input in
            # We need to keep_internal, to keep the `__self__` that would normally be filtered because
            # it starts with "__".
            rpc.deserialize_properties(request.args, keep_unknowns=True, keep_internal=True).items()
        }

    async def _call_response(self, result: CallResult):
        # Note: ret_deps is populated by rpc.serialize_properties.
        ret_deps: Dict[str, List[pulumi.resource.Resource]] = {}
        ret = await rpc.serialize_properties(
            inputs=result.outputs,
            property_deps=ret_deps)

        deps: Dict[str, proto.CallResponse.ReturnDependencies] = {}
        for k, resources in ret_deps.items():
            urns = await asyncio.gather(*(r.urn.future() for r in resources))
            deps[k] = proto.CallResponse.ReturnDependencies(urns=urns)

        # Since `return` is a keyword, we need to pass the args to `CallResponse` using a dictionary.
        resp = {
            'return': ret,
            'returnDependencies': deps,
        }
        if result.failures:
            resp['failures'] = [proto.CheckFailure(property=f.property, reason=f.reason) for f in result.failures]
        return proto.CallResponse(**resp)

    async def Configure(self, request, context) -> proto.ConfigureResponse:  # pylint: disable=invalid-overridden-method
        return proto.ConfigureResponse(acceptSecrets=True, acceptResources=True)

    async def GetPluginInfo(self, request, context) -> proto.PluginInfo:  # pylint: disable=invalid-overridden-method
        return proto.PluginInfo(version=self.provider.version)

    async def GetSchema(self, request: proto.GetSchemaRequest, context) -> proto.GetSchemaResponse:  # pylint: disable=invalid-overridden-method
        if request.version != 0:
            raise Exception(f'unsupported schema version {request.version}')
        schema = self.provider.schema if self.provider.schema else '{}'
        return proto.GetSchemaResponse(schema=schema)

    def __init__(self, provider: Provider, args: List[str], engine_address: str) -> None:
        super().__init__()
        self.provider = provider
        self.args = args
        self.engine_address = engine_address
        self.lock = asyncio.Lock()


def main(provider: Provider, args: List[str]) -> None:  # args not in use?
    """For use as the `main` in programs that wrap a custom Provider
    implementation into a Pulumi-compatible gRPC server.

    :param provider: an instance of a Provider subclass

    :args: command line arguiments such as os.argv[1:]

    """

    argp = argparse.ArgumentParser(description='Pulumi provider plugin (gRPC server)')
    argp.add_argument('engine', help='Pulumi engine address')
    argp.add_argument('--logflow', action='store_true', help='Currently ignored')
    argp.add_argument('--logtostderr', action='store_true', help='Currently ignored')

    engine_address: str = argp.parse_args().engine

    async def serve() -> None:
        server = grpc.aio.server(options=_GRPC_CHANNEL_OPTIONS)
        servicer = ProviderServicer(provider, args, engine_address=engine_address)
        provider_pb2_grpc.add_ResourceProviderServicer_to_server(servicer, server)
        port = server.add_insecure_port(address='0.0.0.0:0')
        await server.start()
        sys.stdout.buffer.write(f'{port}\n'.encode())
        sys.stdout.buffer.flush()
        await server.wait_for_termination()

    try:
        loop = asyncio.get_event_loop()
        try:
            loop.run_until_complete(serve())
        finally:
            loop.close()
    except KeyboardInterrupt:
        pass


T = TypeVar('T')  # pylint: disable=invalid-name


def _as_future(value: T) -> 'asyncio.Future[T]':
    fut: 'asyncio.Future[T]' = asyncio.Future()
    fut.set_result(value)
    return fut


def _empty_as_none(text: str) -> Optional[str]:
    return None if text == '' else text


def _zero_as_none(value: int) -> Optional[int]:
    return None if value == 0 else value


async def _is_resource_reference(the_input: Any, deps: Set[str]) -> bool:
    """
    Returns True if `the_input` is a Resource and only depends on itself.
    """
    return (known_types.is_resource(the_input)
        and len(deps) == 1
        and next(iter(deps)) == await cast(Resource, the_input).urn.future())
