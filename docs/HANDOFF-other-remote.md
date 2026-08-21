# HANDOFF — remote targets that are not Linux

Open task, not started.

Support macOS and Windows build machines as remote targets, and a VM rented
from a cloud provider — provisioned from nothing and used bare, with no
containers or virtualization on it.

The bare shape matters beyond convenience: it is the perf-testing shape. A
machine with no sandbox has no sandboxing overhead in the measurement, and a
rented one holds no PII, so nothing needs protecting from the workload.

Where to start: this is the driver contract of `docs/HANDOFF-linux-remote.md`
against a machine that is not Linux. `targets/remote.sh` assumes Linux in
three places — `t_ccache_dir`, the `nice`/`ionice` pair in `t_exec_build`,
and the `/proc` reads (`loadavg`, `meminfo`) in `_remote_probe`.
