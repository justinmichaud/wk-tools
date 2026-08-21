# Handoff: architecture review and upstreaming pass

**Do not start this until every other handoff in `docs/HANDOFF.md` is done** —
both lanes, including their settings and sandbox audits. The point of doing
this last is that it looks at the system as it actually turned out, not as it
was designed to turn out; starting early means re-doing this review every time
something downstream changes shape.

Two separate lenses, run together because they tend to find the same seams:

1. **Abstract/architect more cleanly, to avoid future breakage.** Places
   where two platforms or two features grew independent, similar-but-not-
   identical implementations, and a shared abstraction would mean a bug fixed
   once instead of twice (or fixed on Linux and silently left broken on
   macOS, which is the failure mode this whole two-lane plan exists to avoid
   in the first place).
2. **Upstream what's generically useful.** Anything here that isn't specific
   to this one machine or this one person's workflow is a candidate to give
   back to the project it depends on, rather than carrying it forever as a
   local patch that bit-rots against the next upstream release.

This is a review, not a rewrite: the output is a decision per candidate below
(worth doing / not worth it / needs a bigger conversation first), not a
requirement to execute all of them.

## Abstraction candidates already visible from the work above

- **The layering decided 2026-08-20** (recorded in
  `docs/HANDOFF-vocabulary.md`, "The layers"):
  the `home` / `lab` / `wk` / `field` / `stock` decomposition and its one-way
  dependency rule. The rule binds new code from the decision date; this review
  is where existing code catches up — directory moves, plus a grep selftest
  that the lab layer (targets/, boot/, image/, the bench mechanics) knows no
  WebKit.

- **The target driver contract** (`t_create`/`t_exec`/`t_info`/`t_list`/
  `t_destroy`) is implemented three times — `targets/container.sh`,
  `targets/remote.sh`, `targets/vm.sh` — written at different times against
  the same implicit interface. Once the remote and cross-compile work
  (the remote target landed 2026-08-19; `docs/HANDOFF-cross-compile.md`,
  `docs/HANDOFF-other-remote.md`) has all landed, check whether the contract
  can be an actual shared interface with a conformance test run against all
  three, instead of three files that happen to agree by convention.
- **`cmd/backup` and the settings-audit workflow** are already
  platform-branched (`case "$(wk_os)" in macos|linux`) doing conceptually the
  same round trip with different file formats. `docs/HANDOFF-settings-audit.md`
  ends up exercising both halves — worth checking afterward whether the
  "diff against committed state, filter known junk, ask, write, verify round
  trip" logic can be one script with a per-platform adapter for the file
  format, rather than parallel prose describing the same steps twice.
- **The egress proxy model vs. the SD-card/image-transfer paths** — both
  Linux (`container/proxy/wk-proxy.py`) and the cross-compile/yocto transfer
  helpers (`docs/HANDOFF-cross-compile.md`, `docs/HANDOFF-sdcard.md`,
  `docs/HANDOFF-yocto.md`) end up solving "move a thing from an isolated
  workspace to somewhere else" — check whether the transfer mechanism should
  be one primitive both features call, instead of each handoff growing its
  own copy step.
- **The profiling tooling** (`docs/HANDOFF-original-helpers.md`) is scoped
  per-target (cli/GTK/WPE/macOS MiniBrowser) and could easily end up as four
  near-identical wrappers around samply/sysprof/heaptrack. Once all four
  exist, check whether they should be one script dispatching on target rather
  than four.

## Upstreaming candidates already visible from the work above

- **The rootless-podman + unix-socket egress-proxy design** (`--network
  none` plus an allowlist-by-hostname proxy over one unix socket, so `wk`
  never needs root on Linux) is a genuine
  answer to a problem rootless podman doesn't solve out of the box (no stable
  selector for nftables to match rootless network-helper traffic on). That's
  worth writing up somewhere podman users would find it, not just in this
  repo's own docs.
- **SDK patches carried locally** — patch 11 (`--isolated`, turning off host
  D-Bus/session-bus/`$HOME` mounts) and patch 3 (gating `--unsafe-caps` behind
  an explicit flag rather than always disabling WebKit's own bwrap sandbox) —
  both look like fixes the `webkit-container-sdk` project itself would want,
  not just this fork. Check whether they're already proposed upstream; if not,
  that's a PR, not a patch to keep re-applying.
- **The RPi5 NUMA kernel work** — `host/linux/rpi5/HANDOFF.md` already flags
  "Path A: Launchpad request to enable `CONFIG_NUMA_EMU` in stock
  linux-raspi (Igalia authored the feature)" as optional follow-up once the
  custom-kernel path (Path B) is proven. This review is the point to actually
  file it, now that Path B has validated the approach works.
- **`gpr` and the profiling wrappers** (`docs/HANDOFF-git-tools.md`,
  `docs/HANDOFF-original-helpers.md`) are generically useful to any WebKit
  contributor, not specific to this machine or this sandbox model. Once
  restored, consider whether they belong in `Tools/Scripts` upstream in
  WebKit itself, or at minimum as a linked-to standalone toolkit from the
  existing `justinmichaud.github.io` wiki pages this whole project already
  cites.
- **The two-lane parallel-machine planning approach in `docs/HANDOFF.md`
  itself** may be worth writing up on the wiki once it's been exercised for
  real — "how to split disposable-workspace tooling work across a Linux box
  and a macOS box without conflicts" is not specific to this repo's contents.

## What "done" looks like

A short written decision per candidate (including the ones this review finds
that aren't listed above — the list here is a starting point, not the full
set) and, for anything upstreamed, a link to the actual PR/issue/post rather
than just an intent recorded here.
