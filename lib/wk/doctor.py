"""Whether each thing is healthy, as rows (state, what, remedy) with state ok | miss | unk | note: this machine's
provisioning and daemons, a machine of the fleet, a guest's desktop and load. One renderer counts the misses."""

import calendar
import json
import os
import re
import shlex
import sys
import time

from wk import daemons, fleet, guest, images, places, priv, project, quiet, reach, record, secrets
from wk.bench import record as bench_record
from wk.clock import Clock
from wk.key.cli import Key
from wk.kv import kv
from wk.machine import TIMED_OUT, Local, Ssh
from wk.machine_cmd import deps as machine_deps
from wk.store import Store, in_vm

OK, MISS, UNK, NOTE = "ok", "miss", "unk", "note"
MARK = {OK: "\033[32mok\033[0m", MISS: "\033[31m--\033[0m", UNK: "\033[33m??\033[0m"}
FLEET_SECONDS = 4   # one fleet machine's probe: a board on the tailnet answers in under a second
ARM_STALE_SECONDS = 5 * 60   # an arm is consumed by the reboot that follows it within moments
SHELLS_WARN, MEM_FREE_WARN_PCT, SWAP_WARN_MB = 12, 15, 1024   # twelve shells measured as nothing wrong; macOS pages below 15% free

GIT_KEYS = (("name", "user.name"), ("email", "user.email"), ("fsmonitor", "core.fsmonitor"), ("manyfiles", "feature.manyFiles"))
GIT_PROBE = "command -v git >/dev/null 2>&1 || exit 0\n" + "".join(
    'printf "git.%s=%%s\\n" "$(git config --get %s 2>/dev/null || true)"\n' % (k, c) for k, c in GIT_KEYS)
SERVICES = (("wk-proxy.service", "egress proxy", "workspaces have no network without it"),
            ("wk-github-inject.service", "credential injector", "a workspace's PR tool and claude get no credential"),
            ("wk-push.service", "push service", "'git push' in a workspace is refused"))
PERF = """cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo unknown
cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo"""
SDK_TAG = re.compile(r"^(.+)-v(\d+)-[0-9a-f]+$")

# What a rebuild cannot get from this repo: (path, kind, how it comes back, where). `where` is "" for every machine,
# "mac" or "linux" by host, or a machine conf whose ssh names this machine.
MACHINE_LOCAL = (
    ("{mirror}", "regenerable", "wk sync clones it again; the podman VM and every guest read this one copy", ""),
    ("{keyring}", "regenerable", "wk key deploy makes new deploy keys (revoke the old ones on GitHub)", ""),
    ("{push_held}", "regenerable", "wk key deploy, and wk key set github-pat / bugzilla-api-key (revoke the old ones)", ""),
    ("{read_pat}", "regenerable", "./setup and wk key set write it from the push keys", ""),
    ("{push_pat}", "regenerable", "./setup and wk key set write it from the push keys", ""),
    ("{bugzilla_key}", "regenerable", "./setup and wk key set write it from the push keys", ""),
    ("{claude_login}", "re-authable", "wk key set claude-login; the injector holds it, every workspace a placeholder", ""),
    ("{tailscale_authkey}", "re-authable", "wk key set tailnet (tag:wk, reusable, not ephemeral)", ""),
    ("{tailscale_api}", "re-authable", "wk key set tailnet-api; only the retiring of a stale node needs it", ""),
    ("{ntfy_topic}", "re-authable", "wk key set ntfy mints a new topic", ""),
    ("{state}/broker", "regenerable", "the broker's request records; the next request makes new ones", ""),
    ("{home}/.ssh/config.d/local", "backed-up", "hand-written ssh entries (host/dotfiles.sh owns the rest)", ""),
    ("{machines}", "backed-up", "hand-written machine confs for this device only", ""),
    ("{state}/ssh/zed_ed25519", "regenerable", "wk zed makes a new one and authorises it", ""),
    ("{state}/mac-tailnet/tolken-bench.state", "regenerable", "the bench install's tailnet identity; lost, it rejoins under a new name", "mac"),
    ("/var/lib/tailscale", "re-authable", "tailscale up; the node name survives in the admin console", "linux"),
    ("{root}/host/linux/rpi5/rpi5.conf", "backed-up", "site WiFi identity (gitignored: the repo is public)", "rpi5"),
    ("{root}/host/linux/rpi5/id_ed25519", "backed-up", "the ssh key rpi5-setup.sh installs (gitignored)", "rpi5"),
    ("{home}/kbuild", "backed-up", "the -numa kernel .debs rpi5-numa-kernel.sh builds in hours; dpkg -i reinstalls them", "rpi5"),
)

