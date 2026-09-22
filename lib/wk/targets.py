"""The targets: where a workspace lives and how it is driven. A `Registry`
names them (container, vm on a macOS host, this machine inside a workspace,
and every `targets/hosts/<name>.conf`); each driver answers the same
contract over a `Machine`."""

import json
import os
import re
import shlex

from wk import act, record, shell
from wk.machine import TIMED_OUT, Local, Result, Ssh
from wk.store import Store

BUILTIN = ("container", "vm", "remote", "local")
READY_MARKER = ".wk-ready"
FIRSTRUN_MARKER = ".wk-firstrun-complete"   # TODO: drop once no pre-marker workspace is left
STATES_NOT_THERE = ("absent", "creating", "broken", "unreachable")

# Run with $PWD inside the checkout; sets `_b` to `main`, a release like `2.52`, or nothing.
UPSTREAM_LINE_BODY = r'''
_u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || _u=''
_b=''
if [ -n "$_u" ]; then
    _br=${_u#*/}
    case "$_br" in
        main) _b=main ;;
        webkitglib/*) _b=${_br#webkitglib/} ;;
    esac
fi
if [ -z "$_b" ]; then
    _rel=$(git for-each-ref --format='%(refname)' --contains HEAD 'refs/remotes/*/webkitglib/*' 2>/dev/null \
        | sed 's#.*/webkitglib/##' | sort -t. -k1,1n -k2,2n | tail -1)
    if [ -n "$_rel" ]; then _b=$_rel
    elif git for-each-ref --format='%(refname)' --contains HEAD 'refs/remotes/*/main' 2>/dev/null | grep -q .; then _b=main
    fi
fi
'''
UPSTREAM_LINE = UPSTREAM_LINE_BODY + "printf '%s' \"${_b:-?}\"\n"


def image_base(root, ws):
    """CFG_RELEASE of an image workspace's profile, or None."""
    for prefix in ("yocto-", "buildroot-"):
        if ws.startswith(prefix):
            conf = os.path.join(root, "image", "configs", ws[len(prefix):] + ".conf")
            return read_conf(conf).get("CFG_RELEASE") or None
    return None


def git_base(target, ws):
    if target.info(ws) in STATES_NOT_THERE:
        return None
    r = target.exec(ws, ["sh", "-c", "cd %s 2>/dev/null || exit 0\n%s" % (shell.sh_quote(target.src(ws)), UPSTREAM_LINE)])
    out = r.out.replace("\r", "").strip().splitlines()
    return out[-1] if r.ok and out else None


def read_conf(path):
    """KEY=value shell assignments, one per line, quotes stripped."""
    out = {}
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


