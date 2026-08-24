# HANDOFF — a per-device connectivity and perf subcommand

Not started as a general mechanism. The work exists for exactly one machine and
only as a hands-on script: `host/linux/rpi5/rpi5-setup.sh` does overclock
(`arm_freq`/`v3d_freq`/`over_voltage_delta`), fan, `wifi.powersave=2`, the
CYW43455 roam-engine disable, a pinned BSSID (both AP radios are DFS) and a wifi
watchdog, calling `sudo` inline throughout.

## Remaining

- A **setup subcommand for all devices** that tests wifi and connectivity: wifi
  channels, reliability, autosleep.
- The same subcommand carries **perf fixes and configs for devices that support
  them** (the rpi5 workstation overclocks).
- **`./setup` checks the status of these fixes and does not run them by
  default** — it reports, and tells you how to run the subcommand. No stage in
  the current list (`tools settings dotfiles claude mcp sharing machine vmtools
  softnet sdk benchkey quiesce`) does this.
- **sudo access goes in the subcommand too**, not scattered inline.

Decide the boundary with `docs/HANDOFF-settings-audit.md` first — which
non-default settings get persisted into the repo at all — so this does not
become a second mechanism for the same question.