# A guest's desktop readings: (key, its good values, what a good reading says, what another says, what reads it); a
# missing or `?` reading is unknown. {v} is the reading.
RESTART = guest.RESTART
REBUILD = guest.REBUILD
DESKTOP = (
    ("screenlock", ("off",), "screen lock off", "the screen lock is on, so the guest comes up asking for a password", "the screen lock"),
    ("update_autoinstall_system", ("0",), "macOS updates will not install themselves",
     "macOS updates install themselves (AutomaticallyInstallMacOSUpdates={v}), rebooting the guest mid-build", "Software Update"),
    ("update_download_system", ("0",), "no update downloads itself", "updates download themselves (AutomaticDownload={v})", "Software Update"),
)


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


def credentials_section(names, verdict_of):
    """A verdict line is <verdict>\\t<detail>, the detail's `    key: value` lines being facts."""
    rows = []
    for name in names:
        line = verdict_of(name)
        v, _, detail = line.partition("\t")
        why = (detail.splitlines() or [""])[0]
        fix = re.search(r"^ *fix: (.*)$", line, re.M)
        if v == "ok":
            rows.append(ok("%s -- %s" % (name, why)))
        elif v == "wide":
            rows.append(unk("%s reaches further than wk spends it" % name, why))
        elif v == "bad":
            rows.append(miss("%s: %s" % (name, why), fix.group(1) if fix else "wk key check"))
        else:
            rows.append(unk("%s: %s" % (name, why), "wk key setup" if v == "absent" else "wk key check"))
    return rows


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
    rows.append(check("%s: git speed settings (fsmonitor, manyFiles)" % label, remedy,
                      f.get("git.fsmonitor") == "true" and f.get("git.manyfiles") == "true"))
    return rows


def vm_guest_git_findings(driver, want):
    rows = []
    for name, _ in driver.list():
        if driver.info(name) != "running":
            continue
        r = driver.exec(name, ["sh", "-c", GIT_PROBE])
        if not (r.out if r.ok else "").strip():
            rows.append(unk("%s (tart guest): git config did not answer" % name, "wk doctor %s" % name))
        else:
            rows += git_config_findings("%s (tart guest)" % name, r.out, "wk start %s (the include is written on every start)" % name, want)
    return rows


def socket_answers(path, machine):
    """A socket file is no evidence of a daemon: a killed one leaves it behind, so the answer is a connect."""
    return daemons.answers(machine, path)


def launchd_daemons(store):
    """(name, socket, setup stage) of what launchd keeps on a macOS host; the podman VM's copies are forwards, not the daemons."""
    return [("credential injector", os.path.join(store.vm_store_dir(), "vm", "github-inject.sock"), "inject"),
            ("request broker", store.runtime_socket(), "broker")]


