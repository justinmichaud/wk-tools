# HANDOFF — the `wk` dispatcher, listing and workspace-lifecycle CLI

General `wk` mechanics not tied to one board, bridge or bench lane: the
dispatcher, `wk status`/`wk ls`, `wk sync`, `wk zed`, `wk profile`, `wk gc`,
the git helpers, and workspace/lock/registry concurrency. Hardware- and
lane-specific items live in the other `docs/HANDOFF-*.md` files.

## Known defects

- [!] a remote build's ssh carries no signal on disconnect; ctrl-c or a stall
      abort leaves the compiler running while the next `wk build` waits
      behind the lock
- [!] a delegated `wk status` is answered by the far machine's own, possibly
      older, wk-tools, which can change the answer and not only the version
- [!] `wk` auto-starts the podman machine even when a macOS VM is already
      running, and both do not fit in 32 GB
- [!] Ctrl-C does not always interrupt a running `wk` command

## Concurrency and crash-only convergence — unverified

Only the container `wk new` firstrun-kill case has been exercised. Owed,
each a `kill -9` mid-command plus a re-run that must converge:

- [ ] `wk sync`, `wk new --target vm`, `wk new --target <remote>` (mid-clone
      / ssh cut), `wk rm` (each target), `wk build`, `wk test`, `wk gc`,
      `wk vm base`/`--refresh` (host-side kill), `wk vm start`/`stop`,
      `wk remote setup`, `wk remote rm`, `wk key register`, `wk skills
      pull/push`, `wk claude`
- [ ] two `wk sync` at once: the second waits or refuses, naming the first
- [ ] two `wk build` on one workspace serialise on every target (only
      exercised on one target so far); `wk vm base --refresh` while one runs
      is refused
- [ ] two `wk vm start` do not corrupt `~/.ssh/config.d/wk`
- [ ] a remote workspace whose clone is cut mid-way reads `creating` from
      *any* machine asking, not just the driving one
- [ ] `tart delete` a guest by hand is detected the same way as a hand-`podman
      rm`
- [ ] deleting `$WK_STORE/ws/<n>` by hand under a live registry entry is
      detected, and `wk gc` refuses to prune what the survivor may still pin

## Listing (`wk status`/`wk ls`) — unverified

- [ ] `NO_COLOR` and a redirected stdout both drop the colour from the table
- [ ] the health block is empty and silent on a machine that has none of its
      lines (no store, no proxy unit, no locks)
- [ ] a workspace whose exec fails (stopped container, guest not booted)
      shows the row without the extra fields, rather than an error
- [ ] a bounded mDNS lookup: a `.local` name nothing answers for must cost
      2s, not the resolver's default
- [ ] a machine on older wk-tools answering a delegated `wk status` by its
      own (possibly wrong) rules — the fleet block flags version skew but not
      that the *answers* can differ
- [ ] `WK_REMOTE_LOCAL`/root computed from `~/.wk-remote` on the machine
      itself rather than a second conf — confirm `wk ls` on a build box
      resolves with no ssh
- [ ] a target that cannot be probed during resolution is reported
      unreachable by name, never silently dropped from the view
- [ ] a workstation that is down is listed unreachable with its timeout; the
      fleet walk never hangs on it
- [ ] wk-tools version skew is flagged with both shas named
- [ ] the same workspace name alive on two machines is reported as a
      conflict, not listed twice
- [ ] two workstations reaching one build box see one state; a disagreement
      names both views
- [ ] the fleet exit code aggregates the worst state found anywhere
- [ ] shared-home remotes (devbox-arm64-2/armhf-2): provisioning the second
      does not clobber the first's identity; each resolves its own target by
      hostname; `wk remote rm` of one leaves the other working
- [ ] builds from two shared-home remotes never collide in checkout or lock
      (keyed per machine, derived not configured)

## `wk zed` — unverified

- [ ] `wk zed --url <ws>` prints the `zed ssh://…` URL without opening Zed, so the
      in-workspace refusal can name a command to run on the local machine

- [ ] a language server that wants the network from inside a workspace is
      refused by the allowlist and says so rather than hanging (Zed's ACP
      registry fetch does this today; nothing has been added for it)
- [ ] `wk new --zed` warns instead of failing when the launch cannot happen
      (zed missing, or a vm workspace with no route yet) — the workspace is
      created either way
- [ ] `wk rm <ws>` takes the `Host wk-<name>` alias with it, ProxyCommand and
      all, so a later `ssh wk-<name>` is an unknown host rather than a hang
      (on a macOS host this removal happens on the host, not forwarded into
      the podman VM, where nothing reads that entry anyway)

## The prompt — unverified

- [ ] a machine in bench mode shows `bench` in the prompt (the
      `/etc/wk-image` marker, the same evidence `wk boot --status` reads) —
      untested on a real booted bench system

## `wk sync` — unverified

- [ ] `wk sync` inside a workspace is refused, naming the host command
- [ ] the refspecs follow the mirror's own layout (origin's branches are its
      heads; every other upstream is namespaced); a fifth upstream needs no
      change here beyond `wk_remotes`
- [ ] bare, inside a workspace: that workspace, no argument needed
- [ ] bare, on a host with exactly one workspace: that one, and it says so
- [ ] bare, on a host with several: lists them and asks, with "all of them
      and this machine's mirror, snapshot and tooling" as the last choice
- [ ] bare, on a host with several and no terminal: refused, naming the two
      scopes that need no answer
- [ ] `--machine` on a Linux workstation does all of it in one place
- [ ] `--all` additionally walks every other machine's own store
- [ ] the dispatcher's forwarding rule for `--machine`/`--all` into the
      podman VM (it should not forward — the VM can see none of the fleet —
      while still forwarding a workspace-scoped sync)
