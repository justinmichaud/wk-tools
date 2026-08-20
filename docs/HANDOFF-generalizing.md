We are doing this now. 

See handoff-architecture-review, handoff cleanup-1, and handoff-vocabulary.

We should look into generalizing this. Think about what this would look like if I worked on other project too, like chromium, llvm, or smaller side projects.

1) Do research on what other webkit contributers have posted about their setups, ides, debugging routes, etc.

2) What about chrome, llvm, linux, etc contributors?

3) Think about how this would work for people only working on the windows port, or only working on apple platforms. How would iOS fit in to this in the future?

4) How would this work if there was an external service for perf testing? What about people that only do remote development? Can I support a properly sandboxed workflow that I can hand off to new contributors, say on a vps, separate from my own?

5) What about people working only on downstream ports? For example, in 5 years, someone wants to fix a bug and do a perf test on mips on WPE 2.22. This repo should be easy to extend for that case.

Come up with some broader command names. For example:

LifeKit:

this command handles my home tasks and network topology, setting up bmc, home-server tasks. This is specific to me, other people might want to extend this more.

TargetKit:

this handles tasks related to multiple machines, containers, vms, perf machine roles, provisioning bench devices, running benchmarks in general. This might be more rigid, fewer customizations.

This also handles cross builds, and qemu for platforms like riscv and armv7 (on x86 boxes or arm64 boxes that can't do 32-bit containers)

WebKitKit: (alias wk)

this handles webkit-specific tasks like building and debugging. These tasks should all work without a target if you do target setup --one-off or something like that, useful for handing to new contributors. They also plug in to the target kit to run benchmarks for WebKit. They should use upstream webkit build/debug tools as much as possible, sysprof stitching, etc. Mostly this is for plug and play into the other tools above, and to document / test the webkit workflows automatically on every system.

WebKitAnalysisKit:

This handles using mya (see @mendolorian's PR), crashpad/breakpad, and other analysis tools to collect and analyze webkit crash reports from the field and performance data. We could extend this to support debian crash dumps too, for example

SandboxKit:

This handles running webkit, chrome, etc in a sandboxed environment that is as close as possible to a stock repo without any fuss. Ex: building debian tarbals, or testing maintainers patches.

Undecided:

Where can we fit in tasks like building debian tarbals or doing chrome vs webkit performance comparisons.

The main goal is to think about all of the users and use cases, do research on other users,

For naming, we should brainstorm this so it isn't ambiguous. Maybe we can name these after places in Galicia, or find a more unix-y name so that they are easy to remember and understand, but can grow organically.