def probe_store(store, machine, branches, env):
    """key=value lines: every mirror branch this tree declares is named, since one it lacks fails every fetch."""
    runtime = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()

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
    proxy = machine.run(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"]).ok \
        or socket_answers(os.path.join(runtime, "wk", "proxy.sock"), machine)
    lines.append("proxy=" + ("ok" if proxy else "no"))
    try:
        pihosts = bool(machine.read(os.path.join(store.store_dir(), "pi-hosts")))
    except OSError:
        pihosts = False
    lines.append("pihosts=" + ("ok" if pihosts else "no"))
    lines.append("broker=" + ("ok" if socket_answers(os.path.join(runtime, "wk", "broker.sock"), machine) else "no"))
    return "\n".join(lines) + "\n" + git_fields(machine)


def report_store(out, gitremedy, fork_key, macos, want):
    """`gitremedy` only when the probe came from another machine; on the same one the config section already checked git."""
    f = kv(out)
    mirror, what = f.get("mirror", ""), "%s mirror" % project.get("CHECKOUT")
    rows = [ok(what) if mirror == "ok" else miss("%s carries no %s" % (what, mirror[4:]), "wk sync --mirror") if mirror.startswith("gap")
            else miss(what, "wk sync")]
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


def sdk_newest(local, tags):
    """The registry tag that supersedes `local`: the highest -v<N> sharing its version, else itself when listed."""
    m = SDK_TAG.match(local)
    if not m:
        return local if local in tags else None
    best_v, best = int(m.group(2)), local
    for t in tags:
        mm = SDK_TAG.match(t)
        if mm and mm.group(1) == m.group(1) and int(mm.group(2)) > best_v:
            best, best_v = t, int(mm.group(2))
    return best


def sdk_row(container):
    local = container.sdk_local()
    if not local:
        return None
    tag = local["image"].rsplit(":", 1)[-1]
    tags = container.sdk_upstream(timeout=FLEET_SECONDS)
    newest = sdk_newest(tag, tags) if tags is not None else None
    if newest == tag:
        return ok("SDK image %s, pulled %s, is upstream's newest" % (tag, local.get("created") or "?"))
    if newest:
        return miss("SDK image %s is behind upstream's %s" % (tag, newest), "WK_SDK_IMAGE=%s:%s wk new ..." % (project.get("SDK_IMAGE"), newest))
    return unk("SDK image %s: the registry %s" % (tag, "did not answer within %ss" % FLEET_SECONDS if tags is None else "lists no tag like it"),
               "wk doctor, again")


def unit_stale(root, unit, machine):
    """A service keeps the program it exec'd: this tree's copy newer than the process is code nobody can read in the tree."""
    prog = daemons.PROGRAMS.get(unit[len("wk-"):-len(".service")], "")
    pid = machine.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit]).out.strip()
    if not prog or not pid.isdigit() or pid == "0":
        return False
    try:
        return machine.mtime(os.path.join(root, prog)) > machine.mtime("/proc/" + pid)
    except OSError:
        return False


def fleet_probe(root, name, cap):
    """The machine's boot driver (wk.boot.cli fleet-probe) asked under a ceiling: None when it did not answer in `cap` seconds."""
    r = Local().run(["env", "PYTHONPATH=" + os.path.join(str(root), "lib"), "WK_SSH_TIMEOUT=%d" % FLEET_SECONDS,
                     sys.executable, "-m", "wk.boot.cli", "fleet-probe", name], input="", timeout=cap)
    if r.rc == TIMED_OUT:
        return None
    if not r.ok:
        return {"error": (r.err.strip().splitlines() or ["exit %d" % r.rc])[-1]}
    try:
        return json.loads(r.out)
    except ValueError:
        return {"error": "the probe answered no JSON"}


def fleet_mode(probeable, mode):
    if probeable == "no":
        return "unknown from here"
    return next((word for prefix, word in (("host", "host mode"), ("base", "base image -- not a bench system"), ("bench", "bench mode"))
                 if mode.startswith(prefix)), "unreachable")


def armed_desync(fields, clock):
    """The record claims an arm the machine has moved past: another boot id, or unconsumed past ARM_STALE_SECONDS; an
    unreadable stamp is no evidence the arm is current."""
    if fields.get("armed_boot") and fields.get("boot_id") and fields["armed_boot"] != fields["boot_id"]:
        return True
    if not fields.get("armed_at"):
        return False
    try:
        return clock.now() - calendar.timegm(time.strptime(fields["armed_at"], "%Y-%m-%dT%H:%M:%SZ")) > ARM_STALE_SECONDS
    except ValueError:
        return True


