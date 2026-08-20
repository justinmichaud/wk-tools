Look at the current repo, and where it is going.

Find places where command verbs are ambiguous, or there is overlapping functionality. Make sure that roles, devices, and targets are all super clear, and that commands are consistent.

Do research on terminal tooling best practices. Make it easy for humans and llms to use this, and make it consistent.

Separate out daily webkit tasks, perf tasks, one-time setup tasks, and ongoing maintenance tasks. Make sure that each command orients the user clearly. Make sure there is always one calculatable source of truth for everything, and no state except permenent configs in this repo. All devices I ever use for wk development should be added here and configured before being used. Make it easy to track devices that change role, like a workstation that can be booted for perf testing. Walk through common workflows in your head (don't actually run them), like uploading a pr to github using git-webkit, and see how it fits into the workflow of having sandboxed dev environments that claude can drive automatically, but where I can still push, rebase, fetch origin or fork, download forks from other people, and upload prs myself. This should also handle edge cases, like making a pr in an armhf container where git-webkit can't run.

Think about the perf workflow. I would set aside a device, and leave it for days to get perf data for me. Claude might drive these tasks too, but needs to do that inside a sandbox.

All of this comes together with roles. We need a precise vocabulary for the role of a device, a target, a container, etc. We need good nouns and verbs for all of this so that it is never confusing. wk status should always start by telling you what role your machine is in, and what place you are in right now (workstation, target, container, vm etc.) and this should match the saved config files synced across all devices. This is a path. So you are on a workstation, and then you might provision a bench device. You are on a bench device, and you might run a test run of WebKit. You are on a workstation, and you want to collect a perf run from a bench device. Names should have short prefixes including their roles to make this unambiguous what kind of patient can be acted upon by which agent command. Do research on this patient and agent metaphor for talking about action. Machines should really be phisical machine + current state, also representing who controls the machine (wk or human), and making sure that a workstation rebooted into the perf os does not get treated the same way as the same machine running the workstation os.

Provisioning a new bench device from scratch should have an interactive command that automates everything that can be automated.

Look at handoff-vocabulary to see what make sense to take from that agent.

Make sure that the rpi hardware layout is also clear. SD card should have what kind of image, what kind of USB, WIFI vs ethernet, and make sure that this is compatable with my bmc->tailnet-bridge renaming idea.

Think about the debugging workflow, or interacting with MiniBrowser.

Think about ways to guarantee maximum build perf.

Clean up tools that are no longer needed after changes to these workflows.

Your task is to first do some research and prepare a plan. Remove handoffs that are already done. Then execute the plan.
