# Handoff: the remote target

`targets/remote.sh` had never been run. It has now — driven from the macOS
host against `devbox-arm64-2`, an 80-core / 250 GB Debian 12 shared build
machine reached through a ProxyJump. This file is what the driver is, what was
decided while making it work, and what is left.

It is still the only target with no isolation at all: a shared build machine
gets a plain checkout in your home directory, no container, no overlay, no
firewall.

## The properties that matter

These boxes are other people's machines, so the interesting requirements are
social rather than technical:

- job count from the **remote** machine's live load average, not this one's
- `nice 19` and `ionice -c3`
- a `flock` so two of your own builds cannot stack
- a hard ceiling (`WK_REMOTE_MAX_JOBS`, default 16) on top of all of it
- per-machine config in `~/.config/wk/targets/<name>.conf`

`wk claude` refuses on remote targets, deliberately: there is no sandbox there,
so relaxed permissions have no blast radius to be contained by. Keep that
refusal. `wk verify` now refuses for the mirror-image reason — it exists to
prove a boundary holds, and reporting "intact" after measuring nothing is worse
than reporting nothing at all.

## What was built

**A target can now be a machine, not just a kind.** `remote` is the one target
you can have several of, so a target name is either one of the four built-in
kinds or the name of a machine configured under `~/.config/wk/targets/`:

    wk new bug-238 --target devbox-arm64-2

`load_target` resolves the name to a driver (`target_kind`), sources the conf
and then the driver, and exports both `WK_TARGET` (the name) and
`WK_TARGET_KIND` (the driver). **Commands that branch on what they are talking
to must use the kind** — `[ "$TARGET" = remote ]` was true for every remote
target only while there could be one. The conf is optional in content but
required in existence: it is what tells a typo'd `--target contianer` from a
machine, and without one the error prints the file to write.

The conf that drove all of this:

    # ~/.config/wk/targets/devbox-arm64-2.conf
    WK_REMOTE_ROOT=/home/igalia/jmichaud/wk
    WK_REMOTE_MAX_JOBS=16

`WK_REMOTE_HOST` is absent because it defaults to the target's own name: a
machine you can reach is a machine already in `~/.ssh/config`, with whatever
ProxyJump, user and key it needs, and restating that here would be a second
place to keep it right.

**The driver answers for the far end, not for this one.** The original file
implemented six functions and inherited the rest, and every inherited default
was a container's: `t_src` was `/src/WebKit`, `t_tools` was `/opt/wk-tools`
(which does not exist there), `t_sync_tools` was a no-op (so it never would),
`t_needs_base` demanded a snapshot from a local store the box has no part in,
and `CCACHE_DIR` was `/ccache`. All six are overridden now, and the two that
were not driver-shaped became contract hooks with defaults:

- `t_ccache_dir` — `/ccache` everywhere else, `$WK_REMOTE_ROOT/cache/ccache`
  here. A shared machine's ccache is not yours; one under your own root is.
- `t_exec_build` — `t_exec` everywhere else. The flock belongs on the build
  alone: on `t_exec` it would also make `wk run` and every one-line probe
  block for up to an hour behind somebody's build, which is a hang with no
  explanation attached.

**Sizing was the substantive bug.** `cmd/build` already selected the polite
calculation for `WK_TARGET=remote`, but every number it fed on was the driving
machine's: `build_jobs polite` read *this* host's `/proc/loadavg` (which on
macOS does not exist at all, so the shared box always looked idle), `host_cores`
counted this laptop's cores, and `avail_mem_mb` measured this laptop's free
memory. So `lib/resources.sh` grew three seams, all of them defaulted to
today's behaviour:

- `WK_AVAIL_MB` **replaces** the memory measurement, where `WK_CGROUP_MB` only
  ever **capped** it. A cap is right for a container, which really is this
  machine with a limit on top, and wrong for a machine across an ssh
  connection — taking the smaller of the two sizes a 250 GB build box from a
  laptop.
- `WK_LOAD` supplies the load average of whichever machine is busy.
- `WK_MAX_JOBS` is a policy ceiling applied last, so it caps the answer rather
  than the inputs.

`cmd/build` and `cmd/test` set all three from `t_mem_mb`/`t_cores`/`t_load` for
a remote target. Measured: 80 cores, load 1, 248 GB free → memory allows 165
jobs, cores allow 79, "never more than half a shared box" allows 40, and the
ceiling lands it on **-j16**.

