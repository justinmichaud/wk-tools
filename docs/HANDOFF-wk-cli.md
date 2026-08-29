# HANDOFF — the `wk` dispatcher, listing and workspace-lifecycle CLI

General `wk` mechanics not tied to one board, bridge or bench lane.

## Known defects

- [ ] a remote build's ssh carries no signal on disconnect; ctrl-c or a stall abort leaves the compiler running while the next `wk build` waits behind the lock [needs a remote target]
- [ ] a delegated `wk status` is answered by the far machine's own, possibly older, wk-tools, which can change the answer and not only the version [needs two machines at different versions]
- [ ] `wk` auto-starts the podman machine even when a macOS VM is already running, and both do not fit in 32 GB [needs a Mac with 32 GB RAM]
- [ ] Ctrl-C does not always interrupt a running `wk` command [needs a running wk command to interrupt]

## Concurrency and crash-only convergence — unverified

Each a `kill -9` mid-command plus a re-run that must converge:

- [ ] `wk sync`, `wk new --target vm`, `wk new --target <remote>` (mid-clone / ssh cut), `wk rm` (each target), `wk build`, `wk test`, `wk gc`, `wk vm base`/`--refresh` (host-side kill), `wk vm start`/`stop`, `wk remote setup`, `wk remote rm`, `wk key register`, `wk skills pull/push`, `wk claude` [needs every target]
- [ ] two `wk sync` at once: the second waits or refuses, naming the first [needs two concurrent runs]
- [ ] two `wk build` on one workspace serialise on every target (only exercised on one target so far); `wk vm base --refresh` while one runs is refused [needs every target]
- [ ] two `wk vm start` do not corrupt `~/.ssh/config.d/wk` [needs a macOS VM]
- [ ] a remote workspace whose clone is cut mid-way reads `creating` from *any* machine asking, not just the driving one [needs a remote target and two machines]
- [ ] `tart delete` a guest by hand is detected the same way as a hand-`podman rm` [needs a macOS VM]
- [ ] deleting `$WK_STORE/ws/<n>` by hand under a live workspace is detected, and `wk gc` refuses to prune what the survivor may still pin [needs a workspace]

## Listing (`wk status`/`wk ls`) — unverified

- [ ] `NO_COLOR` and a redirected stdout both drop the colour from the table [needs a test]
- [ ] the health block is empty and silent on a machine that has none of its lines (no store, no proxy unit, no locks) [needs a bare machine]
- [ ] a workspace whose exec fails (stopped container, guest not booted) shows the row without the extra fields, rather than an error [needs a workspace]
- [ ] a machine on older wk-tools answering a delegated `wk status` by its own (possibly wrong) rules — the fleet block flags version skew but not that the *answers* can differ [needs two machines at different versions]
- [ ] `WK_REMOTE_LOCAL`/root computed from `~/.wk-remote` on the machine itself rather than a second conf — confirm `wk ls` on a build box resolves with no ssh [needs a remote build box]
- [ ] a workstation that is down is listed unreachable with its timeout; the fleet walk never hangs on it [needs an unreachable machine]
- [ ] wk-tools version skew is flagged with both shas named [needs two machines at different versions]
- [ ] two workstations reaching one build box see one state; a disagreement names both views [needs two workstations and a shared remote]
- [ ] the fleet exit code aggregates the worst state found anywhere [needs a test]
- [ ] shared-home remotes (devbox-arm64-2/armhf-2): provisioning the second does not clobber the first's identity; each resolves its own target by hostname; `wk remote rm` of one leaves the other working [needs two shared-home remotes]
- [ ] builds from two shared-home remotes never collide in checkout or lock (keyed per machine, derived not configured) [needs two shared-home remotes]

## `wk zed` — unverified

- [ ] a language server that wants the network from inside a workspace is refused by the allowlist and says so rather than hanging (Zed's ACP registry fetch does this today; nothing has been added for it) [needs a workspace running zed]
- [ ] `wk new --zed` warns instead of failing when the launch cannot happen (zed missing, or a vm workspace with no route yet) — the workspace is created either way [needs zed and a vm workspace]
- [ ] `wk rm <ws>` takes the `Host wk-<name>` alias with it, ProxyCommand and all, so a later `ssh wk-<name>` is an unknown host rather than a hang (on a macOS host this removal happens on the host, not forwarded into the podman VM, where nothing reads that entry anyway) [needs a macOS host]
- [ ] `wk zed <ws>` on a workspace a peer owns: Zed opens the checkout through the peer's own transport, one ssh hop out (only the hop and the URL are verified, against a hand-placed copy of this tree) [needs a peer running this version]

## The prompt — unverified

- [ ] a machine in bench mode shows `bench` in the prompt (the `/etc/wk-image` marker, the same evidence `wk boot --status` reads) [needs a booted bench system]

## `wk sync` — unverified

- [ ] the refspecs follow the mirror's own layout (origin's branches are its heads; every other upstream is namespaced); a fifth upstream needs no change here beyond `wk_remotes` [needs a test]
- [ ] bare, inside a workspace: that workspace, no argument needed [needs a workspace]
- [ ] `--tools` on a Linux workstation refreshes the tooling copies and publishes a snapshot in one place [needs a Linux workstation]
- [ ] `--all` fetches in every workspace on every target, and `--tools` refreshes every machine's own tooling and store [needs the fleet]
- [ ] the dispatcher's forwarding rule for the scope flags into the podman VM (they should not forward — the VM can see none of the fleet — while a bare or workspace-scoped `wk sync` still does, which is what reaches the store in there) [needs the podman VM]
- [ ] `wk status`'s fix message for a behind peer is "commit and push here first" while dirty, and "`wk sync --tools`" once clean — never a `git pull` that provably cannot converge [needs a behind peer]

