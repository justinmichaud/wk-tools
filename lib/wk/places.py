"""The places: where a workspace lives and how it is driven. A `Registry`
names them (container, vm on a macOS host, this machine inside a workspace,
and every build machine and peer in `machines/`); each driver answers the same
contract over a `Machine`."""

import argparse
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import sys

from wk import act, agents, fleet, git, guest, images, kv, presets, reach, record, secrets, sshalias, tools
from wk.machine import TIMED_OUT, Local, PodmanVm, Result, Ssh, TartExec, isolated_module, lib_argv
from wk.resources import Resources, workspace_marker_path
from wk.store import Store, dispatch_place, in_vm, no_such_workspace

BUILTIN = ("container", "vm", "remote", "local")
SDK_REPO = "ghcr.io/igalia/wkdev-sdk"
READY_MARKER = ".wk-ready"
FIRSTRUN_MARKER = ".wk-firstrun-complete"   # TODO: drop once no pre-marker workspace is left
STATES_NOT_THERE = ("absent", "creating", "broken", "unreachable")
READY_TIMEOUT = 300
WK_FLAGS = ("WK_DEBUG", "WK_QUIET", "WK_YES", "WK_FORCE", "WK_DRY_RUN", "WK_NO_DELEGATE", "WK_EXPORTS_READ")
WK_CARRIED = ("WK_ROW_LABEL", "WK_ZED_PUBKEY", "WK_SDK_IMAGE", "WK_NEW_TIMEOUT", "WK_READY_TIMEOUT")
GUEST_SHARES = "/Volumes/My Shared Files"
MIRROR_TAG = "wk-mirror"
GUEST_MIRROR_MOUNT = "/Volumes/" + MIRROR_TAG
GUEST_MIRROR = GUEST_MIRROR_MOUNT + "/mirror/WebKit.git"
GUEST_MOUNT_MIRROR = "/usr/local/libexec/wk-mount-mirror"
TOOLS = "/opt/wk-tools"
BRIDGE = TOOLS + "/container/proxy/ensure-bridge.sh"
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
            if not os.path.isfile(conf):
                return None
            return kv.conf_file(conf).get("CFG_RELEASE") or None
    return None


def git_base(driver, ws):
    if driver.info(ws) in STATES_NOT_THERE:
        return None
    r = driver.exec(ws, ["sh", "-c", "cd %s 2>/dev/null || exit 0\n%s" % (shlex.quote(driver.src(ws)), UPSTREAM_LINE)])
    out = r.out.replace("\r", "").strip().splitlines()
    return out[-1] if r.ok and out else None


def default_root(home):
    return home.rstrip("/") + "/wk"


def remote_marker_path(env):
    return env.get("WK_REMOTE_MARKER") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".wk-remote")


def session_socket_path(env):
    return os.path.join(env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "wk", "display", "wayland-0")


def session_socket_present(env):
    try:
        return stat.S_ISSOCK(os.stat(session_socket_path(env)).st_mode)
    except OSError:
        return False


def zed_cli(machine):
    """The `zed` cli to exec into: on PATH, or the binary a drag-installed Zed.app carries with no PATH symlink."""
    if machine.have("zed"):
        return "zed"
    app = "/Applications/Zed.app/Contents/MacOS/cli"
    return app if machine.run(["test", "-x", app]).ok else None


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
        if act.dry_run():
            return "<the zed key this run would generate>"
        act.info("generated this machine's zed key (%s)" % k)
    try:
        return machine.read(k + ".pub").strip()
    except OSError:
        return None


# A place conf's keys and the WK_* variable each sets; a conf value wins over the environment's.
CONF_ENV = {"driver": "WK_DRIVER", "host": "WK_REMOTE_HOST", "hostname": "WK_REMOTE_HOSTNAME", "root": "WK_REMOTE_ROOT",
            "reference": "WK_REMOTE_REFERENCE", "local": "WK_REMOTE_LOCAL", "peer": "WK_REMOTE_PEER", "tools": "WK_REMOTE_TOOLS",
            "store": "WK_REMOTE_STORE", "cmake": "WK_REMOTE_CMAKE", "libcxx": "WK_REMOTE_LIBCXX", "wpe": "WK_REMOTE_WPE",
            "build_args": "WK_BUILD_ARGS"}
PER_PRESET = ("cmake", "build_args")   # also `<key>_<preset>`, the preset's dashes as underscores: one build preset's own


def conf_key(k):
    if k in CONF_ENV:
        return CONF_ENV[k]
    for stem in PER_PRESET:
        preset_name = k[len(stem) + 1:]
        if k.startswith(stem + "_") and preset_name.replace("_", "-") in presets.PRESETS:
            return "%s_%s" % (CONF_ENV[stem], preset_name)
    return None


def conf_env(conf, path):
    for k in sorted(conf):
        if k != "kind" and conf_key(k) is None:
            raise LookupError("%s: %s is not a key a build machine's conf takes (%s, or %s_<preset>)"
                              % (path, k, ", ".join(CONF_ENV), "|".join(PER_PRESET)))
    return {conf_key(k): v for k, v in conf.items() if k != "kind"}


class Registry:
    def __init__(self, root, env=None, machine=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.machine = machine or Local()
        self.store = Store(self.env)
        self.fleet = fleet.Fleet(self.root, self.env)

    def conf_path(self, name):
        return self.fleet.path(name)

    def known(self):
        return self.fleet.names(fleet.PLACE_KINDS)

    def _conf(self, name):
        try:
            conf = self.fleet.load(name)
        except fleet.ConfError as e:
            raise LookupError(str(e))
        return conf if conf and conf["kind"] in fleet.PLACE_KINDS else None

    def kind(self, name):
        if name in BUILTIN:
            return name
        try:
            conf = self._conf(name)
        except LookupError:
            return None
        return None if conf is None else conf.get("driver") or "remote"

    def marker_path(self):
        return workspace_marker_path(self.env)

    def in_workspace(self):
        return os.path.isfile(self.marker_path())

    def workspace_name(self):
        return kv.kv_file(self.marker_path()).get("name", "")

    def remote_marker_path(self):
        return remote_marker_path(self.env)

    def in_remote_host(self):
        return os.path.isfile(self.remote_marker_path())

    def far_root(self):
        """This far end's root: its own conf's, since two machines of one home share the marker and nothing machine-specific is in it."""
        conf = self.fleet.load(self.self_place()) or {}
        return conf.get("root") or default_root(self.env.get("HOME", os.path.expanduser("~")))

    def self_place(self):
        if not self.in_remote_host():
            return ""
        return self.fleet.named_by_host(record.host_name(self.machine))

    def default(self):
        if self.in_workspace():
            return "local"
        return self.self_place() or "container"

    def vm_listed(self):
        return self.store.vm_store_apart()

    def all(self):
        out = ["container"]
        if self.vm_listed():
            out.append("vm")
        t = self.self_place()
        if t:
            out.append(t)
        # Skipped on the far end of a place: a delegated listing would pay an ssh timeout per machine it has no route to.
        if self.in_remote_host() or in_vm(self.env):
            return out
        me = record.machine_name(self.env)
        for name in self.known():
            if name not in out and name.lower() != me:
                out.append(name)
        return out

    def machines(self):
        return [t for t in self.all() if t not in ("container", "vm", "local")]

    def is_here(self, name):
        """A built-in, or a machine's conf naming this one (`local=1`): asked without ssh."""
        if name not in self.machines():
            return True
        try:
            return getattr(self.load(name), "is_local", False)
        except LookupError:
            return False

    def here(self):
        return [t for t in self.all() if self.is_here(t)]

    def peer_workstations(self):
        """The peers whose own wk answers, each asked once."""
        out = []
        for name in self.machines():
            try:
                t = self.load(name)
            except LookupError:
                continue
            if getattr(t, "peer", False) and t.has_wk():
                out.append(name)
        return out

    def walk(self):
        """The places a listing covers: WK_PLACE's, this workspace's, the ones here when another wk asked, else all."""
        if dispatch_place(self.env):
            return dispatch_place(self.env).split()
        if self.in_workspace():
            return [self.default()]
        if self.env.get("WK_NO_DELEGATE"):
            return self.here()
        return self.all()

    def on_place(self, name, ws):
        """Whether `ws` is on `name`: its directory, its environment, or a creation still running."""
        try:
            t = self.load(name)
        except LookupError:
            return False
        return self._holds(t, ws)

    def _holds(self, t, ws):
        if t.store_machine.isdir(t.store.ws_dir(ws)):
            return True
        if t.info(ws) not in ("absent", "unreachable", ""):
            return True
        return t.creating_now(ws)

    def _asked(self, name, ws):
        """A machine's answer for `ws`, its one probe paid here; one that does not answer is named, since what is there is not in the answer."""
        try:
            t = self.load(name)
        except LookupError:
            return False
        if t.store_machine.isdir(t.store.ws_dir(ws)):
            return True
        ok, why = t.answers()
        if not ok:
            act.warn("could not ask %s over ssh: %s -- what is there is not in this answer" % (name, why))
            return False
        return self._holds(t, ws)

    def locate(self, ws):
        """Every place that answers for `ws`; the machines are asked at once."""
        here = self.here()
        hits = [t for t in here if self.on_place(t, ws)]
        far = [t for t in self.all() if t not in here]
        if hits or not far:
            return hits
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(far)) as pool:
            answers = list(pool.map(lambda m: (m, self._asked(m, ws)), far))
        return [m for m, hit in answers if hit]

    def exists_on(self, t, ws):
        """Whether `ws` is on the loaded place `t`, where a machine that did not answer is not an absence."""
        return self._holds(t, ws) or t.info(ws) == "unreachable"

    def local_workspaces(self):
        seen = []
        for kind in ["container"] + (["vm"] if self.vm_listed() else []):
            try:
                store = self.load(kind).store
            except (LookupError, act.Refused):
                continue
            if store.is_local():
                seen += [n for n in store.workspaces() if n not in seen]
        return seen

    def ws_place(self, ws):
        """The one place holding `ws`; the default when none does."""
        if dispatch_place(self.env):
            return dispatch_place(self.env)
        hits = self.locate(ws)
        if not hits:
            return self.default()
        if len(hits) == 1:
            return hits[0]
        raise LookupError("workspace '%s' exists on places: %s -- this cannot be\n    resolved; remove one, or set WK_PLACE"
                          % (ws, " ".join(hits)))

    def load(self, name):
        kind = self.kind(name)
        if kind is None:
            self._conf(name)   # a conf that does not parse says why
            names = " ".join(self.known())
            raise LookupError(
                "unknown place '%s'.\n    The built-in ones are container, vm, remote and local.%s\n\n"
                "    Anything else is a machine, and needs a conf -- in the registry, so every\n"
                "    device gets it:\n\n        %s\n            kind=build\n            host=%s      # an ssh destination that already works\n"
                "            root=/home/you/wk\n\n    'wk machine setup %s' writes it for you."
                % (name, ("\n    The machines here: " + names) if names else "", self.conf_path(name), name, name))
        env = dict(self.env)
        if name not in BUILTIN:
            env.update(conf_env(self._conf(name), self.conf_path(name)))
        if kind == "container":
            return Container(name, self.root, env, self.machine)
        if kind == "vm":
            return Vm(name, self.root, env, self.machine)
        if kind == "local":
            return LocalWorkspace(name, self.root, env, self.machine)
        return Remote(name, self.root, env, self.machine)

    def default_preset(self, name):
        """The last build's preset, from its task record; else the place's own platform default."""
        driver = self.load(self.ws_place(name))
        rec = record.Records(driver.store.records_dir(), env=driver.env).find("build", name)
        preset_name = rec.field("preset") if rec else ""
        if preset_name:
            act.info("preset: %s -- what '%s' was last built with" % (preset_name, name))
            return preset_name
        return "mac-release" if driver.os() == "macos" else "jsc-release"


