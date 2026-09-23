"""What is provisioned on this machine and what a rebuild still needs: every
check is a row (state, what, remedy), state ok | miss | unk | note, and one
renderer prints the rows and counts the misses."""

import os
import re

from wk import record, shell, targets
from wk.machine import Local
from wk.status import kv, machine_confs
from wk.store import Store

OK, MISS, UNK, NOTE = "ok", "miss", "unk", "note"
MARK = {OK: "\033[32mok\033[0m", MISS: "\033[31m--\033[0m", UNK: "\033[33m??\033[0m"}

GIT_KEYS = (("name", "user.name"), ("email", "user.email"),
            ("fsmonitor", "core.fsmonitor"), ("manyfiles", "feature.manyFiles"))
GIT_PROBE = "command -v git >/dev/null 2>&1 || exit 0\n" + "".join(
    'printf "git.%s=%%s\\n" "$(git config --get %s 2>/dev/null || true)"\n' % (k, c) for k, c in GIT_KEYS)


def ok(what):
    return (OK, what, "")


def miss(what, remedy):
    return (MISS, what, remedy)


def unk(what, remedy):
    return (UNK, what, remedy)


def note(what):
    return (NOTE, what, "")


def check(what, remedy, good):
    return ok(what) if good else miss(what, remedy)


class Report:
    def __init__(self, out):
        self.out = out
        self.missing = 0

    def section(self, title):
        self.out.write("\n%s\n" % title)

    def row(self, row):
        state, what, remedy = row
        if state == OK:
            self.out.write("  %s    %s\n" % (MARK[OK], what))
        elif state == NOTE:
            self.out.write("        %s\n" % what)
        else:
            self.out.write("  %s    %-46s -> %s\n" % (MARK[state], what, remedy))
        self.missing += state == MISS

    def rows(self, rows):
        for r in rows:
            self.row(r)

    def exit_status(self):
        return 1 if self.missing else 0


def findings(text, default_remedy=""):
    """A bash driver's <state>\\t<what>\\t<remedy> lines (ok | wrong | required | wanted | note) as rows."""
    rows = []
    for line in (text or "").splitlines():
        state, what, remedy = (line.split("\t") + ["", ""])[:3]
        if state == "ok":
            rows.append(ok(what))
        elif state in ("wrong", "required", "wanted"):
            rows.append(miss(what, remedy or default_remedy))
        elif state == "note":
            rows.append(unk(what, remedy))
    return rows


# -- credentials: a verdict line is <verdict>\t<detail>, the detail's `    key: value` lines being facts

def cred_verdict(line):
    return line.split("\t", 1)[0]


def cred_detail(line):
    return line.split("\t", 1)[-1]


def cred_first(line):
    return (cred_detail(line).splitlines() or [""])[0]


def cred_fact(line, key):
    m = re.search(r"^ *%s: (.*)$" % re.escape(key), line, re.M)
    return m.group(1) if m else ""


def remote_control_row(where, line):
    fact = cred_fact(line, "remote-control")
    if fact == "allowed":
        return [ok("remote control in %s: allowed by the organization's policy" % where)]
    if fact == "denied":
        return [miss("remote control in %s: denied by the organization's policy" % where, cred_fact(line, "fix"))]
    if not fact:
        return []
    return [unk("remote control in %s: unverified" % where, cred_first(line))]


def credentials_section(names, verdict_of):
    rows = []
    for name in names:
        line = verdict_of(name)
        v, why = cred_verdict(line), cred_first(line)
        if v == "ok":
            rows.append(ok("%s -- %s" % (name, why)))
        elif v == "wide":
            rows.append(unk("%s reaches further than wk spends it" % name, why))
        elif v == "bad":
            rows.append(miss("%s: %s" % (name, why), cred_fact(line, "fix") or "wk key check"))
        elif v == "absent":
            rows.append(unk("%s: %s" % (name, why), "wk key setup"))
        else:
            rows.append(unk("%s: %s" % (name, why), "wk key check"))
        if name == "claude-login":
            rows += remote_control_row("the workspaces this login reaches", line)
    return rows