## Everything else still unticked

- [ ] audit the name `store`: `$WK_STORE` holds `ws/`, `git/WebKit.git`, `base/`, `secrets/`, `push-keys/`, `cache/`, `bench/`, `skills/`, `vm/`, and only the mirror is WebKit's -- decide what each part is called before a second project needs a name [needs a decision]
- [ ] `wk backup` → `./setup` round-trip (`docs/HANDOFF-settings-audit.md`) [needs a machine to reprovision]
- [ ] `wk skills` status/diff/pull/push; pull refuses over uncommitted repo edits [needs a workspace]
- [ ] the skills are workspace-true: an agent started by `wk claude` in a container and in a macOS guest can follow every skill it can trigger without hitting a host-only instruction [needs a container and a macOS VM]
- [ ] `wk key register` / `wk key check`, confirmed end to end [needs a workspace]
- [ ] `wk key` reaches the store through its own `in_vm` (cmd/key) rather than the dispatcher's `where=store`; one hop into the podman VM, not two [needs the podman VM]
- [ ] `wk enter <ws>` lands in a shell; `wk enter <ws> <cmd>` runs the command [needs a workspace]
- [ ] `wk status <ws> --wait` blocks while busy and reports once when not, same exit code as a bare `wk status`; `--timeout S` stops waiting and says so without claiming the work stopped [needs a workspace]
- [ ] `wk logs <ws> -f` follows a live build [needs a workspace]
- [ ] `wk stop --keep-vm` leaves the podman machine running [needs the podman VM]
- [ ] `build_live` (lib/detach.sh): a `state=running` file whose log has not moved for `WK_STALL_SECONDS` is not live, so a `kill -9`'d build no longer refuses every later benchmark [needs a workspace]
- [ ] `wk vm rm` removes `<name>.unfiltered`, so a recreated guest of the same name is not refused by `wk claude` for the previous guest's sins [needs a macOS VM]
- [ ] `ccache_conf_render` (lib/store.sh) renders the same ceiling for the store and for a remote machine's cache, and neither overwrites a config that is already there [needs a remote target]
- [ ] `wk gc` prunes an unreferenced snapshot, keeps the newest, trims ccache, removes a stale bench payload seed, and reports the dirs it keeps [needs a workspace]
- [ ] `wk sync --tools` and `WK_MIRROR_BRANCHES` carry the extra branches [needs the fleet]
- [ ] `wk pick <ws> <id>@main` resolves the identifier without the network and picks it: the arithmetic agrees with the commit's own trailer, a wrong id is `unresolved` rather than picked, a dirty tree is a barrier, a conflict leaves the sequencer [needs a workspace]
- [ ] `container/bin/` helpers on PATH in a workspace: `git-clean`, `commit-count`, `git-sync-fork` (refuses when the fork's main is ahead; fast-forwards otherwise; says so when `wk push` is off) [needs a workspace]
- [ ] the PR workflow, end to end and as one flow: sandboxed agents driving builds while a person pushes, rebases, fetches forks and uploads PRs — `wk push on|off`, `wk remotes --fix`, `wk pr`, `wk pick` and `git-sync-fork` all in the loop, including a PR from an armhf container where `git-webkit` cannot run [needs an armhf container]
- [ ] the MCP server (`wk mcp`) creates and destroys a workspace from Claude Desktop, and refuses past its workspace cap [needs Claude Desktop]
- [ ] `wk doctor` on a freshly set-up machine reports everything ok, and each `--` line's printed fix actually clears that line when run [needs a freshly set-up machine]
- [ ] `wk build --detach` on a remote target: the pre-written `state=running` can reach the far machine before its log is truncated, so a stale log from a previous run can read as stalled for one poll [needs a remote target]
- [ ] `wk ls` inside a workspace prints `?`/`-` for BASE/CHANGES instead of a not-applicable marker [needs a workspace]
- [ ] every command's `--help` prints the actual command line it would run and the configurations it accepts [needs a test]
- [ ] every `WK_*` override read with a default (71 of them) is either documented where the user meets it and exercised by a check, or removed [needs a test]
- [ ] `wk profile --mode sampling` in a real workspace prints the tier breakdown; `--mode bytecode` leaves exactly one JSCProfile json; `--mode samply` in a container refuses with the host remedy when `perf_event_paranoid` > 1 and records otherwise; `--mode instruments` in a macOS guest records a `.trace`; `--fetch` copies a recording out of a guest byte for byte [needs a container and a macOS VM]
- [ ] `wk disk` inside a workspace answers the only version of the question available in there; `wk disk` with the podman machine stopped leaves it stopped [needs a workspace]
- [ ] `wk vm base --rm` deletes the golden base, then asks separately about the pulled OCI image, and existing vm workspaces keep working [needs a macOS VM]
- [ ] `wk new <name> --target <peer>` and `wk rm` of a peer's workspace refuse here and name the command to run over there — every other workspace command is handed to the peer, so these two are the remaining pair a person has to type on the other machine [needs a peer]
- [ ] every command under `cmd/` declares itself to the dispatcher: line 3 is a one-line `# wk <name> <args> -- <summary>` synopsis, and a `# wk:` line in the first 15 lines names `where=` (one of `host|store|local|workspace|dynamic`) and `group=` [needs a test]
