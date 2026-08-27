# HANDOFF — one image model, one bench pipeline

Owed work, in dependency order.

- [ ] declare the Mac cases as configs (`image/configs/perf-macos-tolken.conf`, `perf-macos-benchvm.conf`, `IMG_BUILDER=macvolume|macvm`) so `image_profile_load` covers them
- [ ] fold bench-volume creation/provisioning and the VM golden base into `wk sysimage build` builders, one marker writer (`marker_file`), one manifest [needs the Mac bench volume]
- [ ] replace `cmd/pi pi_deploy_tar`'s ssh+tar path with `wk sysimage write --to <machine>` [needs a Pi card in hand]
- [ ] replace `mac-ab.sh`'s planted LaunchAgent arming with a `BOOT_ARMING=no-network` mode of `wk boot`, sharing `boot_wait_for_mode` in boot/machines.sh [needs a Pi card in hand and the Mac bench volume]
- [ ] collapse `wk pi bench`, `wk bench staged`, `bench/mac-lane.sh`, `bench/mac-ab.sh` into `wk bench run <machine> <plan>` dispatching on the driver, one A/B slot mechanism [needs a Pi card in hand and the Mac bench volume]
- [ ] collapse `cmd_run`/`cmd_staged`/`pi_bench_record` into one `wk bench record`; make `wk bench report` the only reader
