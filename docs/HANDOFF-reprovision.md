# HANDOFF — wiping moose and tolken, and what to change while doing it

The plan this file serves: **wipe moose and tolken and rebuild both from
nothing, to find out whether the scripts in this repository actually are
reproducible.** `docs/HANDOFF-cattle.md` states the rule and holds the ledger
of from-nothing recipes; this file is the punch-list for the day it is tested —
the changes that are only cheap to make while the machine is already being
rebuilt, and the things a rebuild is expected to *catch*.

It is a shopping list, not a task. Nothing here justifies a wipe on its own.

Add to it whenever something turns out to be true of an *install* rather than
of a *machine*. That distinction is the whole point: a fact about an install is
a fact with an expiry date, and the expiry is the rebuild.

Started 2026-08-22.

## Account names — the rule the rebuild should establish

Three kinds of machine, three answers, and the current fleet is inconsistent
about all three:

| kind | account | why |
|---|---|---|
| workstation (moose, tolken host) | `jmichaud` | a person's machine; the name is theirs |
| benchmark install / image (`WK Bench`, the Pi bench sticks) | `bench` (Pi: `root`) | cattle — impersonal on purpose |
| bridge (the phones) | `user` | pmbootstrap's default; not ours to pick |

**The one to fix: tolken's host install is `justinmichaud`.** Every other
machine in the fleet is `jmichaud`, and so is every other `User` line in
`dotfiles/ssh/config`. It is not a decision anybody made — it is what Setup
Assistant was given once, and it has been load-bearing ever since.

What it costs today, which is the argument for correcting it during the rebuild
rather than living with it:

- `dotfiles/ssh/config`'s `Host tolken` needs a per-machine `User` override no
  other fleet machine needs, plus a comment warning the next reader not to
  "tidy" it — because tidying it breaks the login, and the failure
  (`Permission denied (publickey)`) reads exactly like an unauthorised key. The
  wrong thing to go looking at.
- the home directory is `/Users/justinmichaud`, baked into at least
  `container/bench/js3-run-loop.sh` (`WEBKIT_ROOT`).

**And the one to establish: the benchmark install is `bench`, not a person.** A
benchmark install is meant to be reproducible from nothing, by anyone — so an
operator's account name inside it is precisely the hidden state
`HANDOFF-cattle.md` forbids: it makes the recipe depend on who is running it,
and that dependency is only discovered by somebody else trying to follow the
runbook. `bench` rather than `user` because `user` already means pmbootstrap's
default account on the bridges, and one word with two meanings across machine
kinds is how a stanza gets copied to the wrong place. This is already what
`dotfiles/ssh/config` and `wk bench mac-volume --install` expect; the rebuild is
where it becomes true.

### After the rebuild, in this repository

```
# dotfiles/ssh/config -- the 'Host tolken' stanza
-  User justinmichaud
+  User jmichaud
```

…and delete the comment above it explaining why it was different, and the
paragraph on `Host tolken-bench` contrasting the two. Then:

```
grep -rn justinmichaud . | grep -v '^\./\.git/'
```

Two kinds of hit and only one is yours: `/Users/justinmichaud` paths are this
machine's and should go, while `github.com/justinmichaud/…` (the fork remotes
in `lib/store.sh`, `cmd/pr`'s example, the clone URL in `README.md` and
`SETUP.md`) is a **GitHub account name** with nothing to do with the local
user. Do not sweep those up with it.

## Remote Login — turn it on as part of first setup

macOS ships Remote Login off, and it was off on tolken until 2026-08-22 —
which blocked the entire macOS benchmark lane at the first step, before anything
else about the machine could even be *inspected*. `./setup` cannot do it (root,
and effectively a GUI action), so it is a checklist item or it is a half-hour of
confusion later.

```
sudo systemsetup -setremotelogin on
```

Then the driving machines' keys in `~/.ssh/authorized_keys`; `wk bench mac
--preflight` prints the exact key of whichever machine you run it from.

This is **per install**, not per machine: the bench volume has its own
`authorized_keys`, and it is the second one that gets forgotten — because
forgetting it is only discovered after a reboot into a mode you cannot then log
into.

## What a rebuild should be *expected* to catch

The point of the exercise, so these are worth watching for rather than fixing
in advance:

- **the golden guest and the bench volume are the expensive rebuilds.** Both
  are hours, and the bench volume is the fleet's one inherently hands-on
  artifact (Apple Silicon boot policy lives in the machine's own secure
  storage). `wk bench mac-volume` now automates everything either side of the
  credential prompts; whether that is enough is exactly what a rebuild tests.
- **`wk doctor`'s machine-local-state section is the contract.** Anything a
  rebuild loses that is not listed there as `regenerable`, `re-authable` or
  `backed-up` is a bug in the ledger, not bad luck. Read it *before* wiping and
  again after.
- **hand-applied settings are supposed to vanish.** That is obligation 3 in
  `HANDOFF-cattle.md`. Anything that vanishes and is missed should end up in
  `./setup` or `wk backup`, not be re-applied by hand.
- **a hostname that does not match the tailnet name.** tolken reports `Tolken`
  from `hostname -s` while every config spells it `tolken`; `wk bench mac`
  case-folds for exactly this reason, but anything else comparing the two is
  suspect.

## While you are in there

Each is cheap only at install time:

- **Command Line Tools** — `xcode-select --install`. The benchmark driver's
  `prepare_env` does a bare `import objc`, and only Apple's `/usr/bin/python3`
  has pyobjc; Homebrew's does not. The failure looks like a broken benchmark
  rather than a missing package.
- **No FileVault on the bench install** — decryption is CPU work in the middle
  of a measurement. (On a workstation it is a real security tradeoff and not
  automatic; the point is only that it is a variable.)
- **Room in the APFS container** — `wk bench mac-volume` wants 120 GB free and
  refuses below it, because both installs share the container and the disk that
  fills is the one you work on.

## What is deliberately not here

The bench volume's own provisioning list. That is
`docs/HANDOFF-mac-perf-mode.md`, itemised once, and `wk bench mac-volume
--provision` does most of it — a copy here would only drift.