- [ ] `wk status`'s fix message for a behind peer is "commit and push here
      first" while dirty, and "`wk sync --tools`" once clean — never a `git
      pull` that provably cannot converge

## Everything else still unticked

- [ ] `wk backup` → `./setup` round-trips with no spurious changes; its junk
      filters strip what they claim (weather location, WiFi UUIDs,
      last-folder paths, timestamps) — duplicate of `docs/HANDOFF-settings-audit.md`'s own item, do both together
- [ ] `wk skills` status/diff/pull/push; pull refuses over uncommitted repo
      edits
- [ ] the skills are workspace-true: an agent started by `wk claude` in a
      container and in a macOS guest can follow every skill it can trigger
      without hitting a host-only instruction
- [ ] `wk key register` / `wk key check`, confirmed end to end
- [ ] `wk enter <ws>` lands in a shell; `wk enter <ws> <cmd>` runs the command
- [ ] `wk status <ws> --wait` blocks while busy and reports once when not,
      same exit code as a bare `wk status`; `--timeout S` stops waiting and
      says so without claiming the work stopped
- [ ] `wk logs <ws> -f` follows a live build
- [ ] `wk stop --keep-vm` leaves the podman machine running
- [ ] `wk gc` prunes a creation record whose workspace, environment and
      registry entry are all gone, and keeps one still in flight (only the
      first half is exercised, planted by hand)
- [ ] `build_live` (lib/detach.sh): a `state=running` file whose log has not
      moved for `WK_STALL_SECONDS` is not live, so a `kill -9`'d build no
      longer refuses every later benchmark
- [ ] `headless_markers` (lib/resources.sh): a marker in either spelling is
      seen by both `is_headless` and the Linux stage that removes it
- [ ] `wk vm rm` removes `<name>.unfiltered`, so a recreated guest of the
      same name is not refused by `wk claude` for the previous guest's sins
- [ ] `ccache_conf_render` (lib/store.sh) renders the same ceiling for the
      store and for a remote machine's cache, and neither overwrites a
      config that is already there
- [ ] `wk gc` prunes an unreferenced snapshot, keeps the newest, trims
      ccache, removes a stale bench payload seed, and reports the dirs it
      keeps
- [ ] `wk sync --all` and `WK_MIRROR_BRANCHES` carry the extra branches
- [ ] `wk pick <ws> <id>@main` resolves the identifier without the network
      and picks it: the arithmetic agrees with the commit's own trailer, a
      wrong id is `unresolved` rather than picked, a dirty tree is a
      barrier, a conflict leaves the sequencer
- [ ] `container/bin/` helpers on PATH in a workspace: `git-clean`,
      `commit-count`, `git-sync-fork` (refuses when the fork's main is
      ahead; fast-forwards otherwise; says so when `wk push` is off)
- [ ] **the PR workflow, end to end and as one flow**: sandboxed agents
      driving builds while a person pushes, rebases, fetches forks and
      uploads PRs — `wk push on|off`, `wk remotes --fix`, `wk pr`, `wk pick`
      and `git-sync-fork` all in the loop, including a PR from an armhf
      container where `git-webkit` cannot run
- [ ] `wk report` prints the weekly summary (needs gh auth)
- [ ] the MCP server (`wk mcp`) creates and destroys a workspace from Claude
      Desktop, and refuses past its workspace cap
- [ ] `wk doctor` on a freshly set-up machine reports everything ok, and each
      `--` line's printed fix actually clears that line when run
- [ ] `wk build --detach` on a remote target: the pre-written `state=running`
      can reach the far machine before its log is truncated, so a stale log
      from a previous run can read as stalled for one poll
- [ ] `wk ls` inside a workspace prints `?`/`-` for BASE/CHANGES instead of a
      not-applicable marker
- [ ] `wk sync` is slow on rpi5 — measure before theorising
- [ ] every command's `--help` prints the actual command line it would run
      and the configurations it accepts
- [ ] every `WK_*` override read with a default (71 of them) is either
      documented where the user meets it and exercised by a check, or
      removed
- [ ] `wk profile --mode sampling` in a real workspace prints the tier
      breakdown; `--mode bytecode` leaves exactly one JSCProfile json;
      `--mode samply` in a container refuses with the host remedy when
      `perf_event_paranoid` > 1 and records otherwise; `--mode instruments`
      in a macOS guest records a `.trace`; `--fetch` copies a recording out
      of a guest byte for byte
- [ ] `wk disk` inside a workspace answers the only version of the question
      available in there; `wk disk` with the podman machine stopped leaves
      it stopped
- [ ] `wk vm base --rm` deletes the golden base, then asks separately about
      the pulled OCI image, and existing vm workspaces keep working
- [ ] every command under `cmd/` declares itself to the dispatcher: line 3 is
      a one-line `# wk <name> <args> -- <summary>` synopsis, and a `# wk:`
      line in the first 15 lines names `where=` (one of
      `host|local|workspace|dynamic`) and `group=`

## From the 2026-08-26 option audit

- [ ] `--quiet` is parsed locally by ~11 commands; handle it once in `wk` like
      `--force`
- [ ] `wk ls` and `wk find` gain `--json`, the same record shape `wk status`
      emits
- [ ] `--all` means "every machine" in `wk sync` and "every item" elsewhere;
      one meaning, or two words
- [ ] `--count` appears in bench, pick, pr, pi, status, sync: confirm one
      semantics
- [ ] `wk sync` is slow on rpi5: measure before changing -- time the three
      stages (mirror fetch over WiFi; checkout reset/clean; per-workspace
      fetch) with a per-stage timing line under WK_DEBUG, then fix the
      dominant one

