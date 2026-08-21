# Handoff: the remote target

`targets/remote.sh` had never been run. It has now — driven from the macOS
host against `devbox-arm64-2`, an 80-core / 250 GB Debian 12 shared build
machine reached through a ProxyJump — and everything below is either a
decision worth keeping or a dated record of what landed. It is still the only
target with no isolation at all: a shared build machine gets a plain checkout
in your home directory, no container, no overlay, no firewall.

## The properties that matter

These boxes are other people's machines, so the interesting requirements are
social rather than technical:

- job count from the **remote** machine's live load average and free memory,
  not this one's
- `nice 19` and `ionice -c3`
- a per-machine build lock, so two of your own builds cannot stack
- per-machine config in `~/.config/wk/targets/<name>.conf`, or — for a machine
  every device should know about — the shared registry
  `targets/hosts/<name>.conf` in this repository

There is no fixed job ceiling any more. There was one — `WK_REMOTE_MAX_JOBS`,
default 16 — and a fixed number is exactly what the resource policy says not
to have: too small on a 250 GB box, too large on a small one, stale the moment
the machine changes. The count is derived per build from what the machine has
free at the time (`_remote_probe` reads its load and `MemAvailable` on every
invocation); `WK_MAX_JOBS` is still honoured from the environment as a
deliberate one-off, and nothing configures it.

`wk claude` refuses on remote targets, deliberately: there is no sandbox
there, so relaxed permissions have no blast radius to be contained by. Keep
that refusal. `wk verify` refuses for the mirror-image reason — it exists to
prove a boundary holds, and reporting "intact" after measuring nothing is
worse than reporting nothing at all.

## What was built (landed 2026-08-18/19; decisions and their reasons)

**A target can now be a machine, not just a kind.** `remote` is the one target
you can have several of, so a target name is either a built-in kind or the
name of a configured machine: `wk new bug-238 --target devbox-arm64-2`.
`load_target` resolves the name to a driver (`target_kind`), sources the conf
and then the driver, and exports both `WK_TARGET` (the name) and
`WK_TARGET_KIND` (the driver). **Commands that branch on what they are talking
to must use the kind** — `[ "$TARGET" = remote ]` was true for every remote
target only while there could be one. `WK_REMOTE_HOST` defaults to the
target's own name: a machine you can reach is a machine already in
`~/.ssh/config`, with whatever ProxyJump, user and key it needs, and restating
that would be a second place to keep it right.