def fleet_logins_section(peers, verdict_of):
    rows = []
    for peer in peers:
        line = verdict_of(peer)
        v, what = cred_verdict(line), "%s: %s" % (peer, cred_first(line))
        if v in ("ok", "wide"):
            rows.append(ok(what))
            rows += remote_control_row("the workspaces %s makes" % peer, line)
        elif v in ("absent", "bad"):
            rows.append(miss(what, "wk key setup  (from a terminal here: it logs in for %s)" % peer))
        else:
            rows.append(unk(what, "wk key check"))
    return rows


# -- git identity, wherever a git.* blob came from

def git_fields(machine):
    if not machine.run(["which", "git"]).ok:
        return ""
    return "".join("git.%s=%s\n" % (k, machine.run(["git", "config", "--get", c]).out.strip()) for k, c in GIT_KEYS)


def git_config_findings(label, blob, remedy, want):
    f = kv(blob)
    if "git.name" not in f:
        return [unk("%s: git not installed there" % label, "")]
    rows = []
    for v in ("name", "email"):
        have, w = f.get("git." + v, ""), want.get(v, "")
        if w and have == w:
            rows.append(ok("%s: git user.%s = %s" % (label, v, have)))
        elif not have:
            rows.append(miss("%s: git user.%s is not set there" % (label, v), remedy))
        else:
            rows.append(miss("%s: git user.%s there is '%s', not this repo's '%s'" % (label, v, have, w), remedy))
    speed = "%s: git speed settings (fsmonitor, manyFiles)" % label
    rows.append(check(speed, remedy, f.get("git.fsmonitor") == "true" and f.get("git.manyfiles") == "true"))
    return rows


def vm_guest_git_findings(target, want):
    rows = []
    for name, _ in target.list():
        if target.info(name) != "running":
            continue
        r = target.exec(name, ["sh", "-c", GIT_PROBE])
        blob = r.out if r.ok else ""
        if not blob.strip():
            rows.append(unk("%s (tart guest): git config did not answer" % name, "wk vm check %s" % name))
            continue
        rows += git_config_findings("%s (tart guest)" % name, blob, "wk start %s (the include is written on every start)" % name, want)
    return rows


# -- the store, probed where it lives

