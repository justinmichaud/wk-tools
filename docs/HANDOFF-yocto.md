# HANDOFF — the yocto builder: what is left

`wk sysimage build <downstream-yocto-*>` works end to end: both builders behind
one verb, the stages, the detach model with `--stop`, and the preflight. The
full 2.48 build ran (13,130 tasks, 0 errors), the image booted the rpi4 from
its USB stick and handed the board back by itself, and `--stage webkit` has
since cross-built WebKit against the target's own SDK (3736/3736,
`libWPEWebKit-1.1.so`). The image store this section used to describe is being
removed — `docs/HANDOFF-fleet.md` has what is left of that.

## Remaining

1. **The rootfs tarball has no consumer.** The import pulls `rootfs.tar.xz` into
   the store beside `disk.img` — the honest archival form of a rootfs, ~600 MB
   per system — and nothing reads it. Give it a use or stop keeping it.
2. **`--stage toolchain` has not been run** since the base image changed.
3. **The pseudo patch is unresolved, and this blocks trusting the layer.**
   `image/yocto/meta-wk/…/pseudo_%.bbappend` works — without it `update-rc.d`
   and `base-files` die in `do_package` with `got *at() syscall for unknown
   directory` / `tar: Cannot mkdir: Bad address` — but the reason recorded for
   it is refuted. What is established: the failure is a property of **pseudo
   plus this host's kernel (7.0.11 aarch64)**, not of a release branch — 2.46
   pins a different poky whose same-era fakeroot fails identically. The image
   carries no local change either way, since pseudo is a build-time fakeroot and
   is never installed. `--no-local-layer` re-runs the experiment on a host with
   an older kernel; `meta-wk` is one bbappend, so this is also the question of
   whether the layer survives at all.
4. **`zip` is still missing from `container/yocto/Containerfile`** (`unzip`
   only). `built-product-archive archive` shells out to `zip`, so the documented
   deploy path cannot run and `wk pi deploy` falls back to a plain tar. It
   cannot be installed at runtime — `ports.ubuntu.com` is not in the egress
   allowlist and widening that to fetch a zip is the wrong trade. Deferred for a
   real reason: editing the Containerfile retags `WK_SDK_IMAGE`, and an existing
   workspace is pinned to the image it was made from, so the edit forces
   `wk rm` — which would discard an hours-long `populate_sdk` toolchain that
   lives in the workspace (unlike sstate and DL_DIR, which are in the store).
   **Add it the next time that workspace is recreated for another reason.**
5. **Tailscale on the rpi target** — the layer (`image/yocto/meta-wk-tailnet`,
   deliberately not part of `meta-wk`: that layer may only change how an image
   is *built*, item 3 above, and this changes what is *in* one) has now been
   through a build, and the first attempt failed at `do_populate_lic` —
   `LIC_FILES_CHKSUM` resolves relative to `${S}`, the unpacked release
   tarball, which has no LICENSE in it (that is why the layer ships one).
   Fixed by resolving through `${WORKDIR}` instead. A recipe that has never
   been built is not a recipe that works, so this needs a clean run to
   actually confirm now that the known bug is fixed. `IMAGE_INSTALL:append`
   adds the package; `wk sysimage build … --no-tailnet` builds without it, for
   a measurement that has to compare against pre-tailnet numbers.

   What is left is the running of it, in this order, and none of it can be done
   from a workstation alone: **build** (`wk sysimage build <profile>` — the
   fixed layer's first real test), **write** (`disk_seed_tailnet` puts the key
   and the fleet's name for the board onto the card, never into the image),
   **boot**, and confirm the board answers to its own tailnet name. Only then
   do the stored-reachability deletions — `docs/HANDOFF-fleet.md`,
   "Removing the fallback-address plumbing".

   The thing this changes for a *workspace* (`docs/HANDOFF-linux-pi.md`): a
   board on the tailnet is reachable by the egress proxy's `pi-hosts`
   allowlist by address, which is what `wk pi setup` already records — so this
   is the same mechanism, arriving with the image instead of after it.
6. **The macOS half** — unattempted. The builder refuses any target that is not
   a container (a remote target is a shared machine, and this is 100 GB and days
   of CPU; a macOS VM workspace has no store-backed Yocto cache). The podman VM
   on macOS *is* a container target, so it should work, subject to the VM's disk
   being big enough.
7. **The rpi3 targets** — `webkit-2.52-yocto-rpi3-32` (then named
   `webkit-2.52-yocto-rpi3-32`) **has** been built (3.3 GB) and written to the
   board's SD card; `-64` has not. That image is the board's **base image**, the only system on its only medium, so the rpi3
   has no bench system at all and `wk pi bench rpi3` now refuses it by design.
   The board is off. See `docs/HANDOFF-boot.md` and `docs/HANDOFF-ab-bench.md`
   for the second-slot work this needs.
8. **A second `wk sysimage build` of the same profile** has never been timed to
   confirm sstate reuse actually makes it dramatically faster than the first.
9. **First boot is slow (~17 min)** because `packages:` installs over WiFi —
   move anything that does not need a per-machine secret into the rootfs at
   build time instead.

## Constraints and traps that bind this work

- **The image is the runtime; WebKit is sent to it separately, by design.** The
  Dev@CI image carries weston, cmake, perl, gdb and gdbserver and no browser at
  all — no `cog`, no `run-minibrowser`, no `/WebKit`. The browser is cross-built
  on the workstation and deployed every cycle, which is what makes the cycle
  minutes instead of hours. The device therefore needs WebKit's own scripts in a
  tree at `/WebKit/WebKit` — that is `wk pi setup`'s job, not the image's.
- **`t_exec <ws> cat <file>` silently corrupts binary** (1396 bytes out as
  1399): `wkdev-enter` is an interactive-shell wrapper, not a byte pipe. Use
  `t_pull`.
- **Merge the target's `--cmakeargs`, never replace them.** `build-webkit` takes
  one `--cmakeargs` and the last wins, so passing ours would drop the target's —
  including the `bwrap` and `xdg-dbus-proxy` paths the sandbox needs.
  `BUILD_WEBKIT_ARGS` is read out of `Tools/yocto/targets.conf` and ours
  appended, so upstream stays the source of truth.
- **Size `-j` by memory for the cmake/ninja stages too.** `build-webkit` appends
  `-j$(numberOfCPUs)` unless `--makeargs` already carries a `-j`, so it ran -j80
  on WebCore's unified sources and the OOM killer took cc1plus three times — a
  failure that reads as a compiler bug (`fatal error: Killed signal terminated
  program cc1plus`). bitbake's own `PARALLEL_MAKE` was capped; this stage
  bitbake never sees was not.
- **sstate is namespaced by the build-host image**, deliberately — it is a
  correctness fix, not caution.
- **`rm_work` is on by default**, a real trade: it costs a rebuild of a single
  recipe but keeps TMPDIR survivable (`chromium-ozone-wayland` and `gn-native`
  are 21 GB each; `YOC_CHROMIUM=0` keeps chromium out by default and
  `--chromium` puts it back).
- **A wrapper whose command failed can still be `yocto_any_running`** for a
  while afterwards, so a restart refuses with "already running" until `--stop`.
- **The build's egress widening is an input to the sandbox audit**
  (`docs/HANDOFF-sandboxing.md`).
