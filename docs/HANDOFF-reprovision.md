# HANDOFF — wiping moose and tolken, and what to change while doing it

The plan this serves: **wipe both machines and rebuild them from nothing, to
find out whether the scripts in this repository actually are reproducible.**
`CLAUDE.md` states the rule and each machine's conf holds its recipe; this
is the punch-list for the day it is tested.

It is a shopping list, not a task — nothing here justifies a wipe on its own.
Add to it whenever something turns out to be true of an *install* rather than of
a *machine*: that is a fact with an expiry date, and the expiry is the rebuild.

## Remaining — account names, the rule to establish

| kind | account | why |
|---|---|---|
| workstation (moose, tolken host) | `jmichaud` | a person's machine; the name is theirs |
| benchmark install / image (`WK Bench`, the Pi bench sticks) | `bench` (Pi: `root`) | cattle — impersonal on purpose |
| bridge (the phones) | `user` | pmbootstrap's default; not ours to pick |

- **tolken's host install is `justinmichaud`** and every other machine is
  `jmichaud`. It is not a decision anybody made. It costs a per-machine `User`
  override in `dotfiles/ssh/config` (line 58) plus a comment warning the next
  reader not to tidy it — because tidying it breaks login with `Permission
  denied (publickey)`, which reads exactly like an unauthorised key. The home
  directory `/Users/justinmichaud` is baked into at least
  `container/bench/js3-run-loop.sh`.
- **The benchmark install is `bench`, not a person** — an operator's name inside
  it is precisely the hidden state cattle-not-pets forbids, and it is only
  discovered by somebody else trying to follow the runbook. `bench` rather than
  `user` because `user` already means pmbootstrap's default on the bridges.
  `dotfiles/ssh/config` and `wk bench mac-volume --install` already expect it.

After the rebuild: set `User jmichaud` in the `Host tolken` stanza, delete the
comment explaining why it was different and the `Host tolken-bench` contrast
paragraph, then `grep -rn justinmichaud . | grep -v '^\./\.git/'`. Two kinds of
hit and only one is yours: `/Users/justinmichaud` paths go;
`github.com/justinmichaud/…` (the fork remotes in `lib/store.sh`, `cmd/pr`'s
example, the clone URLs in README/SETUP) is a **GitHub account name** and must
not be swept up with it.

## Remaining — Remote Login, as a first-setup step

macOS ships it off, and it blocked the entire macOS benchmark lane at the first
step. `./setup` cannot do it (root, effectively a GUI action):

```
sudo systemsetup -setremotelogin on
```

Then the driving machines' keys in `~/.ssh/authorized_keys`; `wk bench mac
--preflight` prints the exact key of whichever machine you run it from. This is
**per install**, not per machine — the bench volume has its own
`authorized_keys`, and it is the second one that gets forgotten, because
forgetting it is only discovered after a reboot into a mode you cannot log into.

## What a rebuild should be expected to catch

- **The golden guest and the bench volume are the expensive rebuilds** (hours
  each), and the bench volume is the fleet's one inherently hands-on artifact —
  Apple Silicon boot policy lives in the machine's own secure storage.
  `wk bench mac-volume` automates everything either side of the credential
  prompts; whether that is enough is what a rebuild tests.
- **`wk doctor`'s machine-local-state section is the contract.** Anything a
  rebuild loses that is not listed there as `regenerable`, `re-authable` or
  `backed-up` is a bug in the ledger, not bad luck. Read it before wiping and
  again after.
- **Hand-applied settings are supposed to vanish.** Anything that vanishes and
  is missed belongs in `./setup` or `wk backup`, not re-applied by hand.
- **A hostname that does not match the tailnet name** — tolken reports `Tolken`
  from `hostname -s` while every config spells it `tolken`. `wk bench mac`
  case-folds for this reason; anything else comparing the two is suspect.

## While you are in there — cheap only at install time

- **Command Line Tools** (`xcode-select --install`). The benchmark driver's
  `prepare_env` does a bare `import objc`, and only Apple's `/usr/bin/python3`
  has pyobjc. The failure looks like a broken benchmark, not a missing package.
- **No FileVault on the bench install** — decryption is CPU work in the middle
  of a measurement.
- **Room in the APFS container** — `wk bench mac-volume` wants 120 GB free and
  refuses below it, because both installs share the container and the disk that
  fills is the one you work on.

The bench volume's own provisioning list is deliberately not duplicated here:
`docs/HANDOFF-mac-perf-mode.md`, and `wk bench mac-volume --provision` does most
of it.
