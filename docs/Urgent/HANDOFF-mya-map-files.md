# HANDOFF — CAP_CHECKPOINT_RESTORE for mya in container workspaces

mya (WebKit `Source/JavaScriptCore/corpse`, design in
`~/Downloads/mya-images-uuid-design.md`) identifies a Linux target's images by
opening `/proc/<pid>/map_files/<range>`, keeping the descriptors, and giving
liblldb `/proc/self/fd/<n>`. Opening a `map_files` link needs
`CAP_CHECKPOINT_RESTORE` in the **initial** user namespace
(`checkpoint_restore_ns_capable(&init_user_ns)` in `fs/proc/base.c`). The user
decided (2026-09-29) that the container sandbox should grant it.

Measured 2026-09-29 (kernel 6.12, podman VM `wk`, Ubuntu 26.04 SDK):
- in a workspace, both the user and container root get `EPERM` on open; the
  bounding set is `0x800c05fb` (no bit 40), and the uid map is a user namespace
  (`--userns keep-id`);
- in the VM, an unprivileged binary with only `cap_checkpoint_restore=ep` opens
  every `map_files` link of a separate same-uid process, including a file
  deleted after it was mapped;
- file capabilities are ignored on a `nosuid` mount (the VM's `/tmp`).

- [ ] run workspaces so that a process in them can hold `CAP_CHECKPOINT_RESTORE`
      in the init user namespace. `--cap-add=CHECKPOINT_RESTORE` alone is not
      enough under `--userns keep-id`: a capability in the container's user
      namespace does not pass the check. Options: the host user namespace for
      the workspace (a sandbox change: weigh it against what keep-id protects),
      or a small privileged helper outside the container that opens the links
      and passes the descriptors in over a unix socket (the broker already does
      socket hand-offs). [needs a workspace]
- [ ] build step: after a Linux build with `ENABLE_MYA`, `wk build` applies
      `setcap cap_checkpoint_restore+ep` to `bin/mya` and `bin/testLibJSCTools`.
      The capability must be set from the init user namespace: a file capability
      written by root inside a user namespace is a namespaced (v3) capability
      that only applies in that namespace (kernel semantics, not measured
      here). The build tree must not be on a `nosuid` mount.
- [ ] debuginfod: mya uses liblldb's debuginfod symbol locator for system
      libraries. Allow the distro's server (`https://debuginfod.ubuntu.com` for
      the SDK) through the workspace proxy allowlist and set `DEBUGINFOD_URLS`
      in the workspace environment. liblldb 22.1.2 in the SDK has the plugin
      (`lldb -b -o 'plugin list'` shows `symbol-locator debuginfod`).
      Measured 2026-09-29: from a workspace, `curl https://debuginfod.ubuntu.com/buildid/<id>/debuginfo`
      gets `CONNECT tunnel failed, response 403` from the proxy. libstdc++ has no
      local debug package, so its classes resolve to nothing until this works.
- [ ] SDK packages: `liblldb-22-dev` (still missing, so `ENABLE_MYA_HEAP` is
      off in workspaces; see the mya-1 notes). `libc6-dbg` is already present
      and liblldb finds it by build-id.
- [ ] verify with the WebKit side: `testLibJSCTools` image and debug-info
      suites pass in a freshly created workspace. [needs a workspace]

Noticed on the way: Ubuntu's `python3-lldb-22` cannot import `_lldb` (the
native module link is missing), so `import lldb` fails even with
`PYTHONPATH=$(lldb -P)`; scripts only run inside lldb's embedded interpreter.