def path_kind_probe(path):
    """The one `path_kind` shell test; Container, Vm and Remote each run it where the workspace lives."""
    return "if [ -d %s ]; then echo dir; elif [ -e %s ]; then echo file; else echo absent; fi" \
        % ((shlex.quote(path),) * 2)


def path_kind_result(r):
    out = r.out.replace("\r", "").strip()
    return out if out in ("dir", "file", "absent") else ""


class Driver:
    """The contract. `info` answers absent | creating | unreachable | the driver's own word for one that exists."""

    kind = "place"
    needs_base = True
    dir_first = False   # the workspace directory is made before the environment, so an environment without one is no creation
    reads_host_mirror = False

    def __init__(self, name, root, env, machine):
        self.name = name
        self.root = root
        self.env = env
        self.machine = machine
        self.here = machine
        self._store = Store(env)

    @property
    def store(self):
        return self._store

    @property
    def store_machine(self):
        return self.machine

    def records(self, clock=None):
        """The records this machine holds for the place: every driver writes its own, here."""
        return record.of_driver(self, clock, self.here)

    def creating_now(self, ws):
        t = self.records().find("new", ws)
        return bool(t and t.alive(None))

    def creation_finished(self, ws):
        t = self.records().find("new", ws)
        return bool(t and t.field("exit") == "0")

    def src(self, ws):
        return "/src/WebKit"

    def tools(self, ws):
        return TOOLS

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
        return self.machine.run(self.exec_argv(ws, argv, tty)[0], timeout=timeout)

    def pid_alive(self, ws, pid, cap=None):
        """True, False or None (no answer within cap seconds) for a pid inside the workspace -- the one "is it alive in the place" answer."""
        r = self.exec(ws, ["kill", "-0", str(pid)], timeout=cap)
        return True if r.rc == 0 else False if r.rc == 1 else None

    def far_side(self):
        return "none"

    def egress_filtered(self, ws):
        return False

    def agent_sock(self):
        return None

    def _agent_secret(self, secret):
        return next(r for r in secrets.agent_secrets() if r[0] == secret)

    def _agent_secret_file(self, secret):
        """Where the workspace holds it, as login-shell text: the rc names CLAUDE_SECURESTORAGE_CONFIG_DIR, where a `file` row's own tool rewrites it."""
        row = self._agent_secret(secret)
        if row[4] == "file":
            return '"$CLAUDE_SECURESTORAGE_CONFIG_DIR/%s"' % row[1]
        return '"$HOME/%s"' % row[2]

    def install_agents(self, ws):
        agents.install(self.root, self.env, self.here, lambda argv: self.act_exec(ws, argv), ws, self.tools(ws), self.src(ws))

    def agent_secret_present(self, ws, secret):
        return self.exec(ws, ["bash", "-lc", "test -s %s" % self._agent_secret_file(secret)]).ok

    def agent_secret_remedy(self, ws, secret):
        return secrets.Secrets(self.root, self.env, self.machine).agent_secret_remedy(secret)

    def is_here(self):
        """Whether the machine behind this place is the one running this process."""
        return True

    def answers(self):
        """(whether the machine behind this place answers, why not)."""
        return True, ""

    def probe(self):
        """(far side, why it does not answer)."""
        return self.far_side(), ""

    def has_wk(self):
        return False

    def wk(self, *args, env=None, quiet=False):
        """(status, output) of the far side's own wk."""
        return 1, ""

    def podman_machine(self):
        return self.store.podman_machine()

    def wk_cmd(self, args, env):
        """The far side's own wk as one shell line; flags travel as environment, since an older wk there refuses an unknown one."""
        pre, wk, env = self.wk_far(env)
        pre += "".join("%s=1 " % v for v in WK_FLAGS if env.get(v))
        pre += "".join("%s=%s " % (v, shlex.quote(env[v])) for v in WK_CARRIED if env.get(v))
        return "%s%s %s" % (pre, shlex.quote(wk), shlex.join(args))

    def far_wk_or_die(self, cmd):
        far = self.far_side()
        if far == "unreachable":
            act.die("'%s' acts on a workspace on %s, and %s did not answer.\n"
                    "    Nothing here can reach into it: the workspace is that machine's own." % (cmd, self.name, self.name))
        if far != "answering":
            act.die("'%s' acts on a workspace on %s, which has no wk-tools of its own to\n"
                    "    run it:  wk machine setup %s" % (cmd, self.name, self.name))

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
        """absent | creating | broken | present | unreachable: the record and the environment read together.
        Broken is one half gone -- the environment after creation finished, or the directory under a live environment."""
        env = self.info(ws) if info is None else info
        ws_dir, m = self.store.ws_dir(ws), self.store_machine
        if env == "unreachable":
            return env
        if env == "absent":
            if not m.isdir(ws_dir):
                return "absent"
            return "broken" if self.created(ws) or self.creation_finished(ws) else "creating"
        if self.dir_first and not m.isdir(ws_dir) and not self.creating_now(ws):
            return "broken"
        if env == "creating":
            return env
        if self.needs_base and not m.exists(os.path.join(ws_dir, "base-id")):
            return "creating"
        return "present"

    def remake_hint(self, ws):
        reg = Registry(self.root, self.env, self.machine)
        if reg.in_remote_host():
            return "from the workstation:  wk new %s --on %s" % (ws, reg.self_place())
        return "wk new %s --on %s" % (ws, self.name)

    def wait_ready(self, ws, clock, timeout=None):
        """Returns once `ws` is present, or dies naming why it never will be; a creation still running is waited for."""
        timeout = int(timeout or self.env.get("WK_READY_WAIT") or 1800)
        seen = {"said": False}

        def ready():
            st = self.state(ws)
            now = self.creating_now(ws) if st in ("present", "creating") else False
            if st == "present" and now:   # the marker is down at `init`, and the detached run holds the lock through the stages after it
                st = "creating"
            seen["st"] = st
            if st == "present":
                return True
            if st == "absent":
                act.die(no_such_workspace(ws))
            if st == "broken":
                act.die(self._broken_words(ws))
            if st == "unreachable":
                act.die("'%s' lives on a machine that did not answer (%ss).\n"
                        "    Nothing is wrong with the workspace as far as this end can tell -- it\n"
                        "    cannot be reached to ask. Try again, or check the route:\n"
                        "        ssh -o BatchMode=yes %s true"
                        % (ws, reach.ssh_timeout(self.env), getattr(self, "host", "") or "the machine"))
            if not now:
                act.barrier("'%s' was never finished creating, and nothing is creating it now\n"
                            "    (the process that was is gone, with whatever connection started it).\n"
                            "    Usually there is nothing in one worth keeping, so remake it:\n        %s\n"
                            "    --force uses it as it is, which is right when you can see that the\n"
                            "    checkout is complete and only the marker is missing." % (ws, self.remake_hint(ws)))
                return True
            if not seen["said"]:
                seen["said"] = True
                stage = " ".join(self.records().find("new", ws).stage())
                act.info("waiting for '%s' to finish being created%s" % (ws, " (at: %s)" % stage if stage else ""))
                act.log("  follow it:  tail -f %s" % self.create_log(ws))
                act.log("  this end can be killed; creation is detached and continues")
            return False

        if not clock.wait_until(ready, timeout, 2):
            act.die("'%s' was still %s after %ds.\n    Creation is detached, so it may still be going: 'wk status %s' says\n"
                    "    whether the detached run is alive, and %s says what it is doing." % (ws, seen["st"], timeout, ws, self.create_log(ws)))
        if seen["said"]:
            act.info("'%s' is ready" % ws)
        self.converge(ws, clock)

    def converge(self, ws, clock):
        """What a running `ws` needs from this machine's daemons, asserted by every command that waits for it."""

    def daemon_remedy(self, ws, daemon):
        if daemon == "proxy":
            return "systemctl --user status wk-proxy"
        return "systemctl --user status wk-github-inject; the CA is /run/wk/wk-github-ca.pem"

    def _broken_words(self, ws):
        if not self.store_machine.isdir(self.store.ws_dir(ws)):
            return ("'%s' is an environment with no workspace directory: nothing is creating it, and\n"
                    "    what it would run in is gone -- something outside wk removed it.\n"
                    "    Repair:  wk rm %s    (then 'wk new %s' if you still want it)" % (ws, ws, ws))
        return ("'%s' exists as a record and not as a %s workspace: creation\n"
                "    finished, and the environment is gone -- something outside wk removed it.\n"
                "    Repair:  wk rm %s    (then 'wk new %s' if you still want it)" % (ws, self.name, ws, ws))

    def sync_tools(self, ws):
        return True

    def display_state(self, ws):
        st = self.state(ws)
        return self.info(ws) if st == "present" else st

    def create_log(self, ws):
        return os.path.join(self.store.store_dir(), "log", "new-%s.log" % ws)

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
        seen = {}

        def settled():
            seen["st"] = self.info(ws)
            return seen["st"] != "creating"
        return clock.wait_until(settled, self.ready_timeout(timeout), 1) and seen["st"] not in ("absent", "unreachable")

    def destroy(self, ws):
        raise NotImplementedError

    def task_store(self):
        return None

    def results(self, ws):
        return self.store_machine, os.path.join(self.store.ws_dir(ws), "bench")

    def ccache_maxsize(self):
        return self.env.get("WK_CCACHE_MAXSIZE") or "40G"

    def ccache_conf(self):
        return "max_size = %s\n" % self.ccache_maxsize()

    def ensure_dir_mode(self, path, mode):
        self.machine.mkdir(path)
        if not self.machine.run(["find", path, "-maxdepth", "0", "-perm", mode]).out.strip():
            self.machine.act_run(["chmod", mode, path])

    def workspaces(self):
        return sorted(set(self.store.workspaces()) | {n for n, _ in self.list() if n})

    def act_exec(self, ws, argv):
        if act.dry_run():
            sys.stderr.write("would run in %s: %s\n" % (ws, shlex.join(argv)))
            return Result(0)
        return self.exec(ws, argv)

    def wiring_args(self):
        return "", "", ""

    def sync(self, named=False):
        """Refresh this place's furniture: its copy of the tooling, and what it keeps of its own."""
        return True

    def enter_argv(self, ws):
        """(argv, cwd) to `Machine.exec` into a login shell in `ws`; `cwd` is set only when the shell needs starting there rather than told to `cd`."""
        raise NotImplementedError

    def pull(self, ws, src, dest):
        self.machine.copy_out(src, dest)

    def pull_dir(self, ws, src, dest, exclude=()):
        self.machine.copy_tree_out(src, dest, exclude)

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
        """Point an editor at this place over ssh; nothing for one already an ssh destination."""

    def ssh_user(self, ws):
        """The account inside the workspace an editor logs into, or None."""
        return None

    def ssh_proxy(self, ws):
        """What to run here to reach an addressless workspace, or None."""
        return None

    def exec_argv(self, ws, argv, tty=False):
        raise NotImplementedError

    def exec_tty(self, ws, argv, timeout=None):
        """Blocking, this process's own stdio inherited -- a real pty for lldb/samply/xctrace -- control returns here, unlike `enter_argv`'s exec."""
        cmd, cwd = self.exec_argv(ws, argv, tty=True)
        return self.machine.run_tty(cmd, cwd=cwd, timeout=timeout)


    def ccache_dir(self, ws):
        return "/ccache"

    def build_argv(self, ws, argv):
        """(argv, cwd)."""
        return self.exec_argv(ws, argv)

    def build_size(self, ws):
        """(cores, mem_mb, load or None); a load makes the build polite."""
        res = Resources(self.machine, self.env)
        return res.envelope_cores(), res.envelope_mem_mb(), None


