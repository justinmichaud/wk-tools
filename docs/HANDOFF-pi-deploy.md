# HANDOFF — `wk pi deploy` and `wk pi bench`

## Remaining

- [ ] run `wk pi deploy <profile> <board> --slot <name>` and `wk pi bench <board> <plan> --ab base,<pr>` against a board booted into a buildroot 2.38 image: the tunnel, the `wk-board` driver's launch of cog, the running-binary check, and the html report have run only in unit tests [needs a board booted into wpewebkit-2.38-buildroot-rpi{3,4}-32]
- [ ] run a yocto slot the same way (`wk pi deploy yocto-<profile> <board> --slot b`, MiniBrowser under weston): the yocto cross build must link with `--build-id` or the manifest refuses it [needs a yocto cross build and a board]
- [ ] run a real-display bench on the rpi4 [needs a Pi board with a display attached]
- [ ] test a new boot cmdline arg on the SD (unarmed fall-through) before risking it on the bench stick [needs a Pi board]
- [ ] reproduce the rpi4's bench stick with `wk sysimage write` instead of by hand [needs a Pi board and a confirmed erase]
