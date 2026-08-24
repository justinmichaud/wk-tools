# HANDOFF — bench-mode benchmarking: the provenance the runs still lack

A managed system, deployed to external media, that a machine boots into for
benchmarking: no sandboxing inside, driven remotely, for maximum perf stability.
Two lanes now produce real numbers — the Mac's (`wk bench mac-ab`,
`docs/HANDOFF-mac-perf-mode.md`) and the rpi4's (`wk pi bench`,
`docs/HANDOFF-pi-deploy.md`) — and the fields that would keep those numbers
apart do not exist.

Every run records `bench_host`; `bench_host=image` means the whole machine in
bench mode, which is the only kind that means anything for perf.

## Remaining

- **Four provenance fields, none of them implemented.** Verified by grep: none
  of these appears in `cmd/bench`.
  - **`kernel_provenance`** — stock or custom. Perf results represent what
    customers ship, so a bench system runs a **stock kernel**; the rpi5's
    custom `7.0.6-numa` kernel is workstation-only, and older numbers taken on
    it are not a baseline for the image series.
  - **`kernel_arch`** — the width of the *kernel*, beside the `arch` of the
    build. They are not the same fact, and recording only the second makes two
    incomparable runs look like one series: an armhf build on an armhf kernel
    and an armhf build on an arm64 kernel both report `arch=armhf`. The fleet
    has boards where both are physically possible.
  - **`profile`** — `stock` or `oc` (opt-in overclock), plus the fan policy.
    Fan-max is measurement hygiene, not tuning: it keeps a run out of thermal
    throttle. The two profiles must never merge into one series. Nothing sets
    an `oc` profile yet either (`host/linux/rpi5/HANDOFF.md`).
  - **`root_device`** — model, link speed, TRIM/rotational. Cheap flash
    contributes variance rather than a subtractable bias, so USB-stick runs are
    provisional and not comparable with SSD runs. A RAM root would be its own
    value again (`docs/Urgent/HANDOFF-moose-bench.md`).

  Runs differing in any of these are **two series**, the same rule as
  `bench_host`. This matters now rather than in principle: the rpi4's
  Speedometer came off a USB stick with pixman software compositing, and the
  Mac's off a second APFS volume.
- **On-board runs are not in the store at all.** `wk pi bench` prints its result
  and files nothing, so `wk bench ls`/`compare` cannot see it. One piece of work
  with the fields above.
- **No pre-run quiesce preflight.** `wk quiesce status` measures rather than
  trusts, but nothing refuses a run on an install that is still indexing itself.
  The Mac lane proved the stronger version: a software-update scan ran through a
  measured arm while every preference said updates were off. Detection landed
  there (a per-arm `clean`/`scanned` column); the refusal did not.
- **moose has no bench mode** — `docs/Urgent/HANDOFF-moose-bench.md`. The
  constraint it inherits from here still holds: an image run needs a *second*
  machine to hold the runner, and for moose that cannot be moose.
- **The rpi5 is a workstation, not a test device** (decided 2026-08-18): its own
  `./setup`, full tailnet privileges, podman workspaces. It never goes through
  `wk pi setup`, and benchmarking it means booting a bench system whose identity
  is its own (`rpi5-perf.local`, mDNS, because the system carries no tailscale).

## The refusal that is code, not documentation

The content origin for a measured run must resolve to **loopback**, and nothing
in the run path may be a network mount. `run-benchmark` serving the payload from
its own http server satisfies this; a URL in a browser does not.
