# Handoff: the remote target

`targets/remote.sh` is written and has never been run. It is the only target
with no isolation at all: shared build machines get a plain checkout in your
home directory, no container, no overlay, no firewall.

## The properties that matter

These boxes are other people's machines, so the interesting requirements are
social rather than technical, and they are already expressed in the driver:

- job count from live load average, not `nproc` (`build_jobs polite`)
- `nice 19` and `ionice -c3`
- a `flock` so two of your own builds cannot stack
- per-target config in `~/.config/wk/targets/<name>.conf`

`wk claude` refuses on remote targets, deliberately: there is no sandbox there,
so relaxed permissions have no blast radius to be contained by. Keep that
refusal. It is the one place where the answer to "can I run an agent here" is
simply no.

## What to do

1. Write a config for a real machine and run the driver contract end to end:
   `t_create`, `t_exec`, `t_info`, `t_list`, `t_destroy`.
2. `wk build` against it. `cmd/build` already selects the polite calculation
   and `nice 19` when `WK_TARGET=remote`, so the thing to verify is that the
   numbers it derives are sane on a box that is already busy.
3. Decide what `wk sync` means remotely. Today `t_create` clones from
   `$WK_REMOTE_ROOT/mirror` if one exists and from GitHub otherwise; there is
   no code that creates or updates that mirror.
4. `wk status` and `wk logs` read `$WK_STORE/ws/<name>/build.status` on the
   *local* machine, which does not exist for a remote build. Either write the
   status file locally from the driving side, or make status a remote read.

## Traps

**`t_exec` interpolates `$*` into a remote shell command.** It is fine for the
build command it was written for and wrong for anything containing quoting.
Fix it before it gets a second caller.

**The remote target has no proxy and needs none** -- but `cmd/verify` assumes
the container sandbox. It should refuse politely on remote rather than report
failures against properties that were never claimed.

**A shared machine's ccache is not yours.** `WK_STORE/cache/ccache` is a local
path; nothing currently arranges a cache on the remote side, and pointing at a
shared one on a machine you do not administer is a good way to become
unpopular.
