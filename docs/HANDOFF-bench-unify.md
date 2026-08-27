# HANDOFF — one image model, one bench pipeline

Owed work, in dependency order. Each step names its size and the hardware
that verifies it. The duplication it removes: three env.json writers, three
"artifact finished" markers, four write-to-media paths, two Mac drivers beside
the generic stage/boot/staged chain, two A/B slot mechanisms.

- [ ] declare the Mac cases as configs: `image/configs/perf-macos-tolken.conf`,
      `perf-macos-benchvm.conf` (`IMG_BUILDER=macvolume|macvm`), so every image
      the fleet boots is a conf and `image_profile_load` covers all of them (S)
- [ ] one build entry: fold `bench/mac-bench-volume.sh --create/--fetch/
      --install/--provision` and the VM golden base (`targets/vm.sh
      _provision_base`) into `wk sysimage build` builders; one marker writer
      (`marker_file`), one manifest, no `/etc/wk-image` via `sudo tee` and no
      `base.ready` (M; a Mac)
- [ ] one write: `cmd/pi pi_deploy_tar` becomes `wk sysimage write --to
      <machine>` (network transport), not a second ssh+tar path (M; a Pi)
- [ ] one arm: `mac-ab.sh`'s planted LaunchAgent + `kern.boottime` wait
      becomes a `BOOT_ARMING=no-network` mode of `wk boot`, its wait a shared
      `boot_wait_for_mode` in boot/machines.sh (S; a Mac + a Pi)
- [ ] one run: `wk pi bench`, `wk bench staged`, `bench/mac-lane.sh`,
      `bench/mac-ab.sh` collapse into `wk bench run <machine> <plan>`
      dispatching on the driver (`b_bench_root`, `BOOT_ARMING`); one A/B slot
      mechanism (L; rpi3/4/5 + Mac)
- [ ] one record/report: `cmd_run`, `cmd_staged`, `pi_bench_record` call one
      `wk bench record`; `wk bench report` (HTML, histograms, score and time,
      variance per configuration) is the only reader (M; container run)
- [ ] the transient distro perf image for rpi5/moose (`IMG_BUILDER=distro`)
      does not exist; `image/profiles.sh` tombstones `perf-linux-*` to the
      yocto images. Decide whether it is wanted before building it (decision
      in docs/defects)
