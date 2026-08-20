After everything else is done:

We should look into generalizing this. Think about what this would look like if I worked on other project too, like chromium, llvm, or smaller side projects.

LifeKit:

this command handles my home tasks and network topology, setting up bmc, home-server tasks

TargetKit:

this handles tasks related to multiple machines, containers, vms, perf machine roles, provisioning bench devices, running benchmarks in general

WebKitKit: (alias wk)

this handles webkit-specific tasks like building and debugging. These tasks should all work without a target if you do target setup --one-off or something like that. They also plug in to the target kit to run benchmarks for WebKit.