class Registry:
    def __init__(self, root, env=None, machine=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.store = Store(self.env)

    def registry_dir(self):
        return self.env.get("WK_TARGET_REGISTRY") or os.path.join(self.root, "targets", "hosts")

    def conf_path(self, name):
        return os.path.join(self.registry_dir(), name + ".conf")

    def known(self):
        try:
            return sorted(f[:-5] for f in os.listdir(self.registry_dir()) if f.endswith(".conf"))
        except OSError:
            return []

    def kind(self, name):
        if name in BUILTIN:
            return name
        conf = read_conf(self.conf_path(name))
        if not os.path.isfile(self.conf_path(name)):
            return None
        return conf.get("WK_TARGET_KIND") or "remote"

    def marker_path(self):
        return self.env.get("WK_MARKER") or os.path.join(self.env.get("HOME", os.path.expanduser("~")), ".wk-workspace")

    def in_workspace(self):
        return os.path.isfile(self.marker_path())

    def remote_marker_path(self):
        return self.env.get("WK_REMOTE_MARKER") or os.path.join(self.env.get("HOME", os.path.expanduser("~")), ".wk-remote")

    def in_remote_host(self):
        return os.path.isfile(self.remote_marker_path())

    def remote_marker_field(self, key):
        return read_conf(self.remote_marker_path()).get(key, "")

    def default(self):
        if self.in_workspace():
            return "local"
        return self.remote_marker_field("target") or "container"

    def vm_listed(self):
        """A guest exists only on a macOS host, and only where its store is not the container's."""
        if not self.store.macos_host:
            return False
        return bool(self.env.get("WK_VM_STORE")) or self.store.record_dir() != self.store.root()

    def all(self):
        out = ["container"]
        if self.vm_listed():
            out.append("vm")
        t = self.remote_marker_field("target")
        if t:
            out.append(t)
        # Skipped on the far end of a target: a delegated listing would pay an ssh timeout per machine it has no route to.
        if self.in_remote_host() or self.env.get("WK_IN_VM"):
            return out
        me = record.machine_name(self.env)
        for name in self.known():
            if name not in out and name.lower() != me:
                out.append(name)
        return out

    def machines(self):
        return [t for t in self.all() if t not in ("container", "vm", "local")]

    def here(self):
        return [t for t in self.all() if t not in self.machines()]

    def walk(self):
        """The targets a listing covers: WK_TARGET's, this workspace's, the ones here when another wk asked, else all."""
        if self.env.get("WK_TARGET"):
            return self.env["WK_TARGET"].split()
        if self.in_workspace():
            return [self.default()]
        if self.env.get("WK_NO_DELEGATE"):
            return self.here()
        return self.all()

    def on_target(self, name, ws):
        """Whether `ws` is on `name`: its directory, its environment, or a creation still running."""
        try:
            t = self.load(name)
        except LookupError:
            return False
        return self._holds(t, ws)

    def _holds(self, t, ws):
        if os.path.isdir(t.store.ws_dir(ws)):
            return True
        if t.info(ws) not in ("absent", "unreachable", ""):
            return True
        from wk.record import Records
        rec = Records(t.store.record_dir(), env=t.env).find("new", ws)
        return bool(rec and rec.alive(None))

    def _asked(self, name, ws):
        """A machine's answer for `ws`, its one probe paid here; one that does not answer is named, since what is there is not in the answer."""
        try:
            t = self.load(name)
        except LookupError:
            return False
        if os.path.isdir(t.store.ws_dir(ws)):
            return True
        ok, why = t.answers()
        if not ok:
            act.warn("could not ask %s over ssh: %s -- what is there is not in this answer" % (name, why))
            return False
        return self._holds(t, ws)

    def locate(self, ws):
        """Every target that answers for `ws`; the machines are asked at once."""
        hits = [t for t in self.here() if self.on_target(t, ws)]
        if hits or not self.machines():
            return hits
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(self.machines())) as pool:
            answers = list(pool.map(lambda m: (m, self._asked(m, ws)), self.machines()))
        return [m for m, hit in answers if hit]

    def ws_target(self, ws):
        """The one target holding `ws`; the default when none does."""
        if self.env.get("WK_TARGET"):
            return self.env["WK_TARGET"]
        hits = self.locate(ws)
        if not hits:
            return self.default()
        if len(hits) == 1:
            return hits[0]
        raise LookupError("workspace '%s' exists on targets: %s -- this cannot be\n    resolved; remove one, or set WK_TARGET"
                          % (ws, " ".join(hits)))

    def load(self, name):
        kind = self.kind(name)
        if kind is None:
            names = " ".join(self.known())
            raise LookupError(
                "unknown target '%s'.\n    The built-in ones are container, vm, remote and local.%s\n\n"
                "    Anything else is a machine, and needs a conf -- in the registry, so every\n"
                "    device gets it:\n\n        %s\n            WK_REMOTE_HOST=%s      # an ssh destination that already works\n"
                "            WK_REMOTE_ROOT=/home/you/wk\n\n    'wk remote setup %s' writes it for you."
                % (name, ("\n    The machines here: " + names) if names else "", self.conf_path(name), name, name))
        conf = read_conf(self.conf_path(name)) if name not in BUILTIN else {}
        env = dict(self.env)
        env.update(conf)
        if kind == "container":
            return Container(name, self.root, env, self.machine)
        if kind == "vm":
            return Vm(name, self.root, env, self.machine)
        if kind == "local":
            return LocalWorkspace(name, self.root, env, self.machine)
        return Remote(name, self.root, env, self.machine)