**The driver answers for the far end, not for this one.** Every inherited
default was a container's — `t_src=/src/WebKit`, `t_tools=/opt/wk-tools`, a
no-op `t_sync_tools`, a snapshot demand from a store the box has no part in,
`CCACHE_DIR=/ccache`. All overridden, and two became contract hooks with
defaults: `t_ccache_dir` (a shared machine's ccache is not yours; one under
your own root is) and `t_exec_build` (the build lock belongs on the build
alone — on `t_exec` it would make `wk run` and every one-line probe block for
up to an hour behind somebody's build, a hang with no explanation attached).

**Sizing was the substantive bug.** Every number the polite calculation fed on
was the driving machine's: this host's `/proc/loadavg` (absent on macOS, so
the shared box always looked idle), this laptop's cores, this laptop's free
memory. `lib/resources.sh` grew three seams, defaulted to the old behaviour:
`WK_AVAIL_MB` **replaces** the memory measurement where `WK_CGROUP_MB` only
capped it (a cap is right for a container, which really is this machine with a
limit on top, and wrong across an ssh — taking the smaller of the two sizes a
250 GB box from a laptop); `WK_LOAD` supplies the load of whichever machine is
busy; `WK_MAX_JOBS` is a policy ceiling applied last. Measured at the time:
80 cores, load 1, 248 GB free → memory allowed 165 jobs, cores 79, and the
then-extant ceiling landed it on -j16. (The ceiling has since been removed —
see above.)

**One ssh round trip, then a shared connection.** The driver probes the box
once for `$HOME`, `nproc`, load and `MemAvailable`, and `_rsh` runs everything
through a multiplexed connection (`ControlMaster=auto`, `ControlPersist=60`,
socket under `$(wk_state_dir)/ssh`). Without it every probe is a fresh
handshake through the jump host — seconds each, several per command.
`ConnectTimeout=10`, because `wk status` walks every target it knows and a
machine that is off must cost ten seconds rather than a TCP timeout;
`BatchMode`, because nothing here may ever stop to ask for a passphrase.

**What the box cannot do is not the driver's fault.** Debian 12 ships clang
against libstdc++ 12, which has no `<format>`; WTF has required it since
2026-06-16, so trunk cannot be built there with the distro toolchain. The
pipeline was proved end to end on `webkitgtk-2.52.6` instead — worth knowing
for its own sake: a remote workspace is a plain clone with the mirror's tags
in it, so building a release branch on the build box costs one checkout.

## Provisioning — `wk remote setup <target>` / `wk remote rm <target>`

The companion to `wk pi setup`, shaped by one rule: **it never needs root and
never asks for it.** A build box belongs to everyone who logs into it; a tool
that installs packages, edits `/etc` or changes a login shell on a machine six
other people are using is a tool that gets banned — and then the sandbox story
has a hole in it where the build machine used to be. So every prerequisite is
*checked* rather than installed, everything written is under `$HOME`, and
everything is reversible. Setup probes the machine and reports what it found,
offers to write the target conf if there is none, pushes wk-tools, runs
`remote/provision.sh` there, and then *asks* about what it found lying around.

**The inverse, `wk remote rm`** (2026-08-19, verified as a full rm→setup round
trip on buildbox4), undoes everything setup left — the sourced rc lines,
`~/.wk-remote`, the machine-side conf — asks separately, with the size, before
removing the remote root (which may hold checkouts another box sharing the
`$HOME` owns), and forgets the local conf last, so an ssh failure partway
leaves a target that can be re-run. It refuses while workspaces are still
registered to the target (`wk rm` needs the conf it was about to delete).

**zsh, without root.** `chsh` is the obvious way and the wrong one: it wants a
password, LDAP often refuses it, and on these boxes `$HOME` is shared between
several machines, so the login shell is one setting for all of them and not
ours to change. `shell/bashrc` already execs zsh from bash when it finds one —
per session, reversible with `NO_ZSH=1`, invisible to everyone else — so
provisioning just sources that rc from the machine's rc files. Where there is
no zsh (installing one needs root, so a real case) it says so and stays in
bash; the same rc configures both.

**Cleanup is asked, never assumed.** A shared machine accumulates: an old
wk-tools clone, a mirror a system-wide repository has made redundant,
workspaces nobody remembers. Each is offered once, with its size attached —
on a machine whose MOTD asks everyone to keep disk use down, "13 GB" is the
whole argument. `confirm` declines by itself when there is no terminal, so an
unattended re-run removes nothing. The one thing fixed without asking is a
*broken* line rather than a configuration: a stale rc source line that was
printing errors into every interactive shell on the box.

## Using `wk` on the machine itself

The same driver drives the same workspaces from either end. Setup leaves two
files on the box: `~/.wk-remote` (`target=<name>`, `root=<path>`) and the conf
with `WK_REMOTE_LOCAL=1`. The marker says "this machine is the far end of a
remote target" — a third marker because the existing one ("this machine *is* a
workspace") names one workspace and a build box holds several. It makes
`default_target` resolve to it, so a bare `wk ls` or `wk build` in a shell
there does the right thing, and makes the workstation-only commands refuse.
That refusal is not tidiness: `wk sync` would build a 13 GB mirror in a home
directory whose MOTD asks everyone to keep disk use down, and `wk gc` would
prune a store that is not where this machine's workspaces come from.

`WK_REMOTE_LOCAL=1` makes `_rsh` run its script with `bash -c` instead of over
ssh. Everything else is identical, which is the point: one set of paths, one
job policy, one lock, so the two ends cannot answer differently. `wk` is on
PATH there because `shell/bashrc` puts its own directory there, outside the
interactive guard — so `ssh box bash -lc 'wk ls'` works; a plain `ssh box wk
ls` cannot without root (a non-login, non-interactive shell reads no profile).

Measured on `devbox-arm64-2`: `wk build zz jsc-release` there **2m30s**
against 8m47s for the same build cold from the workstation — the shared ccache
under the remote root, 31.5% hits.

**A build machine does not create or destroy workspaces.** `wk new` and
`wk rm` refuse there and point at the workstation. The reason is the registry:
it records which target each workspace belongs to, and it lives on the
workstation because that is where the question is asked. A `wk new` on the box
would write half that state on the machine that never reads it; a `wk rm`
there would delete the checkout while the workstation went on listing it.

**The build state is the machine's, not the driver's.** A build can be started
from either end, and a `wk status` that only saw the half it started reported
`build=none` about a build running in front of you. So the canonical copy
lives beside the checkout, at `$WK_REMOTE_ROOT/ws/<name>/build.{status,log}`,
and both ends agree on it: the log is written **on the machine that is
building** (teed there, with the same bytes streaming back for the watchdog
and the terminal; on the box itself `$WK_STORE` *is* the remote root, so
`run_watched` already writes that file and the tee is dropped — two writers on
one log interleave into something that reads like a corrupted build); the
status is pushed as it changes (`t_state_put`), with its `log=` field
rewritten to the path that exists over there; and `wk status` / `wk logs`
**ask the machine** (`t_has_wk` / `t_wk`), exit status included, because a
script branching on `wk status` must not be told 0 by the end that did not run
the build. Measured: a build driven from the box reports identically on the
workstation, and both ends exit 1 on failure. A machine that has never been
through `wk remote setup` has no `wk` to ask; the commands degrade to the
local transcript.

**The build lock is taken on the machine that builds**, by `lib/lockrun.sh` in
the copy of wk-tools that `t_sync_tools` pushed — it cannot be taken here,
because a lock dies with its holder and the holder that matters is the build,
not the ssh session, which a detached build outlives by hours. It was a
`flock` under the remote root and stopped being one for the reason
`lib/common.sh` ("locks") gives: a flock is held by the open file descriptor,
so anything the build leaves running holds the machine's build lock for as
long as it lives. One lock mechanism on both ends is also one fewer thing a
shared machine has to have installed.

## The decisions

**Clone from the machine's own repository when it has one.** Igalia's build
boxes publish a WebKit mirror and say so in the MOTD, and the same MOTD asks
everyone to keep disk use down — so using theirs is not a micro-optimisation,
it is following the house rules. `t_create` looks for one:
`WK_REMOTE_REFERENCE` if the conf names it, otherwise any WebKit path
advertised in `/etc/motd`, `/etc/motd.d/*` or `/run/motd.dynamic`.
**Advertised or configured, and nothing else** — a path that merely exists is
somebody's checkout, not an invitation. Static files only: `/etc/update-motd.d`
holds scripts, and running someone else's scripts to read a hint is not a
trade worth making. **And verified before use**, because buildbox4's
advertisement is stale — it names `/var/git/WebKit.git` and there is no such
directory; the candidate fails a `git rev-parse` and the fallback takes over.
A plain local clone, not `--shared`: git hardlinks the objects, so the
workspace does not depend on a repository the sysadmins repack on a schedule.
Measured: `.git` costs 69 MB of new data against a 13 GB source. `origin`
stays pointed at the shared repository; `github` is added alongside.

**Without one, the driver keeps its own mirror — which is also what `wk sync`
means remotely.** One bare mirror per remote root, fetched in `t_create`, each
workspace cloned out of it with `--shared`; only `main`, matching
`wk_mirror_branches`; `gc.auto 0`, because the workspaces borrow its objects
and a repack underneath a live clone is how that breaks. Measured on
`devbox-arm64-2`: first fetch 25 minutes and 13 GB; every workspace after it,
39 seconds and no objects. `wk sync --target <machine>` (2026-08-19) refreshes
the far side between builds — the tooling, and the mirror where the machine
has no shared repository; `_remote_mirror_update` is shared with `t_create`,
so a machine that never had a mirror gets one.

**Driving-side state is per machine.** `$WK_STORE` for this target is
`$(wk_state_dir)/remote/<target-name>`: two boxes can each have a workspace
called `bug-238`, and `wk status` with no argument walks the store of every
target it knows.

**Per-target driver state is reset between loads.** A driver is a sourced file
that sets globals, and one process loads several targets — `wk status` does
exactly that. `_target_reset_vars` clears the per-machine variables before
each load and re-seeds them from a snapshot of the environment taken once, so
an explicit `WK_REMOTE_HOST=... wk ...` still wins while the second machine
cannot inherit the first one's host, root or measured capacity.

**A shared registry, and peers** (2026-08-19). A target's shared half lives in
`targets/hosts/<name>.conf` in this repository, and the machine-local conf
overrides it line by line — the machine-local half used to be the only half,
so a machine configured on one device did not exist on the others. The
registry travels with the tree `t_sync_tools` pushes, so `target_all` skips it
on a machine that is the far end of a target (a build box drives nothing).
`WK_REMOTE_PEER=1` marks another *workstation*, asked and not driven — no
tooling pushed, no creation, no destruction, and `t_has_wk` satisfied without
`~/.wk-remote`, because that marker would make the machine refuse the
host-only commands on itself.

## Traps

- **An interrupted `wk build` does not stop the remote build.** ssh without a
  pty does not carry the signal, so ctrl-c (and the watchdog's stall abort)
  ends the local half while the compiler keeps going at the far end. The build
  lock then makes the next `wk build` wait for it, up to an hour, which reads
  as a hang. If this bites, the fix is a remote-side pid file and a
  `wk build --abort`, not a shorter lock timeout.
- **An ssh must never read this side's stdin unless the command wants it.**
  Two bugs came from that, both found here: the pushed build status arrived
  empty, because resolving its destination path in the same pipeline drank the
  content; and a `du` in the cleanup loop could swallow the answer being typed
  at the prompt after it. Questions go through `_rsh_q` (`ssh -n`); only
  `t_exec`, `t_exec_tty`, `t_wk` and the status write forward stdin. Anything
  added to this driver has to pick a side.
- **`wk run` inherits the box's login shell.** `t_exec` runs `bash -lc`, so
  whatever the machine's own dotfiles print lands on stderr. That is a shared
  machine's business, not ours: we provision nothing there and should not
  start.

## What is left

- **`wk gui` does not refuse a remote target**, and should: there is no seat,
  no display, and no reason to open a browser window on somebody else's build
  machine. It currently falls through the Linux seat check and advises
  `wk session on`, which is advice for the wrong computer.
- **`wk gc` never looks at a remote store.** Nothing large lives on the
  driving side (a log and a status file), but the remote root itself — mirror,
  ccache, dead workspaces — has no reclaim path beyond `wk rm`.
- **`remote/provision.sh` still checks for `flock` as a hard prerequisite**,
  and nothing uses flock any more — the build lock is `lib/lockrun.sh`. The
  check should go, or name what is actually required.
- **Delegated output interleaves.** `wk status`/`wk logs` against a machine
  return two streams over one connection, and stdout rows can arrive before
  the stderr headings that introduce them. Merging them remotely would fix the
  order and put `wk logs --all` on stderr, which is worse.
- **A shared `$HOME` across boxes is warned about, not handled.** A workspace
  name is then the same directory on all of them — one build tree, which
  cannot hold two architectures. The warning tells you to give each box its
  own `WK_REMOTE_ROOT`; nothing enforces it, and the mirror is then per root
  too, which is the one thing you would rather share.
- **The removal half of setup's cleanup prompts is unexercised** — the
  declining path is verified; nobody has answered yes to one yet, because the
  only real candidate was the user's own older checkout and that is not a
  decision to make for them.
- **`wk enter --zed`** prints the right `ssh://` URL but has not been used in
  anger, and `t_exec_tty` (the pty path, for `wk run --lldb`) is unexercised.
- **The non-Linux-remote case** is `docs/HANDOFF-other-remote.md`: the same
  driver contract against a machine that is not Linux.

Done since first writing, recorded here so nobody re-opens them: `wk ls` asks
the drivers now (`walk_targets` + `target_workspaces`, with machines that have
their own `wk` answering for themselves through `t_wk`), so the "t_list is
called by nothing" note is obsolete; and `wk bench` refuses every target but
the container by name (`cmd/bench`), which settles "should it refuse on a
shared machine" the right way.
