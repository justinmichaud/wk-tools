"""The targets: where a workspace lives and how it is driven. A `Registry`
names them (container, vm on a macOS host, this machine inside a workspace,
and every `targets/hosts/<name>.conf`); each driver answers the same
contract over a `Machine`."""

import json
import os
import pwd
import re
import shlex
import sys

from wk import act, record, shell, sshalias
from wk.machine import TIMED_OUT, Local, Result, Ssh
from wk.resources import Resources, workspace_marker_path
from wk.store import Store

BUILTIN = ("container", "vm", "remote", "local")
READY_MARKER = ".wk-ready"
FIRSTRUN_MARKER = ".wk-firstrun-complete"   # TODO: drop once no pre-marker workspace is left
STATES_NOT_THERE = ("absent", "creating", "broken", "unreachable")
READY_TIMEOUT = 300
GUEST_MIRROR = "/Volumes/My Shared Files/mirror/WebKit.git"   # where macOS automounts the tart share `mirror`
PROXY = "http://127.0.0.1:3128"
NO_PROXY = "localhost,127.0.0.1,::1"
MOTD_REFERENCE = '''
        cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null \\
        | grep -oE "/[A-Za-z0-9._/-]*[Ww]eb[Kk]it(\\.git)?" | sort -u \\
        | while read -r p; do
              git -C "$p" rev-parse --verify -q refs/heads/main >/dev/null 2>&1 || continue
              echo "$p"; break
          done'''

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


def zed_key_path(env):
    return os.path.join(Store(env).state_dir(), "ssh", "zed_ed25519")


def zed_key_pub(machine, env):
    """This machine's own zed key, generated the first time anything asks for it."""
    if env.get("WK_ZED_PUBKEY"):
        return env["WK_ZED_PUBKEY"]
    k = zed_key_path(env)
    d = os.path.dirname(k)
    if not machine.isdir(d):
        machine.mkdir(d)
        machine.act_run(["chmod", "0700", d])
    if not machine.exists(k):
        r = machine.act_run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                             "wk zed key (%s)" % (record.machine_name(env) or "host"), "-f", k])
        if not r.ok:
            return None
        act.info("generated this machine's zed key (%s)" % k)
    try:
        return machine.read(k + ".pub").strip()
    except OSError:
        return None


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
        return workspace_marker_path(self.env)

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
        return self.store.vm_store() is not None

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

    def default_config(self, name):
        """The last build's config, from its task record; else the target's own platform default."""
        target = self.load(self.ws_target(name))
        rec = record.Records(target.store.record_dir(), env=target.env).find("build", name)
        cfg = rec.field("config") if rec else ""
        if cfg:
            act.info("config: %s -- what '%s' was last built with" % (cfg, name))
            return cfg
        return "mac-release" if target.os() == "macos" else "jsc-release"


def path_kind_probe(path):
    """The one `path_kind` shell test; Container, Vm and Remote each run it where the workspace lives."""
    return "if [ -d %s ]; then echo dir; elif [ -e %s ]; then echo file; else echo absent; fi" \
        % ((shell.sh_quote(path),) * 2)


def path_kind_result(r):
    out = r.out.replace("\r", "").strip()
    return out if out in ("dir", "file", "absent") else ""


class Target:
    """The contract. `info` answers absent | creating | unreachable | the driver's own word for one that exists."""

    kind = "target"
    needs_base = True

    def __init__(self, name, root, env, machine):
        self.name = name
        self.root = root
        self.env = env
        self.machine = machine
        self._store = Store(env)

    @property
    def store(self):
        return self._store

    def src(self, ws):
        return "/src/WebKit"

    def tools(self, ws):
        return "/opt/wk-tools"

    def mirror_dir(self):
        return ""

    def os(self):
        return "linux"

    def arch(self, ws):
        return "native"

    def lldb_opts(self):
        return ""

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

    def egress_filtered(self, ws):
        return False

    def agent_sock(self):
        return None

    def _agent_secret(self, secret):
        return next(r for r in shell.agent_secrets(self.root, self.machine) if r[0] == secret)

    def _agent_secret_file(self, secret):
        """Where the workspace holds it, as login-shell text: the rc names CLAUDE_SECURESTORAGE_CONFIG_DIR, where a `file` row's own tool rewrites it."""
        row = self._agent_secret(secret)
        if row[4] == "file":
            return '"$CLAUDE_SECURESTORAGE_CONFIG_DIR/%s"' % row[1]
        return '"$HOME/%s"' % row[2]

    def agent_secret_present(self, ws, secret):
        return self.exec(ws, ["bash", "-lc", "test -s %s" % self._agent_secret_file(secret)]).ok

    def agent_secret_remedy(self, ws, secret):
        return shell.agent_secret_store_remedy(self.root, self.machine, secret)

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

    def create_log(self, ws):
        return os.path.join(self.store.root(), "log", "new-%s.log" % ws)

    def store_init(self):
        raise NotImplementedError

    def sdk_refresh(self):
        return True

    def create(self, ws, base=None, arch="native"):
        raise NotImplementedError

    def ready_timeout(self, timeout):
        return int(timeout if timeout is not None else self.env.get("WK_READY_TIMEOUT") or READY_TIMEOUT)

    def ready(self, ws, clock, timeout=None):
        """Whether the workspace came to exist within the timeout, `info` asked once a second; absent and unreachable do not improve with waiting."""
        for _ in range(self.ready_timeout(timeout)):
            st = self.info(ws)
            if st != "creating":
                return st not in ("absent", "unreachable")
            clock.sleep(1)
        return False

    def destroy(self, ws):
        raise NotImplementedError

    def ensure_dir_mode(self, path, mode):
        self.machine.mkdir(path)
        if not self.machine.run(["find", path, "-maxdepth", "0", "-perm", mode]).out.strip():
            self.machine.act_run(["chmod", mode, path])

    def workspaces(self):
        return sorted(set(self.store.workspaces()) | {n for n, _ in self.list() if n})

    def act_exec(self, ws, argv):
        if act.dry_run():
            sys.stderr.write("would run in %s: %s\n" % (ws, " ".join(shlex.quote(a) for a in argv)))
            return Result(0)
        return self.exec(ws, argv)

    def wiring_args(self):
        return "", "", ""

    def sync(self, named=False):
        """Refresh this target's furniture: its copy of the tooling, and what it keeps of its own."""
        return True

    def enter_argv(self, ws):
        """(argv, cwd) to `os.execvp` into a login shell in `ws`; `cwd` is set only when the shell needs starting there rather than told to `cd`."""
        raise NotImplementedError

    def pull(self, ws, src, dest):
        self.machine.copy_out(src, dest)

    def pull_dir(self, ws, src, dest):
        self.machine.copy_tree_out(src, dest)

    def push(self, ws, src, dest):
        self.machine.copy_in(src, dest)

    def push_dir(self, ws, src, dest):
        self.machine.copy_tree_in(src, dest)

    def path_kind(self, ws, path):
        """dir | file | absent | "" (a probe that answered nothing is not evidence of absent)."""
        if self.machine.isdir(path):
            return "dir"
        if self.machine.exists(path):
            return "file"
        return "absent"

    def ssh_host(self, ws):
        """The ssh destination for Zed and the generated alias."""
        return "wk-%s" % ws

    def ssh_prepare(self, ws):
        """Point an editor at this target over ssh; nothing for one already an ssh destination."""

    def ssh_user(self, ws):
        """The account inside the workspace an editor logs into, or None."""
        return None

    def ssh_proxy(self, ws):
        """What to run here to reach an addressless workspace, or None."""
        return None

    def exec_argv(self, ws, argv, tty=False):
        raise NotImplementedError

    def exec_tty(self, ws, argv, timeout=None):
        """Blocking, this process's own stdio inherited -- a real pty for lldb/samply/xctrace -- control returns here, unlike `enter_argv`'s `os.execvp`."""
        cmd, cwd = self.exec_argv(ws, argv, tty=True)
        return self.machine.run_tty(cmd, cwd=cwd, timeout=timeout)


    def ccache_dir(self, ws):
        return "/ccache"

    def build_argv(self, ws, argv):
        """(argv, cwd)."""
        return self.exec_argv(ws, argv)

    def task_put(self, ws, task):
        """The record already sits in the store the building machine reports from."""

    def build_size(self, ws):
        """(cores, mem_mb, load or None); a load makes the build polite."""
        res = Resources(self.machine, self.env)
        return res.envelope_cores(), res.envelope_mem_mb(), None