def device(root, name, env, machine, probe=fleet_probe, answers=None):
    """`wk doctor <machine>`: its tailnet names, whether it answers, a bench machine's system and arm, and a board's
    governor and temperature. Nothing is changed and nothing is started."""
    conf = fleet.Fleet(root, env).load(name)
    r = reach.Reach(machine, env)
    for n in r.names(name):
        p = r.peer(n)
        yield unk("%s is not a tailnet node" % n, "wk machine probe %s" % name) if p is None else \
            check("%s on the tailnet at %s" % (n, p[1]), "power it on, or: wk machine probe %s" % name, p[2] == "up")
    if conf["kind"] not in fleet.BENCH_KINDS:
        from wk.machine_cmd import Machines
        up, why = (answers or Machines(root, env, here=machine).answers)(name, conf)
        yield check("%s answers" % name if up else "%s does not answer: %s" % (name, why), "wk machine probe %s" % name, up)
        return
    cap = FLEET_SECONDS * 5
    fields = probe(root, name, cap)
    if not fields or "error" in fields:
        yield unk("%s: the fleet probe %s" % (name, "failed: " + fields["error"] if fields else "did not answer within %ss" % cap),
                  "wk machine probe %s" % name)
        return
    mode = fleet_mode(fields["probeable"], fields["mode"])
    yield check("%s answers in %s" % (name, mode) if fields["mode"] else "%s is %s" % (name, mode), "wk machine probe %s" % name,
                bool(fields["mode"]) and not mode.startswith("unreachable"))
    if fields.get("armed"):
        what = "armed for %s by %s since %s" % (fields["armed"], fields.get("armed_by") or "?", fields.get("armed_at") or "?")
        yield miss(what + ", and the record was never cleared", "wk boot %s --status" % name) if armed_desync(fields, Clock()) else note(what)
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


def named(rows, name):
    """`rows` with the guest they are about named in each remedy."""
    return [(st, what, remedy.replace("<name>", name)) for st, what, remedy in rows]


def desktop_rows(rd, name):
    """Guest `name`'s desktop readings (guest.desktop_readings) as rows."""
    v = rd.get
    cu = v("console_user", "")
    rows = [miss("nobody is logged in at the window (console user '%s'), so there is no desktop" % cu, RESTART)
            if cu in ("root", "", "?") else ok("logged in at the window as " + cu)]
    for key, good, yes, no, what in DESKTOP:
        val = v(key, "")
        rows.append(unk("%s could not be read in there" % what, RESTART) if val in ("", "?") else ok(yes) if val in good
                    else miss(no.format(v=val), REBUILD))
    pending = v("setupassistant_pending", "")
    rows.append(miss("Setup Assistant will put a modal pane on the desktop: " + pending, REBUILD) if pending
                else ok("Setup Assistant already clicked through"))
    py, pin = v("pyobjc", ""), v("pin", "")
    rows.append(ok("pyobjc %s: a browser can be driven and held in front" % py) if py and py == pin else
                miss("pyobjc here is %s and this fleet measures with %s" % (py or "absent", pin), RESTART + "  (the settle installs it)"))
    os_v, seen = v("os_product", ""), v("setupassistant_seen_product", "")
    rows.append(unk("the guest did not say which macOS it runs", RESTART) if os_v in ("", "?") else ok("Setup Assistant has seen macOS " + os_v)
                if seen == os_v else miss("Setup Assistant will show its 'what is new in macOS' pane (it saw %s, this is %s)" % (seen, os_v), REBUILD))
    windows = v("windows", "")
    rows.append(unk("the window server was not asked what is on the screen", RESTART) if windows in ("", "?") else
                miss("on the screen, and nothing wk runs put it there: " + v("uninvited"), REBUILD) if v("uninvited") else
                ok("nothing on the screen but %d window(s) wk put there" % sum(":0:" in e for e in windows.split(";"))))
    rows.append(ok("no authentication sheet is up") if v("securityagent") == "down" else
                unk("SecurityAgent is up; every login has one for a moment", "wk doctor <name>  -- still up means it waits for a password"))
    rows.append(note("%s has the focus" % v("frontapp", "?")))
    return named(rows + findings(v("quiet", "")), name)


