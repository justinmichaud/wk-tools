# HANDOFF — `wk pi deploy` and `wk pi bench`: the open half

Both verbs are built and both have run on hardware: `wk pi deploy <ws> <machine>
[--slot]` and `wk pi bench <machine> <plan> [--slot|--ab|--count|--timeout]`
(`cmd/pi`), with `wk pi setup` provisioning the WebKit tree on the board.
Speedometer 3 completes on the rpi4.

## Remaining

- **`wk pi bench` prints `result after 0s`** for a run that took 22 minutes. The
  elapsed counter is wrong; the result is not. Cosmetic, and printed next to a
  number.
- **The result is printed and not saved.** Nothing files it beside `wk bench`'s
  own runs, so `wk bench ls`/`compare` cannot see an on-board result — the
  remaining half of "record provenance next to `wk bench`'s results". Two
  on-board runs cannot currently be compared to each other by any means the
  tool offers, which is most of why anyone takes a second one. Once fixed,
  `wk bench compare` needs scipy in the workspace that built the thing being
  compared — a plain yocto workspace is Ubuntu 24.04 and PEP 668 refuses a bare
  `pip3 install --user`; `--break-system-packages` with `--user` is the answer,
  since the workspace is disposable by construction.
- **`wk pi bench --ab` should compare only rounds where both arms finished.** A
  surviving arm whose partner crashed biases the comparison (a crash usually
  correlates with the heavier binary under a memory ceiling) — drop it from the
  comparison but keep it in the store, since the run that finished is still
  evidence about why its partner did not.
- **The skeleton is not a skeleton.** `wk pi setup` does a `--depth 1` clone —
  427,711 files, ~4.2 GB, slower onto a Pi's USB stick than cross-building
  WebKit was. The board only needs the scripts it runs there
  (`Tools/CISupport/built-product-archive`, `Tools/Scripts/run-benchmark`,
  `run-minibrowser` and their imports), so a sparse checkout of `Tools/`
  (~5,000 files) is the right shape.
- **A manifest can disagree with its disk**: the rpi4's still records
  `display_forced` after the mode was removed from the stick's cmdline by hand,
  so the run warns about something it is not doing. Reconcile provenance and
  disk.
- **A real-display run has never happened** on the rpi4. The rpi3 now runs a
  rescue and is on the tailnet, but neither board has a bench system to measure —
  `docs/HANDOFF-ab-bench.md` is the work list for getting an A/B off them.
- **`zip` is missing from the yocto workspace image**, so `wk pi deploy` falls
  back to a plain tar instead of the documented `built-product-archive` path
  (`docs/HANDOFF-yocto.md` item 5).
- **Test a new boot cmdline arg on the SD (unarmed fall-through) before
  risking it on the bench stick.**
- **The rpi4's bench stick is still hand-stamped**, not reproduced by `wk
  sysimage write` — needs a confirmed erase to redo it by code.

## Constraints that bind the remaining work

- **A benchmark keeps its score in the DOM**, so a `run-minibrowser` launch can
  prove completion and never a number. The harness is `run-benchmark`, which
  also serves the payload from its own loopback http server — that is what keeps
  the measured-run rule without extra work.
- **"No display attached" and "no compositor" are different problems.**
  `wk pi bench` picks drm+gl when an HDMI connector is live and weston's **RDP
  backend + pixman** when none is (a virtual head; nothing connects to the port),
  generating its own keys into `/etc/wk-bench`, and announces a pixman run as
  not comparable with real display hardware. Do not use `video=` to force a
  mode: it hangs vc4 in probe and takes the board off the network.
- **The rpi3 is 32-bit WPE with its own OOM behaviour** — the rpi3 skill is the
  reference for completion/crash detection; do not re-derive it.
- **`wk pi bench` targets whatever identity the booted system announces** — a
  perf system's is mDNS, a downstream image's is whatever `wk pi setup` gave it.
  Nothing here may assume which system the board booted.