class Target:
    """The contract. `info` answers absent | creating | unreachable | the
    driver's own word for one that exists."""

    kind = "target"
    egress_filtered = False
    needs_base = True

    def __init__(self, name, root, env, machine):
        self.name = name
        self.root = root
        self.env = env
        self.machine = machine
        self.store = Store(env)

    def src(self, ws):
        return "/src/WebKit"

    def tools(self, ws):
        return "/opt/wk-tools"

    def os(self):
        return "linux"

    def arch(self, ws):
        return "native"

    def list(self):
        raise NotImplementedError

    def info(self, ws):
        raise NotImplementedError

    def created(self, ws):
        return True

    def exec(self, ws, argv, tty=False, timeout=None):
        raise NotImplementedError

    def far_side(self):
        return "none"

    def is_here(self):
        """Whether the machine behind this target is the one running this process."""
        return True

    def answers(self):
        """(whether the machine behind this target answers, why not)."""
        return True, ""

    def probe(self):
        """(far side, why it does not answer)."""
        return self.far_side(), ""

    def has_wk(self):
        return False

    def wk(self, *args, env=None, quiet=False):
        """(status, output) of the far side's own wk."""
        return 1, ""

    def branch(self, ws):
        if self.info(ws) in STATES_NOT_THERE:
            return "-"
        r = self.exec(ws, ["git", "-C", self.src(ws), "rev-parse", "--abbrev-ref", "HEAD"])
        out = r.out.replace("\r", "").strip()
        return out if r.ok and out else "-"

    def delegates(self):
        return False

    def start(self, ws):
        raise NotImplementedError

    def stop(self, ws):
        raise NotImplementedError

    def state(self, ws, info=None):
        """absent | creating | broken | present | unreachable: the record and
        the environment read together (lib/target.sh's ws_state)."""
        env = self.info(ws) if info is None else info
        ws_dir = self.store.ws_dir(ws)
        if env in ("creating", "unreachable"):
            return env
        if env == "absent":
            if not os.path.isdir(ws_dir):
                return "absent"
            return "broken" if self.created(ws) else "creating"
        if self.needs_base and not os.path.isfile(os.path.join(ws_dir, "base-id")):
            return "creating"
        return "present"

    def display_state(self, ws):
        st = self.state(ws)
        return self.info(ws) if st == "present" else st


