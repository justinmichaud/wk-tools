# Setting up a new machine

From a bare macOS or Ubuntu 26.04 install to a working WebKit environment.

The whole thing is one script plus three manual steps that need credentials or a
password prompt. Nothing here is order-sensitive except that `./setup` comes
first.

---

## 0. What you need beforehand

**macOS.** Four things, all signed installers or Apple tooling. `./setup`
installs nothing and will tell you which are missing:

| | Where |
|---|---|
| Xcode command line tools | `xcode-select --install` |
| podman | <https://podman.io/docs/installation#macos> — the official `.pkg`, **not** Homebrew |
| Zed | <https://zed.dev/download> |
| Tailscale | <https://tailscale.com/download/macos> |

Homebrew is deliberately not used and not required.

**Ubuntu 26.04.** Nothing. `./setup` installs stock apt packages plus Tailscale
(the one third-party repository); Zed and Claude Code install into `~/.local`
via their own scripts.

---

## 1. Clone and run setup

```sh
git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools
cd ~/Development/wk-tools
./setup
```

Takes a few minutes on macOS, mostly downloading the VM image.

`./setup` is idempotent — run it whenever you sit down, and any time you change
something in this repo. A second run in a row reports **no changes**. If it
reports changes on an unmodified machine, that is a bug.

What it does:

- Verifies the host tools above.
- Applies desktop settings (keyboard shortcuts, dark mode, text substitution
  off) and links the dotfiles it owns.
- Installs the Claude config into `~/.claude`.
- Registers the `wk` MCP server with Claude Desktop.
- Creates the `wk` podman VM, sized to leave your desktop usable.
- Syncs this repo into the VM, clones and patches the container SDK, and loads
  the workspace firewall.

Individual stages: `./setup --stage <tools|settings|dotfiles|claude|mcp|machine|vmtools|quiesce>`.

> **It will offer to delete `podman-machine-default`.** Say yes unless you have
> containers in it you care about. applehv runs only one VM at a time, so a
> leftover machine actively prevents `wk` from starting — and it is typically
> tens of gigabytes.

### Existing files

`setup` never clobbers anything. A real file where it wants a symlink is moved
to `<name>.wk-backup` once, and it edits `~/.zshrc`, `~/.bashrc`, `~/.gitconfig`
and `~/.ssh/config` in place by appending a single guarded line each.

---

## 2. The quiesce helper (one sudo prompt)

```sh
./setup --stage quiesce
```

Needs a terminal, because sudo has to prompt. Installs a root-owned helper and
a `NOPASSWD` sudoers rule scoped to that one path, validated with `visudo`
before it goes in, so `wk quiesce on` never asks for a password again.

Skip this if you are not benchmarking. Everything else works without it.

---

## 3. The build key (one GitHub step)

Workspaces push to your fork with a dedicated key. `setup` generates it; you
register it once:

```sh
wk key register
```

Add the printed key at
<https://github.com/justinmichaud/WebKit/settings/keys/new> **with write access
enabled**. Without write access workspaces can read but not push.

It is a GitHub *deploy key*, so it is scoped to that one repository — a
workspace cannot push anywhere else with it, whatever its git config says.

---

## 4. First sources

```sh
wk sync
```

The first run clones WebKit and takes a while. After that it is incremental and
fast.

By default the mirror carries `main` only — WebKit has ~920 branches and
mirroring all of them costs tens of gigabytes for histories nobody checks out.
For more:

```sh
WK_MIRROR_BRANCHES="main safari-7620-branch" wk sync
wk sync --all          # every configured remote, not just origin
```

A workspace can also fetch any branch straight from GitHub on demand.

---

## 5. Use it

Measured on an M4 (10 cores / 32 GB), with the VM at 8 cores / 20 GB:

| | |
|---|---|
| `wk new` | ~30 s, including the Claude CLI install |
| `wk build jsc-release`, cold ccache | ~5 min |
| `wk build jsc-release`, warm ccache | ~3 min |


```sh
wk new bug-238               # instant, whatever the checkout size
wk build bug-238 jsc-release
wk run   bug-238 -- -e 'print(1+1)'
wk claude bug-238            # sandboxed agent
wk rm    bug-238             # reclaims everything it created
```

`wk build --list` shows the configs. `zed ssh://wk-bug-238/src/WebKit` opens it
in Zed.

---

## 6. Optional: Raspberry Pi test devices

Only if you run benchmarks on real hardware.

Put the Pis on an isolated guest network, then:

```sh
wk pi setup rpi5
wk pi setup rpi4
```

Works over SSH against a running device and needs **no image rebuild** — which
matters because the rpi4 runs a buildroot image. It installs Tailscale's static
binaries, tags the node `tag:wk`, and records its tailnet address so the
workspace firewall lets it through.

One manual step, in the Tailscale admin console — grant `tag:wk` access to
itself:

```hujson
"tagOwners": { "tag:wk": ["autogroup:admin"] },
"grants": [
  { "src": ["tag:wk"], "dst": ["tag:wk"], "ip": ["*"] },
  { "src": ["autogroup:member"], "dst": ["*"], "ip": ["*"] }
]
```

> Do not reuse the existing `tag:server`: it covers moose, nextcloud, immich,
> overleaf and the gateway, and workspaces would get all of them.

---

## 7. Optional: macOS VMs for Apple-port builds

```sh
wk vm new mac-rel
wk vm start mac-rel
```

Needs [Tart](https://tart.run). Apple permits exactly **two** macOS VMs per
host and Virtualization.framework enforces it, so `wk vm new` refuses at the
limit rather than failing opaquely.

---

## Moving to another machine

Nothing is machine-specific except what `wk backup` captures. On the new
machine, clone and `./setup`. To carry your current desktop settings across,
run `wk backup` on the old one first and commit the result — it writes live
settings back into `host/macos/defaults.conf`, `host/macos/symbolichotkeys.plist`
and `host/linux/config.dconf`.

`wk backup` never captures credentials. `~/.config/gh` holds an OAuth token and
is excluded explicitly.

---

## Knowing whether a build is alive

`wk build` reports its own state, so this never has to be inferred from a
process list:

```sh
wk status              # all workspaces; exit code is machine-readable
wk status bug-238      # 0 ok/idle, 1 failed, 2 running, 3 stalled
wk logs   bug-238      # errors first, then recent output
wk logs   bug-238 -f   # live
```

While a build runs it prints a heartbeat with the ninja progress counter, and
if the log goes silent it says so rather than letting you wait:

- **300 s of silence** → warning, plus diagnostics: last progress, how many
  compilers are actually running, free memory, and any cgroup OOM kills.
- **1800 s of silence** → the job is killed and reported as stalled (exit 124).

Tune with `WK_STALL_SECONDS`, `WK_ABORT_SECONDS`, `WK_HEARTBEAT_SECONDS`.

On failure the first real error is extracted for you. That matters more than it
sounds: compilers keep going past the first error, so the end of a WebKit build
log is usually unrelated to what actually broke.

## If something is wrong

```sh
WK_DEBUG=1 ./setup          # verbose
wk ls                       # workspaces, their snapshots, disk use
wk gc                       # reclaim disk, including fstrim
```

**Re-running `./setup` is the repair tool.** The VM's configuration — tooling,
SDK checkout and patches, firewall, network, packages — is regenerated from this
repo on every run, so anything changed by hand inside the VM is reverted. Your
data is not touched: the mirror, snapshots, workspaces, caches, shared skills
and the build key all survive.

To start the VM over completely:

```sh
podman machine rm wk && ./setup && wk sync
```

That loses the mirror and every workspace, so it is a last resort rather than a
first move.