**One ssh round trip, then a shared connection.** The driver probes the box
once for `$HOME`, `nproc`, load and `MemAvailable`, and `_rsh` runs everything
through a multiplexed connection (`ControlMaster=auto`, `ControlPersist=60`,
socket under `$(wk_state_dir)/ssh`). Without it every `t_src`, `t_tools`,
`t_info` and capacity question is a fresh handshake through the jump host —
seconds each, several per command. `ConnectTimeout=10` because `wk status`
walks every target it knows and a machine that is off must cost ten seconds
rather than a TCP timeout; `BatchMode` because nothing here may ever stop to
ask for a passphrase.

**What the box cannot do, and it is not the driver's fault.** The first real
`wk build` there failed 106 seconds in:

    Source/WTF/wtf/FormattedLogging.h:29:10: fatal error: 'format' file not found

Debian 12 ships clang 18 against libstdc++ 12, which has no `<format>`; WTF has
required it since 2026-06-16, so **trunk cannot be built on that machine** with
its distro toolchain. Nothing on the box helps: clang 11/13/18/19 are all
installed and all use the same libstdc++, and there is no g++-13. libc++-19
does have `<format>`, and the box already has podman and a
`webkit-container-sdk` checkout — which is the real answer, and is lane A step
6 rather than this one.

The pipeline was proved end to end on `webkitgtk-2.52.6` instead (the last
release before the `<format>` dependency), checked out with
`wk enter rt1 git checkout --detach webkitgtk-2.52.6`. Worth knowing for its
own sake: a remote workspace is a plain clone with the mirror's tags in it, so
"build a release branch on the build box" costs one checkout.

## Provisioning the machine — `wk remote setup <target>`

The companion to `wk pi setup`, and shaped by one rule: **it never needs root
and never asks for it.** A build box belongs to everyone who logs into it. A
tool that installs packages, edits `/etc` or changes a login shell on a machine
six other people are using is a tool that gets banned — and then the sandbox
story has a hole in it where the build machine used to be. So every
prerequisite is *checked* rather than installed, everything written is under
`$HOME`, and everything is reversible.

    wk remote setup devbox-arm64-2

It probes the machine and reports what it found (OS, arch, cores, memory, free
space, zsh, ccache, flock), offers to write the target conf if there is none,
pushes wk-tools, runs `remote/provision.sh` there, and then *asks* about what
it found lying around. `flock` is the one hard requirement — without it two of
your own builds cannot be serialised, which is the one thing a shared machine
must not allow.

**zsh, without root.** `chsh` is the obvious way and the wrong one: it wants a
password, LDAP often refuses it outright, and on these boxes `$HOME` is shared
between several machines, so the login shell is one setting for all of them and
not ours to change. `shell/bashrc` already execs zsh from bash when it finds
one — per session, reversible with `NO_ZSH=1`, invisible to everyone else — so
provisioning just sources that rc from `~/.bashrc`, `~/.zshrc` and
`~/.bash_profile`. Where there is no zsh — and installing one needs root, so
that is a real case — it says so and stays in bash; the same rc configures
both, so only the line editor differs. That branch is written and unexercised:
buildbox4 had no zsh when this was built and has one now, so both machines
here take the first path.

**Cleanup is asked, never assumed.** A shared machine accumulates: an old
wk-tools clone, a mirror that a system-wide repository has made redundant,
workspaces nobody remembers. Each is offered once, with its size attached,
because on a machine whose MOTD asks everyone to keep disk use down "13 GB" is
the whole argument. `confirm` declines by itself when there is no terminal, so
an unattended re-run removes nothing. The one thing fixed without asking is a
*broken* line rather than a configuration: a stale `wk-tools/bashrc` source
line from before the tree was restructured, which was printing three
`setopt: command not found` errors into every interactive shell on this box —
and into the middle of every `wk run`.

## Using `wk` on the machine itself

The same driver drives the same workspaces from either end. `wk remote setup`
leaves two files on the box:

    ~/.wk-remote                        target=<name>, root=<path>
    ~/.config/wk/targets/<name>.conf    WK_REMOTE_LOCAL=1, plus the root and ceiling

`~/.wk-remote` is a third marker, and it needed to be. The existing one says
"this machine **is** a workspace" and names one; a build box holds several at
once, so that shape does not fit. This one says "this machine is the far end of
a remote target" — which makes `default_target` resolve to it, so a bare
`wk ls` or `wk build bug-238 jsc-release` in a shell there does the right
thing, and makes the workstation-only commands refuse. That refusal is not
tidiness: `wk sync` would build a 13 GB mirror in a home directory whose MOTD
asks everyone to keep disk use down, and `wk gc` would prune a store that is
not where this machine's workspaces come from.

