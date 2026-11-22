# wk: status

What of pass 1 (`DESIGN.md`, Plan) exists, and how far each piece is proven.
"Unit-tested" means `wk selftest` types it into fake machines; no real server,
store or Ansible run is in that. "Live" means it ran on a real machine.

## Pass 1

| Piece | Implemented | Unit-tested | Verified live |
| --- | --- | --- | --- |
| Launcher, dispatcher, declared commands, `--dry-run`, `--yes`, `--force`, exit codes | yes | yes | no |
| Machine confs, the fleet, `wk machine ls` | yes | yes | no |
| Hand-over to a server over ssh, `wk ls --json` routing | yes | yes | no |
| The reverse forward a command declares (`wk ai` to a borrowed box) | yes | yes: the ssh argv and the port | no |
| `wk help`, `wk selftest` | yes | yes | no |
| The mirror, `wk new`, `wk rm`, `wk ls`, `wk enter`, `wk sync` | yes | yes, and against a real local git | no |
| The `plain` driver | yes | yes | no |
| The `container` driver (rootless podman, the SDK image) | yes | yes: the podman lines | no |
| The pueue client, `wk status`, `wk stop` | yes | yes, against pueue's own state fixture | no |
| Job sizing | yes | yes | no |
| The memory guard (cgroup ceiling, watchdog) | yes | yes | no |
| Build presets, `wk build`, `wk run`, `wk test` | yes | yes | no |
| The store's client (the `bao` CLI), `wk key set`, `check`, `ls` | yes | yes: the argv and the refusals | no |
| Each credential's rule and live probe | yes | yes, HTTP scripted | no |
| The injector (proxy, TLS, refresh of the login) | yes | yes, on loopback sockets | no |
| The credential wrapper (`bwrap`, Seatbelt, minimal environment) and its probe | yes | yes: the argv and the verdicts | no |
| Hidden directories on the credential server (`injector/`, `openbao/`) | yes | yes | no |
| `wk ai claude`, `wk ai pi`, the build-tool shims | yes | yes; the shim against a real `sh` | no |
| `wk ai claude` on a borrowed box through the forward | yes | yes | no |
| `wk machine setup`, `wk key sync`: the inventory and the `ansible-playbook` line | yes | yes | no |
| The playbook: roles `wk`, `pueue`, `mirror`, `sdk`, `agents` | yes | no: only its YAML is parsed | no |
| The playbook: roles `openbao`, `injector` | yes | no: only its YAML is parsed | no |
| The playbook: roles `credentials`, `borrowed-claude` | yes | no: only its YAML is parsed | no |
| `wk doctor`, `wk gc` | yes | yes | no |
| `wk machine install`: the refusals, the question, the `ansible-playbook` line, `--rehearse` | yes | yes | no |
| The rules for `tailnet-api-key`, `wifi-ssid`, `wifi-psk`, `bmc/<machine>/user` and `password` | yes | yes, HTTP scripted | no |
| The install playbook (`ansible/install.yml`, `ansible/install/`) | yes | no: its YAML is parsed and its autoinstall template rendered by a real Ansible | no |

## Verified live

Nothing yet.

## Not yet verified live

Each is a check owed on a real machine. None has been run.