class Container(Target):
    kind = "container"
    egress_filtered = True

    def _podman(self):
        if os.uname().sysname == "Darwin" and not self.env.get("WK_IN_VM"):
            return ["podman", "-c", self.env.get("WK_MACHINE", "wk")]
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def user(self):
        return self.env.get("WK_CONTAINER_USER") or os.environ.get("USER") or "wk"

    def home(self):
        return "/home/" + self.user()

    def sdk(self):
        if self.env.get("WK_IN_VM"):
            return self.env.get("WK_SDK") or "/opt/webkit-container-sdk"
        return self.env.get("WK_SDK") or os.path.join(
            self.env.get("XDG_DATA_HOME") or os.path.join(self.env.get("HOME", ""), ".local", "share"), "webkit-container-sdk")

    def arch(self, ws):
        path = os.path.join(self.store.ws_dir(ws), "arch")
        try:
            return self.machine.read(path).strip() or "native"
        except OSError:
            return "native"

    def list(self):
        r = self.machine.run(self._podman() + ["ps", "-a", "--filter", "name=^wk-", "--format", "{{.Names}}\t{{.Status}}"])
        rows = []
        for line in r.out.splitlines():
            name, _, status = line.partition("\t")
            if name.startswith("wk-"):
                rows.append((name[3:], status))
        return rows

    def created(self, ws):
        home = os.path.join(self.store.ws_dir(ws), "home")
        return self.machine.exists(os.path.join(home, READY_MARKER)) or self.machine.exists(os.path.join(home, FIRSTRUN_MARKER))

    def info(self, ws):
        r = self.machine.run(self._podman() + ["inspect", self.ctr(ws), "--format", "{{.State.Status}}"])
        st = r.out.strip() if r.ok else "absent"
        if not st or st == "absent":
            return "absent"
        return st if self.created(ws) else "creating"

    def branch(self, ws):
        head = os.path.join(self.store.ws_dir(ws), "changes", ".git", "HEAD")
        if not self.machine.exists(head):
            base = self.store.ws_base_id(ws)
            if not base:
                return "-"
            head = os.path.join(self.store.base_path(base), ".git", "HEAD")
        try:
            ref = self.machine.read(head).strip()
        except OSError:
            return "-"
        if ref.startswith("ref: refs/heads/"):
            return ref[len("ref: refs/heads/"):]
        if ref.startswith("ref: "):
            return ref[len("ref: "):]
        return "detached %s" % ref[:10]

    def exec(self, ws, argv, tty=False, timeout=None):
        cmd = [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-enter"), "--quiet", "--name", self.ctr(ws)]
        if not tty:
            cmd.append("--no-tty")
        cmd += ["--exec", "--", "/opt/wk-tools/container/proxy/ensure-bridge.sh", *argv]
        return self.machine.run(cmd, timeout=timeout)

    def is_here(self):
        return bool(self.env.get("WK_IN_VM")) or os.uname().sysname != "Darwin"

    def machine_name(self):
        return self.env.get("WK_MACHINE", "wk")

    def machine_state(self):
        """running | stopped | absent | ...: podman's own word for the machine, asked once per Container."""
        if not hasattr(self, "_machine_state"):
            r = self.machine.run(["podman", "machine", "inspect", self.machine_name(), "--format", "{{.State}}"])
            self._machine_state = r.out.strip() if r.ok else "absent"
        return self._machine_state

    def far_side(self):
        if self.is_here():
            return "none"
        return "answering" if self.machine_state() == "running" else "stopped"

    def has_wk(self):
        return self.far_side() == "answering"

    def start(self, ws):
        r = self.machine.act_run(self._podman() + ["start", self.ctr(ws)])
        return r.ok

    def stop(self, ws):
        r = self.machine.act_run(self._podman() + ["stop", "--time", "30", self.ctr(ws)])
        return r.ok


class Vm(Target):
    kind = "vm"
    egress_filtered = True
    needs_base = False

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self.store = Store(dict(env, WK_STORE=self.vm_store()))

    def user(self):
        return self.env.get("WK_VM_USER") or "admin"

    def src(self, ws):
        return "/Users/%s/WebKit" % self.user()

    def tools(self, ws):
        return "/Users/%s/wk-tools" % self.user()

    def home(self):
        return "/Users/" + self.user()

    def os(self):
        return "macos"

    def vm_store(self):
        return self.env.get("WK_VM_STORE") or self.store.record_dir()

    def key(self):
        return os.path.join(self.vm_store(), "vm", "id_ed25519")

    def base(self):
        return self.env.get("WK_VM_BASE") or "wk-base"

    def tart(self):
        """tart is a signed .app reached through a symlink; the three places it is looked for."""
        for c in (shell_which("tart"),
                  os.path.join(self.env.get("HOME", ""), ".local", "bin", "tart"),
                  os.path.join(self.env.get("HOME", ""), ".local", "share", "tart", "tart.app", "Contents", "MacOS", "tart")):
            if c and os.access(c, os.X_OK):
                return os.path.realpath(c)
        return None

    def vm(self, ws):
        return "wk-" + ws

    def _vms(self):
        bin = self.tart()
        if not bin:
            return []
        r = self.machine.run([bin, "list", "--format", "json"])
        try:
            vms = json.loads(r.out) if r.ok else []
        except ValueError:
            vms = []
        return [v for v in vms if str(v.get("Source", "")).lower() == "local"]

    def list(self):
        rows = []
        for v in self._vms():
            n = v.get("Name", "")
            if n.startswith("wk-") and n != self.base():
                rows.append((n[3:], v.get("State", "")))
        return rows

    def vm_state(self, ws):
        return next((v.get("State", "absent") for v in self._vms() if v.get("Name") == self.vm(ws)), "absent")

    def created(self, ws):
        return self.machine.exists(os.path.join(self.store.ws_dir(ws), READY_MARKER))

    def info(self, ws):
        st = self.vm_state(ws)
        if st == "absent":
            return "absent"
        return st if self.created(ws) else "creating"

    def ip(self, ws):
        if self.vm_state(ws) != "running":
            return None
        r = self.machine.run([self.tart(), "ip", self.vm(ws), "--wait", "30"])
        return r.out.strip() or None

    def exec(self, ws, argv, tty=False, timeout=None):
        ip = self.ip(ws)
        if not ip:
            return Result(1, "", "'%s' is not running (wk vm start %s)" % (ws, ws))
        cmd = " ".join(shlex.quote(a) for a in argv)
        return self.machine.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=%s" % self.env.get("WK_SSH_TIMEOUT", "10"),
                                 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                                 "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=10", "-i", self.key(),
                                 "%s@%s" % (self.user(), ip), "bash -lc %s" % shlex.quote(cmd)], timeout=timeout)

    def stop(self, ws):
        return shell.guest_stop(self.root, ws) == 0

    def start(self, ws):
        return shell.guest_start(self.root, ws) == 0