`WK_REMOTE_LOCAL=1` makes `_rsh` run its script with `bash -c` instead of over
ssh — there is nothing to connect to, and trying would need an sshd loop and a
key to itself. Everything else is identical, which is the point: one set of
paths, one job policy, one flock, so the two ends cannot answer differently
about where a checkout is or how many jobs a build may have.

`wk` is on PATH there because `shell/bashrc` puts its own directory there, and
that block sits outside the interactive guard — so `ssh box bash -lc 'wk ls'`
works as well as a login shell does. A plain `ssh box wk ls` does not, and
cannot without root: a non-login, non-interactive shell reads no profile, and
the only rootless fix would be a second copy of `wk` on a PATH that such a
shell does not have either.

Measured on `devbox-arm64-2`: `wk build zz jsc-release` there **2m30s** against
8m47s for the same build cold from the workstation — the shared ccache under
the remote root, 31.5% hits.

**A build machine does not create or destroy workspaces.** `wk new` and `wk rm`
both refuse there and point at the workstation. The reason is the registry: it
records which target each workspace belongs to, and that record is what sends a
later `wk build bug-238` to the right machine. It lives on the workstation
because that is where the question is asked. A `wk new` on the box would write
half that state on the machine that never reads it, and a `wk rm` there would
delete the checkout while the workstation went on listing it. So the machine
holds checkouts and builds them; it does not own them.

What that leaves is one asymmetry, and it is honest rather than hidden:
**a build's log and status live wherever it was started from.** The driving
side is the only one that knows how a build ended, so `wk status` on the
workstation reports `build=none` for a build run on the box, and the box
reports it in full. The *flock* is shared, which is the part that matters: two
builds cannot stack whichever end starts them.

`wk ls` and `wk status` see everything from either end regardless —
`target_all` walks every *configured* machine, not just the ones the registry
has an entry for, and both commands union the local store with the driver's own
listing. Without that, a machine that holds six workspaces reports none.

## The decisions

**Clone from the machine's own repository when it has one.** Igalia's build
boxes publish a WebKit mirror and say so in the MOTD — buildbox4's reads
"instead of cloning the git repo from webkit.org, clone the one from
/var/git/WebKit.git (is updated every 10 minutes)", with a footnote that a
local clone hardlinks `.git/objects`. Since the same MOTD asks everyone to keep
disk use down, using theirs is not a micro-optimisation: it is the difference
between following the house rules on a shared machine and not.

So `t_create` looks for one: `WK_REMOTE_REFERENCE` if the conf names it,
otherwise any WebKit path advertised in `/etc/motd`, `/etc/motd.d/*` or
`/run/motd.dynamic`. **Advertised or configured, and nothing else** — a path
that merely exists is somebody's checkout, not an invitation; a path in the
MOTD is the sysadmins telling every user to clone it. Static files only, too:
`/etc/update-motd.d` holds scripts, and running someone else's scripts to read
a hint is not a trade worth making.

**And verified before use, because buildbox4's advertisement is stale**: it
names `/var/git/WebKit.git`, and there is no such directory on that machine.
The MOTD outlived the repository. An unchecked hint would have turned every
`wk new` there into a confusing clone failure; instead the candidate fails a
`git rev-parse refs/heads/main` and the fallback takes over.

A plain local clone, not `--shared`: git hardlinks the objects, so the
workspace does not depend on a repository the sysadmins repack on a schedule.
Measured against a 13 GB source — `.git` costs **69 MB** of new data, the
packfile shows `links=2`, and what is actually spent is the 6.3 GB working
tree. `origin` stays pointed at the shared repository, which is refreshed every
ten minutes and far closer than GitHub; `github` is added alongside so an
upstream fetch is always one named remote away.

**Without one, the driver keeps its own mirror — and that is also the answer to
"what does `wk sync` mean remotely".** It means this, and it happens in
`t_create`. `wk sync` is a host-store command — a bare mirror plus the
hardlinked snapshots the overlay scheme is built on — and none of that exists
on a machine where a workspace is a plain clone. What carries over is the
*reason* the mirror exists, so the driver creates one per remote root, fetches
it, and clones each workspace out of it with `--shared`. Only `main`, matching
`wk_mirror_branches`; `gc.auto 0`, for the same reason the local mirror sets it
— the workspaces borrow its objects and a repack underneath a live clone is how
that breaks. Measured on `devbox-arm64-2`, which advertises nothing: the first
fetch is 25 minutes and 13 GB (tags come with it, and they reach into the
release branches); every workspace after it costs 39 seconds and no objects at
all.