def show(r):
    """A captured command's output, into the log this driver writes."""
    sys.stderr.write(r.out + r.err)


def arch_image(root, arch):
    if arch == "armhf":
        return read_conf(os.path.join(root, "lib", "arch.sh")).get("WK_IMAGE_ARMHF", "")
    return ""


class Container(Target):
    kind = "container"

    def _podman(self):
        if os.uname().sysname == "Darwin" and not self.env.get("WK_IN_VM"):
            return ["podman", "-c", self.env.get("WK_MACHINE", "wk")]
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def egress_filtered(self, ws):
        return True

    def agent_sock(self):
        return "/run/wk/ssh-agent.sock"

    def rootless(self):
        r = self.machine.run(self._podman() + ["info", "--format", "{{.Host.Security.Rootless}}"])
        return r.out.strip() if r.ok else "unknown"

    def user(self):
        return self.env.get("WK_CONTAINER_USER") or pwd.getpwuid(os.getuid()).pw_name

    def home(self):
        return "/home/" + self.user()

    def lldb_opts(self):
        """podman's seccomp allow-list lacks personality(ADDR_NO_RANDOMIZE); stop-on-exec would hijack `wk gui --lldb ui`."""
        return "-O 'settings set target.disable-aslr false' -O 'settings set target.process.stop-on-exec false'"

    def mirror_dir(self):
        """Mounted at this machine's own path, so a `--shared` snapshot's alternates resolve on both sides."""
        return self.store.mirror()

    def sdk(self):
        if self.env.get("WK_IN_VM"):
            return self.env.get("WK_SDK") or "/opt/webkit-container-sdk"
        return self.env.get("WK_SDK") or os.path.join(
            self.env.get("XDG_DATA_HOME") or os.path.join(self.env.get("HOME", ""), ".local", "share"), "webkit-container-sdk")

    def sdk_env(self):
        """The environment every SDK script reads, and refuses to run without (`env` prefix for an argv)."""
        return ["env", "WKDEV_SDK=%s" % self.sdk(), "WKDEV_CONTAINER_UID=%d" % os.getuid(), "WKDEV_CONTAINER_GID=%d" % os.getgid(),
                "WKDEV_CONTAINER_USER=%s" % self.user(), "WKDEV_CONTAINER_SHELL=/bin/bash"]

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
        return self.machine.run(self.exec_argv(ws, argv, tty)[0], timeout=timeout)

    def exec_argv(self, ws, argv, tty=False):
        cmd = self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-enter"), "--quiet", "--name", self.ctr(ws)]
        if not tty:
            cmd.append("--no-tty")
        return cmd + ["--exec", "--", "/opt/wk-tools/container/proxy/ensure-bridge.sh", *argv], None

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

    def wk(self, *args, env=None, quiet=False):
        """(status, output) of the podman VM's wk, whose command line is lib/target.sh's vm_wk_cmd."""
        env = os.environ if env is None else env
        line = shell.ask(self.root, "vm_wk_cmd", *args, env=env, quiet=True)
        if line is None:
            return 1, ""
        r = self.machine.run(["podman", "machine", "ssh", self.machine_name(), "--", line + ("" if quiet else " 2>&1")], input="")
        return r.rc, r.out

    def start(self, ws):
        r = self.machine.act_run(self._podman() + ["start", self.ctr(ws)])
        return r.ok

    def stop(self, ws):
        r = self.machine.act_run(self._podman() + ["stop", "--time", "30", self.ctr(ws)])
        return r.ok

    def tools_src(self):
        return self.env.get("WK_TOOLS_SRC") or ("/opt/wk-tools" if self.env.get("WK_IN_VM") else self.root)

    def sync(self, named=False):
        act.info("nothing to copy: a container bind-mounts this checkout (%s) at\n"
                 "  /opt/wk-tools, so the tooling in one is never stale. What the VM has\n"
                 "  installed rather than mounted -- the proxy and injector units, the skills,\n"
                 "  its packages -- comes from:  ./setup --stage vmtools" % self.root)
        return True

    def runtime_dir(self):
        return os.path.join(self.env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "wk")

    def ccache_maxsize(self):
        return self.env.get("WK_CCACHE_MAXSIZE") or "40G"

    def exists(self, ws):
        return self.machine.run(self._podman() + ["container", "exists", self.ctr(ws)]).ok

    def store_init(self):
        root = self.store.root()
        for d in ("", "git", "base", "ws", "cache/ccache", "cache/yocto/downloads", "cache/yocto/sstate", "cache/buildroot/dl",
                  "cache/buildroot/ccache", "cache/bench", "bench", "skills"):
            self.machine.mkdir(os.path.join(root, d) if d else root)
        conf = os.path.join(root, "cache", "ccache", "ccache.conf")
        if not self.machine.exists(conf):
            self.machine.write(conf, shell.ccache_conf(self.root, self.env))
        for d in (self.store.secrets_dir(), self.store.agent_rw_dir()):
            self.ensure_dir_mode(d, "0700")
        rc = shell.secrets_publish(self.root, self.env)
        if rc:
            raise act.Refused(rc)

    def sdk_refresh(self):
        r = self.machine.act_run(["bash", os.path.join(self.root, "container", "sdk-refresh.sh"), self.sdk()])
        show(r)
        if not r.ok:
            act.die("refreshing the webkit-container-sdk checkout failed (above); wkdev-create\n"
                    "    would otherwise ask for whatever image tag was current when this checkout\n    was last fetched.")
        return True

    # podman makes a missing mount destination as container root: the mirror's is inside the home where this machine's store is under $HOME.
    def _ensure_home_mountpoint(self, ws_dir, dest):
        home = self.home() + "/"
        if dest.startswith(home) and len(dest) > len(home):
            self.machine.mkdir(os.path.join(ws_dir, "home", dest[len(home):]))

    def sandbox_flags(self, arch):
        rt = self.runtime_dir()
        self.machine.mkdir(rt)
        flags = ["--volume", "%s:/run/wk" % rt, "--env", "WK_PROXY_SOCKET=/run/wk/proxy.sock"]
        for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            flags += ["--env", "%s=%s" % (v, PROXY)]
        for v in ("no_proxy", "NO_PROXY"):
            flags += ["--env", "%s=%s" % (v, NO_PROXY)]
        flags += ["--env", "WAYLAND_DISPLAY=/run/wk/display/wayland-0"]
        if shell.arch_has_gpu(self.root, self.machine, arch):
            flags += shell.gpu_flags(self.root, self.machine)
        return flags

    def create_flags(self, ws, base, arch):
        ws_dir, store, mirror = self.store.ws_dir(ws), self.store.root(), self.store.mirror()
        mirror_dir = os.path.dirname(mirror)
        res = Resources(self.machine, self.env, self.os())
        volumes = [("%s:/opt/wk-tools:ro" % self.tools_src()), "%s:%s:ro" % (mirror_dir, mirror_dir)]
        flags = ["--volume", volumes[0], "--volume", volumes[1], "--env", "WK_MIRROR=%s" % mirror,
                 "--volume", "%s:/src/WebKit:O,upperdir=%s/changes,workdir=%s/overlay-work" % (self.store.base_path(base), ws_dir, ws_dir),
                 "--volume", "%s/build:/src/WebKit/WebKitBuild" % ws_dir,
                 "--volume", "%s:/var/lib/wk/ws/%s" % (ws_dir, ws)]
        for sub, dest in (("cache/ccache", "/ccache"), ("cache/yocto", "/cache/yocto"), ("cache/buildroot", "/cache/buildroot"),
                          ("cache/bench", "/cache/bench"), ("bench", "/bench"), ("skills", "/skills")):
            flags += ["--volume", "%s/%s:%s" % (store, sub, dest)]
        flags += ["--volume", "%s:/secrets:ro" % self.store.secrets_view_dir("container"),
                  "--volume", "%s/agent-rw:/agent-rw" % store,
                  "--memory", "%dm" % res.envelope_mem_mb(), "--cpus", str(res.envelope_cores())]
        for kv in ("CCACHE_DIR=/ccache", "CCACHE_MAXSIZE=%s" % self.ccache_maxsize(), "CCACHE_BASEDIR=/src/WebKit",
                   "CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime", "CCACHE_PCH_EXTSUM=true",
                   "CCACHE_DEPEND=true", "CCACHE_NOHASHDIR=true", "DL_DIR=/cache/yocto/downloads", "SSTATE_DIR=/cache/yocto/sstate",
                   "BR2_DL_DIR=/cache/buildroot/dl", "BR2_CCACHE_DIR=/cache/buildroot/ccache", "WK_WORKSPACE=%s" % ws,
                   "WK_ARCH=%s" % arch, "WKDEV_OFFLINE=1", "WK_LOCAL_STORE=/var/lib/wk"):
            flags += ["--env", kv]
        return flags + self.sandbox_flags(arch)

    def create_argv(self, ws, base, arch):
        u = self.user()
        argv = self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-create"), "--network", "none", "--isolated"]
        if arch != "native":
            argv += ["--arch", "arm"]
        image = self.env.get("WK_SDK_IMAGE") or arch_image(self.root, arch)
        if image:
            argv += ["--image", image]
        return argv + ["--name", self.ctr(ws), "--shell", "/bin/bash", "--user", u, "--group", u,
                       "--home", os.path.join(self.store.ws_dir(ws), "home"), "--additional-flags", " ".join(self.create_flags(ws, base, arch))]

    def create(self, ws, base=None, arch="native"):
        ws_dir = self.store.ws_dir(ws)
        if not self.machine.isdir(self.store.base_path(base)):
            act.die("base snapshot %s not found; run 'wk sync' first" % base)
        if self.exists(ws):
            act.die("workspace '%s' already exists" % ws)
        for d in (ws_dir, "changes", "overlay-work", "home", "build"):
            self.machine.mkdir(d if d == ws_dir else os.path.join(ws_dir, d))
        self._ensure_home_mountpoint(ws_dir, os.path.dirname(self.store.mirror()))
        self.machine.write(os.path.join(ws_dir, "arch"), arch + "\n")
        argv = self.create_argv(ws, base, arch)
        if self.env.get("WK_SDK_IMAGE"):
            act.info("using workspace image %s (WK_SDK_IMAGE)" % self.env["WK_SDK_IMAGE"])
        act.info("creating workspace '%s' from base %s (rootless-proxy, %s)" % (ws, base, arch))
        r = self.machine.act_run(argv)
        show(r)
        if not r.ok:
            act.die("wkdev-create failed for '%s' (exit %d); what it said is above" % (ws, r.rc), r.rc)
        r = self.machine.act_run(["install", "-m", "0755", os.path.join(self.root, "container", "firstrun.sh"),
                                  os.path.join(ws_dir, "home", ".wkdev-firstrun")])
        if not r.ok:
            act.die("installing firstrun.sh into '%s' failed (exit %d); wkdev-create made the container "
                     "but it is not usable -- run 'wk rm %s' and retry" % (ws, r.rc, ws), r.rc)
        # Last: create() reads this file's presence as "the workspace finished setting up".
        self.machine.write(os.path.join(ws_dir, "base-id"), base + "\n")

    def ready(self, ws, clock, timeout=None):
        for _ in range(self.ready_timeout(timeout)):
            if self.created(ws):
                return True
            if not self.exists(ws):
                break
            clock.sleep(1)
        act.warn("initialisation did not complete; last output from the container:")
        r = self.machine.run(self._podman() + ["logs", self.ctr(ws)])
        for line in [l for l in (r.out + r.err).splitlines() if l.strip()][-8:]:
            sys.stderr.write("    %s\n" % line)
        return False

    def destroy(self, ws):
        c, ws_dir = self.ctr(ws), self.store.ws_dir(ws)
        if self.exists(ws):
            self.machine.act_run(self._podman() + ["rm", "-f", c])
            act.info("removed container %s" % c)
        if not self.machine.isdir(ws_dir):
            return
        self.machine.act_run(["podman", "unshare", "rm", "-rf", ws_dir])   # keep-id: root's files inside are a subordinate uid
        self.machine.remove(ws_dir)
        if not act.dry_run() and self.machine.isdir(ws_dir):
            act.warn("could not fully remove %s" % ws_dir)
        else:
            act.info("removed %s" % ws_dir)

    def _ctr_user(self, ws):
        """podman's own word for the container's user, its `WorkingDir`; None when it does not know the container."""
        r = self.machine.run(self._podman() + ["inspect", self.ctr(ws), "--format", "{{.Config.WorkingDir}}"])
        home = r.out.strip() if r.ok else ""
        if home.startswith("/home/") and len(home) > len("/home/"):
            return home[len("/home/"):]
        return None

    def enter_argv(self, ws):
        """Spelled out rather than wkdev-enter's own login shell: without the token/keyring
        bridge, `git-webkit pr` reports a locked macOS Keychain instead of a missing token."""
        return (self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-enter"),
                                  "--name", self.ctr(ws), "--exec", "--",
                                  "/opt/wk-tools/container/proxy/ensure-bridge.sh",
                                  "/usr/bin/env", "USER=%s" % self.user(), "/bin/bash", "--login"], None)

    def pull(self, ws, src, dest):
        r = self.machine.act_run(self._podman() + ["cp", "%s:%s" % (self.ctr(ws), src), dest])
        if not r.ok:
            raise OSError(r.err.strip() or "podman cp failed")

    def push(self, ws, src, dest):
        r = self.machine.act_run(self._podman() + ["cp", src, "%s:%s" % (self.ctr(ws), dest)])
        if not r.ok:
            raise OSError(r.err.strip() or "podman cp failed")

    def pull_dir(self, ws, src, dest):
        self.machine.remove(dest)
        self.machine.mkdir(dest)
        r = self.machine.act_run(self._podman() + ["cp", "%s:%s/." % (self.ctr(ws), src), dest])
        if not r.ok:
            raise OSError(r.err.strip() or "podman cp failed")

    def push_dir(self, ws, src, dest):
        u = self._ctr_user(ws)
        if u is None:
            act.die("workspace '%s' has no container to reach (podman does not know it)" % ws)
        r = self.machine.act_run(self._podman() + ["exec", "--user", u, self.ctr(ws), "/bin/sh", "-c",
                                                    "rm -rf %s && mkdir -p %s" % (shell.sh_quote(dest), shell.sh_quote(dest))])
        if not r.ok:
            raise OSError(r.err.strip() or "could not clear %s" % dest)
        r = self.machine.act_run(self._podman() + ["cp", src + "/.", "%s:%s" % (self.ctr(ws), dest)])
        if not r.ok:
            raise OSError(r.err.strip() or "podman cp failed")

    def path_kind(self, ws, path):
        u = self._ctr_user(ws)
        if u is None:
            act.die("workspace '%s' has no container to reach (podman does not know it)" % ws)
        r = self.machine.run(self._podman() + ["exec", "--user", u, self.ctr(ws), "/bin/sh", "-c", path_kind_probe(path)])
        return path_kind_result(r)

    def ssh_user(self, ws):
        return self._ctr_user(ws)

    def ssh_proxy(self, ws):
        return "%s %s" % (os.path.join(self.root, "container", "ssh-transport.sh"), ws)

    def ssh_prepare(self, ws):
        """An sshd inside the container so Zed reaches it like every target, over the `Host wk-<ws>` alias."""
        c = self.ctr(ws)
        u = self._ctr_user(ws)
        if u is None:
            act.die("no container workspace called '%s' on this machine.\n"
                    "    'wk ls' lists the ones there are, and 'wk start' brings the podman machine up\n"
                    "    if it is stopped. (An editor reaches a container over podman from here: the\n"
                    "    workspace has no network interface, so there is no other route in.)" % ws)
        h = "/home/%s" % u
        if not self.machine.run(self._podman() + ["exec", c, "test", "-x", "/usr/sbin/sshd"]).ok:
            act.info("installing openssh-server in '%s' (once per workspace; Zed needs an sshd to talk to)" % ws)
            r = self.machine.act_run(self._podman() + ["exec", c, "/opt/wk-tools/container/proxy/ensure-bridge.sh", "/bin/sh", "-c",
                                                        "apt-get update -qq && apt-get install -y -qq --no-install-recommends openssh-server"])
            show(r)
            if not r.ok or not self.machine.run(self._podman() + ["exec", c, "test", "-x", "/usr/sbin/sshd"]).ok:
                act.die("could not install openssh-server in '%s', and Zed needs that one package.\n"
                        "    A refused fetch is logged by the egress proxy as 'DENY <host>:<port>' -- it\n"
                        "    runs as the wk-proxy user service on the machine that holds the containers\n"
                        "    (journalctl --user -u wk-proxy), and its allowlist is\n"
                        "    container/proxy/wk-proxy.py." % ws)
        script = ("set -e\n"
                  "install -d -m 0700 '%(h)s/.wk-ssh' '%(h)s/.ssh'\n"
                  "[ -f '%(h)s/.wk-ssh/ssh_host_ed25519_key' ] ||\n"
                  "    ssh-keygen -q -t ed25519 -N '' -C 'wk-%(ws)s host key' -f '%(h)s/.wk-ssh/ssh_host_ed25519_key'\n"
                  "touch '%(h)s/.ssh/authorized_keys'\n"
                  "chmod 0600 '%(h)s/.ssh/authorized_keys'") % {"h": h, "ws": ws}
        r = self.machine.act_run(self._podman() + ["exec", "--user", u, c, "/bin/sh", "-c", script])
        if not r.ok:
            act.die("could not prepare the ssh identity in '%s'" % ws)
        pub = zed_key_pub(self.machine, self.env)
        if pub is None:
            act.die("could not create this machine's zed key")
        if not self.machine.run(self._podman() + ["exec", "--user", u, c, "grep", "-qsF", pub, "%s/.ssh/authorized_keys" % h]).ok:
            r = self.machine.act_run(self._podman() + ["exec", "-i", "--user", u, c, "/bin/sh", "-c",
                                                        "cat >> '%s/.ssh/authorized_keys'" % h], input=pub + "\n")
            if not r.ok:
                act.die("could not authorise the editor's key in '%s'" % ws)
            act.info("authorised the editor's key in '%s'" % ws)
        sshalias.alias_set(self.machine, self.env, ws, "wk-%s.container.invalid" % ws, u,
                           identity=zed_key_path(self.env), extra=("ProxyCommand %s" % self.ssh_proxy(ws),))


