# HANDOFF — the sandbox escape audit

The mechanisms this file used to also describe are built and in daily use:
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
  compensating controls, and both TESTING.md lines for them are unticked.
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
  NOPASSWD root (`docs/HANDOFF-boot.md`); and **the rpi5 grants
  `(ALL) NOPASSWD: ALL`** — `sudo -n -l` on the board shows it, so root costs
  nothing at all there, and `wk sudo status` is what reports it. Remedy:
  `wk sudo require`. Fix the rpi5 now; narrowing moose's grant is the audit's.

Run it alongside `docs/Security/HANDOFF-tailscale.md`: its items 5-7 are the
same question asked from the network side.