class LocalWorkspace(Target):
    """Inside a workspace: the workspace is this machine."""

    kind = "local"
    needs_base = False

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self.store = Store(dict(env, WK_STORE=env.get("WK_LOCAL_STORE") or Store(env).state_dir()))
        marker = read_conf(env.get("WK_MARKER") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".wk-workspace"))
        self.ws_name = marker.get("name", "")
        self.ws_src = marker.get("src", "")
        self.ws_arch = marker.get("arch", "") or "native"

    def src(self, ws):
        return self.ws_src

    def tools(self, ws):
        return self.root

    def home(self):
        return self.env.get("HOME", os.path.expanduser("~"))

    def os(self):
        return "macos" if os.uname().sysname == "Darwin" else "linux"

    def arch(self, ws):
        return self.ws_arch

    def list(self):
        return [(self.ws_name, "running")]

    def info(self, ws):
        return "running" if ws == self.ws_name else "absent"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["bash", "-lc", "exec " + " ".join(shlex.quote(a) for a in argv)], timeout=timeout)


class Remote(Target):
    """A machine of its own, reached over ssh (or this machine, when ~/.wk-remote names the target)."""

    kind = "remote"

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        marker = read_conf(env.get("WK_REMOTE_MARKER") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".wk-remote"))
        self.host = env.get("WK_REMOTE_HOST") or (name if name != "remote" else "")
        self.peer = bool(env.get("WK_REMOTE_PEER"))
        self.is_local = bool(env.get("WK_REMOTE_LOCAL")) or marker.get("target") == name
        self.needs_base = self.is_local
        root_there = env.get("WK_REMOTE_ROOT") or (marker.get("root", "") if self.is_local else "")
        if self.is_local and root_there:
            store = env.get("WK_REMOTE_STORE") or root_there
        else:
            store = env.get("WK_REMOTE_STORE") or os.path.join(Store(env).state_dir(), "remote", name)
        self.store = Store(dict(env, WK_STORE=store))
        self.probe_seconds = int(env.get("WK_PROBE_SECONDS") or 20)
        self.here = machine
        if not self.is_local and self.host:
            self.machine = Ssh(self.host, opts=self.ssh_opts(), timeout=int(env.get("WK_SSH_TIMEOUT") or 10), via=machine)
        self._probed = None
        self._has_wk = None
        self._peer_rows = None

    # ServerAliveInterval/CountMax because ConnectTimeout covers the TCP connect and nothing after it: a machine that accepts the connection and then stops answering -- a wedged sshd, a box deep in swap -- held `wk status <ws>` and `wk logs <ws>` past a 300s wait with no bound of their own (measured 2026-09-17, with moose down). Four missed keepalives at 15s is a session given up inside a minute, and a healthy long build answers them at the protocol level however busy the box is.
    def ssh_opts(self):
        d = os.path.join(Store(self.env).state_dir(), "ssh")
        os.makedirs(d, exist_ok=True)
        return ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ControlMaster=auto",
                "-o", "ControlPath=%s/%%h-%%p-%%r" % d, "-o", "ControlPersist=60"]

    def _far(self):
        if self.is_local or self.host:
            return self.machine
        act.die("target '%s' has no host to reach.\n    Set WK_REMOTE_HOST in %s, or\n"
                "    name the target after a machine your ~/.ssh/config already knows:\n        wk new <name> --target devbox-arm64-2"
                % (self.name, Registry(self.root, self.env).conf_path(self.name)))

    def _sh(self, text, timeout=None):
        return self._far().run(["sh", "-c", text], timeout=timeout)

    def probed(self):
        """The one round trip, memoised: home, cores, load, mem_mb, ionice, os, root; `why` when the machine did not answer."""
        if self._probed is not None:
            return self._probed
        r = self._sh(PROBE_SCRIPT, timeout=self.probe_seconds)
        if r.rc == TIMED_OUT:
            self._probed = {"why": "timed out after %ss" % self.probe_seconds}
        elif not r.ok:
            self._probed = {"why": ssh_last_word(r)}
        else:
            self._probed = parse_probe(r.out, self.env.get("WK_REMOTE_ROOT", ""))
        return self._probed

    def _probe_or_die(self):
        p = self.probed()
        if p.get("why") is not None:
            act.die("cannot reach '%s' over ssh: %s\n    This target has no way in but ssh, and it is not interactive: the key,\n"
                    "    the ProxyJump and the host entry all have to work non-interactively.\n"
                    "    What BatchMode refuses to ask -- a new host key, a passphrase -- one\n"
                    "    interactive  ssh %s true  asks and settles." % (self.host, p["why"], self.host))
        return p

    def answers(self):
        if self.is_local:
            return True, ""
        why = self.probed().get("why")
        return why is None, why or ""

    def root_there(self):
        return self._probe_or_die()["root"]

    def ws_dir_there(self, ws):
        return "%s/ws/%s" % (self.root_there(), ws)

    def home(self):
        return self._probe_or_die()["home"]

    def os(self):
        return self._probe_or_die().get("os") or "linux"

    def cores(self):
        return self._probe_or_die().get("cores") or 1

    def load(self):
        return self._probe_or_die().get("load") or 0

    def mem_mb(self):
        return self._probe_or_die().get("mem_mb") or 1024

    def src(self, ws):
        if self.peer and ws:
            return shell.peer_src(self.root, self.name, ws) or ""
        return self.ws_dir_there(ws) + "/WebKit"

    def tools(self, ws):
        t = self.env.get("WK_REMOTE_TOOLS", "")
        if not t:
            return self.root_there() + "/tools"
        return t if t.startswith("/") else "%s/%s" % (self.home(), t)

    def _peer_list(self):
        if self._peer_rows is None:
            self._peer_rows = []
            rc, out = self.wk("ls", "--json", env=dict(self.env, WK_NO_DELEGATE="1"), quiet=True)
            if rc == 0:
                try:
                    doc = json.loads(out)
                except ValueError:
                    doc = {}
                self._peer_rows = [(w.get("name", ""), w.get("state", "")) for w in doc.get("workspaces", [])]
        return self._peer_rows

    def list(self):
        if self.peer:
            return self._peer_list()
        if not self.answers()[0]:
            return []
        try:
            names = self._far().listdir(self.root_there() + "/ws")
        except OSError:
            return []
        return [(n, "present") for n in names if n and not n.startswith(".")]

    def info(self, ws):
        """One round trip: no directory is absent, no `.wk-ready` is creating, and no answer is unreachable, never absent."""
        if not self.answers()[0]:
            return "unreachable"
        if self.peer:
            st = next((state for n, state in self._peer_list() if n == ws), "")
            if st in ("creating", "unreachable"):
                return st
            return "present" if st else "absent"
        d = shlex.quote(self.ws_dir_there(ws))
        r = self._sh("if [ ! -d %s ]; then echo absent; elif [ -f %s/%s ]; then echo present; else echo creating; fi"
                     % (d, d, READY_MARKER))
        return (r.out.strip() if r.ok else "") or "unreachable"

    def created(self, ws):
        return self.info(ws) == "present"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self._sh("cd %s && %s" % (shlex.quote(self.src(ws)), " ".join(shlex.quote(a) for a in argv)), timeout=timeout)

    def is_here(self):
        return self.is_local

    def has_wk(self):
        if self.is_local or not self.answers()[0]:
            return False
        if self._has_wk is None:
            wk = shlex.quote(self.tools("") + "/wk")
            test = "test -x %s" % wk if self.peer else "test -f $HOME/.wk-remote && test -x %s" % wk
            self._has_wk = self._sh(test).ok
        return self._has_wk

    def far_side(self):
        if self.is_local:
            return "none"
        if not self.answers()[0]:
            return "unreachable"
        return "answering" if self.has_wk() else "no-wk"

    def probe(self):
        ok, why = self.answers()
        return (self.far_side(), "") if ok else ("unreachable", why)

    def delegates(self):
        if self.is_local:
            return False
        return self.peer or self.has_wk()

    def wk_cmd(self, args, env):
        """The far machine's own wk; the flags travel as environment, since an unknown argument is fatal on an old copy of wk over there."""
        pre = "".join("%s=1 " % v for v in ("WK_DEBUG", "WK_QUIET", "WK_YES", "WK_FORCE", "WK_DRY_RUN") if env.get(v))
        for v in ("WK_ROW_LABEL", "WK_NO_DELEGATE", "WK_ZED_PUBKEY"):
            if env.get(v):
                pre += "%s=%s " % (v, "1" if v == "WK_NO_DELEGATE" else shlex.quote(env[v]))
        return "cd $HOME && %s%s %s" % (pre, shlex.quote(self.tools("") + "/wk"), " ".join(shlex.quote(a) for a in args))

    def wk(self, *args, env=None, quiet=False):
        env = os.environ if env is None else env
        r = self._sh(self.wk_cmd(args, env) + ("" if quiet else " 2>&1"))
        return r.rc, r.out

    def start(self, ws):
        act.info("'%s' has no notion of starting a single workspace -- nothing to bring up for '%s'" % (self.name, ws))
        return True

    def stop(self, ws):
        act.err("the '%s' target has no notion of stopping a single workspace -- '%s' is left running" % (self.name, ws))
        return False