class Vm(Target):
    kind = "vm"
    needs_base = False
    agent_rw_share = "agent-rw"

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self._vm_store = None

    @property
    def store(self):
        if self._vm_store is None:
            d = self.vm_store()
            if d is None:
                act.die("the vm target has no store of its own on this machine -- set WK_VM_STORE apart from WK_STORE")
            self._vm_store = Store(dict(self.env, WK_STORE=d))
        return self._vm_store

    def user(self):
        return self.env.get("WK_VM_USER") or "admin"

    def src(self, ws):
        return "/Users/%s/WebKit" % self.user()

    def tools(self, ws):
        return "/Users/%s/wk-tools" % self.user()

    def home(self):
        return "/Users/" + self.user()

    def mirror_dir(self):
        return GUEST_MIRROR

    def os(self):
        return "macos"

    def build_size(self, ws):
        cores, mem = shell.target_size(self.root, self.machine, self.name, ws)
        return cores, mem, None

    def vm_store(self):
        return Store(self.env).vm_store()

    def vm_dir(self):
        return os.path.join(self.store.root(), "vm")

    def key(self):
        return os.path.join(self.vm_dir(), "id_ed25519")

    def egress_filtered(self, ws):
        return not self.machine.exists(os.path.join(self.vm_dir(), ws + ".unfiltered"))

    def agent_sock(self):
        return "/Users/%s/.wk-ssh-agent.sock" % self.user()

    def agent_secret_remedy(self, ws, secret):
        if self._agent_secret(secret)[4] == "file" and not self.exec(ws, ["bash", "-lc", 'test -d "$CLAUDE_SECURESTORAGE_CONFIG_DIR"']).ok:
            return ("the %s share is not mounted in '%s': 'wk vm stop %s', then 'wk vm start %s' boots it with the share"
                    % (self.agent_rw_share, ws, ws, ws))
        return super().agent_secret_remedy(ws, secret)

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

    def _guest_ssh_opts(self):
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=10", "-i", self.key()]

    def _guest_ssh(self, ws):
        """An `Ssh` onto the guest's own address, its opts identical to `exec`'s -- None while it is not running."""
        ip = self.ip(ws)
        if not ip:
            return None
        return Ssh("%s@%s" % (self.user(), ip), opts=self._guest_ssh_opts(),
                  timeout=int(self.env.get("WK_SSH_TIMEOUT") or 10), via=self.machine)

    def _guest_ssh_or_die(self, ws):
        m = self._guest_ssh(ws)
        if m is None:
            act.die("'%s' is not running (wk vm start %s)" % (ws, ws))
        return m

    def enter_argv(self, ws):
        ip = self.ip(ws)
        if not ip:
            act.die("'%s' is not running (wk vm start %s)" % (ws, ws))
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%s" % self.env.get("WK_SSH_TIMEOUT", "10")] + self._guest_ssh_opts()
        return (["ssh", "-t"] + opts + ["%s@%s" % (self.user(), ip),
                "cd %s 2>/dev/null; exec $SHELL -l" % shell.sh_quote(self.src(ws))], None)

    def exec_argv(self, ws, argv, tty=False):
        ip = self.ip(ws)
        if not ip:
            act.die("'%s' is not running (wk vm start %s)" % (ws, ws))
        opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=%s" % self.env.get("WK_SSH_TIMEOUT", "10")] + self._guest_ssh_opts()
        cmd = " ".join(shlex.quote(a) for a in argv)
        return (["ssh"] + (["-t"] if tty else []) + opts + ["%s@%s" % (self.user(), ip), "bash -lc %s" % shlex.quote(cmd)], None)

    def pull(self, ws, src, dest):
        self._guest_ssh_or_die(ws).copy_out(src, dest)

    def push(self, ws, src, dest):
        self._guest_ssh_or_die(ws).copy_in(src, dest)

    def pull_dir(self, ws, src, dest):
        self._guest_ssh_or_die(ws).copy_tree_out(src, dest)

    def push_dir(self, ws, src, dest):
        self._guest_ssh_or_die(ws).copy_tree_in(src, dest)

    def path_kind(self, ws, path):
        r = self._guest_ssh_or_die(ws).run(["sh", "-c", path_kind_probe(path)])
        return path_kind_result(r)

    def ssh_host(self, ws):
        ip = self.ip(ws)
        return "%s@%s" % (self.user(), ip) if ip else None

    def ssh_user(self, ws):
        return self.user()

    def stop(self, ws):
        return shell.guest_stop(self.root, ws) == 0

    def sync(self, named=False):
        ok = True
        for g, _ in self.list():
            if self.info(g) != "running":
                sys.stderr.write("  %-24s not running -- skipped\n" % g)
            elif shell.sync_tools(self.root, self.machine, self.name, g):
                sys.stderr.write("  %-24s ok\n" % g)
            else:
                ok = False
        return ok

    def start(self, ws):
        return shell.guest_start(self.root, ws) == 0

    def tart_or_die(self):
        bin = self.tart()
        if not bin:
            act.die("tart is not installed.\n    Install the signed bundle (it needs the virtualization entitlement, so the\n"
                    "    .app must stay intact):\n      mkdir -p ~/.local/share/tart ~/.local/bin\n"
                    "      curl -fsSLO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz\n"
                    "      tar -xzf tart.tar.gz -C ~/.local/share/tart/\n"
                    "      ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart\n"
                    "    Licence: FSL-1.1-ALv2; internal use is a Permitted Purpose (README.md, Setup).")
        return bin

    def store_init(self):
        self.machine.mkdir(self.store.root())
        self.machine.mkdir(os.path.join(self.store.root(), "ws"))
        self.ensure_dir_mode(self.vm_dir(), "0700")

    def running_vms(self):
        """Every VM on this host, the podman machine included: Virtualization.framework counts them against one limit."""
        names = [v["Name"][3:] for v in self._vms() if v.get("State") == "running" and str(v.get("Name", "")).startswith("wk-")]
        r = self.machine.run(["podman", "machine", "inspect", self.env.get("WK_MACHINE", "wk"), "--format", "{{.State}}"])
        if r.ok and r.out.strip() == "running":
            names.append("podman machine %s" % self.env.get("WK_MACHINE", "wk"))
        return names

    def create(self, ws, base=None, arch="native"):
        v, ws_dir, mirror = self.vm(ws), self.store.ws_dir(ws), self.store.mirror()
        if self.vm_state(ws) != "absent":
            act.die("workspace '%s' already exists" % ws)
        if not self.machine.isdir(mirror):
            act.die("no WebKit mirror on this machine for '%s' to clone its checkout from\n    (%s does not exist):  wk sync    makes it" % (ws, mirror))
        rc = shell.vm_ensure_base(self.root, self.env)
        if rc:
            raise act.Refused(rc)
        why = shell.vm_base_stale(self.root, self.env)
        if why and self.env.get("WK_VM_FORCE"):
            act.warn("WK_VM_FORCE=1 -- '%s' is cloned from a base that\n  predates its own provisioning inputs: %s" % (ws, why))
        elif why:
            act.die("'%s' predates its own provisioning inputs: %s.\n  '%s' would be a clone of it, carrying the desktop settings of the day it\n"
                    "  was sealed -- which is how a guest comes up behind Setup Assistant, where\n  nothing in the guest can clear it:\n"
                    "      wk vm base --rebuild     hours; existing guests are unaffected\n  WK_VM_FORCE=1 clones it anyway." % (self.base(), why, ws))
        running = self.running_vms()
        if len(running) >= int(self.env.get("WK_VM_MAX") or 2):
            act.warn("%d VM(s) already running on this host; you will have to stop one before starting '%s':\n%s"
                     % (len(running), ws, "\n".join("      " + n for n in running)))
        act.info("cloning %s -> %s (APFS copy-on-write)" % (self.base(), v))
        res = Resources(self.machine, self.env, "macos")
        cpus = self.env.get("WK_VM_CPUS") or str(res.envelope_cores())
        mem = self.env.get("WK_VM_MEM_MB") or str(res.envelope_mem_mb())
        tart = self.tart_or_die()
        for argv in ([tart, "clone", self.base(), v],
                     [tart, "set", v, "--cpu", cpus, "--memory", mem, "--random-mac", "--display", self.env.get("WK_VM_DISPLAY") or "1280x800", "--display-refit"]):
            r = self.machine.act_run(argv)
            show(r)
            if not r.ok:
                act.die("%s failed for '%s' (exit %d); what it said is above" % (" ".join(argv[1:2]), ws, r.rc), r.rc)
        self.machine.mkdir(ws_dir)
        self.machine.write(os.path.join(ws_dir, READY_MARKER), "")

    def runners(self, v):
        r = self.machine.run(["pgrep", "-f", "tart run .*[[:space:]]%s$" % v])
        return [int(p) for p in r.out.split() if p.isdigit()] if r.ok else []

    def destroy(self, ws):
        v, ws_dir = self.vm(ws), self.store.ws_dir(ws)
        if v == self.base():
            act.die("refusing to delete the golden base (wk vm base --rebuild)")
        if self.vm_state(ws) != "absent":
            tart = self.tart_or_die()
            self.machine.act_run([tart, "stop", v])
            r = self.machine.act_run([tart, "delete", v])
            for pid in self.runners(v):   # `tart delete` leaves the `tart run` alive, holding a VM slot
                self.machine.kill(pid)
            left = " ".join(str(p) for p in self.runners(v))
            if left:
                act.warn("a 'tart run' for '%s' is still alive (pid %s) and holds a\n    VM slot the next guest needs:  kill -9 %s" % (v, left, left))
            show(r)
            if not r.ok:
                act.die("tart delete %s failed (exit %d); what it said is above" % (v, r.rc), r.rc)
            act.info("deleted VM %s" % v)
        if self.machine.isdir(ws_dir):
            self.machine.remove(ws_dir)
            act.info("removed %s" % ws_dir)
        for f in (ws + ".run.log", ws + ".unfiltered"):
            self.machine.remove(os.path.join(self.vm_dir(), f))


