"""The targets: where a workspace lives and how it is driven. A `Registry`
names them (container, vm on a macOS host, this machine inside a workspace,
and every `targets/hosts/<name>.conf`); each driver answers the same
contract over a `Machine`. The remote driver's probe still goes through the
bash library."""

import json
import os
import re
import shlex

from wk import shell
from wk.machine import Local, Result
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

    def remote_marker_field(self, key):
        path = self.env.get("WK_REMOTE_MARKER") or os.path.join(self.env.get("HOME", os.path.expanduser("~")), ".wk-remote")
        return read_conf(path).get(key, "")

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
        for name in self.known():
            if name not in out:
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
        if os.path.isdir(t.store.ws_dir(ws)):
            return True
        if t.info(ws) not in ("absent", "unreachable", ""):
            return True
        from wk.record import Records
        rec = Records(t.store.record_dir(), env=t.env).find("new", ws)
        return bool(rec and rec.alive(None))

    def locate(self, ws):
        """Every target that answers for `ws`; the machines are asked at once."""
        hits = [t for t in self.here() if self.on_target(t, ws)]
        if hits or not self.machines():
            return hits
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(self.machines())) as pool:
            answers = list(pool.map(lambda m: (m, self.on_target(m, ws)), self.machines()))
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

    def probe(self):
        """(far side, why it does not answer)."""
        return self.far_side(), ""

    def has_wk(self):
        return False

    def wk(self, *args, env=None, quiet=False):
        """(status, output) of the far side's own wk."""
        return shell.machine_wk(self.root, self.name, *args, env=env, quiet=quiet)

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

    def far_side(self):
        if self.is_here():
            return "none"
        r = self.machine.run(["podman", "machine", "inspect", self.env.get("WK_MACHINE", "wk"), "--format", "{{.State}}"])
        return "answering" if r.ok and r.out.strip() == "running" else "stopped"

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
        return shell.run(self.root, '. "$WK_ROOT/targets/vm.sh"; t_start', ws) == 0


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
    """A machine of its own. Its probe (is it answering, does it run wk) is
    the bash driver's, asked through the bridge."""

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

    def _ask(self, fn, *args):
        return shell.ask(self.root, "load_target %s >/dev/null 2>&1; %s" % (shlex.quote(self.name), fn), *args)

    def src(self, ws):
        return self._ask("t_src", ws) or ""

    def tools(self, ws):
        return self._ask("t_tools", ws) or ""

    def home(self):
        return self._ask("t_home") or ""

    def os(self):
        return self._ask("t_os") or "linux"

    def list(self):
        out = self._ask("t_list") or ""
        return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]

    def info(self, ws):
        return self._ask("t_info", ws) or "unreachable"

    def created(self, ws):
        return self.info(ws) == "present"

    def exec(self, ws, argv, tty=False, timeout=None):
        script = shell._script("load_target %s >/dev/null 2>&1; t_exec" % shlex.quote(self.name), self.root)
        return self.machine.run(["bash", "-c", script, "wk", ws, *argv], timeout=timeout)

    def is_here(self):
        return self.is_local

    def far_side(self):
        return self._ask("t_far_side") or "unreachable"

    def probe(self):
        out = self._ask("_probe() { if t_answers; then printf 'side=%s\\n' \"$(t_far_side)\"; "
                        "else printf 'side=unreachable\\nwhy=%s\\n' \"$WK_FAR_WHY\"; fi; }; _probe") or ""
        fields = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)
        return fields.get("side") or "unreachable", fields.get("why", "")

    def has_wk(self):
        return self.far_side() == "answering"

    def delegates(self):
        if self.is_local:
            return False
        return self.peer or self.has_wk()

    def stop(self, ws):
        return shell.ws_stop(self.root, self.name, ws) == 0

    def start(self, ws):
        return shell.run(self.root, "load_target %s >/dev/null 2>&1; t_start" % shlex.quote(self.name), ws) == 0


def shell_which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None