- TODO: `ansible-playbook --syntax-check` of `ansible/site.yml` with a real Ansible (the `live` test in `tests/test_provision_setup.py` runs it where Ansible is installed).
- TODO: `wk machine setup` on a Linux server that is also the credential server, twice: the second run reports `changed=0`.
- TODO: `wk machine setup` on a Mac and on a borrowed box, twice each.
- TODO: `wk machine setup --dry-run` on a bare and on a provisioned server: what Ansible's check mode reports, and that it stops nowhere it should not.
- TODO: Ansible on the owner's servers without `become`: fact gathering, the login shell's `PATH` as the tasks' `PATH`, `loginctl enable-linger` and `systemctl --user` over a session with no login.
- TODO: the `wk` role: `ansible.builtin.git` at a commit sha from the public URL, and the profile line as each login shell reads it.
- TODO: pueue 4.0.5: the pinned binaries' digests, the daemon under wk's config as a systemd user unit, the JSON of `status` and `log`, a task's environment (it is the `pueue add`'s), and the client reaching the socket from inside a container.
- TODO: pueued as a launchd agent on a Mac reached only by ssh: `launchctl bootstrap gui/<uid>` with no one at the console, and the agent after a reboot.
- TODO: pueued on a borrowed box: `--daemonize` inside someone else's container, and what remains of it after the container restarts.
- TODO: the mirror role: `wk sync` run by Ansible for the first fetch of WebKit, which takes tens of minutes over one ssh session.
- TODO: podman and the SDK image: `containers.podman.podman_image`, the `podman run` line (keep-id, the host's name, `HOME`, the home mounted) with the real image, subordinate ids, and `wk` typed inside the container.
- TODO: the agents role: the official Claude Code installer under a staging `HOME` at the pinned version, and `npm install --ignore-scripts` of pi.
- TODO: OpenBao 2.7.3: the release archive's layout (a `bao` at its root), the `raft` storage and `static` seal stanzas as written, the listener on the tailnet address before Tailscale is up at boot, and the server unsealed after a reboot with no person.
- TODO: `bao operator init -format=json -recovery-shares=1 -recovery-threshold=1` against a static seal: that it is accepted, and the JSON it prints (`root_token`).
- TODO: the `bao` CLI as the client uses it: `kv get -field=value` (and whether it ends its output with a newline), `kv put ... value=-` from stdin, `kv list -format=json`, the words and exit code for a path that holds nothing ("No value found", 2), and a `bao login` token.
- TODO: the credentials role: values through `lookup('ansible.builtin.pipe', 'bao kv get ...')`, that no value reaches a log, argv or a diff, and `copy` leaving a JSON document as it was given.
- TODO: the injector role: the unit, the CA it makes at first start, and the delivered-digest marker keeping a refreshed login.
- TODO: the injector and Remote Control: Claude reading the placeholder from `claude-login/`, TLS through the injector's CA, a refresh, and Remote Control through the proxy.
- TODO: the wrapper: the Seatbelt profile on macOS, `bwrap` inside a container (three hidden directories on the credential server), and whether WebKit's own bubblewrap sandbox runs inside it.
- TODO: git wiring with `git-webkit`: `env.sh` as `git-webkit` and `gh` read it, a push through the deploy-key alias, a pull request, a Bugzilla login with `bugzilla-password`.
- TODO: a borrowed box: a writable child cgroup, `nice` and `ionice`, and Claude as root under `--permission-mode auto`.
- TODO: Claude on a borrowed box: the reverse forward through the gateway's `ProxyJump`, `ExitOnForwardFailure` on a taken port, the session through `http://127.0.0.1:<port>`, and Remote Control over it.
- TODO: a live run of every pass 1 command on a Linux server, a Mac and a borrowed box, from a client its owner configured.
- TODO: `wk machine install`: nothing of it has run against a BMC, a media host or a server. Each line below is unverified.
- TODO: install: `xorriso -boot_image any replay` on Ubuntu's arm64 live-server image: that the result still boots under UEFI, and that its `/boot/grub/grub.cfg` has the `linux /casper/vmlinuz ---` line the playbook edits.
- TODO: install: subiquity reading `/cdrom/autoinstall.yaml` with `autoinstall` on the kernel command line: no question asked, on a BMC's virtual CD.
- TODO: install: the BMC's Redfish as `community.general.redfish_command` drives it: `VirtualMediaInsert` under the Manager, the `TransferProtocolType` it lists (AMI firmware commonly lists NFS, CIFS and HTTPS and not HTTP), whether remote media must first be enabled, `SetOneTimeBoot` to `Cd` in UEFI mode, `PowerForceRestart`, `PowerOn`, `DisableBootOverride`.
- TODO: install: the BMC fetching a 3.8 GB image from `python3 -m http.server`, which answers no Range request.
- TODO: install: the media host: `systemd-run` of the server as root, `nft --json --handle list chain` and a rule found again by its comment, and `ansible.builtin.copy` of the image over the tailnet.
- TODO: install: WiFi in the installer and on the first boot: `wpasupplicant` from the image's own pool, the conf's interface name under Ubuntu's kernel, and cloud-init waiting for a WiFi network before it installs packages.
- TODO: install: Ubuntu's generic kernel on the server's hardware (the machine now runs a vendor kernel).
- TODO: install: the installer powering the machine off, the BMC reporting `PowerState` `Off`, and the machine booting its disk at the next `PowerOn` with the image ejected.
- TODO: install: cloud-init's `apt.sources` with `signed-by=$KEY_FILE` for Tailscale's repository, and `tailscale up --auth-key=file:` with `--advertise-tags` under the stored key.
- TODO: install: the tailnet API with `tailnet-api-key`: listing devices, deleting the old node, and the new node taking the bare name.
- TODO: install: `StrictHostKeyChecking=accept-new` through Ansible's ssh for the new host key, and Tailscale SSH admitting the conf's user under the tailnet's policy.
- TODO: install: `wk machine setup` on a freshly installed server.
