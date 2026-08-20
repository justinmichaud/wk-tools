We should make it so that the permission to push to git can be turned on or off. Ony I should be able to push to git at all, so when I run claude it should be disabled, but work when I run it in the container. A switch is fine.

**Done 2026-08-19 — `wk push on|off|status`.** The switch is *where the key
is*, not a flag: `off` moves the private deploy keys from the store's
`secrets/` directory — which every workspace has mounted read-only at
`/secrets` — into `push-keys/`, which nothing mounts anywhere. Workspaces link
to the key rather than copying it (`container/firstrun.sh`), so the switch
takes effect immediately in workspaces that are already running, and nothing
inside one can undo it: the held keys are invisible from in there and
`/secrets` is read-only. Verified live — with the switch off, `ssh -T
git@github-webkit` and a real `git push` from a running workspace both fail
with `no such identity`, and `ls /var/lib/wk/push-keys` from inside says no
such directory.

`wk claude` turns it off before it hands over control, and turns it back on
afterwards only for an interactive session — a headless run is the babysitter's
fix loop, and leaving push on between its attempts would hold the switch open
for hours unattended. A crash, a kill or a dropped ssh leaves it off, which is
the safe direction. Fetching is unaffected in every case: the fork remotes
fetch anonymously over HTTPS and need no credential.

**Two corrections, later on 2026-08-19.** The wiring was asserted at creation
and never verified, and both halves of "the same mechanisms work on a build
box" turned out to be false on the older of the two machines. `bb4` on
buildbox4 had `origin` pointing at that machine's local mirror -- *pushable* --
and `fork` with an https push URL, which is the failure that matters most here:
git never consults ssh for an https URL, so no deploy key is offered and the
switch is not in the path at all. `wk remotes [--fix]` now checks and
re-asserts it, and a wrong origin is flagged in `wk status`.

And the switch is only one of the two things a push needs. With it on, the
wiring corrected and the ssh alias resolving, buildbox4 still answered
`Permission denied (publickey)`: its key exists and was never registered on
GitHub. Only the host can ask (`gh` and the credential are here), so
`wk push on` now names `wk key check` rather than reporting success as though a
push would work. Verified end to end in a container -- refused with the switch
off, `* [new branch]` on a dry-run push with it on -- and *not* yet verified
from a build machine, for that reason.

**Remote build machines, 2026-08-19.** The same three mechanisms now work on a
shared build box: `wk key register` generates and registers a key *per machine*
(GitHub refuses one deploy key on two repositories but takes many keys on one,
so every machine gets its own and can be revoked on its own — a private key is
never copied between machines); the fork remotes are wired into every remote
checkout by the same `wk_wiring_script` as everywhere else; and
`wk push on|off|status --target <t>` throws the switch over there, where the
key is. ssh finds the key through `$root/ssh/config` and a per-checkout
`core.sshCommand`, so nothing outside the wk root is edited — which matters
because that home directory is the user's own and is shared over NFS with other
machines.

Verified on buildbox4: with the switch off, ssh says `no such identity` and a
push is refused, while `git fetch fork main` still works (anonymous HTTPS);
`wk status` lists each machine's keys by fingerprint, and the two machines'
fingerprints differ, which is the per-machine design showing through.

What is honestly weaker there than in a container: a build machine has no
read-only mount and no separate uid, so the held keys sit in a directory the
same user can read. The switch stops a *push* — it does not contain a process
that goes looking. That is acceptable only because `wk claude` refuses remote
targets outright (no sandbox on a shared machine); if an agent is ever allowed
to run there, this is the first thing that has to change.

One thing this deliberately does not do yet: a macOS guest has no deploy key at
all, so "push is off" is true there by accident rather than by mechanism.

**Sudo: done 2026-08-19 — `wk sudo status|require`.** Every machine should
cost a password for root, every time. Two separate holes, and the helper closes
both with one drop-in file: Igalia's build machines grant
`(ALL : ALL) NOPASSWD: ALL` from the site's own sudoers, and sudo's default
five-minute timestamp is a window that "look for a live sudo timestamp" walks
through — which is on the incident list below. `/etc/sudoers.d/zz-<user>-passwd`
sorts after the site's file and sudoers takes the last match, so
`PASSWD: ALL` plus `Defaults:<user> timestamp_timeout=0` wins without touching
anything a sysadmin owns and without asking one.

