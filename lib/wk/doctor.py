"""What is provisioned on this machine and what a rebuild still needs: every
check is a row (state, what, remedy), state ok | miss | unk | note, and one
renderer prints the rows and counts the misses."""

import os
import re
import shlex

from wk import bridge, fleet, git, guest, priv, reach, record, secrets, status, targets
from wk.bench import record as bench_record
from wk.clock import Clock
from wk.key.cli import Key
from wk.kv import kv
from wk.machine import Local, Ssh
from wk.machine_cmd import Machines, deps as machine_deps
from wk.status import machine_confs
from wk.store import Store, in_vm

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
    return rows


# -- git identity, wherever a git.* blob came from

def git_fields(machine):
    if not machine.have("git"):
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
            rows.append(unk("%s (tart guest): git config did not answer" % name, "wk doctor %s" % name))
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

    mirror = store.mirror_dir()
    if not machine.isdir(mirror):
        lines = ["mirror=no"]
    else:
        gap = [b for b in branches if not machine.run(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/heads/" + b]).ok]
        lines = ["mirror=ok" if not gap else "mirror=gap " + " ".join(gap)]
    lines.append("base=" + ("ok" if filled(store.snapshots_dir()) else "no"))
    lines.append("skills=" + ("ok" if filled(os.path.join(store.store_dir(), "skills")) else "no"))
    proxy = machine.run(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"]).ok or sock("proxy.sock")
    lines.append("proxy=" + ("ok" if proxy else "no"))
    try:
        pihosts = bool(machine.read(os.path.join(store.store_dir(), "pi-hosts")))
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
    rows.append(check("snapshot", "wk sync", f.get("base") == "ok"))
    rows.append(check("fork push key", "wk key deploy  (needs gh auth)", fork_key))
    rows.append(check("shared skills seeded", "./setup --stage vmtools" if macos else "./setup --stage machine", f.get("skills") == "ok"))
    rows.append(check("egress proxy running", "./setup --stage sdk, then systemctl --user status wk-proxy", f.get("proxy") == "ok"))
    rows.append(check("pi-hosts populated", "wk machine setup rpi5 / rpi4  (skip if no Pi devices)", f.get("pihosts") == "ok"))
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
    return miss("%s: %s%% %s, cap reads %s (want %s)" % (name, pct, st, cur or "?", limit or "?"), "wk machine setup %s" % name)


def mac_battery_line(out):
    lines = out.splitlines()
    m = re.search(r"\s(\d{1,3})%;", lines[1]) if len(lines) > 1 else None
    if not m:
        return None
    return "%s, %s%% -- no OS limit exists" % ("plugged in" if "'AC Power'" in lines[0] else "on battery", m.group(1))


PERF = """cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown
cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo"""


def device(root, name, env, machine, probe=status.fleet_probe, answers=None):
    """`wk doctor <machine>`: its tailnet names, whether it answers, a bench device's system and arm, and a
    board's governor and temperature, each a row. Nothing is changed and nothing is started."""
    conf = fleet.Fleet(root, env).load(name)
    r = reach.Reach(machine, env)
    for n in r.names(name):
        p = r.peer(n)
        if p is None:
            yield unk("%s is not a tailnet node" % n, "wk machine probe %s" % name)
        else:
            yield check("%s on the tailnet at %s" % (n, p[1]), "power it on, or: wk machine probe %s" % name, p[2] == "up")
    if conf["kind"] not in fleet.BENCH_KINDS:
        up, why = (answers or Machines(root, env, here=machine).answers)(name, conf)
        yield check("%s answers" % name if up else "%s does not answer: %s" % (name, why), "wk machine probe %s" % name, up)
        return
    cap = status.fleet_timeout(env)
    fields = probe(root, name, cap, env)
    if not fields or "error" in fields:
        yield unk("%s: the fleet probe %s" % (name, "failed: " + fields["error"] if fields else "did not answer within %ss" % cap),
                  "wk machine probe %s" % name)
        return
    mode = status.fleet_mode(fields["probeable"], fields["mode"], fields["bridge"])
    yield check("%s answers in %s" % (name, mode) if fields["mode"] else "%s is %s" % (name, mode), "wk machine probe %s" % name,
                bool(fields["mode"]) and not mode.startswith("unreachable"))
    if fields.get("armed"):
        what = "armed for %s by %s since %s" % (fields["armed"], fields.get("armed_by") or "?", fields.get("armed_at") or "?")
        yield miss(what + ", and the record was never cleared", "wk boot %s --status" % name) if status.armed_desync(
            fields, Clock()) else note(what)
    else:
        yield ok("not armed")
    yield note("media: %s" % fields.get("media", "unknown"))
    if conf["kind"] != "board" or not fields["mode"]:
        return
    dest = conf.get("bench_ssh") if fields["mode"].startswith("bench") else conf.get("ssh") or name
    out = Ssh(dest, timeout=reach.ssh_timeout(env), via=machine).run(["sh", "-c", PERF]).out.replace("\r", "").split("\n")
    gov, temp = (out + ["", ""])[:2]
    yield note("cpu governor on %s: %s" % (dest, gov.strip() or "unknown"))
    if temp.strip().isdigit():
        yield note("temperature on %s: %dC" % (dest, int(temp) // 1000))


def gh_authenticated(machine=None):
    """`gh auth status` exits 0 for a configured account whose token has expired, so the api call is the one that means anything."""
    return (machine or Local()).run(["gh", "api", "user"]).ok


class Host:
    """What a Doctor asks of the host that tests replace: gh's token and the privileged helpers' table and grants."""

    @staticmethod
    def gh_authenticated(root, machine):
        return gh_authenticated(machine)

    @staticmethod
    def priv_helpers(root, env=None):
        return priv.helpers()

    @staticmethod
    def priv_answers(root, path, machine):
        return priv.answers(path, machine)


class Doctor:
    """This machine's checks; `sh` answers what is asked of the host (Host)."""

    def __init__(self, root, env=None, machine=None, macos=None, sh=Host, mc=machine_deps, keys=None):
        self.root = root
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.sh = sh
        self.mc = mc
        self.store = Store(self.env)
        self.macos = (os.uname().sysname == "Darwin") if macos is None else macos
        self.macos_host = self.macos and not in_vm(self.env)
        self.home = self.store.home()
        self.reg = targets.Registry(root, self.env, self.machine)
        self.container = self.reg.load("container")
        self._paths = None
        self._keys = keys

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
            sec = secrets.Secrets(self.root, self.env, self.machine)
            self._paths = {"push_held": self.store.keyring_push_dir(), "read_pat": sec.machine_read_pat(),
                           "tailscale_api": sec.cred_path("tailnet-api"), "tailscale_authkey": sec.cred_path("tailnet"), "ntfy_topic": self.store.keyring_ntfy_topic()}
            self._paths.update(("secret." + r[0], sec.cred_path(r[0])) for r in secrets.agent_secrets())
        return self._paths

    def keys(self):
        """`wk key`'s own answers: the credentials here, and each peer's through its `wk key verdict`."""
        if self._keys is None:
            self._keys = Key(self.root, self.env, self.machine, reg=self.reg)
        return self._keys

    def podman_state(self):
        return self.container.machine_state()

    def in_vm(self, command):
        """Its stdout, None when it failed."""
        r = self.machine.run(["podman", "machine", "ssh", self.container.podman_machine(), "--", command], input="")
        return r.out.strip() if r.ok else None

    def probe_store(self):
        return probe_store(self.store, self.machine, git.mirror_branches(self.env), self.env)

    def sections(self, everything):
        yield "host tools", self.host_tools()
        yield "credentials", credentials_section(self.keys().settable(), self.keys().stored_verdict)
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
        for t in self.reg.all():
            if t != "container" and self.reg.kind(t) == "remote":
                yield "build machine: %s" % t, self.build_machine(t)
        yield "battery", self.battery()

    def host_tools(self):
        if self.macos:
            yield check("Xcode command line tools", "xcode-select --install", self.machine.run(["xcode-select", "-p"]).ok)
            yield check("podman", "install the official pkg from podman.io", self.machine.have("podman"))
            yield check("zed", "https://zed.dev/download", targets.zed_cli(self.machine) is not None)
            yield check("tailscale", "https://tailscale.com/download/macos",
                        self.machine.have("tailscale") or self.machine.isdir("/Applications/Tailscale.app"))
        else:
            for tool, what in (("podman", "podman"), ("zsh", "zsh"), ("cage", "cage (benchmark kiosk)"), ("wlr-randr", "wlr-randr (session off)")):
                yield check(what, "./setup --stage tools", self.machine.have(tool))
        if self.machine.have("nmap"):
            yield ok("nmap (wk machine probe)")
        else:
            yield unk("nmap absent -- only 'wk machine probe' needs it", "nmap.org, the .dmg" if self.macos else "./setup  (host/linux/apt.txt)")
        yield check("jq (claude hook)", "./setup --stage tools", self.machine.have("jq"))
        yield check("git-lfs", "./setup --stage tools", self.machine.have("git-lfs")
                    or self.machine.run(["test", "-x", os.path.join(self.home, ".local", "bin", "git-lfs")]).ok)
        yield check("gh", "install gh, then: gh auth login", self.machine.have("gh"))
        if self.machine.have("gh"):
            yield check("gh authenticated", "gh auth login   (then: wk key deploy)", self.sh.gh_authenticated(self.root, self.machine))

    def root_access(self):
        r = self.machine.run(["env", "WK_QUIET=1", os.path.join(self.root, "cmd", "key"), "sudo", "status"])
        out = (r.out + r.err).strip()
        yield ok("sudo: " + out) if r.ok else miss("sudo: " + out, "wk key sudo setup")

    def config(self):
        def linked(path, target):
            return self.machine.run(["readlink", path]).out.strip() == target
        yield check("~/.claude/settings.json is the HOST settings", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "settings.json"), os.path.join(self.root, "claude", "settings-host.json")))
        yield check("~/.claude/CLAUDE.md is the HOST briefing", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "CLAUDE.md"), os.path.join(self.root, "claude", "CLAUDE-host.md")))
        yield check("shell rc sources shell/bashrc", "./setup --stage dotfiles",
                    any("wk-tools/shell/bashrc" in self.read(os.path.join(self.home, rc)) for rc in (".zshrc", ".bashrc")))
        if self.machine.have("git"):
            have, want = kv(git_fields(self.machine)), self.want()
            for v in ("name", "email"):
                yield check("git user.%s is the repo's" % v, "./setup --stage dotfiles", have.get("git." + v, "") == want.get(v, ""))
            yield check("git speed settings (fsmonitor, manyFiles)", "./setup --stage dotfiles",
                        have.get("git.fsmonitor") == "true" and have.get("git.manyfiles") == "true")

    def local_state(self, path, kind, how):
        disp = "~" + path[len(self.home):] if path.startswith(self.home) else path
        if self.macos_host and path.startswith(self.store.store_dir() + "/"):
            disp += " (podman VM)"
            if self.podman_state() != "running":
                return unk("%s -- not visible while the podman machine is stopped" % disp, "%s: %s" % (kind, how))
            present = self.in_vm("test -e %s" % shlex.quote(path)) is not None
        else:
            present = self.machine.exists(path)
        if present:
            return ok("%s (%s) -- %s" % (disp, kind, how))
        return unk("%s (absent)" % disp, "%s: %s" % (kind, how))

    def machine_local(self):
        store, p = self.store, self.paths()
        for d in bench_record.task_roots(self.machine, store.records_dir()):
            yield self.local_state(d, "backed-up", "benchmark runs and their provenance -- not regenerable at any price; a rerun is a "
                                   "different measurement; wk bench export <task> copies one out")
        if bench_record.tasks(bench_record.outside(store), self.machine):
            yield self.local_state(bench_record.outside(store), "backed-up", "bench tasks outside any workspace, which no command reads "
                                   "until each is moved into its workspace's bench/: wk gc names each one's move")
        yield self.local_state(store.mirror_dir(), "regenerable",
                               "wk sync clones WebKit into it again (the one copy here; the podman VM and every tart guest read it)")
        yield self.local_state(store.keyring_dir(), "regenerable", "wk key deploy makes new deploy keys (revoke the old ones on GitHub)")
        yield self.local_state(p["push_held"], "regenerable",
                               "wk key deploy makes new deploy keys; wk key set github-pat and wk key set bugzilla-api-key store new ones "
                               "(revoke the old ones on GitHub and Bugzilla)")
        yield self.local_state(p["read_pat"], "regenerable",
                               "./setup and wk key set github-pat both write it from the token in %s" % p["push_held"])
        for key, path in p.items():
            if key.startswith("secret."):
                yield self.local_state(path, "re-authable",
                                       "wk key set %s stores one; every workspace this machine makes starts authenticated with it" % key[7:])
        yield self.local_state(p["tailscale_authkey"], "re-authable",
                               "wk key set tailnet asks for one (tag:wk, reusable, not ephemeral, longest expiry); joined nodes are unaffected")
        yield self.local_state(p["tailscale_api"], "re-authable",
                               "wk key set tailnet-api asks for one; only this machine holds it, and only writes that must retire a stale node need it")
        yield self.local_state(p["ntfy_topic"], "re-authable",
                               "wk key set ntfy mints the ntfy.sh topic lib/wk/notify.py publishes to; only this machine holds it, "
                               "and a new one is one subscription away")
        yield self.local_state(os.path.join(store.state_dir(), "broker"), "regenerable",
                               "request records (argv, log, status) the fleet-request broker writes; the next request makes new ones")
        yield self.local_state(os.path.join(self.home, ".ssh", "config.d", "local"), "backed-up",
                               "hand-written ssh entries (host/dotfiles.sh moves them here and owns the rest)")
        machines = fleet.Fleet(self.root, self.env)
        yield self.local_state(machines.local_dir(), "backed-up",
                               "hand-written machine confs for this device only, their keys over machines/<name>.conf's")
        yield self.local_state(os.path.join(store.state_dir(), "ssh", "zed_ed25519"), "regenerable",
                               "wk zed makes a new one and re-authorises it in the workspace")
        if self.macos:
            yield self.local_state(os.path.join(store.state_dir(), "mac-tailnet", "tolken-bench.state"), "regenerable",
                                   "the bench install's tailnet node identity, kept on the host install the way a board keeps its bench node "
                                   "on its rescue; lost, it rejoins under a new name")
        else:
            rpi5 = dict(machine_confs(self.root, self.env)).get("rpi5")
            if rpi5 and rpi5.get("ssh") == self.machine_name():
                yield self.local_state(os.path.join(self.root, "host", "linux", "rpi5", "rpi5.conf"), "backed-up",
                                       "site WiFi identity (gitignored: repo is public); rpi5.conf.example documents the shape")
                yield self.local_state(os.path.join(self.root, "host", "linux", "rpi5", "id_ed25519"), "backed-up",
                                       "the ssh key rpi5-setup.sh installs from beside itself (gitignored: repo is public)")
                yield self.local_state(os.path.join(self.home, "kbuild"), "backed-up",
                                       "the -numa kernel .debs rpi5-numa-kernel.sh builds in 1-2 hours; dpkg -i reinstalls them")
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
            yield miss("/var/lib/tailscale (podman VM) -- the machine holds this workstation's image workspaces and reaches no board without it",
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
            yield miss("podman machine '%s'" % self.container.podman_machine(), "./setup --stage machine")
            return
        if state != "running":
            yield unk("podman machine '%s' is stopped" % self.container.podman_machine(), "wk start, then re-run wk doctor for the store checks")
            return
        yield ok("podman machine '%s' running" % self.container.podman_machine())
        out = self.in_vm("WK_STORE=/var/lib/wk python3 /opt/wk-tools/cmd/doctor --probe-store")
        if not out:
            yield unk("store inside the VM", "/opt/wk-tools missing in the VM? run ./setup --stage sdk")
            return
        yield from report_store(out, "podman machine ssh %s -- git config --global include.path /opt/wk-tools/dotfiles/gitconfig" % self.container.podman_machine(),
                                fork_key, True, self.want())

    def privileged_helpers(self):
        for name, where, what, path, sudoers in self.sh.priv_helpers(self.root, env=self.env):
            if where == "linux" and self.macos:
                continue
            if not self.machine.run(["test", "-x", path]).ok:
                yield miss("%s (%s)" % (name, what), "./setup --stage quiesce  (interactive sudo)")
            elif self.sh.priv_answers(self.root, path, self.machine):
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
        softnet = guest.softnet_bin(self.env)
        yield check("softnet installed SUID root", "./setup --stage softnet  (interactive sudo)",
                    self.machine.run(["test", "-x", softnet, "-a", "-u", softnet]).ok)
        from wk.sysimage import guestbase
        yield from findings(guestbase.Base(vm).findings())
        yield from vm_guest_git_findings(vm, self.want())

    def build_machine(self, t):
        target = self.reg.load(t)
        probe = self.mc.probe(target, self.root)
        if not probe:
            yield unk("%s did not answer" % t, "ssh %s true  -- then re-run; nothing was changed" % t)
            return
        rows = self.mc.findings(self.root, probe, self.env, self.machine)
        yield from findings(machine_deps.findings_text(rows), "see 'wk machine setup %s'" % t)
        why = self.mc.stale(target, self.root)
        if why:
            yield miss("provisioning on %s predates its inputs: %s" % (t, why), "wk machine setup %s" % t)
        else:
            yield ok("provisioned from this tree's remote/provision.sh + remote/deps.sh")

    def battery(self):
        phones = bridge.Bridge(self.root, self.env, self.machine)
        for name in phones.names():
            try:
                blob = phones.battery(name)
            except bridge.Unreachable:
                yield unk("%s: did not answer" % name, "wk machine status %s" % name)
                continue
            yield battery_verdict(name, blob)
        if self.macos:
            line = mac_battery_line(self.machine.run(["pmset", "-g", "batt"]).out)
            if line:
                yield unk("this Mac: " + line, "docs/Urgent/HUMAN-battery.md -- no OS knob exists yet")