def show(r):
    """A captured command's output, into the log this driver writes."""
    sys.stderr.write(r.out + r.err)


TART_APP = ".local/share/tart/tart.app/Contents/MacOS/tart"


def tart_path(env):
    """On PATH, or in ~/.local/bin, which README.md's Setup links and ssh's PATH lacks; resolved into the bundle, whose entitlement it needs."""
    dirs = [d for d in env.get("PATH", "").split(os.pathsep) if d]
    found = shutil.which("tart", path=os.pathsep.join(dirs + [os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "bin")]))
    return os.path.realpath(found) if found else None


def arch_image(arch):
    return presets.IMAGE_ARMHF if arch == "armhf" else ""


def podman_vm(machine, name, timeout=None):
    """`podman machine inspect`'s record of `name` (State, Resources, SSHConfig), or None where podman knows no such machine."""
    r = machine.run(["podman", "machine", "inspect", name], timeout=timeout)
    if not r.ok:
        return None
    try:
        return json.loads(r.out)[0]
    except (ValueError, IndexError, KeyError) as e:
        act.die("'podman machine inspect %s' answered what this end cannot read (%s)" % (name, e))


def podman_vm_field(rec, path):
    v = rec
    for k in path.split("."):
        v = v.get(k, "") if isinstance(v, dict) else ""
    return v


def podman_vm_route(rec):
    """(opts, dest) for ssh straight into the machine `rec` describes: `podman machine ssh` carries no terminal and no socket forward."""
    c = rec.get("SSHConfig") or {}
    if not (c.get("Port") and c.get("IdentityPath") and c.get("RemoteUsername")):
        act.die("podman names no ssh port, key and user for its machine '%s', so nothing can reach it.\n"
                "    ./setup --stage machine makes it again" % rec.get("Name", "?"))
    return (["-p", str(c["Port"]), "-i", c["IdentityPath"], "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"], "%s@127.0.0.1" % c["RemoteUsername"])