def load_rows(probe, name):
    """What is resident in guest `name` (vm/load-probe.sh's proc= lines and readings) as rows."""
    procs, vals, groups = [], {}, {}
    for line in probe.splitlines():
        key, _, val = line.partition("=")
        rss, _, comm = val.strip().partition(" ")
        if key == "proc" and rss.isdigit():
            procs.append((int(rss), comm.strip()))
        elif key:
            vals[key] = val.strip()
    for rss, comm in procs:
        fam = next((f for f, pat in guest.LOAD_FAMILIES if re.search(pat, comm)), None)
        if fam:
            n, k = groups.get(fam, (0, 0))
            groups[fam] = (n + 1, k + rss)
    shells, kb = groups.get("shell", (0, 0))
    others = ", ".join("%d %s process(es)" % (n, fam) for fam, (n, _) in groups.items() if fam != "shell")
    rows = [ok("%d shells resident (%d MB)" % (shells, kb // 1024)) if shells <= SHELLS_WARN else
            miss("%d shells are resident in there, holding %d MB%s" % (shells, kb // 1024, " -- alongside " + others if others else ""),
                 RESTART + " takes them all with it; an editor's remote server outlives its window")]
    free, total = vals.get("mem_free_pct", ""), vals.get("mem_total_mb", "?")
    top = ", ".join("%s (%d MB)" % (c.rsplit("/", 1)[-1], r // 1024) for r, c in sorted(procs, reverse=True)[:3])
    rows.append(unk("memory pressure could not be read in there", RESTART) if not free.isdigit() else
                ok("%s%% of the %s MB in that guest is free" % (free, total)) if int(free) >= MEM_FREE_WARN_PCT else
                miss("%s%% of the %s MB in that guest is free, and macOS calls that pressure: the biggest are %s" % (free, total, top),
                     RESTART + "; a build in there is paging"))
    m = re.search(r"used = ([0-9.]+)M", vals.get("swapusage", ""))
    if m and float(m.group(1)) > SWAP_WARN_MB:
        rows.append(note("the guest is using %d MB of swap: a build in there is slower than its numbers say" % float(m.group(1))))
    return named(rows, name)


def guest_rows(vm, ws):
    """A macOS guest's base, desktop and load."""
    from wk.sysimage import guestbase
    rows = findings(guestbase.Base(vm).findings())
    g = vm.guest(ws)
    if g is None:
        return rows + [unk("'%s' is not running, so its desktop and its load cannot be read" % ws, "wk start %s" % ws)]
    rd = guest.desktop_readings(vm.root, vm.machine, g)
    rows += desktop_rows(rd, ws) if rd else [unk("'%s' did not answer the desktop probe" % ws, "wk doctor %s" % ws)]
    load = guest.load_probe(vm.root, g)
    return rows + (load_rows(load, ws) if load else [unk("'%s' did not answer the load probe" % ws, "wk doctor %s" % ws)])


def gh_authenticated(machine=None):
    """`gh auth status` passes an expired token; the api call is the one that means anything."""
    return (machine or Local()).run(["gh", "api", "user"]).ok


class Doctor:
    """This machine's checks, every one asked through `machine`."""

    def __init__(self, root, env=None, machine=None, macos=None):
        self.root = root
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.store = Store(self.env)
        self.macos = (os.uname().sysname == "Darwin") if macos is None else macos
        self.macos_host = self.macos and not in_vm(self.env)
        self.home = self.store.home()
        self.reg = places.Registry(root, self.env, self.machine)
        self.container = self.reg.load("container")
        self._paths = None

    def hostname(self):
        return record.host_name(self.machine)

    def want(self):
        conf = os.path.join(self.root, "dotfiles", "gitconfig")
        return {v: self.machine.run(["git", "config", "--file", conf, "--get", "user." + v]).out.strip() for v in ("name", "email")}

    def paths(self):
        if self._paths is None:
            sec = secrets.Secrets(self.root, self.env, self.machine)
            self._paths = {"push_held": self.store.keyring_push_dir(), "read_pat": sec.machine_read_pat(),
                           "push_pat": sec.machine_pat(), "bugzilla_key": sec.machine_bugzilla_key(),
                           "push_copy": os.path.dirname(sec.machine_push_key("")), "claude_login": sec.cred_path("claude-login"),
                           "tailscale_api": sec.cred_path("tailnet-api"), "tailscale_authkey": sec.cred_path("tailnet"),
                           "ntfy_topic": self.store.keyring_ntfy_topic()}
            self._paths.update(("secret." + r[0], sec.cred_path(r[0])) for r in secrets.agent_secrets())
        return self._paths

    def in_vm(self, command):
        """Its stdout in the podman machine, None when it failed."""
        r = self.container.store_machine.run(["sh", "-c", command], input="")
        return r.out.strip() if r.ok else None

    def probe_store(self):
        return probe_store(self.store, self.machine, images.mirror_branches(self.env), self.env)

    def sections(self, everything):
        yield "host tools", self.host_tools()
        yield "credentials", self.credentials()
        yield "root access", self.root_access()
        yield "config (./setup owns these)", self.config()
        yield "machine-local state (everything a rebuild cannot get from this repo)", self.machine_local()
        yield "workspaces store", self.workspaces_store()
        yield "daemons", self.daemons()
        yield "privileged helpers", self.privileged_helpers()
        if not self.macos:
            yield "benchmarking", self.benchmarking()
        if self.macos_host:
            yield "macOS VM place (optional -- Apple ports)", self.vm_place()
        if not everything:
            return
        for t in self.reg.machines():
            yield "build machine: %s" % t, self.build_machine(t)
        me = record.machine_name(self.env, self.machine)
        for name in fleet.Fleet(self.root, self.env).names(fleet.BENCH_KINDS):
            if name != me:
                yield "bench machine: %s" % name, device(self.root, name, self.env, self.machine)

    def credentials(self):
        keys = Key(self.root, self.env, self.machine, reg=self.reg)
        yield from credentials_section(keys.settable(), keys.stored_verdict)

    def host_tools(self):
        have = self.machine.have
        if self.macos:
            yield check("Xcode command line tools", "xcode-select --install", self.machine.run(["xcode-select", "-p"]).ok)
            yield check("podman", "install the official pkg from podman.io", have("podman"))
            yield check("zed", "https://zed.dev/download", places.zed_cli(self.machine) is not None)
            yield check("tailscale", "https://tailscale.com/download/macos", have("tailscale") or self.machine.isdir("/Applications/Tailscale.app"))
        else:
            for tool, what in (("podman", "podman"), ("zsh", "zsh"), ("cage", "cage (benchmark kiosk)"), ("wlr-randr", "wlr-randr (session off)")):
                yield check(what, "./setup --stage tools", have(tool))
        yield ok("nmap (wk machine probe)") if have("nmap") else \
            unk("nmap absent -- only 'wk machine probe' needs it", "nmap.org, the .dmg" if self.macos else "./setup  (host/linux/apt.txt)")
        yield check("jq (claude hook)", "./setup --stage tools", have("jq"))
        yield check("git-lfs", "./setup --stage tools", have("git-lfs") or self.machine.run(["test", "-x", os.path.join(self.home, ".local", "bin", "git-lfs")]).ok)
        yield check("gh", "install gh, then: gh auth login", have("gh"))
        if have("gh"):
            yield check("gh authenticated", "gh auth login   (then: wk key deploy)", gh_authenticated(self.machine))

    def root_access(self):
        r = self.machine.run([os.path.join(self.root, "wk"), "key", "sudo", "status", "--quiet"])
        out = (r.out + r.err).strip()
        yield ok("sudo: " + out) if r.ok else miss("sudo: " + out, "wk key sudo setup")

    def config(self):
        def linked(path, dest):
            return self.machine.run(["readlink", path]).out.strip() == dest
        yield check("~/.claude/settings.json is the HOST settings", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "settings.json"), os.path.join(self.root, "claude", "settings-host.json")))
        yield check("~/.claude/CLAUDE.md is the HOST briefing", "./setup --stage claude",
                    linked(os.path.join(self.home, ".claude", "CLAUDE.md"), os.path.join(self.root, "claude", "CLAUDE-host.md")))

        def read(path):
            try:
                return self.machine.read(path)
            except OSError:
                return ""
        yield check("shell rc sources shell/bashrc", "./setup --stage dotfiles",
                    any("wk-tools/shell/bashrc" in read(os.path.join(self.home, rc)) for rc in (".zshrc", ".bashrc")))
        yield from git_config_findings("this machine", git_fields(self.machine), "./setup --stage dotfiles", self.want())

    def local_state(self, path, kind, how):
        disp = "~" + path[len(self.home):] if path.startswith(self.home) else path
        if self.macos_host and path.startswith(self.store.store_dir() + "/"):
            disp += " (podman VM)"
            if self.container.machine_state() != "running":
                return unk("%s -- not visible while the podman machine is stopped" % disp, "%s: %s" % (kind, how))
            present = self.in_vm("test -e %s" % shlex.quote(path)) is not None
        else:
            present = self.machine.exists(path)
        return ok("%s (%s) -- %s" % (disp, kind, how)) if present else unk("%s (absent)" % disp, "%s: %s" % (kind, how))

    def machine_local(self):
        store, p = self.store, self.paths()
        for d in bench_record.task_roots(self.machine, store.records_dir()):
            yield self.local_state(d, "backed-up", "benchmark runs: a rerun is a different measurement; wk bench export <task> copies one out")
        if bench_record.tasks(bench_record.outside(store), self.machine):
            yield self.local_state(bench_record.outside(store), "backed-up", "bench tasks outside any workspace: wk gc names each one's move")
        me = record.machine_name(self.env, self.machine)
        confs = fleet.Fleet(self.root, self.env)
        values = dict(p, mirror=store.mirror_dir(), keyring=store.keyring_dir(), state=store.state_dir(), home=self.home,
                      machines=confs.local_dir(), root=self.root)
        for path, kind, how, where in MACHINE_LOCAL:
            if where in ("mac", "linux") and where != ("mac" if self.macos else "linux"):
                continue
            if where not in ("", "mac", "linux") and (confs.kind(where) is None or confs.load(where).get("ssh") != me):
                continue
            yield self.local_state(path.format(**values), kind, how)
        if p["push_copy"] != p["push_held"]:
            yield self.local_state(p["push_copy"], "regenerable", "./setup and wk key deploy copy the deploy keys here for the push service")
        for key, path in p.items():
            if key.startswith("secret."):
                yield self.local_state(path, "re-authable", "wk key set %s; every workspace made here starts with it" % key[7:])
        if not self.macos_host:
            return
        if self.container.machine_state() != "running":
            yield unk("/var/lib/tailscale (podman VM) -- not visible while the podman machine is stopped", "re-authable: ./setup --stage machine")
            return
        ip = (self.in_vm("sudo tailscale ip -4 2>/dev/null | head -1") or "").replace("\r", "").strip()
        yield ok("/var/lib/tailscale (podman VM) (re-authable) -- on the tailnet at %s" % ip) if ip else \
            miss("/var/lib/tailscale (podman VM) -- it reaches no board without it", "./setup --stage machine  (needs: wk key set tailnet)")

    def workspaces_store(self):
        fork_key = self.machine.exists(os.path.join(self.paths()["push_held"], "build_key_fork"))
        name = self.container.podman_machine()
        if not self.macos_host:
            yield from report_store(self.probe_store(), "", fork_key, self.macos, self.want())
        elif self.container.machine_state() == "absent":
            yield miss("podman machine '%s'" % name, "./setup --stage machine")
            return
        elif self.container.machine_state() != "running":
            yield unk("podman machine '%s' is stopped" % name, "wk start, then re-run wk doctor for the store checks")
            return
        else:
            yield ok("podman machine '%s' running" % name)
            out = self.in_vm("WK_STORE=/var/lib/wk python3 /opt/wk-tools/wk doctor --probe-store")
            if not out:
                yield unk("store inside the VM", "/opt/wk-tools missing in the VM? run ./setup --stage sdk")
                return
            yield from report_store(out, "podman machine ssh %s -- git config --global include.path /opt/wk-tools/dotfiles/gitconfig" % name,
                                    fork_key, True, self.want())
        sdk = sdk_row(self.container)
        if sdk:
            yield sdk

    def daemons(self):
        if self.macos_host:
            for name, sock, stage in launchd_daemons(self.store):
                yield check("%s answers on this Mac" % name, "./setup --stage " + stage, socket_answers(sock, self.machine))
        if not self.machine.have("systemctl"):
            return
        for unit, label, cost in SERVICES:
            run = lambda *a: self.machine.run(["systemctl", "--user", *a, unit])   # noqa: E731
            if not run("cat").ok:
                continue
            if not run("is-active", "--quiet").ok:
                yield miss("%s stopped: %s" % (label, cost), "systemctl --user start %s" % unit[:-len(".service")])
            else:
                yield check("%s running" % label, "./setup   (restarts it on the program in the tree now)",
                            not unit_stale(self.root, unit, self.machine))

    def privileged_helpers(self):
        for name, where, what, path, sudoers in priv.helpers():
            if where == "linux" and self.macos:
                continue
            if not self.machine.run(["test", "-x", path]).ok:
                yield miss("%s (%s)" % (name, what), "./setup --stage quiesce  (interactive sudo)")
            elif priv.answers(path, self.machine):
                yield ok("%s (%s)" % (name, what))
            else:
                yield miss("%s: installed, and sudo -n still asks for a password" % name,
                           "%s has to be the last match 'sudo -l' shows, naming %s exactly -- ./setup --stage quiesce" % (sudoers, path))

    def benchmarking(self):
        yield check("render group configured", "./setup --stage tools, then log out and in", "render" in self.machine.run(["id", "-nG"]).out.split())
        on = [w for f, w in (("caffeinate.pid", "caffeinate"), ("daemons_paused", "daemons-paused"), ("raiser.pid", "raiser"))
              if self.machine.exists(os.path.join(quiet.state_dir(self.store), f))]
        yield miss("this machine is quiesced (%s)" % " ".join(on), "wk quiesce off") if on else ok("not quiesced")

    def vm_place(self):
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
        driver = self.reg.load(t)
        probe = machine_deps.probe(driver, self.root)
        if not probe:
            yield unk("%s did not answer" % t, "ssh %s true  -- then re-run; nothing was changed" % t)
            return
        yield from findings(machine_deps.findings_text(machine_deps.findings(self.root, probe, self.env, self.machine)),
                            "see 'wk machine setup %s'" % t)
        why = machine_deps.stale(driver, self.root)
        yield miss("provisioning on %s predates its inputs: %s" % (t, why), "wk machine setup %s" % t) if why else \
            ok("provisioned from this tree's remote/provision.sh + remote/deps.sh")