PROBE_SCRIPT = """
        echo "$HOME"
        u=$(uname -s)
        echo "$u"
        if [ "$u" = Linux ]; then
            nproc
            cat /proc/loadavg
            echo "===MEM==="
            cat /proc/meminfo
        else
            sysctl -n hw.ncpu
            sysctl -n vm.loadavg
            echo "===MEM==="
            vm_stat
        fi
        echo "===IONICE==="
        command -v ionice >/dev/null 2>&1 && echo yes || echo no"""


def ssh_last_word(r):
    """The one line a person acts on: ssh's last non-blank stderr line, its prefixes stripped."""
    lines = [l for l in r.err.splitlines() if l.strip()]
    line = lines[-1] if lines else ""
    for prefix in ("ssh: ", "kex_exchange_identification: "):
        if line.startswith(prefix):
            line = line[len(prefix):]
    return line or "ssh exited %d and said nothing" % r.rc


def parse_probe(text, root=""):
    """`sysctl -n vm.loadavg` puts the load average second where /proc/loadavg puts it first, and `vm_stat` reports pages where /proc/meminfo has MemAvailable in kB."""
    lines = text.splitlines()
    home = lines[0] if lines else ""
    uname = lines[1] if len(lines) > 1 else ""
    cores = _int(lines[2] if len(lines) > 2 else "") or 1
    section, head, mem, ionice = "head", [], [], "no"
    for line in lines[3:]:
        if line == "===MEM===":
            section = "mem"
        elif line == "===IONICE===":
            section = "ionice"
        elif section == "head":
            head.append(line)
        elif section == "mem":
            mem.append(line)
        elif line:
            ionice = line
    load, mem_mb = 0, 0
    if uname == "Linux":
        load = _int(head[0].split()[0]) if head and head[0].split() else 0
        for l in mem:
            if l.startswith("MemAvailable:"):
                mem_mb = _int(l.split()[1]) // 1024
                break
    else:
        f = head[0].split() if head else []
        load = _int(f[1]) if len(f) > 1 else 0
        ps, pages = 0, 0
        for l in mem:
            if "page size of" in l:
                ps = int(re.search(r"[0-9]+", l).group(0))
            for key in ("Pages free:", "Pages inactive:", "Pages speculative:"):
                if l.startswith(key):
                    pages += _int(l.split()[-1].rstrip("."))
        mem_mb = pages * ps // 1024 // 1024 if ps else 0
    return {"home": home, "cores": cores, "load": load, "mem_mb": mem_mb, "ionice": ionice,
            "os": "macos" if uname == "Darwin" else "linux", "root": root or home + "/wk"}


def _int(s):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def shell_which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None