class Container(Driver):
    kind = "container"
    dir_first = True
    reads_host_mirror = True

    def podman(self):
        if os.uname().sysname == "Darwin" and not in_vm(self.env):
            return ["podman", "-c", self.podman_machine()]
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def egress_filtered(self, ws):
        return True

    @property
    def store_machine(self):
        return self.machine if self.is_here() else PodmanVm(self.podman_machine(), via=self.machine)

    def task_store(self):
        return None if self.is_here() else (self.store_machine, self.store.store_dir())

    def agent_sock(self):
        return "/run/wk/ssh-agent.sock"

    def rootless(self):
        r = self.machine.run(self.podman() + ["info", "--format", "{{.Host.Security.Rootless}}"])
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
        return self.store.mirror_dir()

    def sdk(self):
        if in_vm(self.env):
            return self.env.get("WK_SDK") or "/opt/webkit-container-sdk"
        return self.env.get("WK_SDK") or os.path.join(
            self.env.get("XDG_DATA_HOME") or os.path.join(self.env.get("HOME", ""), ".local", "share"), "webkit-container-sdk")

    def sdk_env(self):
        """The environment every SDK script reads, and refuses to run without (`env` prefix for an argv)."""
        return ["env", "WKDEV_SDK=%s" % self.sdk(), "WKDEV_CONTAINER_UID=%d" % os.getuid(), "WKDEV_CONTAINER_GID=%d" % os.getgid(),
                "WKDEV_CONTAINER_USER=%s" % self.user(), "WKDEV_CONTAINER_SHELL=/bin/bash"]

    def sdk_local(self):
        """The pulled SDK image and its pull date, or None with none pulled; sdk_upstream's registry tags, or None past `timeout`."""
        r = self.machine.run(self.podman() + ["images", "--format", "{{.Repository}}:{{.Tag}}"])
        img = next((l for l in r.out.splitlines() if l.startswith(SDK_REPO + ":")), None) if r.ok else None
        if not img:
            return None
        c = self.machine.run(self.podman() + ["image", "inspect", img, "--format", "{{.Created}}"])
        return {"image": img, "created": c.out.strip()[:10] if c.ok else ""}

    def sdk_upstream(self, timeout=None):
        r = self.machine.run(self.podman() + ["search", "--list-tags", SDK_REPO, "--limit", "100"], timeout=timeout)
        if not r.ok:
            return None
        return [parts[1] for parts in (line.split() for line in r.out.splitlines()[1:]) if len(parts) > 1]

    def arch(self, ws):
        path = os.path.join(self.store.ws_dir(ws), "arch")
        try:
            return self.store_machine.read(path).strip() or "native"
        except OSError:
            return "native"

    def list(self):
        r = self.machine.run(self.podman() + ["ps", "-a", "--filter", "name=^wk-", "--format", "{{.Names}}\t{{.Status}}"])
        rows = []
        for line in r.out.splitlines():
            name, _, status = line.partition("\t")
            if name.startswith("wk-"):
                rows.append((name[3:], status))
        return rows

    def created(self, ws):
        home, m = os.path.join(self.store.ws_dir(ws), "home"), self.store_machine
        return m.exists(os.path.join(home, READY_MARKER)) or m.exists(os.path.join(home, FIRSTRUN_MARKER))

    def info(self, ws):
        r = self.machine.run(self.podman() + ["inspect", self.ctr(ws), "--format", "{{.State.Status}}"])
        st = r.out.strip() if r.ok else "absent"
        if not st or st == "absent":
            return "absent"
        return st if self.created(ws) else "creating"

    def branch(self, ws):
        head = os.path.join(self.store.ws_dir(ws), "changes", ".git", "HEAD")
        if not self.machine.exists(head):
            base = self.store.ws_snapshot_id(ws)
            if not base:
                return "-"
            head = os.path.join(self.store.snapshot_tree(base), ".git", "HEAD")
        try:
            ref = self.machine.read(head).strip()
        except OSError:
            return "-"
        if ref.startswith("ref: refs/heads/"):
            return ref[len("ref: refs/heads/"):]
        if ref.startswith("ref: "):
            return ref[len("ref: "):]
        return "detached %s" % ref[:10]

    def exec_argv(self, ws, argv, tty=False):
        if not self.is_here():
            env = {k: v for k, v in os.environ.items() if k != "WK_DRY_RUN"}
            return ["podman", "machine", "ssh", self.podman_machine(), "--", self.wk_cmd(["enter", ws, "--", *argv], env)], None
        cmd = self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-enter"), "--quiet", "--name", self.ctr(ws)]
        if not tty:
            cmd.append("--no-tty")
        return cmd + ["--exec", "--", BRIDGE, *argv], None

    def is_here(self):
        return in_vm(self.env) or os.uname().sysname != "Darwin"

    def machine_state(self):
        """running | stopped | absent | ...: podman's own word for the machine, asked once per Container."""
        if not hasattr(self, "_machine_state"):
            rec = podman_vm(self.machine, self.podman_machine())
            self._machine_state = (rec.get("State") or "absent") if rec else "absent"
        return self._machine_state

    def far_side(self):
        if self.is_here():
            return "none"
        return "answering" if self.machine_state() == "running" else "stopped"

    def has_wk(self):
        return self.far_side() == "answering"

    def wk_far(self, env):
        """The VM is part of this machine, so its records name this host as itself."""
        return "WK_IN_VM=1 WK_HOST_SELF=1 ", TOOLS + "/wk", dict(env, WK_ROW_LABEL=record.row_label(env) or record.machine_name(env, self.machine))

    def wk(self, *args, env=None, quiet=False):
        env = os.environ if env is None else env
        r = self.machine.run(["podman", "machine", "ssh", self.podman_machine(), "--", self.wk_cmd(args, env) + ("" if quiet else " 2>&1")], input="")
        return r.rc, r.out

    def start(self, ws):
        secrets.Secrets(self.root, self.env, self.machine).pat_converge_machine()
        r = self.machine.act_run(self.podman() + ["start", self.ctr(ws)])
        return r.ok

    def stop(self, ws):
        r = self.machine.act_run(self.podman() + ["stop", "--time", "30", self.ctr(ws)])
        return r.ok

    def tools_src(self):
        return self.env.get("WK_TOOLS_SRC") or (TOOLS if in_vm(self.env) else self.root)

    def sync(self, named=False):
        act.info("nothing to copy: a container bind-mounts this checkout (%s) at\n"
                 "  /opt/wk-tools, so the tooling in one is never stale. What the VM has\n"
                 "  installed rather than mounted -- the proxy and injector units, the skills,\n"
                 "  its packages -- comes from:  ./setup --stage vmtools" % self.root)
        return True

    def runtime_dir(self):
        return os.path.join(self.env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid(), "wk")

    def exists(self, ws):
        return self.machine.run(self.podman() + ["container", "exists", self.ctr(ws)]).ok

    def store_init(self):
        root = self.store.store_dir()
        for d in ("", "git", "base", "ws", "cache/ccache", "cache/yocto/downloads", "cache/yocto/sstate", "cache/buildroot/dl",
                  "cache/buildroot/ccache", "cache/bench", "skills"):
            self.machine.mkdir(os.path.join(root, d) if d else root)
        conf = os.path.join(root, "cache", "ccache", "ccache.conf")
        if not self.machine.exists(conf):
            self.machine.write(conf, self.ccache_conf())
        for d in (self.store.keyring_dir(), self.store.keyring_agent_rw_dir()):
            self.ensure_dir_mode(d, "0700")
        secrets.Secrets(self.root, self.env, self.machine).store_publish()

    def sdk_refresh(self):
        r = self.machine.act_run(["bash", os.path.join(self.root, "container", "sdk-refresh.sh"), self.sdk()], stream=True)
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
        if presets.arch_has_gpu(arch):
            r = self.machine.run(lib_argv(self.root, "host/linux/gpu.sh", "gpu_flags"))
            sys.stderr.write(r.err)
            flags += r.out.split() if r.ok else []
        return flags

    def create_flags(self, ws, base, arch):
        ws_dir, store, mirror = self.store.ws_dir(ws), self.store.store_dir(), self.store.mirror_dir()
        mirror_dir = os.path.dirname(mirror)
        res = Resources(self.machine, self.env, self.os())
        volumes = ["%s:%s:ro" % (self.tools_src(), TOOLS), "%s:%s:ro" % (mirror_dir, mirror_dir)]
        flags = ["--volume", volumes[0], "--volume", volumes[1], "--env", "WK_MIRROR=%s" % mirror,
                 "--volume", "%s:/src/WebKit:O,upperdir=%s/changes,workdir=%s/overlay-work" % (self.store.snapshot_tree(base), ws_dir, ws_dir),
                 "--volume", "%s/build:/src/WebKit/WebKitBuild" % ws_dir,
                 "--volume", "%s:/var/lib/wk/ws/%s" % (ws_dir, ws)]
        for sub, dest in (("cache/ccache", "/ccache"), ("cache/yocto", "/cache/yocto"), ("cache/buildroot", "/cache/buildroot"),
                          ("cache/bench", "/cache/bench"), ("skills", "/skills")):
            flags += ["--volume", "%s/%s:%s" % (store, sub, dest)]
        flags += ["--volume", "%s:/secrets:ro" % self.store.keyring_view_dir("container"),
                  "--volume", "%s/agent-rw:/agent-rw" % store,
                  "--memory", "%dm" % res.envelope_mem_mb(), "--cpus", str(res.envelope_cores())]
        for pair in ("CCACHE_DIR=/ccache", "CCACHE_MAXSIZE=%s" % self.ccache_maxsize(), "CCACHE_BASEDIR=/src/WebKit",
                   "CCACHE_SLOPPINESS=pch_defines,time_macros,include_file_mtime,include_file_ctime", "CCACHE_PCH_EXTSUM=true",
                   "CCACHE_DEPEND=true", "CCACHE_NOHASHDIR=true", "DL_DIR=/cache/yocto/downloads", "SSTATE_DIR=/cache/yocto/sstate",
                   "BR2_DL_DIR=/cache/buildroot/dl", "BR2_CCACHE_DIR=/cache/buildroot/ccache", "WK_WORKSPACE=%s" % ws,
                   "WK_ARCH=%s" % arch, "WKDEV_OFFLINE=1", "WK_LOCAL_STORE=/var/lib/wk"):
            flags += ["--env", pair]
        return flags + self.sandbox_flags(arch)

    def sdk_image(self):
        return self.env.get("WK_SDK_IMAGE")

    def check_sdk_tag(self):
        r = self.machine.run(self.sdk_env() + [os.path.join(self.sdk(), "scripts", "helpers", "print-sdk-version")])
        tag = r.out.strip() if r.ok else ""
        published = self.sdk_upstream() if tag else None   # a registry that cannot be asked is left to the pull's own failure
        if published is None or tag in published:
            return
        series = tag.split("-v")[0] + "-v"
        newest = max((t for t in published if re.fullmatch(re.escape(series) + r"\d+-[0-9a-f]+", t)),
                     key=lambda t: int(t[len(series):].split("-")[0]), default="<tag>")
        act.die("the SDK checkout asks for image %s:%s, which upstream has not published.\n"
                "    The newest published tag of that series is %s.\n"
                "    Use it:  WK_SDK_IMAGE=%s:%s wk new ..." % (SDK_REPO, tag, newest, SDK_REPO, newest))

    def create_argv(self, ws, base, arch):
        u = self.user()
        argv = self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-create"), "--network", "none", "--isolated"]
        if arch != "native":
            argv += ["--arch", "arm"]
        image = self.sdk_image() or arch_image(arch)
        if image:
            argv += ["--image", image]
        return argv + ["--name", self.ctr(ws), "--shell", "/bin/bash", "--user", u, "--group", u,
                       "--home", os.path.join(self.store.ws_dir(ws), "home"), "--additional-flags", " ".join(self.create_flags(ws, base, arch))]

    def create(self, ws, base=None, arch="native"):
        ws_dir = self.store.ws_dir(ws)
        if not self.machine.isdir(self.store.snapshot_tree(base)):
            act.die("snapshot %s not found; run 'wk sync' first" % base)
        if self.exists(ws):
            act.die("workspace '%s' already exists" % ws)
        if not (self.sdk_image() or arch_image(arch)):
            self.check_sdk_tag()
        for d in (ws_dir, "changes", "overlay-work", "home", "build"):
            self.machine.mkdir(d if d == ws_dir else os.path.join(ws_dir, d))
        self._ensure_home_mountpoint(ws_dir, os.path.dirname(self.store.mirror_dir()))
        self.machine.write(os.path.join(ws_dir, "arch"), arch + "\n")
        argv = self.create_argv(ws, base, arch)
        if self.sdk_image():
            act.info("using workspace image %s (WK_SDK_IMAGE)" % self.sdk_image())
        act.info("creating workspace '%s' from base %s (rootless-proxy, %s)" % (ws, base, arch))
        r = self.machine.act_run(argv, stream=True)
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
        if clock.wait_until(lambda: self.created(ws) or not self.exists(ws), self.ready_timeout(timeout), 1) and self.created(ws):
            return True
        act.warn("initialisation did not complete; last output from the container:")
        r = self.machine.run(self.podman() + ["logs", self.ctr(ws)])
        for line in [l for l in (r.out + r.err).splitlines() if l.strip()][-8:]:
            sys.stderr.write("    %s\n" % line)
        return False

    def destroy(self, ws):
        c, ws_dir = self.ctr(ws), self.store.ws_dir(ws)
        if self.exists(ws):
            self.machine.act_run(self.podman() + ["rm", "-f", c])
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
        r = self.machine.run(self.podman() + ["inspect", self.ctr(ws), "--format", "{{.Config.WorkingDir}}"])
        home = r.out.strip() if r.ok else ""
        if home.startswith("/home/") and len(home) > len("/home/"):
            return home[len("/home/"):]
        return None

    def enter_argv(self, ws):
        """Spelled out rather than wkdev-enter's own login shell: without the token/keyring
        bridge, `git-webkit pr` reports a locked macOS Keychain instead of a missing token."""
        return (self.sdk_env() + [os.path.join(self.sdk(), "scripts", "host-only", "wkdev-enter"),
                                  "--name", self.ctr(ws), "--exec", "--",
                                  BRIDGE,
                                  "/usr/bin/env", "USER=%s" % self.user(), "/bin/bash", "--login"], None)

    def _cp(self, src, dest):
        r = self.machine.act_run(self.podman() + ["cp", src, dest])
        if not r.ok:
            raise OSError(r.err.strip() or "podman cp failed")

    def pull(self, ws, src, dest):
        self._cp("%s:%s" % (self.ctr(ws), src), dest)

    def push(self, ws, src, dest):
        self._cp(src, "%s:%s" % (self.ctr(ws), dest))

    def pull_dir(self, ws, src, dest, exclude=()):
        if exclude:
            act.die("the container driver cannot exclude paths (%s): copy the whole tree, or make the selection "
                    "inside the workspace first" % " ".join(exclude))
        self.machine.remove(dest)
        self.machine.mkdir(dest)
        self._cp("%s:%s/." % (self.ctr(ws), src), dest)

    def push_dir(self, ws, src, dest):
        u = self._ctr_user(ws)
        if u is None:
            act.die("workspace '%s' has no container to reach (podman does not know it)" % ws)
        r = self.machine.act_run(self.podman() + ["exec", "--user", u, self.ctr(ws), "/bin/sh", "-c",
                                                    "rm -rf %s && mkdir -p %s" % (shlex.quote(dest), shlex.quote(dest))])
        if not r.ok:
            raise OSError(r.err.strip() or "could not clear %s" % dest)
        self._cp(src + "/.", "%s:%s" % (self.ctr(ws), dest))

    def path_kind(self, ws, path):
        u = self._ctr_user(ws)
        if u is None:
            act.die("workspace '%s' has no container to reach (podman does not know it)" % ws)
        r = self.machine.run(self.podman() + ["exec", "--user", u, self.ctr(ws), "/bin/sh", "-c", path_kind_probe(path)])
        return path_kind_result(r)

    def ssh_user(self, ws):
        return self._ctr_user(ws)

    def ssh_proxy(self, ws):
        return "%s container %s" % (os.path.join(self.root, "container", "ssh-transport"), ws)

    def sshd_cmd(self, u):
        h = "/home/" + u
        return ("mkdir -p /run/sshd && exec %s /bin/sh -c "
                "'exec /usr/sbin/sshd -i -e -f /dev/null -o HostKey=%s/.wk-ssh/ssh_host_ed25519_key "
                "-o AuthorizedKeysFile=.ssh/authorized_keys -o UsePAM=no -o PidFile=none -o PermitRootLogin=no "
                "-o AllowUsers=%s -o LogLevel=ERROR -o Subsystem=\"sftp internal-sftp\" -o SetEnv=\"$WK_SSH_SETENV\"'" % (BRIDGE, h, u))

    def ssh_transport(self, ws):
        u = self._ctr_user(ws)
        if u is None:
            act.die("workspace '%s' has no container to reach (podman does not know it)" % ws)
        os.execvp("podman", self.podman() + ["exec", "-i", self.ctr(ws), "/bin/sh", "-c", self.sshd_cmd(u)])

    def ssh_prepare(self, ws):
        """An sshd inside the container so Zed reaches it like every place, over the `Host wk-<ws>` alias."""
        c = self.ctr(ws)
        u = self._ctr_user(ws)
        if u is None:
            act.die("no container workspace called '%s' on this machine.\n"
                    "    'wk ls' lists the ones there are, and 'wk start' brings the podman machine up\n"
                    "    if it is stopped. (An editor reaches a container over podman from here: the\n"
                    "    workspace has no network interface, so there is no other route in.)" % ws)
        h = "/home/%s" % u
        if not self.machine.run(self.podman() + ["exec", c, "test", "-x", "/usr/sbin/sshd"]).ok:
            act.info("installing openssh-server in '%s' (once per workspace; Zed needs an sshd to talk to)" % ws)
            r = self.machine.act_run(self.podman() + ["exec", c, BRIDGE, "/bin/sh", "-c",
                                                        "apt-get update -qq && apt-get install -y -qq --no-install-recommends openssh-server"])
            show(r)
            if not r.ok or not self.machine.run(self.podman() + ["exec", c, "test", "-x", "/usr/sbin/sshd"]).ok:
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
        r = self.machine.act_run(self.podman() + ["exec", "--user", u, c, "/bin/sh", "-c", script])
        if not r.ok:
            act.die("could not prepare the ssh identity in '%s'" % ws)
        pub = zed_key_pub(self.machine, self.env)
        if pub is None:
            act.die("could not create this machine's zed key")
        if not self.machine.run(self.podman() + ["exec", "--user", u, c, "grep", "-qsF", pub, "%s/.ssh/authorized_keys" % h]).ok:
            r = self.machine.act_run(self.podman() + ["exec", "-i", "--user", u, c, "/bin/sh", "-c",
                                                        "cat >> '%s/.ssh/authorized_keys'" % h], input=pub + "\n")
            if not r.ok:
                act.die("could not authorise the editor's key in '%s'" % ws)
            act.info("authorised the editor's key in '%s'" % ws)
        sshalias.alias_set(self.machine, self.env, ws, "wk-%s.container.invalid" % ws, u,
                           identity=zed_key_path(self.env), extra=("ProxyCommand %s" % self.ssh_proxy(ws),))


