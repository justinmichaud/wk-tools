# HANDOFF — remote targets that are not Linux

Not started. Support Windows build machines as remote targets, and a VM
rented from a cloud provider — provisioned from nothing and used bare, with
no containers or virtualization on it.

The bare shape matters beyond convenience: it is the perf-testing shape. A
machine with no sandbox has no sandboxing overhead in the measurement, and a
rented one holds no PII, so nothing needs protecting from the workload.

Also good for new contributors

## Remaining

`targets/remote.sh` does not assume Linux: `_remote_probe_cmd` and
`_remote_probe_parse` branch on the remote's own `uname -s`
(`nproc`/`/proc/loadavg`/`/proc/meminfo` vs. `sysctl -n hw.ncpu`/`sysctl -n
vm.loadavg`/`vm_stat`), `t_exec_build` uses `ionice` only when the probe found
one on the far machine, and `t_ccache_dir` was already a plain POSIX path.
Proven against the three real Linux remotes (buildbox4, devbox-arm64-2,
moose); the macOS/BSD side is exercised only by `_remote_probe_parse` on
captured samples (`tests/test_remote_driver.py`) -- no macOS or BSD remote is
available to verify against live.

Then: a real macOS/BSD remote to verify against, Windows support, the bare
cloud VM, and the PII-free perf-testing case. None attempted.