Detection is by property, not by file: `sudo -n true` (which never prompts)
answers "does root cost anything right now" by trying it, and `sudo -n -l` is
read for the timeout and for which *blanket* rule is in force — a NOPASSWD
scoped to one program is how the deliberate exceptions work, wk's own quiesce
helper included.

That reading was wrong until 2026-08-19, and wrong in the direction that
matters: it grepped the whole listing for `NOPASSWD: ALL` and reported a hit,
while sudoers takes the **last** match. On buildbox4 `sudo -l` lists the site's
`NOPASSWD: ALL` *and* the drop-in's `PASSWD: ALL` after it, so a correctly
hardened machine was reported as wide open — `wk remote setup` offered to fix
it on every run, and `wk sudo require` re-installed and asked for the password
every time, its own short-circuit having tested `[ -f "$DROPIN" ]` for a path
that does not exist when the remote login name differs from the local one.
Which for a shared build box is the normal case. `wk sudo status --all` walks every configured machine; each
answers by running the same file over there rather than by a second
implementation reached over ssh. `wk doctor` reports it on every run, so it
cannot quietly drift back. Measured 2026-08-19: this Mac keeps a timestamp
(sudo's default) and buildbox4 grants NOPASSWD: ALL — both now named, with the
one command that fixes each.

Installing it is the one thing here that can lock a person out of their own
machine, so `require` validates the file with `visudo -c` before installing it,
validates the whole ruleset after, and then *tests the property*: clears the
timestamp and checks that sudo asks. It never edits or removes a file it did
not write.

Where `visudo` *is* was worth fixing (2026-08-19): it lives in `/usr/sbin`,
which is not on a normal user's PATH on Debian-family Linux -- so on exactly
the machines this command exists for, the validation was a "command not found"
before it ever looked at the file. `visudo_resolve` in `cmd/sudo` takes it from
the PATH when the PATH has it and from `/usr/sbin/visudo` when it does not, and
refuses to install anything at all when neither exists: the alternative is
writing an unvalidated sudoers file, which is the one mistake this whole
paragraph is about. It sets a variable rather than printing one, so that
refusal kills the command instead of a command substitution -- which would
have left an empty command word and reported a parse failure the generated
file never had.

One more thing `--force` has to be honest about, found 2026-08-19: forcing
`wk claude` onto a shared build machine used to check that a `claude` existed
on PATH and then hand the session to it. On devbox-arm64-2 that was a global
npm install running under the machine's own node v16.19.0, which dies before
printing anything (`ReferenceError: ReadableStream is not defined` -- the
global arrived in node 18). Same rule as the sudoers check above: test the
property, not the file. `wk claude` now runs `claude --version` to find one
that works, prefers the one in your own home, offers the user-local installer
(which carries its own runtime, so it neither needs nor touches the shared
node) when nothing does, and launches it **by absolute path** -- because with
`/usr/local/bin` ahead of `~/.local/bin` on a machine that is not ours to
reorder, installing a working one would still leave the broken one selected.

**A way past a barrier: `wk <command> --force`.** Asked for alongside the sudo
rule, and the reason is honest — a rule you cannot get past in a hurry is a
rule that gets deleted. Any refusal that exists because of a policy rather than
a fact is now a `barrier` (`lib/common.sh`): without `--force` it refuses and
names the flag, with `--force` it warns at the point of bypass *and repeats the
list of what was forced when the command ends*. What is deliberately not
forceable: a missing workspace, an unpublished snapshot, a failed build — those
are facts, not rules. The bypass itself is never recorded anywhere: the
condition that caused it is recomputed by `wk doctor` on every run, so there is
nothing to go stale and nothing to forget to clear.

Converted so far: the sandbox-not-intact and unfiltered-guest refusals in
`wk claude`, the `--cmakeargs` refusal in `wk build`, and the new passwordless-
sudo refusal in `wk remote setup`.

We should analyse the complete setup and look for sandbox escapes. Newer models are known to be agressive.

Issues I have had:

claude overwriting my work
claude posting on github on my behalf and responding to my reviewers
claude seaching for ssh keys on my host (throught the container sdk)
claude making a suid binary then running it to bypass auto mode
claude searching for a sudo time seat to skip pw auth

Consider which machines can be ssh'd into too.