class Vm(Driver):
    kind = "vm"
    needs_base = False
    reads_host_mirror = True
    agent_rw_share = "agent-rw"
    mirror_share = "mirror"
    mirror_tag = MIRROR_TAG

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self._vm_store = None

    def install_agents(self, ws):
        return None

    @property
    def store(self):
        if self._vm_store is None:
            if not self.vm_store_apart():
                act.die("the vm place has no store of its own on this machine -- set WK_VM_STORE apart from WK_STORE")
            self._vm_store = Store(dict(self.env, WK_STORE=Store(self.env).vm_store_dir()))
        return self._vm_store

    def user(self):
        return guest.vm_user(self.env)

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
        return self.cores(ws), self.mem_mb(ws), None

    def configured(self, v, key):
        r = self.machine.run([self.tart_or_die(), "get", v, "--format", "json"])
        try:
            got = json.loads(r.out).get(key) if r.ok else None
        except ValueError:
            got = None
        return int(got) if isinstance(got, (int, float)) or (isinstance(got, str) and got.isdigit()) else None

    def cores(self, ws):
        c = self.configured(self.vm(ws), "CPU")
        if c is None:
            c = guest.vm_cpus(self.env)
        return c if c is not None else Resources(self.machine, self.env, "macos").envelope_cores()

    def mem_mb(self, ws):
        m = self.configured(self.vm(ws), "Memory")
        if m is None:
            m = guest.vm_mem_mb(self.env)
        return m if m is not None else Resources(self.machine, self.env, "macos").envelope_mem_mb()

    def keyring_agent_rw_dir(self):
        return GUEST_SHARES + "/" + self.agent_rw_share

    def login_note(self):
        guest.login_note(self.env)

    def remount_mirror(self, ws):
        """A fresh mount re-reads the refs a refresh renamed over; the mirror has its own tag, so agent-rw is never touched."""
        r = self.act_exec(ws, ["sudo", "-n", GUEST_MOUNT_MIRROR, MIRROR_TAG, GUEST_MIRROR_MOUNT])
        return "" if r.ok else (r.err.strip() or r.out.strip() or "exit %d" % r.rc)

    def check_rows(self, ws):
        return guest.check_rows(self, ws)

    def vm_store_apart(self):
        return Store(self.env).vm_store_apart()

    def vm_dir(self):
        """Where the guests' daemons and keys live, even where the vm store is the container's and lists no guest."""
        return os.path.join(Store(self.env).vm_store_dir(), "vm")

    def key(self):
        return os.path.join(self.vm_dir(), "id_ed25519")

    def egress_filtered(self, ws):
        return not self.machine.exists(os.path.join(self.vm_dir(), ws + ".unfiltered"))

    def agent_sock(self):
        return "/Users/%s/.wk-ssh-agent.sock" % self.user()

    def agent_secret_remedy(self, ws, secret):
        if self._agent_secret(secret)[4] == "file" and not self.exec(ws, ["bash", "-lc", 'test -d "$CLAUDE_SECURESTORAGE_CONFIG_DIR"']).ok:
            return ("the %s share is not mounted in '%s': 'wk stop %s', then 'wk start %s' boots it with the share"
                    % (self.agent_rw_share, ws, ws, ws))
        return super().agent_secret_remedy(ws, secret)

    def base(self):
        return guest.base_name(self.env)

    def tart(self):
        return tart_path(self.env)

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

    def state_of(self, v):
        return next((x.get("State", "absent") for x in self._vms() if x.get("Name") == v), "absent")

    def vm_state(self, ws):
        return self.state_of(self.vm(ws))

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

    def guest_of(self, v):
        return TartExec(self.tart_or_die(), v, via=self.machine)

    def guest(self, ws):
        return self.guest_of(self.vm(ws)) if self.vm_state(ws) == "running" else None

    def _guest_or_die(self, ws):
        g = self.guest(ws)
        if g is None:
            act.die("'%s' is not running (wk start %s)" % (ws, ws))
        return g

    def exec(self, ws, argv, tty=False, timeout=None):
        g = self.guest(ws)
        if g is None:
            return Result(1, "", "'%s' is not running (wk start %s)" % (ws, ws))
        return g.run(argv, timeout=timeout)

    def enter_argv(self, ws):
        g = self._guest_or_die(ws)
        self.login_note()
        return g.argv("cd %s 2>/dev/null; exec \"${SHELL:-/bin/zsh}\" -l" % shlex.quote(self.src(ws)), tty=True), None

    def exec_argv(self, ws, argv, tty=False):
        return self._guest_or_die(ws).argv(shlex.join(argv), tty=tty), None

    def pull(self, ws, src, dest):
        self._guest_or_die(ws).copy_out(src, dest)

    def push(self, ws, src, dest):
        self._guest_or_die(ws).copy_in(src, dest)

    def pull_dir(self, ws, src, dest, exclude=()):
        self._guest_or_die(ws).copy_tree_out(src, dest, exclude)

    def push_dir(self, ws, src, dest):
        self._guest_or_die(ws).copy_tree_in(src, dest)

    def path_kind(self, ws, path):
        return path_kind_result(self._guest_or_die(ws).run(["sh", "-c", path_kind_probe(path)]))

    def ssh_user(self, ws):
        return self.user()

    def ssh_proxy(self, ws):
        return "%s vm %s" % (os.path.join(self.root, "container", "ssh-transport"), ws)

    def ssh_transport(self, ws):
        # macOS sshd cannot open an audit session on a pipe (`sshd -i` dies), so stdio is bridged to the guest's own sshd on loopback.
        os.execvp(self.tart_or_die(), [self.tart_or_die(), "exec", "-i", self.vm(ws), "/usr/bin/nc", "127.0.0.1", "22"])

    def ssh_argv(self, ws):
        return ["ssh", "-o", "ProxyCommand=" + self.ssh_proxy(ws), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=60", "-o", "ServerAliveCountMax=10",
                "-i", self.key(), "-l", self.user(), self.vm(ws) + ".vm.invalid"]

    def ssh_prepare(self, ws):
        sshalias.alias_set(self.machine, self.env, ws, self.vm(ws) + ".vm.invalid", self.user(), self.key(),
                           extra=("ProxyCommand %s" % self.ssh_proxy(ws),))

    def ssh_host(self, ws):
        return "wk-%s" % ws if self.vm_state(ws) == "running" else None

    def stop(self, ws):
        return guest.stop(self, ws)

    def sync(self, named=False):
        ok = True
        for g, _ in self.list():
            if self.info(g) != "running":
                sys.stderr.write("  %-24s not running -- skipped\n" % g)
            elif self.sync_tools(g):
                sys.stderr.write("  %-24s ok\n" % g)
            else:
                ok = False
        return ok

    def sync_tools(self, ws):
        """A git bundle, not a mount (a guest shares only agent-rw and the mirror); the marker goes with the tooling that reads it."""
        g = self._guest_or_die(ws)
        return tools.push(self.root, self.machine, g, self.tools(ws), self.env) and self.write_marker(ws, g)

    def write_marker(self, ws, guest):
        """A bench image (its images.MARKER) is no workspace, so it keeps none."""
        path = self.home() + "/.wk-workspace"
        try:
            if guest.run(["test", "-f", images.MARKER]).ok:
                guest.remove(path)
            else:
                guest.write(path, "# wk: this machine IS a workspace. Written by lib/wk/places.py.\nname=%s\nsrc=%s\n"
                            % (ws, self.src(ws)))
        except OSError as e:
            act.warn("could not settle %s's workspace marker: %s" % (ws, e))
            return False
        return True

    def converge(self, ws, clock):
        if self.vm_state(ws) == "running":
            guest.Host(self, clock).start_proxy()

    def daemon_remedy(self, ws, daemon):
        return "wk start %s" % ws

    def start(self, ws):
        ip = guest.start(self, ws)
        if not ip:
            return True
        self.ssh_prepare(ws)
        act.info("%s is up at %s (ssh alias wk-%s)" % (ws, ip, ws))
        act.log("  wk build %s mac-release\n  zed ssh://wk-%s%s" % (ws, ws, self.src(ws)))
        return True

    def tart_or_die(self):
        bin = self.tart()
        if not bin:
            act.die("tart is not installed: not on PATH, and not in ~/.local/bin.\n    Install the signed bundle (it needs the virtualization entitlement, so the\n"
                    "    .app must stay intact):\n      mkdir -p ~/.local/share/tart ~/.local/bin\n"
                    "      curl -fsSLO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz\n"
                    "      tar -xzf tart.tar.gz -C ~/.local/share/tart/\n"
                    "      ln -sfn ~/%s ~/.local/bin/tart\n"
                    "    Licence: FSL-1.1-ALv2; internal use is a Permitted Purpose (README.md, Setup)." % TART_APP)
        return bin

    def store_init(self):
        self.machine.mkdir(self.store.store_dir())
        self.machine.mkdir(os.path.join(self.store.store_dir(), "ws"))
        self.ensure_dir_mode(self.vm_dir(), "0700")

    def _podman_up(self):
        rec = podman_vm(self.machine, self.podman_machine())
        return rec if rec and rec.get("State") == "running" else None

    def podman_running(self):
        return self._podman_up() is not None

    def podman_mem_mb(self):
        rec = self._podman_up()
        return int(rec["Resources"]["Memory"]) if rec else 0

    def podman_containers(self):
        """An unreadable answer counts as busy: stopping a machine underneath something is the mistake this guards."""
        if not self.podman_running():
            return 0
        r = self.machine.run(["podman", "machine", "ssh", self.podman_machine(), "--", "podman ps -q | grep -c . || true"])
        n = re.sub(r"[^0-9]", "", r.out) if r.ok else ""
        return int(n) if n else 1

    def committed_mem_mb(self, skip):
        return sum(self.configured(v["Name"], "Memory") or 0 for v in self._vms()
                   if v.get("State") == "running" and v.get("Name") != skip)

    def running_vms(self):
        """Every VM on this host, the podman machine included: Virtualization.framework counts them against one limit."""
        names = [v["Name"][3:] for v in self._vms() if v.get("State") == "running" and str(v.get("Name", "")).startswith("wk-")]
        if self.podman_running():
            names.append("podman machine %s" % self.podman_machine())
        return names

    def create(self, ws, base=None, arch="native"):
        v, ws_dir, mirror = self.vm(ws), self.store.ws_dir(ws), self.store.mirror_dir()
        if self.vm_state(ws) != "absent":
            act.die("workspace '%s' already exists" % ws)
        if not self.machine.isdir(mirror):
            act.die("no WebKit mirror on this machine for '%s' to clone its checkout from\n    (%s does not exist):  wk sync    makes it" % (ws, mirror))
        from wk.sysimage import guestbase
        base = guestbase.Base(self)
        with base.host.lock().held("guest-base"):
            base.ensure()
        why = base.stale()
        if why and guest.vm_forced(self.env):
            act.warn("WK_VM_FORCE=1 -- '%s' is cloned from a base that\n  predates its own provisioning inputs: %s" % (ws, why))
        elif why:
            act.die("'%s' predates its own provisioning inputs: %s.\n  '%s' would be a clone of it, carrying the desktop settings of the day it\n"
                    "  was sealed -- which is how a guest comes up behind Setup Assistant, where\n  nothing in the guest can clear it:\n"
                    "      %s --rebuild     hours; existing guests are unaffected\n  WK_VM_FORCE=1 clones it anyway."
                    % (self.base(), why, ws, guest.BASE_BUILD))
        running = self.running_vms()
        if len(running) >= guest.vm_max(self.env):
            act.warn("%d VM(s) already running on this host; you will have to stop one before starting '%s':\n%s"
                     % (len(running), ws, "\n".join("      " + n for n in running)))
        act.info("cloning %s -> %s (APFS copy-on-write)" % (self.base(), v))
        res = Resources(self.machine, self.env, "macos")
        cpus, mem = guest.vm_cpus(self.env), guest.vm_mem_mb(self.env)
        cpus = str(res.envelope_cores() if cpus is None else cpus)
        mem = str(res.envelope_mem_mb() if mem is None else mem)
        tart = self.tart_or_die()
        for argv in ([tart, "clone", self.base(), v],
                     [tart, "set", v, "--cpu", cpus, "--memory", mem, "--random-mac", "--display", guest.display(self.env), "--display-refit"]):
            r = self.machine.act_run(argv, stream=True)
            if not r.ok:
                act.die("%s failed for '%s' (exit %d); what it said is above" % (" ".join(argv[1:2]), ws, r.rc), r.rc)
        self.machine.mkdir(ws_dir)
        self.machine.write(os.path.join(ws_dir, READY_MARKER), "")

    def runners(self, v):
        r = self.machine.run(["pgrep", "-f", "tart run .*[[:space:]]%s$" % v])
        return [int(p) for p in r.out.split() if p.isdigit()] if r.ok else []

    def delete_vm(self, v):
        """The one delete: `tart delete` leaves the `tart run` alive, holding a VM slot the next guest needs."""
        tart = self.tart_or_die()
        self.machine.act_run([tart, "stop", v])
        r = self.machine.act_run([tart, "delete", v])
        for pid in self.runners(v):
            self.machine.kill(pid)
        left = " ".join(str(p) for p in self.runners(v))
        if left:
            act.warn("a 'tart run' for '%s' is still alive (pid %s) and holds a\n    VM slot the next guest needs:  kill -9 %s" % (v, left, left))
        show(r)
        if not r.ok:
            act.die("tart delete %s failed (exit %d); what it said is above" % (v, r.rc), r.rc)
        act.info("deleted VM %s" % v)

    def destroy(self, ws):
        v, ws_dir = self.vm(ws), self.store.ws_dir(ws)
        if v == self.base():
            act.die("refusing to delete the golden base (%s --rm)" % guest.BASE_BUILD)
        if self.vm_state(ws) != "absent":
            self.delete_vm(v)
        for f in (ws + ".run.log", ws + ".unfiltered", ws + ".agent-forward.log", ws + ".broker-forward.log"):
            self.machine.remove(os.path.join(self.vm_dir(), f))
        # The directory goes last: it is what a re-run of a killed rm finds and destroys again.
        if self.machine.isdir(ws_dir):
            self.machine.remove(ws_dir)
            act.info("removed %s" % ws_dir)