class LocalWorkspace(Target):
    kind = "local"
    needs_base = False

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self._store = Store(dict(env, WK_STORE=env.get("WK_LOCAL_STORE") or Store(env).state_dir()))
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

    def mirror_dir(self):
        if self.os() == "macos":
            return GUEST_MIRROR
        return self.env.get("WK_MIRROR") or self.store.mirror()

    def arch(self, ws):
        return self.ws_arch

    def list(self):
        return [(self.ws_name, "running")]

    def info(self, ws):
        return "running" if ws == self.ws_name else "absent"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(self.exec_argv(ws, argv)[0], timeout=timeout)

    def exec_argv(self, ws, argv, tty=False):
        return ["bash", "-lc", "exec " + " ".join(shlex.quote(a) for a in argv)], None

    def store_init(self):
        self.machine.mkdir(self.store.ws_dir(self.ws_name))

    def create(self, ws, base=None, arch="native"):
        act.die("a workspace cannot create a workspace -- run 'wk new %s' on the host" % ws)

    def destroy(self, ws):
        act.die("a workspace cannot destroy itself -- run 'wk rm %s' on the host" % self.ws_name)

    def enter_argv(self, ws):
        act.die("already inside workspace '%s'" % self.ws_name)

    def ssh_host(self, ws):
        act.die("a workspace has no ssh route to itself")

    def build_size(self, ws):
        """This workspace's own cgroup limits, else the machine's."""
        res = Resources(self.machine, self.env)
        cores = mem = None
        try:
            quota, period = (self.machine.read("/sys/fs/cgroup/cpu.max").split() + [""])[:2]
            if quota != "max" and period.isdigit():
                cores = int(quota) // int(period)
        except OSError:
            pass
        try:
            limit = self.machine.read("/sys/fs/cgroup/memory.max").strip()
            if limit.isdigit():
                mem = int(limit) // 1024 // 1024
        except OSError:
            pass
        return cores or res.host_cores(), mem or res.host_mem_mb(), None


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
        self._store = Store(dict(env, WK_STORE=store))
        self.probe_seconds = int(env.get("WK_PROBE_SECONDS") or 20)
        self.here = machine
        if not self.is_local and self.host:
            self.machine = Ssh(self.host, opts=self.ssh_opts(), timeout=int(env.get("WK_SSH_TIMEOUT") or 10), via=machine)
        self._probed = None
        self._has_wk = None
        self._peer_rows = None
        self._routes = {}
        self._reference = None

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

    def _sh_act(self, text):
        return self._far().act_run(["sh", "-c", text])

    def label(self):
        return self.host or self.name

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

    def build_size(self, ws):
        return self.cores(), self.mem_mb(), self.load()

    def ccache_dir(self, ws):
        return self.root_there() + "/cache/ccache"

    def build_argv(self, ws, argv):
        """lockrun: a lock descriptor would be inherited by what the build leaves behind."""
        prio = "nice -n 19" + (" ionice -c3" if self._probe_or_die().get("ionice") == "yes" else "")
        tee = "" if self.is_local else " 2>&1 | tee %s" % shlex.quote(self.ws_dir_there(ws) + "/build.log")
        text = "set -o pipefail\ncd %s && %s remote-build -w 3600 -- %s %s%s" % (
            shlex.quote(self.src(ws)), shlex.quote(self.tools(ws) + "/lib/lockrun.sh"), prio,
            " ".join(shlex.quote(a) for a in argv), tee)
        if self.is_local:
            return ["bash", "-c", text], None
        return ["ssh"] + self._far().opts + [self.host, "bash -c " + shlex.quote(text)], None

    def task_put(self, ws, task):
        """`wk status` asks the machine that builds, so the record goes there with its own log and name."""
        if self.is_local:
            return
        far = "%s/task/%s" % (self.root_there(), task.id)
        new = far + ".new"
        q = shlex.quote
        lines = ["rm -rf %s && mkdir -p %s/steps" % (q(new), q(new))]
        for p in sorted(task.path.rglob("*")):
            if p.is_file() and ".tmp." not in p.name:
                lines.append("printf '%%s' %s > %s" % (q(p.read_text()), q(new + "/" + str(p.relative_to(task.path)))))
        lines.append("printf '%%s\\n' %s > %s" % (q(self.ws_dir_there(ws) + "/build.log"), q(new + "/log")))
        lines.append("printf '%%s\\n' %s > %s" % (q(self.host), q(new + "/machine")))
        lines.append("rm -rf %s && mv %s %s" % (q(far), q(new), q(far)))
        if not self._sh(" &&\n".join(lines)).ok:
            act.warn("could not record '%s's build state on %s -- 'wk status %s'\n    may show stale information until it answers again"
                     % (ws, self.host, ws))

    def src(self, ws):
        if self.peer and ws:
            return self._peer_route(ws)[1]
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

    def enter_argv(self, ws):
        self._probe_or_die()
        if self.is_local:
            return ([os.environ.get("SHELL", "/bin/sh"), "-l"], self.src(ws))
        return (["ssh", "-t"] + self.machine.opts + [self.host, "cd %s && exec $SHELL -l" % shlex.quote(self.src(ws))], None)

    def exec_argv(self, ws, argv, tty=False):
        if self.is_local:
            return list(argv), self.src(ws)
        line = "cd %s && %s" % (shlex.quote(self.src(ws)), " ".join(shlex.quote(a) for a in argv))
        return ["ssh"] + (["-t"] if tty else []) + self._far().opts + [self.host, line], None

    def exec_tty(self, ws, argv, timeout=None):
        """`exec_argv` already resolves to a literal command (its own `ssh` when not local) -- run
        by `self.here`, never `self.machine`, which would double-wrap it onto a non-local target."""
        cmd, cwd = self.exec_argv(ws, argv, tty=True)
        return self.here.run_tty(cmd, cwd=cwd, timeout=timeout)

    def pull(self, ws, src, dest):
        self.machine.copy_out(src, dest)

    def push(self, ws, src, dest):
        self.machine.copy_in(src, dest)

    def pull_dir(self, ws, src, dest):
        self.machine.copy_tree_out(src, dest)

    def push_dir(self, ws, src, dest):
        self.machine.copy_tree_in(src, dest)

    def path_kind(self, ws, path):
        r = self.machine.run(["sh", "-c", path_kind_probe(path)])
        return path_kind_result(r)

    def ssh_host(self, ws):
        """The configured destination, not a generated alias (which could not carry a ProxyJump)."""
        if self.is_local:
            return None
        self._far()
        if self.peer and ws:
            return "wk-%s" % ws
        return self.host

    def ssh_prepare(self, ws):
        if not (self.peer and ws):
            return
        user, src, proxy = self._peer_route(ws)
        if not proxy:
            act.die("'%s' on %s is reached at an address on that machine's\n"
                    "    own network, which is not this one's. Open it from %s:\n"
                    "        ssh %s wk zed %s" % (ws, self.host, self.host, self.host, ws))
        sshalias.alias_set(self.here, self.env, ws, "wk-%s.%s.invalid" % (ws, self.name), user,
                           identity=zed_key_path(self.env), extra=("ProxyCommand ssh %s %s" % (self.host, proxy),))

    def _peer_route(self, ws):
        """(user, src, proxy) an addressless peer workspace answers with, over `wk zed --route`, asked once."""
        if ws in self._routes:
            return self._routes[ws]
        env = dict(self.env, WK_ZED_PUBKEY=zed_key_pub(self.here, self.env) or "")
        rc, out = self.wk("zed", ws, "--route", env=env, quiet=True)
        if rc != 0:
            act.die("%s could not open a route into '%s'; what it said is above.\n"
                    "    A copy of wk-tools that has never heard of 'wk zed --route' says so as a usage\n"
                    "    error -- that one is fixed by bringing the machine up to date:  wk sync --tools" % (self.host, ws))
        kv = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)
        if not kv.get("user") or not kv.get("src"):
            act.die("%s said nothing an editor can use about '%s'" % (self.host, ws))
        self._routes[ws] = (kv["user"], kv["src"], kv.get("proxy", ""))
        return self._routes[ws]

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

    def store_init(self):
        self.here.mkdir(self.store.root())
        self.here.mkdir(os.path.join(self.store.root(), "ws"))

    def reference(self):
        """A shared WebKit checkout this machine's admins keep (named in the conf, or by its MOTD), verified to hold main."""
        if self._reference is None:
            ref = self.env.get("WK_REMOTE_REFERENCE", "")
            if not ref:
                r = self._sh(MOTD_REFERENCE)
                ref = r.out.strip() if r.ok else ""
            self._reference = ref
        return self._reference

    def mirror_dir(self):
        return "" if self.reference() else self.root_there() + "/mirror"

    def wiring_args(self):
        ssh_config = self.root_there() + "/ssh/config"
        if self.reference():
            return "shared", self.reference(), ssh_config
        return "mirror", self.mirror_dir(), ssh_config

    def _wire(self, src):
        n, u, c = self.wiring_args()
        if not self._sh_act(shell.wiring_script(self.root, src, self.mirror_dir(), n, u, c, env=self.env)).ok:
            act.warn("could not wire the remotes in %s" % src)

    def _mirror_update(self, root):
        act.info("updating the WebKit mirror on %s (first run clones it)" % self.label())
        r = self._sh_act("set -e\n mkdir -p %s %s\n %s" % (shlex.quote(root + "/ws"), shlex.quote(root + "/cache/ccache"),
                                                          shell.mirror_refresh_script(self.root, self.mirror_dir(), env=self.env)))
        for line in r.out.splitlines():
            f = line.split()
            if len(f) == 3 and f[0] == "mirror-fetch":
                act.log("  %-8s %s" % (f[1], f[2]))
        if not r.ok:
            act.die("could not update the WebKit mirror on %s" % self.label())

    def sync(self, named=False):
        """A peer pulls, and publishes its own snapshot only once it matches this checkout and was named."""
        tools, host = self.tools(""), self.label()
        if self.peer:
            r = self._sh_act("cd %s && git pull --ff-only" % shlex.quote(tools))
            show(r)
            if not r.ok:
                sys.stderr.write("  %-24s git pull --ff-only failed there\n" % self.name)
                return False
            mine = _kv(self.here.run(["env", "WK_ROOT=" + self.root, os.path.join(self.root, "cmd", "version")]).out)
            theirs = _kv(self._sh(shlex.quote(tools + "/cmd/version")).out)
            if not mine.get("sha") or (mine.get("sha"), mine.get("dirty")) != (theirs.get("sha"), theirs.get("dirty")):
                sys.stderr.write("  %-24s pulled, still DIFFERS (%s, this machine has %s)\n  %-24s %s\n"
                                 % (self.name, _ident(theirs), _ident(mine), "", tools_why_behind(self.here, self.root)))
                return False
            sys.stderr.write("  %-24s pulled, in sync\n" % self.name)
            if not named:
                act.info("%s keeps a store of its own -- its mirror and snapshot untouched" % host)
                act.log("  name it for those:  wk sync --tools %s" % self.name)
                return True
            act.info("running 'wk sync --tools' on %s -- its mirror, its snapshot" % host)
            rc, out = self.wk("sync", "--tools", env=dict(self.env, WK_NO_DELEGATE="1"))
            sys.stderr.write(out)
            return rc == 0
        ok = shell.sync_tools(self.root, self.here, self.name, "")
        if ok:
            sys.stderr.write("  %-24s pushed %s\n" % (self.name, self.here.run(["git", "-C", self.root, "rev-parse", "HEAD"]).out.strip()))
        if self.reference():
            act.info("workspaces here clone from %s, which this machine's admins keep up to date" % self.reference())
            act.log("  nothing of ours to fetch: no mirror is kept on %s" % host)
            return ok
        self._mirror_update(self.root_there())
        act.info("the WebKit mirror on %s is up to date" % host)
        return ok

    def create(self, ws, base=None, arch="native"):
        if self.peer:
            act.die("'%s' is a workstation, not a build machine for this one.\n    Its workspaces are its own -- containers or guests, from its own store --\n"
                    "    and this driver would make a plain checkout under ~/wk instead. Create it\n    there:  ssh %s wk new %s" % (self.label(), self.label(), ws))
        self._probe_or_die()
        root, wsd, host = self.root_there(), self.ws_dir_there(ws), self.label()
        st = self.info(ws)
        if st == "creating":
            act.die("'%s' on %s is a checkout that never finished being\n    made, and destroying it did not take. Remove it by hand and try again:\n"
                    "        ssh %s rm -rf %s" % (ws, host, host, shlex.quote(wsd)))
        if st == "unreachable":
            act.die("cannot reach %s to create '%s'" % (host, ws))
        if st != "absent":
            act.die("workspace '%s' already exists on %s" % (ws, host))
        ref = self.reference()
        if ref:
            act.info("cloning from %s (this machine's shared WebKit, hardlinked)" % ref)
            r = self._sh_act("set -e\n mkdir -p %s %s\n git clone --quiet -b main %s %s"
                             % (shlex.quote(root + "/ws"), shlex.quote(root + "/cache/ccache"), shlex.quote(ref), shlex.quote(wsd + "/WebKit")))
            if not r.ok:
                act.die("could not clone %s on %s" % (ref, host))
        else:
            self._mirror_update(root)
            r = self._sh_act("git clone --quiet --shared -b main %s %s" % (shlex.quote(root + "/mirror"), shlex.quote(wsd + "/WebKit")))
            if not r.ok:
                act.die("could not create the checkout on %s" % host)
        self._wire(wsd + "/WebKit")
        conf = shlex.quote(root + "/cache/ccache/ccache.conf")
        self._sh_act("[ -f %s ] || printf %%s %s > %s" % (conf, shlex.quote(shell.ccache_conf(self.root, self.env)), conf))
        self.here.mkdir(self.store.ws_dir(ws))
        if not self._sh_act("touch %s" % shlex.quote(wsd + "/" + READY_MARKER)).ok:   # last: an ssh cut mid-clone leaves it creating
            act.die("could not mark '%s' ready on %s -- treat it as half-made\n    and re-run 'wk new %s --target %s'" % (ws, host, ws, self.name))
        act.info("remote workspace '%s' created on %s (%s)" % (ws, host, wsd))

    def destroy(self, ws):
        """The record here outlives anything the far side has not confirmed gone: a re-run finds it and retries."""
        host = self.label()
        if self.peer:
            r = self._sh_act(self.wk_cmd(("rm", ws), dict(self.env, WK_YES="1")) + " 2>&1")
            show(r)
            if not r.ok:
                act.die("%s did not destroy '%s'; what its own wk said is above.\n    Nothing here was changed -- re-run 'wk rm %s' once that is settled." % (host, ws, ws))
            self.here.remove(self.store.ws_dir(ws))
            self._peer_rows = None   # the listing read before the removal is what the read-back must not see
            act.info("'%s' destroyed on %s, by that machine's own wk" % (ws, host))
            return
        self._probe_or_die()
        wsd = self.ws_dir_there(ws)
        r = self._sh_act("rm -rf %s" % shlex.quote(wsd))
        show(r)
        if not r.ok:
            act.die("could not remove %s on %s; what ssh said is above.\n    The record of '%s' here is kept -- re-run 'wk rm %s' once it answers." % (wsd, host, ws, ws))
        self.here.remove(self.store.ws_dir(ws))
        act.info("removed remote workspace '%s' from %s" % (ws, host))


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


def _kv(text):
    return dict(line.partition("=")[::2] for line in text.splitlines() if "=" in line)


def _ident(ver):
    return (ver.get("sha") or "")[:12] + ("+dirty" if ver.get("dirty") == "yes" else "")


def tools_why_behind(machine, root):
    """Why a peer pulling from this checkout's upstream does not arrive at its commit, or ""."""
    def git(*args):
        r = machine.run(["git", "-C", root] + list(args))
        return r.out.strip() if r.ok else None
    if git("rev-parse", "--git-dir") is None:
        return "this copy of wk-tools is not a git checkout, so nothing can pull from it"
    up = git("rev-parse", "--abbrev-ref", "@{upstream}") or ""
    if git("status", "--porcelain", "--untracked-files=no"):
        return "this machine has uncommitted changes -- a peer pulls from %s, so commit and push them first" % (up or "the upstream")
    if not up:
        return "branch '%s' has no upstream here, so there is nothing for a peer to pull from" % (git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD")
    ahead = _int(git("rev-list", "--count", up + "..HEAD"))
    return "this machine is %d commit(s) ahead of %s -- push them, then re-run" % (ahead, up) if ahead else ""


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
