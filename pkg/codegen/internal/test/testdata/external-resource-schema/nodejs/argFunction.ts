// *** WARNING: this file was generated by test. ***
// *** Do not edit by hand unless you're certain you know what you are doing! ***

import * as pulumi from "@pulumi/pulumi";
import { input as inputs, output as outputs } from "./types";
import * as utilities from "./utilities";

import * as pulumiRandom from "@pulumi/random";

export function argFunction(args?: ArgFunctionArgs, opts?: pulumi.InvokeOptions): Promise<ArgFunctionResult> {
    args = args || {};
    if (!opts) {
        opts = {}
    }

    if (!opts.version) {
        opts.version = utilities.getVersion();
    }
    return pulumi.runtime.invoke("example::argFunction", {
        "name": args.name,
    }, opts);
}

export interface ArgFunctionArgs {
    name?: pulumiRandom.RandomPet;
}

export interface ArgFunctionResult {
    readonly age?: number;
}
