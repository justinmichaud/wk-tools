# HANDOFF — a per-device connectivity and perf subcommand

Not started; today the work exists only as `host/linux/rpi5/rpi5-setup.sh`, hand-run for one machine.

- [ ] decide the boundary with `docs/HANDOFF-settings-audit.md` (which non-default settings get persisted at all) before building this [decision]
- [ ] add a setup subcommand for all devices that tests wifi channels, reliability, and autosleep [decision]
- [ ] carry perf fixes and configs for devices that support them (the rpi5 workstation overclock) in the same subcommand [decision]
- [ ] make `./setup` report the status of these fixes without running them by default [decision]
- [ ] move sudo access for these fixes into the subcommand instead of scattered inline calls [decision]