class LocalWorkspace(Driver):
    kind = "local"
    needs_base = False

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self._store = Store(dict(env, WK_STORE=env.get("WK_LOCAL_STORE") or Store(env).state_dir()))
        marker = kv.kv_file(workspace_marker_path(env))
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
        return self.store.container_mirror_dir() or self.store.mirror_dir()

    def arch(self, ws):
        return self.ws_arch

    def list(self):
        return [(self.ws_name, "running")]

    def info(self, ws):
        return "running" if ws == self.ws_name else "absent"

    def exec_argv(self, ws, argv, tty=False):
        return ["bash", "-lc", "exec " + shlex.join(argv)], None

    def store_init(self):
        self.machine.mkdir(self.store.ws_dir(self.ws_name))

    def create(self, ws, base=None, arch="native"):
        act.die("a workspace cannot create a workspace -- run 'wk new %s' on the host" % ws)

    def destroy(self, ws):
        act.die("a workspace cannot destroy itself -- run 'wk rm %s' on the host" % self.ws_name)

    def start(self, ws):
        act.info("'%s' is the workspace this runs in, so it is up -- nothing to start" % self.ws_name)
        return True

    def stop(self, ws):
        act.die("a workspace cannot stop itself -- run 'wk stop %s' on the host" % self.ws_name)

    def enter_argv(self, ws):
        act.die("already inside workspace '%s'" % self.ws_name)

    def ssh_host(self, ws):
        act.die("a workspace has no ssh route to itself")

    def build_size(self, ws):
        """A container is sized by its own cgroup, where `max` is no limit, so the machine's; a guest has no cgroup."""
        res = Resources(self.machine, self.env, self.os())
        if self.os() == "macos":
            return res.host_cores(), res.host_mem_mb(), None
        quota, period = self._cgroup("cpu.max", r"(max|[0-9]+) [0-9]+").split()
        mem = self._cgroup("memory.max", r"max|[0-9]+")
        return (res.host_cores() if quota == "max" else max(1, int(quota) // int(period)),
                res.host_mem_mb() if mem == "max" else int(mem) // 1024 // 1024, None)

    def _cgroup(self, name, shape):
        path = "/sys/fs/cgroup/" + name
        try:
            text = self.machine.read(path).strip()
        except OSError as e:
            act.die("cannot read %s (%s): a build in a container is sized by its cgroup's limits" % (path, e))
        if not re.fullmatch(shape, text):
            act.die("%s reads %r, which is not what cgroup v2 writes there" % (path, text))
        return text


class Remote(Driver):
    """A machine of its own, reached over ssh (or this machine, when ~/.wk-remote names the place)."""

    kind = "remote"
    needs_base = False

    def __init__(self, name, root, env, machine):
        super().__init__(name, root, env, machine)
        self.host = env.get("WK_REMOTE_HOST") or (name if name != "remote" else "")
        self.peer = bool(env.get("WK_REMOTE_PEER"))
        try:
            here_place = Registry(root, env, machine).self_place()
        except LookupError:
            here_place = ""
        self.is_local = bool(env.get("WK_REMOTE_LOCAL")) or (bool(here_place) and here_place == name)
        self.conf_root = env.get("WK_REMOTE_ROOT", "")
        root_there = self.conf_root or (default_root(env.get("HOME", os.path.expanduser("~"))) if self.is_local else "")
        if self.is_local and root_there:
            store = env.get("WK_REMOTE_STORE") or root_there
        else:
            store = env.get("WK_REMOTE_STORE") or os.path.join(Store(env).state_dir(), "remote", name)
        self._store = Store(dict(env, WK_STORE=store))
        self.probe_seconds = int(env.get("WK_PROBE_SECONDS") or 20)
        if not self.is_local and self.host:
            self.machine = Ssh(self.host, opts=self.ssh_opts(), control_dir=self.ssh_dir(), timeout=reach.ssh_timeout(env), via=machine)
        self._probed = None
        self._has_wk = None
        self._peer_rows = None
        self._routes = {}
        self._reference = None

    def ssh_dir(self):
        return os.path.join(Store(self.env).state_dir(), "ssh")

    # ConnectTimeout covers only the TCP connect; the keepalives give up on a machine that accepts the connection and then stops answering (four misses at 15s).
    def ssh_opts(self):
        d = self.ssh_dir()
        return ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ControlMaster=auto",
                "-o", "ControlPath=%s/%%h-%%p-%%r" % d, "-o", "ControlPersist=60"]

    def _far(self):
        if self.is_local or self.host:
            return self.machine
        act.die("place '%s' has no host to reach.\n    Set host= in %s, or\n"
                "    name the place after a machine your ~/.ssh/config already knows:\n        wk new <name> --on devbox-arm64-2"
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
            try:
                self._probed = parse_probe(r.out, self.conf_root)
            except ValueError as e:
                self._probed = {"why": "it answered the probe with what this end cannot read: %s" % e, "unreadable": True}
        return self._probed

    def _probe_or_die(self):
        p = self.probed()
        if p.get("unreadable"):
            act.die("'%s' %s.\n    PROBE_SCRIPT (lib/wk/places.py) is what ran: run its lines there to see which answers\n"
                    "    differently from the Linux and macOS shapes it reads." % (self.host, p["why"]))
        if p.get("why") is not None:
            act.die("cannot reach '%s' over ssh: %s\n    This place has no way in but ssh, and it is not interactive: the key,\n"
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
        return self._probe_or_die()["os"]

    def cores(self):
        return self._probe_or_die()["cores"]

    def load(self):
        return self._probe_or_die()["load"]

    def mem_mb(self):
        return self._probe_or_die()["mem_mb"]

    def build_size(self, ws):
        return self.cores(), self.mem_mb(), self.load()

    def ccache_dir(self, ws):
        return self.root_there() + "/cache/ccache"

    def build_argv(self, ws, argv):
        prio = "nice -n 19" + (" ionice -c3" if self._probe_or_die().get("ionice") == "yes" else "")
        tee = "" if self.is_local else " 2>&1 | tee %s" % shlex.quote(self.ws_dir_there(ws) + "/build.log")
        text = "set -o pipefail\ncd %s && %s run remote-build -w 3600 -- %s %s%s" % (
            shlex.quote(self.src(ws)), shlex.join(isolated_module(self.tools(ws) + "/lib", "wk.lock")), prio,
            shlex.join(argv), tee)
        if self.is_local:
            return ["bash", "-c", text], None
        return self._far().argv("bash -c " + shlex.quote(text)), None

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
        return self._sh("cd %s && %s" % (shlex.quote(self.src(ws)), shlex.join(argv)), timeout=timeout)

    def enter_argv(self, ws):
        self._probe_or_die()
        if self.is_local:
            return ([os.environ.get("SHELL", "/bin/sh"), "-l"], self.src(ws))
        return self.machine.argv("cd %s && exec \"$SHELL\"" % shlex.quote(self.src(ws)), tty=True), None

    def exec_argv(self, ws, argv, tty=False):
        if self.is_local:
            return list(argv), self.src(ws)
        return self._far().argv("cd %s && %s" % (shlex.quote(self.src(ws)), shlex.join(argv)), tty=tty), None

    def exec_tty(self, ws, argv, timeout=None):
        """`exec_argv` is already a literal command (its own `ssh`), run by `self.here`: `self.machine` would wrap it twice."""
        cmd, cwd = self.exec_argv(ws, argv, tty=True)
        return self.here.run_tty(cmd, cwd=cwd, timeout=timeout)

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
        route = kv.kv(out)
        if not route.get("user") or not route.get("src"):
            act.die("%s said nothing an editor can use about '%s'" % (self.host, ws))
        self._routes[ws] = (route["user"], route["src"], route.get("proxy", ""))
        return self._routes[ws]

    @property
    def store_machine(self):
        return self.here

    def sync_tools(self, ws):
        if self.peer:
            act.debug("not pushing wk-tools to %s: it is a workstation with its own checkout" % self.label())
            return True
        dest = self.tools(ws)
        if self.is_local:
            if self.root != dest:
                act.warn("running %s/wk, but this place's tooling is %s" % (self.root, dest))
            return True
        return tools.push(self.root, self.here, self._far(), dest, self.env)

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

    def wk_far(self, env):
        return "cd $HOME && ", self.tools("") + "/wk", env

    def hand_over(self, cmd, args, tty, readonly=False, env=None):
        """The far side's wk running `cmd`; a box at another wk-tools commit is refused it, or warned on a read-only one."""
        self.far_wk_or_die(cmd)
        if not self.peer:
            self.tools_level_or_refuse(cmd, readonly)
        return self.machine.argv(self.wk_cmd([cmd, *args], dict(env or os.environ, WK_ROW_LABEL=self.name)), tty=tty)

    def tools_level_or_refuse(self, cmd, readonly):
        theirs = kv.kv(self.wk("doctor", "--probe-tools", quiet=True)[1]).get("sha", "")
        mine = tools.head(self.root, self.here)
        if tools.sha_matches(theirs, mine):
            return
        why = ("wk-tools on %s is at %s, and this workstation's at %s.\n    Bring it level:  wk sync --tools %s"
               % (self.name, theirs[:12] or "?", mine[:12] or "?", self.name))
        if readonly:
            act.warn(why)
        else:
            act.barrier("'%s' is not handed to %s: %s" % (cmd, self.name, why))

    def wk(self, *args, env=None, quiet=False):
        env = os.environ if env is None else env
        r = self._sh(self.wk_cmd(args, env) + ("" if quiet else " 2>&1"))
        return r.rc, r.out

    def start(self, ws):
        act.info("'%s' has no notion of starting a single workspace -- nothing to bring up for '%s'" % (self.name, ws))
        return True

    def stop(self, ws):
        act.err("the '%s' place has no notion of stopping a single workspace -- '%s' is left running" % (self.name, ws))
        return False

    def store_init(self):
        self.here.mkdir(self.store.store_dir())
        self.here.mkdir(os.path.join(self.store.store_dir(), "ws"))

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

    def _forks(self):
        return [tuple(r) for r in secrets.forks()]

    def _wire(self, src):
        n, u, c = self.wiring_args()
        script = git.wiring_script(src, self.mirror_dir(), self._forks(), git.mirror_branches(self.env), n, u, c)
        if not self._sh_act(script).ok:
            act.warn("could not wire the remotes in %s" % src)

    def _mirror_update(self, root):
        act.info("updating the WebKit mirror on %s (first run clones it)" % self.label())
        script = git.mirror_refresh_script(self.mirror_dir(), git.mirror_branches(self.env))
        r = self._sh_act("set -e\n mkdir -p %s %s\n %s" % (shlex.quote(root + "/ws"), shlex.quote(root + "/cache/ccache"),
                                                          script))
        for line in r.out.splitlines():
            f = line.split()
            if len(f) == 3 and f[0] == "mirror-fetch":
                act.log("  %-8s %s" % (f[1], f[2]))
        if not r.ok:
            act.die("could not update the WebKit mirror on %s" % self.label())

    def sync(self, named=False):
        """A peer pulls, and publishes its own snapshot only once it matches this checkout and was named."""
        far_tools, host = self.tools(""), self.label()
        if self.peer:
            r = self._sh_act("cd %s && git pull --ff-only" % shlex.quote(far_tools))
            show(r)
            if not r.ok:
                sys.stderr.write("  %-24s git pull --ff-only failed there\n" % self.name)
                return False
            mine = tools.identity(self.root, self.here)
            theirs = kv.kv(self._sh(shlex.quote(far_tools + "/wk") + " doctor --probe-tools").out)
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
        ok = self.sync_tools("")
        if ok:
            sys.stderr.write("  %-24s pushed %s\n" % (self.name, tools.head(self.root, self.here)))
        if self.reference():
            act.info("workspaces here clone from %s, which this machine's admins keep up to date" % self.reference())
            act.log("  nothing of ours to fetch: no mirror is kept on %s" % host)
            return ok
        self._mirror_update(self.root_there())
        act.info("the WebKit mirror on %s is up to date" % host)
        return ok

    def create(self, ws, base=None, arch="native"):
        self._probe_or_die()
        root, wsd, host = self.root_there(), self.ws_dir_there(ws), self.label()
        st = self.info(ws)
        if st == "creating":
            act.die("'%s' on %s is a checkout that never finished being\n    made, and destroying it did not take. Remove it by hand and try again:\n"
                    "        rm -rf %s" % (ws, host, shlex.quote(wsd)))
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
        self._sh_act("[ -f %s ] || printf %%s %s > %s" % (conf, shlex.quote(self.ccache_conf()), conf))
        self.here.mkdir(self.store.ws_dir(ws))
        if not self._sh_act("touch %s" % shlex.quote(wsd + "/" + READY_MARKER)).ok:   # last: an ssh cut mid-clone leaves it creating
            act.die("could not mark '%s' ready on %s -- treat it as half-made\n    and re-run 'wk new %s --on %s'" % (ws, host, ws, self.name))
        act.info("remote workspace '%s' created on %s (%s)" % (ws, host, wsd))

    def results(self, ws):
        """A peer's own wk names where it holds `ws`'s tasks (`python3 -m wk.bench.record home`), reached through the peer."""
        if not self.peer:
            return self.machine, self.ws_dir_there(ws) + "/bench"
        r = self._sh("cd $HOME && " + shlex.join(isolated_module(self.tools("") + "/lib", "wk.bench.record") + ["home", ws]))
        try:
            doc = json.loads(r.out) if r.ok else None
        except ValueError:
            doc = None
        if not doc:
            act.die("%s did not say where '%s' keeps its bench tasks:\n    %s\n"
                    "    A copy of wk-tools there that predates the question answers nothing: wk sync --tools"
                    % (self.label(), ws, (r.err.strip() or r.out.strip() or "rc %d" % r.rc).replace("\n", "\n    ")))
        m = self.machine
        for kind, dest in doc["via"]:
            m = (PodmanVm if kind == "podman" else Ssh)(dest, via=m)
        return m, doc["path"]

    def task_store(self):
        return None if self.peer or self.is_local else (self.machine, self.root_there())

    def destroy(self, ws):
        """Another machine's own wk destroys its workspace; the record here outlives anything it has not confirmed gone."""
        host = self.label()
        if not self.is_local:
            self._probe_or_die()
            if self.info(ws) == "absent":
                self.here.remove(self.store.ws_dir(ws))
                act.info("'%s' is already gone from %s; its record here is removed" % (ws, host))
                return
            exports_read_here = {} if self.peer else {"WK_EXPORTS_READ": "1"}
            r = self.here.act_run(self.hand_over("rm", [ws], tty=False, env=dict(os.environ, WK_YES="1", **exports_read_here)))
            show(r)
            if not r.ok:
                act.die("%s did not destroy '%s'; what its own wk said is above.\n    Nothing here was changed -- re-run 'wk rm %s' once that is settled." % (host, ws, ws))
            self.here.remove(self.store.ws_dir(ws))
            self._peer_rows = None   # the listing read before the removal is what the read-back must not see
            act.info("'%s' destroyed on %s, by that machine's own wk" % (ws, host))
            return
        wsd = self.ws_dir_there(ws)
        r = self._sh_act("rm -rf %s" % shlex.quote(wsd))
        show(r)
        if not r.ok:
            act.die("could not remove %s (above); re-run 'wk rm %s'" % (wsd, ws))
        self.here.remove(self.store.ws_dir(ws))
        act.info("removed workspace '%s' (%s)" % (ws, wsd))


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
    ahead = git("rev-list", "--count", up + "..HEAD")
    if ahead is None:
        return "git could not count this machine's commits past %s" % up
    return "this machine is %s commit(s) ahead of %s -- push them, then re-run" % (ahead, up) if ahead != "0" else ""


def ssh_last_word(r):
    """The one line a person acts on: ssh's last non-blank stderr line, its prefixes stripped."""
    lines = [l for l in r.err.splitlines() if l.strip()]
    line = lines[-1] if lines else ""
    for prefix in ("ssh: ", "kex_exchange_identification: "):
        if line.startswith(prefix):
            line = line[len(prefix):]
    return line or "ssh exited %d and said nothing" % r.rc


def parse_probe(text, root=""):
    """`vm_stat` reports pages where /proc/meminfo has MemAvailable in kB; ValueError names a figure the answer lacks."""
    lines = text.splitlines()
    home = lines[0] if lines else ""
    uname = lines[1] if len(lines) > 1 else ""
    cores = _num(lines[2] if len(lines) > 2 else "", "the core count")
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
    f = head[0].replace("{", " ").split() if head else []
    load = _num(f[0] if f else "", "the load average")
    if uname == "Linux":
        avail = [l.split()[1:2] for l in mem if l.startswith("MemAvailable:")]
        mem_mb = _num((avail[0] or [""])[0] if avail else "", "/proc/meminfo's MemAvailable") // 1024
    else:
        size = [re.search(r"page size of ([0-9]+)", l) for l in mem if "page size of" in l]
        if not size or not size[0]:
            raise ValueError("vm_stat printed no page size")
        pages = sum(_num(l.split()[-1].rstrip("."), l.split(":")[0]) for l in mem
                    if l.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")))
        mem_mb = pages * int(size[0].group(1)) // 1024 // 1024
    return {"home": home, "cores": cores, "load": load, "mem_mb": mem_mb, "ionice": ionice,
            "os": "macos" if uname == "Darwin" else "linux", "root": root or default_root(home)}


def _num(s, what):
    try:
        return int(float(s))
    except ValueError:
        raise ValueError("%s is %r, not a number" % (what, s))


def store_state(store):
    """(path -> mode, digest) for what store-init can change; workspaces, snapshots and the mirror are not walked."""
    out = {}

    def note(p):
        st = os.lstat(p)
        digest = ""
        if os.path.isfile(p) and not os.path.islink(p):
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        out[p] = (st.st_mode, digest)

    for top, deep in ((store.store_dir(), False), (store.keyring_dir(), True), (store.keyring_agent_rw_dir(), False)):
        for d, dirs, files in os.walk(top):
            note(d)
            for f in files:
                note(os.path.join(d, f))
            if not deep:
                dirs[:] = [x for x in dirs if os.path.join(d, x).count(os.sep) - top.count(os.sep) < 3
                           and x not in ("ws", "base", "git", "skills", "task", "log")]
    return out


def main(argv, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="python3 -m wk.places")
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("store-init", help="make the container store; print each path it changed")
    sub.add_parser("tart", help="print the tart binary's path (tart_path); exit 1 where there is none")
    vm = sub.add_parser("podman-vm", help="print each field of the podman machine's record, or (name=Field.Sub) a shlex-quoted "
                                           "shell assignment for wk_eval; exit 1 where there is none")
    vm.add_argument("fields", nargs="+", metavar="Field.Sub|name=Field.Sub")
    args = ap.parse_args(argv)
    t = Registry(images.root(env), env).load("container")
    if args.verb == "tart":
        if not tart_path(env):
            return 1
        print(tart_path(env))
        return 0
    if args.verb == "podman-vm":
        rec = podman_vm(t.machine, t.podman_machine())
        if rec is None:
            return 1
        for field in args.fields:
            var, eq, path = field.partition("=")
            v = podman_vm_field(rec, path if eq else var)
            print("%s=%s" % (var, shlex.quote(str(v))) if eq else v)
        return 0
    before = store_state(t.store)
    t.store_init()
    after = store_state(t.store)
    sys.stdout.write("".join("%s\n" % p for p in sorted(after) if before.get(p) != after[p]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except act.Refused as e:
        sys.exit(e.status)
