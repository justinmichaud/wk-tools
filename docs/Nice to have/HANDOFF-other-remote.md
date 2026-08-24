# HANDOFF — remote targets that are not Linux

Not started. Support macOS and Windows build machines as remote targets, and a
VM rented from a cloud provider — provisioned from nothing and used bare, with
no containers or virtualization on it.

The bare shape matters beyond convenience: it is the perf-testing shape. A
machine with no sandbox has no sandboxing overhead in the measurement, and a
rented one holds no PII, so nothing needs protecting from the workload.

## Remaining

This is the remote-target driver contract (`targets/remote.sh`, proven against
Linux build machines) run against a machine that is not Linux. That file assumes
Linux in exactly three places, all still present:

- `t_ccache_dir` (line 359) — a POSIX path under the remote root;
- `nice -n 19 ionice -c3` in `t_exec_build` (line 592);
- `_remote_probe` reading `/proc/loadavg` and `/proc/meminfo` (lines 210-211).

Then: the bare cloud VM, and the PII-free perf-testing case. Neither attempted.