def probe_store(store, machine, branches, env):
    """key=value lines: every mirror branch this tree declares is named, since one it lacks fails every fetch."""
    runtime = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()

    def sock(name):
        return machine.run(["test", "-S", os.path.join(runtime, "wk", name)]).ok

    def filled(path):
        try:
            return bool(machine.listdir(path))
        except OSError:
            return False

    mirror = store.mirror()
    if not machine.isdir(mirror):
        lines = ["mirror=no"]
    else:
        gap = [b for b in branches if not machine.run(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/heads/" + b]).ok]
        lines = ["mirror=ok" if not gap else "mirror=gap " + " ".join(gap)]
    lines.append("base=" + ("ok" if filled(store.base_dir()) else "no"))
    lines.append("skills=" + ("ok" if filled(os.path.join(store.root(), "skills")) else "no"))
    proxy = machine.run(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"]).ok or sock("proxy.sock")
    lines.append("proxy=" + ("ok" if proxy else "no"))
    try:
        pihosts = bool(machine.read(os.path.join(store.root(), "pi-hosts")))
    except OSError:
        pihosts = False
    lines.append("pihosts=" + ("ok" if pihosts else "no"))
    lines.append("broker=" + ("ok" if sock("broker.sock") else "no"))
    return "\n".join(lines) + "\n" + git_fields(machine)


def report_store(out, gitremedy, fork_key, macos, want):
    """`gitremedy` only when the probe came from another machine; on the same one the config section already checked git."""
    f = kv(out)
    mirror = f.get("mirror", "")
    if mirror == "ok":
        rows = [ok("WebKit mirror")]
    elif mirror.startswith("gap"):
        rows = [miss("WebKit mirror carries no %s" % mirror[4:], "wk sync --mirror")]
    else:
        rows = [miss("WebKit mirror", "wk sync")]
    rows.append(check("base snapshot", "wk sync", f.get("base") == "ok"))
    rows.append(check("fork push key", "wk key deploy  (needs gh auth)", fork_key))
    rows.append(check("shared skills seeded", "./setup --stage vmtools" if macos else "./setup --stage machine", f.get("skills") == "ok"))
    rows.append(check("egress proxy running", "./setup --stage sdk, then systemctl --user status wk-proxy", f.get("proxy") == "ok"))
    rows.append(check("pi-hosts populated", "wk pi setup rpi5 / rpi4  (skip if no Pi devices)", f.get("pihosts") == "ok"))
    rows.append(check("fleet-request broker reachable from workspaces",
                      "./setup --stage broker  (macOS: it also publishes the socket into the VM)", f.get("broker") == "ok"))
    if gitremedy:
        rows += git_config_findings("container machine", out, gitremedy, want)
    return rows


# -- the bridge phones and this Mac

def battery_verdict(name, blob):
    f = kv(blob)
    pct, st, limit, cur = f.get("percent") or "?", f.get("status") or "?", f.get("limit", ""), f.get("current", "")
    if cur and cur != "?" and cur == limit:
        return ok("%s: %s%% %s, capped at %s%%" % (name, pct, st, cur))
    return miss("%s: %s%% %s, cap reads %s (want %s)" % (name, pct, st, cur or "?", limit or "?"), "wk bridge setup %s" % name)


def mac_battery_line(out):
    lines = out.splitlines()
    m = re.search(r"\s(\d{1,3})%;", lines[1]) if len(lines) > 1 else None
    if not m:
        return None
    return "%s, %s%% -- no OS limit exists" % ("plugged in" if "'AC Power'" in lines[0] else "on battery", m.group(1))


class Doctor:
    """This machine's checks; `sh` answers what the bash library still holds (lib/wk/shell.py)."""

    def __init__(self, root, env=None, machine=None, macos=None, sh=shell):
        self.root = root
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.sh = sh
        self.store = Store(self.env)
        self.macos = (os.uname().sysname == "Darwin") if macos is None else macos
        self.macos_host = self.macos and not self.env.get("WK_IN_VM")
        self.home = self.store.home()
        self.reg = targets.Registry(root, self.env, self.machine)
        self.container = self.reg.load("container")
        self._paths = None

    def have(self, name):
        return self.machine.run(["which", name]).ok

    def hostname(self):
        return record.host_name(self.machine)

    def read(self, path):
        try:
            return self.machine.read(path)
        except OSError:
            return ""

    def want(self):
        conf = os.path.join(self.root, "dotfiles", "gitconfig")
        return {v: self.machine.run(["git", "config", "--file", conf, "--get", "user." + v]).out.strip() for v in ("name", "email")}

    def paths(self):
        if self._paths is None:
            self._paths = self.sh.local_state_paths(self.root, env=self.env)
        return self._paths

    def podman_state(self):
        return self.container.machine_state()

    def in_vm(self, command):
        return self.sh.in_machine(self.root, command, env=self.env, quiet=True)

    def probe_store(self):
        return probe_store(self.store, self.machine, self.sh.mirror_branches(self.root, env=self.env), self.env)

    def sections(self, everything):
        yield "host tools", self.host_tools()
        yield "credentials", credentials_section(self.sh.cred_settable(self.root, env=self.env),
                                                 lambda n: self.sh.cred_verdict(self.root, n, env=self.env))
        yield "root access", self.root_access()
        yield "config (./setup owns these)", self.config()
        yield "machine-local state (everything a rebuild cannot get from this repo)", self.machine_local()
        yield "workspaces store", self.workspaces_store()
        yield "privileged helpers", self.privileged_helpers()
        if not self.macos:
            yield "benchmarking", self.benchmarking()
        if self.macos_host:
            yield "macOS VM target (optional -- Apple ports)", self.vm_target()
        if not everything:
            return
        yield "claude.ai login on the other workstations (each its own)", fleet_logins_section(
            self.sh.peer_workstations(self.root, env=self.env),
            lambda p: self.sh.peer_cred_verdict(self.root, p, "claude-login", env=self.env))
        for t in self.reg.all():
            if t != "container" and self.reg.kind(t) == "remote":
                yield "build machine: %s" % t, self.build_machine(t)
        yield "battery", self.battery()

    def host_tools(self):
        if self.macos:
            yield check("Xcode command line tools", "xcode-select --install", self.machine.run(["xcode-select", "-p"]).ok)
            yield check("podman", "install the official pkg from podman.io", self.have("podman"))
            yield check("zed", "https://zed.dev/download", self.have("zed") or self.machine.isdir("/Applications/Zed.app"))
            yield check("tailscale", "https://tailscale.com/download/macos",
                        self.have("tailscale") or self.machine.isdir("/Applications/Tailscale.app"))
        else:
            for tool, what in (("podman", "podman"), ("zsh", "zsh"), ("cage", "cage (benchmark kiosk)"), ("wlr-randr", "wlr-randr (session off)")):
                yield check(what, "./setup --stage tools", self.have(tool))
        if self.have("nmap"):
            yield ok("nmap (wk find)")
        else:
            yield unk("nmap absent -- only 'wk find' needs it", "nmap.org, the .dmg" if self.macos else "./setup  (host/linux/apt.txt)")
        yield check("jq (claude hook)", "./setup --stage tools", self.have("jq"))
        yield check("gh", "install gh, then: gh auth login", self.have("gh"))
        if self.have("gh"):
            yield check("gh authenticated", "gh auth login   (then: wk key deploy)", self.sh.gh_authenticated(self.root, env=self.env))

    def root_access(self):
        r = self.machine.run(["env", "WK_QUIET=1", os.path.join(self.root, "cmd", "sudo"), "status"])
        out = (r.out + r.err).strip()
        yield ok("sudo: " + out) if r.ok else miss("sudo: " + out, "wk sudo setup")

    def config(self):
        def linked(path, target):
            return self.machine.run(["readlink", path]).out.strip() == target
        yield check("~/.claude/settings.json is the HOST settings", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "settings.json"), os.path.join(self.root, "claude", "settings-host.json")))
        yield check("~/.claude/CLAUDE.md is the HOST briefing", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "CLAUDE.md"), os.path.join(self.root, "claude", "CLAUDE-host.md")))
        yield check("shell rc sources shell/bashrc", "./setup --stage dotfiles",
                    any("wk-tools/shell/bashrc" in self.read(os.path.join(self.home, rc)) for rc in (".zshrc", ".bashrc")))
        if self.have("git"):
            have, want = kv(git_fields(self.machine)), self.want()
            for v in ("name", "email"):
                yield check("git user.%s is the repo's" % v, "./setup --stage dotfiles", have.get("git." + v, "") == want.get(v, ""))
            yield check("git speed settings (fsmonitor, manyFiles)", "./setup --stage dotfiles",
                        have.get("git.fsmonitor") == "true" and have.get("git.manyfiles") == "true")

    def local_state(self, path, kind, how):
        disp = "~" + path[len(self.home):] if path.startswith(self.home) else path
        if self.macos_host and path.startswith(self.store.root() + "/"):
            disp += " (podman VM)"
            if self.podman_state() != "running":
                return unk("%s -- not visible while the podman machine is stopped" % disp, "%s: %s" % (kind, how))
            present = self.in_vm("test -e %s" % shell.sh_quote(path)) is not None
        else:
            present = self.machine.exists(path)
        if present:
            return ok("%s (%s) -- %s" % (disp, kind, how))
        return unk("%s (absent)" % disp, "%s: %s" % (kind, how))

    def machine_local(self):
        store, p = self.store, self.paths()
        yield self.local_state(store.bench_dir(), "backed-up",
                               "benchmark runs and their provenance -- not regenerable at any price; a rerun is a different measurement")
        yield self.local_state(store.mirror(), "regenerable",
                               "wk sync clones WebKit into it again (the one copy here; the podman VM and every tart guest read it)")
        yield self.local_state(store.secrets_dir(), "regenerable", "wk key deploy makes new deploy keys (revoke the old ones on GitHub)")
        yield self.local_state(p["push_held"], "regenerable",
                               "wk key deploy makes new deploy keys; wk key set github-pat and wk key set bugzilla-api-key store new ones "
                               "(revoke the old ones on GitHub and Bugzilla)")
        yield self.local_state(p["read_pat"], "regenerable",
                               "./setup and wk key set github-pat both write it from the token in %s" % p["push_held"])
        for key, path in p.items():
            if key.startswith("secret."):
                yield self.local_state(path, "re-authable",
                                       "wk key set %s stores one; every workspace this machine makes starts authenticated with it" % key[7:])
        yield self.local_state(self.env.get("WK_TS_AUTHKEY") or os.path.join(self.home, ".config", "wk", "tailscale-authkey"), "re-authable",
                               "wk key set tailnet asks for one (tag:wk, reusable, not ephemeral, longest expiry); joined nodes are unaffected")
        yield self.local_state(p["tailscale_api"], "re-authable",
                               "wk key set tailnet-api asks for one; only this machine holds it, and only writes that must retire a stale node need it")
        yield self.local_state(p["ntfy_topic"], "re-authable",
                               "wk key set ntfy mints the ntfy.sh topic wk_notify (lib/store.sh) publishes to; only this machine holds it, "
                               "and a new one is one subscription away")
        yield self.local_state(os.path.join(store.state_dir(), "broker"), "regenerable",
                               "request records (argv, log, status) the fleet-request broker writes; the next request makes new ones")
        yield self.local_state(os.path.join(self.home, ".ssh", "config.d", "local"), "backed-up",
                               "hand-written ssh entries (host/dotfiles.sh moves them here and owns the rest)")
        yield self.local_state(os.path.join(store.state_dir(), "ssh", "zed_ed25519"), "regenerable",
                               "wk zed makes a new one and re-authorises it in the workspace")
        if self.macos:
            yield self.local_state(os.path.join(store.state_dir(), "mac-tailnet", "tolken-bench.state"), "regenerable",
                                   "the bench install's tailnet node identity, kept on the host install the way a board keeps its bench node "
                                   "on its rescue; lost, it rejoins under a new name")
        else:
            rpi5 = dict(machine_confs(self.root, self.env)).get("rpi5")
            if rpi5 and rpi5.get("NODE_SSH") == self.machine_name():
                yield self.local_state(os.path.join(self.root, "host", "linux", "rpi5", "rpi5.conf"), "backed-up",
                                       "site WiFi identity (gitignored: repo is public); rpi5.conf.example documents the shape")
            yield self.local_state("/var/lib/tailscale", "re-authable",
                                   "tailscale up re-authenticates; the node name survives via the admin console")
        if not self.macos_host:
            return
        if self.podman_state() != "running":
            yield unk("/var/lib/tailscale (podman VM) -- not visible while the podman machine is stopped", "re-authable: ./setup --stage machine")
            return
        ip = (self.in_vm("sudo tailscale ip -4 2>/dev/null | head -1") or "").replace("\r", "").strip()
        if ip:
            yield ok("/var/lib/tailscale (podman VM) (re-authable) -- on the tailnet at %s; ./setup --stage machine re-joins it "
                     "and the node name survives via the admin console" % ip)
        else:
            yield miss("/var/lib/tailscale (podman VM) -- the machine holds this workstation's lanes and reaches no board without it",
                       "./setup --stage machine, which needs a live tailnet auth key: wk key set tailnet")

    def machine_name(self):
        return record.machine_name(self.env, self.machine)

    def workspaces_store(self):
        fork_key = self.machine.exists(os.path.join(self.paths()["push_held"], "build_key_fork"))
        if not self.macos_host:
            yield from report_store(self.probe_store(), "", fork_key, self.macos, self.want())
            return
        state = self.podman_state()
        if state == "absent":
            yield miss("podman machine '%s'" % self.container.machine_name(), "./setup --stage machine")
            return
        if state != "running":
            yield unk("podman machine '%s' is stopped" % self.container.machine_name(), "wk start, then re-run wk doctor for the store checks")
            return
        yield ok("podman machine '%s' running" % self.container.machine_name())
        out = self.in_vm("WK_STORE=/var/lib/wk python3 /opt/wk-tools/cmd/doctor --probe-store")
        if not out:
            yield unk("store inside the VM", "/opt/wk-tools missing in the VM? run ./setup --stage sdk")
            return
        yield from report_store(out, "podman machine ssh %s -- git config --global include.path /opt/wk-tools/dotfiles/gitconfig" % self.container.machine_name(),
                                fork_key, True, self.want())

    def privileged_helpers(self):
        for name, where, what, path, sudoers in self.sh.priv_helpers(self.root, env=self.env):
            if where == "linux" and self.macos:
                continue
            if not self.machine.run(["test", "-x", path]).ok:
                yield miss("%s (%s)" % (name, what), "./setup --stage quiesce  (interactive sudo)")
            elif self.sh.priv_answers(self.root, path, env=self.env):
                yield ok("%s (%s)" % (name, what))
            else:
                yield miss("%s: installed, and sudo -n still asks for a password" % name,
                           "%s has to be the last match 'sudo -l' shows, and name %s character for character -- ./setup --stage quiesce"
                           % (sudoers, path))

    def benchmarking(self):
        yield check("render group configured", "./setup --stage tools, then log out and in", "render" in self.machine.run(["id", "-nG"]).out.split())

    def vm_target(self):
        vm = self.reg.load("vm")
        if not vm.tart():
            yield unk("tart not installed", "README.md, Setup -- only needed for Apple-port builds")
            return
        yield ok("tart installed")
        softnet = self.env.get("WK_SOFTNET_BIN") or "/usr/local/bin/softnet"
        yield check("softnet installed SUID root", "./setup --stage softnet  (interactive sudo)",
                    self.machine.run(["test", "-x", softnet, "-a", "-u", softnet]).ok)
        base = self.sh.vm_base_findings(self.root, env=self.env)
        if base:
            yield from findings(base, "wk vm ls")
        else:
            yield unk("golden base VM: the vm driver did not answer", "wk vm ls")
        yield from vm_guest_git_findings(vm, self.want())

    def build_machine(self, t):
        probe = self.sh.remote_probe(self.root, t, env=self.env)
        if not probe:
            yield unk("%s did not answer" % t, "ssh %s true  -- then re-run; nothing was changed" % t)
            return
        yield from findings(self.sh.remote_findings(self.root, probe, env=self.env), "see 'wk remote setup %s'" % t)
        why = self.sh.remote_provision_stale(self.root, t, env=self.env)
        if why:
            yield miss("provisioning on %s predates its inputs: %s" % (t, why), "wk remote setup %s" % t)
        else:
            yield ok("provisioned from this tree's remote/provision.sh + remote/deps.sh")

    def battery(self):
        bridge = os.path.join(self.root, "cmd", "bridge")
        names = self.machine.run([bridge, "ls", "--names"])
        for name in names.out.split() if names.ok else []:
            blob = self.machine.run([bridge, "battery", name])
            if not (blob.ok and blob.out.strip()):
                yield unk("%s: did not answer" % name, "wk bridge status %s" % name)
                continue
            yield battery_verdict(name, blob.out)
        if self.macos:
            line = mac_battery_line(self.machine.run(["pmset", "-g", "batt"]).out)
            if line:
                yield unk("this Mac: " + line, "docs/Urgent/HUMAN-battery.md -- no OS knob exists yet")
