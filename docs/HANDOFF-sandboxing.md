# HANDOFF — the sandbox escape audit

These mechanisms are built and in daily use:
`wk push on|off|status` (deploy keys held outside the workspace; `wk claude`
turns the switch off before an agent takes over), sudo costing a password
everywhere, and `wk <command> --force` as the one loud way past a barrier.

**The audit itself has not been run, on either platform.** It is scheduled last
on purpose: everything built before it changes the attack surface, so auditing
earlier means auditing again.

## The request, kept verbatim

> We should analyse the complete setup and look for sandbox escapes. Newer
> models are known to be agressive.
>
> Issues I have had:
>
> claude overwriting my work
> claude posting on github on my behalf and responding to my reviewers
> claude seaching for ssh keys on my host (throught the container sdk)
> claude making a suid binary then running it to bypass auto mode
> claude searching for a sudo time seat to skip pw auth
>
> Consider which machines can be ssh'd into too.

That incident list is the test plan. None of it has been re-attempted against
the current tree.

## Remaining — still asked, not yet built

- **A way to fully disable `git commit` inside a container**, for whoever wants
  it off entirely rather than gated by the push switch.

## Remaining — the surface to cover

One holistic pass over the system as it actually ships: session D-Bus, host
mounts, suid-binary-to-bypass-auto-mode, credential and ssh-key search, and
every surface added since the request:

- **The egress allowlist is far wider than Anthropic/GitHub/PyPI.**
  `container/proxy/wk-proxy.py`'s `ALLOWED_HOSTS` now carries browserbench,
  webkit.org, gnome.org, igalia.com **and a general-purpose top-sites browsing
  set with their CDNs**, on ports 80 and 443 — so a workspace can reach
  user-generated content and ad/analytics networks. The file flags this for the
  audit itself. `BLOCKED_NETS` and the exact-match `pi-hosts` exemption are the
  compensating controls, and neither has a test yet.
- **`apt` from the Ubuntu archive** (`ports.`/`archive.`/`security.ubuntu.com`,
  so `wk zed` can install openssh-server in a workspace, which
  is the only thing an editor can talk to over the `podman exec` transport). A
  workspace already has `sudo`, so what this permits is any package in the
  distribution, installed inside the sandbox. The alternative on the table was a
  derived image built *outside* the sandbox, where the network is not the
  workspace's; the wider allowlist was taken instead as a deliberate call, and
  the audit should decide whether it stays. Nothing else in the tree fetches
  from these hosts.
- **The fleet-request broker** (`container/broker/wk-broker.py`).
  A second unix socket in the one directory a workspace can see,
  and the only path from inside the sandbox to physical hardware: it accepts
  seven verbs (`capabilities`, `status`, `stage`, `arm`, `keep`, `run`,
  `release`, `disarm`) and turns each into a fixed `wk boot` / `wk pi` argv on
  the workstation. It is a *narrowing* — a workspace could not reach a board at
  all before, and still cannot reach one directly — but it is new surface and
  the audit should treat it as such. What to attack, specifically: the argument
  validation (`NAME_RE`, `want_*`), the claim that no request word ever reaches
  a shell, the claim that `WK_FORCE` cannot be set from a request, and the
  per-machine in-flight lock. The one verb that removes a safety net is `keep`,
  which cancels a board's self-return watchdog; `build_keep` argues for it.
  On macOS the socket reaches the containers through an `ssh -R` remote unix
  forward the broker holds open into the podman machine — a Mac-initiated
  connection with no listener on any network address, which is its own thing to
  look at.
- **Remote targets** — shared build machines a workspace's work runs on.
- **The tailnet bridges** — `wk bridge`, camera streaming, and a routed
  10.99.x segment reachable from the workstation.
- **The device paths** — `wk pi deploy` and `wk pi bench` put code on a board
  and run it; `wk sysimage write` writes physical disks over ssh.
- **The yocto build's own egress widening** (`docs/HANDOFF-yocto.md`).
- **The restored git helpers** — `wk pr`, `wk pick` and `container/bin/git-sync-fork`
  all reach GitHub, and only the push switch stands between an agent and a
  publish.
- **macOS, as a separate pass**, against the Tart VM model — different in kind,
  so it may hide different bugs. One input is already on the record: the golden
  base provisions with **egress unfiltered**, because Softnet's flags are passed
  in `t_start` only, so the base boots on plain vmnet. Host-driven and one-shot,
  but it should be a decision on the record rather than an accident. Also verify
  the push switch behaves the same there.
- **Two live sudo grants that contradict the password rule**, both reported by
  the tooling itself rather than found by the audit: moose's `/usr/bin/tee` is
  NOPASSWD, which is passwordless write to any file and therefore equivalent to
  NOPASSWD root; and **the rpi5 grants
  `(ALL) NOPASSWD: ALL`** — `sudo -n -l` on the board shows it, so root costs
  nothing at all there, and `wk sudo status` is what reports it. Remedy:
  `wk sudo setup`. Fix the rpi5 now; narrowing moose's grant is the audit's.

Run it alongside `docs/Security/HANDOFF-tailscale.md`: its items 5-7 are the
same question asked from the network side.