**Build state stays on the driving side.** `wk status` and `wk logs` read
`$WK_STORE/ws/<name>/build.status` locally, and for a remote build that file is
written locally too — the driving side is the only side that knows how the
build ended, and a status file on a machine you share is a file somebody else's
`wk` could be writing. `$WK_STORE` for this target is
`$(wk_state_dir)/remote/<target-name>`, per machine: two boxes can each have a
workspace called `bug-238`, and `wk status` with no argument walks every target
there is.

**Per-target driver state is reset between loads.** A driver is a sourced file
that sets globals, and one process can load several targets — `wk status` does
exactly that. `_target_reset_vars` clears the per-machine variables before each
load and re-seeds them from a snapshot of the environment taken once, so an
explicit `WK_REMOTE_HOST=... wk ...` still wins while the second remote machine
cannot inherit the first one's host, root or measured capacity.

## Traps

**The `t_exec` `$*` bug is fixed** (it uses `sh_quote`), and so is the second
one it would have grown: `cd` into the checkout now quotes the path as well.

**`wk verify` and `wk claude` both refuse here**, and the refusals say why.
Neither is a placeholder to be filled in later — there is no property to
measure.

**An interrupted `wk build` does not stop the remote build.** ssh without a pty
does not carry the signal, so ctrl-c (and the watchdog's stall abort) ends the
local half while the compiler keeps going at the far end. The flock then makes
the next `wk build` wait for it, up to an hour, which reads as a hang. If this
bites, the fix is a remote-side pid file and a `wk build --abort`, not a
shorter lock timeout.

**`wk run` inherits the box's login shell.** `t_exec` runs `bash -lc`, so
whatever the machine's own dotfiles print lands on stderr — on this box, a
stale copy of an old `wk-tools/bashrc` emitting `setopt: command not found`
three times per invocation. That is a shared machine's business, not ours: we
provision nothing there and should not start.

## What is left

- **The removal half of `wk remote setup`'s cleanup prompts is unexercised.**
  The declining path is verified (no terminal, nothing removed); nobody has
  answered yes to one yet, because the only real candidate on this machine is
  the user's own older checkout and that is not a decision to make for them.
- **buildbox4 is configured but not provisioned.** It answered the MOTD probe
  and the reference check; `wk remote setup buildbox4` has never been run
  against it, so it has no `~/.wk-remote` and no shell rc of ours.
- **A shared `$HOME` across boxes is warned about, not handled.** These
  machines say so in the MOTD, and it means a workspace name is the same
  directory on all of them — one build tree, which cannot hold two
  architectures. The warning tells you to give each box its own
  `WK_REMOTE_ROOT`; nothing enforces it, and the mirror is then per root too,
  which is the one thing you would rather share.
- **`wk gui` does not refuse a remote target**, and should: there is no seat,
  no display, and no reason to open a browser window on somebody else's build
  machine. It currently falls through the Linux seat check and advises
  `wk session on`, which is advice for the wrong computer.
- **`wk bench` on a remote target is unexamined.** It resolves a target like
  everything else, but a shared machine is not a benchmark machine — nothing is
  quiesced, nothing is pinned, and somebody else's build is a confounder. It
  should probably refuse by default.
- **`wk gc` never looks at a remote store.** Nothing large lives there (a log
  and a status file), but the remote root itself — mirror, ccache, dead
  workspaces — has no reclaim path at all beyond `wk rm`.
- **The macOS-remote-target case** (`docs/HANDOFF-other-remote.md`) is the same
  driver contract against a machine that is not Linux; `t_ccache_dir`,
  `nice`/`ionice` and `/proc/*` in the probe are the three places that assume
  otherwise.
- **`wk enter --zed`** prints the right `ssh://` URL but has not been used in
  anger, and `t_exec_tty` (the pty path, for `wk run --lldb`) has not been
  exercised either.
- **`t_list` is written to the contract and called by nothing.** `wk ls` walks
  the local store instead, which is why a remote workspace lists with `?` for
  its base and `-` for its changes — both are overlay concepts. Either `wk ls`
  should ask the driver, or those two columns should be admitted to be
  container-only.
