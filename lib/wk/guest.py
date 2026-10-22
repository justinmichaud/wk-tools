"""A macOS guest's start, stop, convergence and admission, its desktop's readings, and its host daemons' rows (lib/wk/daemons.py)."""

import os
import shlex
import sys
import time

from wk import act, agents, claudelogin, daemons, git, images, project, secrets, tools
from wk.act import Refused, debug, die, info, log, warn
from wk.clock import Clock
from wk.daemons import Daemon
from wk.lock import Lock
from wk.machine import Local
from wk.store import GUEST_BROKER_SOCKET, GUEST_PUSH_SOCKET, Store, no_such_workspace

SUBNET = "192.168.2"   # Softnet's own network, not vmnet's 192.168.64
PROXY_PORT = "3128"
SOFTNET = "/usr/local/bin/softnet"
CLOCK_SKEW = "30"      # not zero: the reading is taken through the guest agent, so a round trip is in every compare
BOOT_WAIT = 180
FORWARD_WAIT = 4
FORWARDS = ("push", "broker")

# The one request a guest's start makes of the push service; its refusal is the exit status.
PUSH_LISTEN = """import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5)
s.connect(sys.argv[1])
s.sendall((json.dumps({"verb": "listen", "workspace": sys.argv[2]}) + "\\n").encode())
sys.exit(0 if "listening" in json.loads(s.makefile().readline()) else 1)
"""

PASSWORD = "admin"
DISPLAY = "1280x800"
BASE = "wk-base"
BASE_PRESET = "macos-guest-base"
BASE_BUILD = "wk sysimage build " + BASE_PRESET
RESTART = "wk stop <name> && wk start <name>"
REBUILD = BASE_BUILD + " --rebuild, then re-create this guest"
HOST_FREE_WARN_GB, HOST_FREE_MIN_GB = 80, 25
DISK_GB = 320
QUIET, WINDOWS, PYOBJC = "bench/mac-quiet-desktop.sh", "bench/mac-window-probe.sh", "bench/mac-pyobjc.sh"
LLDB_HEADER = "# wk: written by lib/wk/guest.py. See wk run --lldb, wk gui --lldb.\n"
CLAUDE_LINKS = ("settings.json", "hooks", "CLAUDE.md", "skills")
READINGS = """. "$0/%s"; . "$0/%s"; . "$0/%s"
printf 'pin=%%s\\n' "$WK_PYOBJC_VERSION"
[ -z "$3" ] || printf 'unexpected=%%s\\n' "$(wk_window_unexpected "$3")"
{ wk_quiet_desktop_findings "$1" "$2"; wk_quiet_cpu_findings "$1" "$2"; } | sed 's/^/row=/'
""" % (QUIET, WINDOWS, PYOBJC)

LOAD_FAMILIES = (
    ("shell", r"(^|/)(-?zsh|bash|sh|dash|tcsh|fish|login)$"),
    ("editor remote server", r"(zed-remote-server|\.zed_server/|\.vscode-server/)"),
    ("agent", r"(^|/)claude$|/claude/versions/"),
    ("ssh session", r"(^|/)sshd(-session)?$"),
)


class Host:

    def __init__(self, vm, clock=None):
        self.vm, self.machine, self.env, self.root = vm, vm.machine, vm.env, vm.root
        self.clock = clock or Clock()
        self.secrets = secrets.Secrets(vm.root, vm.env, vm.machine, host_side=True)
        self.dir = vm.vm_dir()
        self._addr = None
        self._lock = None

    def path(self, name):
        return os.path.join(self.dir, name)

    def unfiltered(self):
        return is_unfiltered(self.env)

    def port(self):
        return proxy_port(self.env)

    def softnet(self):
        return softnet_bin(self.env)

    def proxy_addr(self):
        if self._addr is None:
            net = (self.env.get("WK_VM_SUBNET") or SUBNET) + "."
            found = [f[1] for f in (line.split() for line in self.machine.run(["ifconfig"]).out.splitlines())
                     if len(f) > 1 and f[0] == "inet" and f[1].startswith(net)]
            self._addr = self.env.get("WK_VM_PROXY_ADDR") or (found[0] if found else net + "1")
        return self._addr

    def softnet_flags(self):
        """Default-deny, and the one allow is the proxy: Softnet applies at `tart run` and cannot be added later."""
        if self.unfiltered():
            warn("WK_VM_UNFILTERED=1 -- this guest gets the open network, with no egress filter")
            return []
        if not self.machine.run(["test", "-x", self.softnet()]).ok:
            die("softnet is not installed, so this guest's egress would not be filtered.\n"
                "    Install it:  ./setup --stage softnet   (needs a terminal for sudo)\n"
                "    Or set WK_VM_UNFILTERED=1 to boot with the open network anyway.")
        return ["--net-softnet", "--net-softnet-block=0.0.0.0/0", "--net-softnet-allow=%s/32" % self.proxy_addr()]

    def daemon_pid(self, pidfile):
        try:
            pid = self.machine.read(pidfile).strip()
        except OSError:
            return None
        return int(pid) if pid.isdigit() and self.machine.alive(int(pid)) else None

    def spawn(self, argv, log, pidfile):
        self.machine.remove(log)
        pid = self.machine.spawn(argv, log)
        self.machine.write(pidfile, "%d\n" % pid)
        return pid

    def supervisor(self):
        return daemons.Launchd(self.machine, self.root, Store(self.env).state_dir(), self.clock)

    def daemons(self):
        login = self.secrets.cred_path("claude-login")
        self.secrets.ensure_dir(os.path.dirname(login), "0700")
        store, push = Store(self.env), self.push_sock()
        at = "%s:%s" % (self.proxy_addr(), self.port())
        proxy = {"WK_PROXY_UNIX": "0", "WK_PROXY_TCP": at, "WK_STORE": self.vm.store.store_dir(),
                 "WK_PUSH_SOCKET": push, **{k: v for k, v in self.inject_env().items() if k in ("WK_INJECT_SOCK", "WK_INJECT_READ_SOCK",
                                                                                              "WK_INJECT_CA_OUT")}}
        return {"github-inject": Daemon("github-inject", self.inject_env(), self.path("github-inject.sock"), lock=login),
                "broker": Daemon("broker", {"WK_ROOT": self.root, "WK_STORE": store.store_dir(),
                                            "WK_BROKER_PUBLISH_MACHINE": store.podman_machine()}, store.runtime_socket()),
                "push": Daemon("push", {"WK_PUSH_SOCKET": push, "WK_PUSH_GUEST_DIR": self.dir}, push),
                "proxy": Daemon("proxy", proxy, at, boot=False)}

    def ensure(self, word):
        self.machine.mkdir(self.dir)
        sup = self.supervisor()
        ok = sup.ensure(self.daemons()[word])
        for line in sup.changes:
            info(line)
        return ok

    def start_proxy(self):
        if self.unfiltered():
            return True
        with self.lock().held("vm-daemons"):
            self.start_inject()
            addr = self.proxy_addr()
            if not self.clock.wait_until(lambda: ("inet %s " % addr) in self.machine.run(["ifconfig"]).out, 15, 0.5):
                warn("the guest bridge never got address %s; not starting the proxy" % addr)
                return False
            return self.ensure("proxy")

    def credentials_converge(self):
        """The one writer of what this injector serves every guest: each start and each `wk key set`."""
        self.machine.mkdir(self.dir)
        held = (("read-github-pat", "github-pat"), ("push-github-pat", "github-pat"), ("push-bugzilla-api-key", "bugzilla-api-key"))
        if all([self.secrets.cred_sync(self.path(f), name) for f, name in held]):
            return True
        warn("could not converge the tokens in %s; a read from a guest answers 401" % self.dir)
        return False

    def inject_env(self):
        return {"WK_INJECT_SOCK": self.path("github-inject.sock"), "WK_INJECT_READ_SOCK": self.path("github-inject-read.sock"),
                "WK_INJECT_DIR": self.path("github-inject"),
                "WK_INJECT_CA_OUT": self.path("wk-github-ca.pem"), "WK_INJECT_PAT": self.path("push-github-pat"),
                "WK_INJECT_READ_PAT": self.path("read-github-pat"), "WK_INJECT_BUGZILLA_KEY": self.path("push-bugzilla-api-key"),
                "WK_INJECT_CLAUDE_LOGIN": self.secrets.cred_path("claude-login"),
                "WK_INJECT_PLAIN_SOCK": self.path("claude-inject.sock"),
                "WK_INJECT_PUBLISH_MACHINE": Store(self.env).podman_machine()}

    def start_inject(self):
        self.credentials_converge()
        if daemons.answers(self.machine, self.path("github-inject.sock")):
            return True
        warn("the credential injector is not running on this Mac, so '%s pr' and Claude in a guest get no\n"
             "  credential:  ./setup --stage inject" % project.get("PR_TOOL"))
        return False

    def push_sock(self):
        return Store(self.env).push_socket()

    def start_push(self):
        """The push service every guest's push reaches, run here because a guest cannot see a unix socket across the hypervisor."""
        with self.lock().held("vm-daemons"):
            return self.ensure("push")

    def push_listen(self, ws):
        if not self.start_push():
            return None
        if not self.machine.act_run(["/usr/bin/python3", "-c", PUSH_LISTEN, self.push_sock(), ws]).ok:
            return None
        return self.path(ws + ".push.sock")

    def lock(self):
        if self._lock is None:
            self._lock = Lock(Store(self.env), self.machine, self.clock)
        return self._lock

    def forward_pidfile(self, ws, what):
        return self.path("%s.%s-forward.pid" % (ws, what))

    def forward_start(self, ws, guest, what, far, near):
        """`ssh -R far:near` over the guest's sshd on tart exec (Vm.ssh_argv): a guest cannot see a unix socket across the hypervisor."""
        with self.lock().held("vm-%s-forward-%s" % (what, ws)):
            pidfile = self.forward_pidfile(ws, what)
            if self.daemon_pid(pidfile) is not None:
                return True
            if not guest.act_run(["rm", "-f", far]).ok:
                return False
            ssh = self.vm.ssh_argv(ws)
            log = self.path("%s.%s-forward.log" % (ws, what))
            pid = self.spawn(ssh[:-1] + ["-N", "-o", "ExitOnForwardFailure=yes", "-R", "%s:%s" % (far, near), ssh[-1]], log, pidfile)
            if act.dry_run() or self.clock.wait_until(lambda: guest.run(["test", "-S", far]).ok, FORWARD_WAIT, 0.2):
                return True
            self.machine.kill(pid)
            self.machine.remove(pidfile)
            warn("the %s forward into '%s' did not come up; see %s" % (what, ws, log))
            return False

    def forward_stop(self, ws, what):
        with self.lock().held("vm-%s-forward-%s" % (what, ws)):
            pidfile = self.forward_pidfile(ws, what)
            pid = self.daemon_pid(pidfile)
            if pid is not None:
                self.machine.kill(pid)
            self.machine.remove(pidfile)


# In order; a failed step is named and passed over, except the last, whose refusal is the start's.
STEPS = (
    ("guest_tools_push", warn, "wk-tools in {ws} is not this tree's commit; 'wk sync --tools' puts it there once it is committed"),
    ("write_marker", debug, "could not settle {ws}'s workspace marker"),
    ("write_shell_rc", warn, "could not wire {ws}'s shell; 'wk' will not be on PATH in there"),
    ("write_lldbinit", debug, "could not write .lldbinit in {ws}"),
    ("set_guest_clock", warn, "could not set {ws}'s clock; TLS in there will fail as CERT_NOT_YET_VALID"),
    ("set_guest_egress", warn, "could not set {ws}'s egress; nothing in there will reach the outside"),
    ("write_agent_secrets", warn, "could not write the agent credentials into {ws}; an agent in there will ask you to log in"),
    ("write_checkout", warn, "{ws}'s checkout is not wired and set up (above); 'wk sync {ws} --fix' once it is up"),
    ("install_agents", warn, "{ws} has no working coding agents (above); 'wk rm {ws}' and 'wk new' remake it"),
    ("write_claude_config", warn, "could not link ~/.claude in {ws}; an agent in there would have no instructions"),
    ("push_forward", warn, "could not reach the push service from {ws}; a push from in there is refused"),
    ("broker_forward", warn, "could not reach the request broker from {ws}; 'wk sync' in there refreshes no mirror"),
    ("settle_desktop", warn, "could not settle {ws}'s desktop; 'wk doctor {ws}' says what is in front of the window"),
    ("report_desktop", None, ""),
)


class Guest:

    def __init__(self, host, ws, m):
        self.host, self.vm, self.ws, self.m = host, host.vm, ws, m
        self.secrets = host.secrets

    def converge(self):
        for name, level, why in STEPS:
            if getattr(self, name)():
                continue
            if level is None:
                raise Refused(1)
            level(why.format(ws=self.ws))

    def _said(self, r):
        sys.stderr.write(r.err.replace("\r", ""))
        return r.ok

    def guest_tools_push(self):
        return tools.push(self.host.root, self.host.machine, self.m, self.vm.tools(self.ws), self.host.env)

    def write_marker(self):
        return self.vm.write_marker(self.ws, self.m)

    def write_shell_rc(self):
        return self._said(self.m.act_run(["bash", "-s", self.vm.tools(self.ws)],
                                         input=tree(self.host.root, "vm/shell-rc.sh")))

    def write_lldbinit(self):
        text = LLDB_HEADER + "command script import %s/%s\n" % (self.vm.src(self.ws), project.get("LLDB_SCRIPT")) \
            + tree(self.host.root, "dotfiles/lldbinit")
        return self._said(self.m.act_run(["sh", "-c", 'cat > "$HOME/.lldbinit"'], input=text))

    def write_checkout(self):
        src, mirror, forks = self.vm.src(self.ws), self.vm.mirror_dir(), self.secrets.forks()
        script = tree(self.host.root, "vm/checkout.sh") + git.wiring_script(src, mirror, forks, images.mirror_branches(self.host.env)) \
            + git.pr_tool_setup_script(src, forks)
        t0 = self.host.clock.now()
        r = self.m.act_run(["env", "WK_SRC=" + src, "WK_MIRROR=" + mirror, "WK_TOOLS=" + self.vm.tools(self.ws), "bash", "-s"],
                           input=script)
        out = (r.out + r.err).replace("\r", "")
        if "checkout=cloned" in out:
            info("%s's %s checkout made from its mirror in %ds" % (self.ws, project.get("CHECKOUT"), self.host.clock.now() - t0))
        if "setup=ok" in out:
            info("%s is set up in %s" % (project.get("PR_TOOL"), self.ws))
        if not r.ok:
            sys.stderr.write("".join("    %s\n" % l for l in out.splitlines()[-5:]))
        return r.ok

    def install_agents(self):
        try:
            agents.install(self.host.root, self.host.env, self.host.machine, self.m.act_run, self.ws, self.vm.tools(self.ws),
                           self.vm.src(self.ws))
        except Refused:
            return False
        return True

    def write_claude_config(self):
        home = self.vm.home() + "/.claude"
        return self._said(self.m.act_run(["mkdir", "-p", home])) and self._said(
            self.m.act_run(["ln", "-sfn", *("%s/claude/%s" % (self.vm.tools(self.ws), f) for f in CLAUDE_LINKS), home]))

    def settle_desktop(self):
        """The password is the script's own first line: as an argument it would be in `ps` on both machines."""
        script = "WK_VM_PASSWORD=%s\n" % shlex.quote(password(self.host.env)) + quiet_script(self.host.root, self.host.machine) \
            + tree(self.host.root, PYOBJC, "vm/desktop.sh")
        return self._said(self.m.act_run(["bash", "-s"], input=script))

    def report_desktop(self):
        from wk import doctor
        rd = desktop_readings(self.host.root, self.host.machine, self.m)
        if not rd:
            return True
        info("the guest's desktop, as it is now ('wk doctor %s' asks again):" % self.ws)
        doctor.Report(sys.stderr).rows(doctor.desktop_rows(rd, self.ws))
        blocked = blockers(rd)
        if not blocked:
            return True
        lines = "".join("      %s\n" % b for b in blocked)
        if vm_forced(self.host.env):
            warn("WK_VM_FORCE=1 -- '%s' is handed over with this in front of its desktop:\n%s" % (self.ws, lines))
            return True
        die("'%s' is not usable: something is in front of its desktop.\n%s    A clone cannot clear this itself -- Setup "
            "Assistant's account pane needs\n    Apple's servers, and a guest's egress filter refuses them. It is cleared\n"
            "    once, on the base every guest is cloned from:\n      %s --rebuild     hours; then re-create this guest\n"
            "    WK_VM_FORCE=1 hands the guest over anyway." % (self.ws, lines, BASE_BUILD))

    def set_guest_clock(self):
        """`tart clone` hands a clone the base's clock, and NTP is UDP, which the CONNECT proxy cannot carry."""
        now, r = int(self.host.clock.now()), self.m.run(["date", "-u", "+%s"])
        if not r.out.strip().isdigit():
            return False
        skew = abs(now - int(r.out.strip()))
        if skew <= int(clock_skew(self.host.env)):
            return True
        if not self.m.act_run(["sudo", "-n", "date", "-u", time.strftime("%m%d%H%M%Y.%S", time.gmtime(now))]).ok:
            return False
        info("%s's clock was %ds out; set from this host" % (self.ws, skew))
        return True

    def set_guest_egress(self):
        h = self.host
        addr = "" if h.unfiltered() else h.proxy_addr()
        try:
            ca = h.machine.read(h.path("wk-github-ca.pem")) if addr else ""
        except OSError:
            ca = ""
        ca = ca if "BEGIN CERTIFICATE" in ca else ""
        debug("guest egress in %s: %s" % (self.ws, addr or "off"))
        script = here_doc("/tmp/.wk-github-ca.new", ca) + here_doc("/tmp/.wk-egress.new", self.egress_env(addr, ca)) + tree(h.root, "vm/egress.sh")
        return self.m.act_run(["env", "WK_ADDR=" + addr, "WK_PORT=" + h.port(), "bash", "-s"], input=script).ok

    def egress_env(self, addr, ca):
        if not addr:
            return ""
        url, local, bundle = "http://%s:%s" % (addr, self.host.port()), "localhost,127.0.0.1,::1", "$HOME/.wk-ca-bundle.pem"
        env = [(v, url) for v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")] + [("no_proxy", local), ("NO_PROXY", local),
               ("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")]
        if ca:
            env += [(v, bundle) for v in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")] + [
                ("GITHUB_COM_USERNAME", self.secrets.github_user()), ("GITHUB_COM_TOKEN", claudelogin.PLACEHOLDER),
                ("SSL_CERT_FILE", bundle), ("NODE_EXTRA_CA_CERTS", "$HOME/.wk-github-ca.pem"), ("GH_TOKEN", claudelogin.PLACEHOLDER)]
            bz_user, bz_password = project.get("BUGZILLA_ENV")
            if self.secrets.bugzilla_user():
                env += [(bz_user, self.secrets.bugzilla_user()), (bz_password, claudelogin.PLACEHOLDER)]
        return "# wk: written by lib/wk/guest.py on every start. Softnet denies everything but this address.\n" \
            + "".join("export %s=%s\n" % kv for kv in env)

    def write_agent_secrets(self):
        return secrets.deliver(self.m, "vm", self.secrets.cred_read, self.ws, self.vm.tools(self.ws))

    def push_forward(self):
        near = self.host.push_listen(self.ws)
        return bool(near) and self.host.forward_start(self.ws, self.m, "push", self.vm.home() + "/" + GUEST_PUSH_SOCKET, near)

    def broker_forward(self):
        """The host broker's socket at the one a workspace's broker client dials; a broker that starts later is reached then."""
        return self.host.forward_start(self.ws, self.m, "broker", self.vm.home() + "/" + GUEST_BROKER_SOCKET,
                                       Store(self.host.env).runtime_socket())


def setting(var, default=None, kind=str):
    return lambda env: kind(env[var]) if env.get(var) else default


is_unfiltered = setting("WK_VM_UNFILTERED", False, bool)
softnet_bin = setting("WK_SOFTNET_BIN", SOFTNET)
vm_user = setting("WK_VM_USER", "admin")
vm_max = setting("WK_VM_MAX", 2, int)
vm_share = setting("WK_VM_SHARE", False, bool)
vm_forced = setting("WK_VM_FORCE", False, bool)
vm_cpus = setting("WK_VM_CPUS", None, int)
vm_mem_mb = setting("WK_VM_MEM_MB", None, int)
vm_disk_gb = setting("WK_VM_DISK_GB", DISK_GB, int)
display = setting("WK_VM_DISPLAY", DISPLAY)
base_name = setting("WK_VM_BASE", BASE)
password = setting("WK_VM_PASSWORD", PASSWORD)
proxy_port = setting("WK_VM_PROXY_PORT", PROXY_PORT)
clock_skew = setting("WK_VM_CLOCK_SKEW", CLOCK_SKEW)


def here_doc(path, text):
    return "cat > %s <<'WKDOC'\n%s\nWKDOC\n" % (path, text.rstrip("\n")) if text else "rm -f %s\n" % path


def runlog_tail(machine, path):
    """The only place a `tart run` says why it died ("The number of VMs exceeds the system limit")."""
    try:
        lines = machine.read(path).splitlines()
    except OSError:
        lines = []
    if not lines:
        return "    nothing -- %s is empty" % path
    return "\n".join("      " + line for line in lines[-5:]) + "\n    (%s)" % path


def tree(root, *rels):
    out = []
    for rel in rels:
        with open(os.path.join(str(root), rel), errors="replace") as f:
            out.append(f.read())
    return "".join(out)


def login_note(env):
    log("  the guest's own window logs in as %s / %s" % (vm_user(env), password(env)))
    log("  (wk itself goes through the tart guest agent; this is for a prompt on the screen)")
    log("  wk doctor <name>     what is in front of that window, and what is piling up in it")


def bench(root, machine, script, *args):
    r = machine.run(["bash", "-c", script, str(root), *args])
    return r.out if r.ok else ""


def quiet_script(root, machine):
    return bench(root, machine, '. "$0/%s"; wk_quiet_desktop_script' % QUIET)


def unexpected(root, machine, windows):
    return bench(root, machine, '. "$0/%s"; wk_window_unexpected "$1"' % WINDOWS, windows).strip()


def window_reading(root, machine, g):
    r = g.run(["bash", "-s"], input=quiet_script(root, machine) + tree(root, WINDOWS) + "wk_window_probe\n")
    return value(r.out.replace("\r", ""), "windows") if r.ok else ""


def desktop_probe(root, machine, g):
    """Streamed, never the guest's own wk-tools: it answers about a guest whose copy is older than this tree."""
    r = g.run(["bash", "-s"], input=quiet_script(root, machine) + tree(root, WINDOWS, PYOBJC, "vm/desktop-probe.sh"))
    return r.out.replace("\r", "") if r.ok else ""


def load_probe(root, g):
    r = g.run(["bash", "-s"], input=tree(root, "vm/load-probe.sh"))
    return r.out.replace("\r", "") if r.ok else ""


def value(probe, key):
    got = [l[len(key) + 1:] for l in probe.splitlines() if l.startswith(key + "=")]
    return got[-1] if got else ""


def desktop_readings(root, machine, g):
    """vm/desktop-probe.sh's readings, the pyobjc `pin`, the `uninvited` windows and the `quiet` findings; None unanswered."""
    probe = desktop_probe(root, machine, g)
    if not probe:
        return None
    rd = {l.partition("=")[0]: l.partition("=")[2] for l in probe.splitlines() if "=" in l}
    windows = rd.get("windows", "")
    lib = bench(root, machine, READINGS, probe, RESTART, "" if windows in ("", "?") else windows)
    rd.update(pin=value(lib, "pin"), uninvited=value(lib, "unexpected").rstrip(";"),
              quiet="".join(l[4:] + "\n" for l in lib.splitlines() if l.startswith("row=")))
    return rd


def blockers(rd):
    return ([("a window nothing here put there: " + rd["uninvited"])] if rd.get("uninvited") else []) + [
        why for key, bad, why in (("securityagent", ("up",), "an authentication sheet (SecurityAgent) is up"),
                                  ("console_user", ("root", "", "?"), "nobody is logged in at the window, so there is no desktop"),
                                  ("screenlock", ("on",), "the screen lock is on, so the guest comes up asking for a password"))
        if rd.get(key, "") in bad]


def setup_assistant(g):
    """A count, not pgrep's status: tart exec failing says 1 as well."""
    r = g.run(["sh", "-c", "pgrep -f 'Setup Assistant.app/Contents/MacOS' | grep -c . || true"])
    n = r.out.replace("\r", "").strip() if r.ok else ""
    return "unreachable" if not n else "gone" if n == "0" else "up"


def unblock_desktop(root, g):
    """Over the Accessibility API, which answers off the console with SIP disabled: no preference stops the pane."""
    if setup_assistant(g) != "up":
        return True
    info("driving Setup Assistant off the screen over the Accessibility API")
    r = g.act_run(["/usr/bin/python3", "-"], input=tree(root, "vm/desktop-unblock.py"))
    sys.stderr.write((r.out + r.err).replace("\r", ""))
    return r.ok and setup_assistant(g) == "gone"


def admit(host, name, mine):
    """Virtualization.framework counts every VM on the host, the podman machine too, against one limit."""
    vm, env = host.vm, host.env
    running, most = vm.running_vms(), vm_max(env)
    if len(running) >= most:
        die("%d VM(s) are already running on this host:\n%s    Virtualization.framework permits %d and refuses the next one "
            "with\n    VZErrorDomain code 6, in that guest's run log and nowhere else. Free a slot\n    with 'wk stop <name>', "
            "or with 'podman machine stop %s' -- that machine\n    carries the container workspaces, which survive it being down."
            % (len(running), "".join("      %s\n" % n for n in running), most, vm.podman_machine()))
    memory_budget(host, name, mine)
    host_disk(host)


def memory_budget(host, name, mine):
    """Everything holding memory holds all of it, busy or not, so this refuses rather than letting a link find out."""
    from wk.resources import Resources
    vm, env = host.vm, host.env
    res = Resources(host.machine, env, "macos")
    budget, guests, pod = res.envelope_mem_mb(), vm.committed_mem_mb(name), vm.podman_mem_mb()
    if mine + pod + guests <= budget:
        return
    # An idle podman machine holds the whole envelope; `wk` starts it again on the next container command.
    if pod and vm.podman_containers() == 0 and mine + guests <= budget:
        info("stopping the idle podman machine to free %dMB for '%s'" % (pod, name))
        host.machine.act_run(["podman", "machine", "stop", vm.podman_machine()])
        pod = vm.podman_mem_mb()
        if not pod:
            return
        warn("the podman machine did not stop; '%s' may not fit" % name)
    if mine + pod + guests <= budget:
        return
    if vm_share(env):
        warn("'%s' (%dMB) on top of %dMB podman + %dMB of running guests exceeds the %dMB envelope -- continuing because "
             "WK_VM_SHARE is set" % (name, mine, pod, guests, budget))
        return
    spare = budget - pod - guests
    advice = ("      WK_VM_MEM_MB=%d, then retry\n          run it in the %dMB that is actually free" % (spare, spare)
              if spare >= 4096 else "      (only %dMB is unspoken for, which is not enough to build in --\n       freeing one "
              "of the above is the realistic option)" % spare)
    rows = ("      %-26s %6d MB   running\n" % ("podman machine '%s'" % vm.podman_machine(), pod) if pod else "") \
        + ("      %-26s %6d MB   running\n" % ("other macOS guest(s)", guests) if guests else "") \
        + "      %-26s %6d MB   requested\n" % ("macOS VM '%s'" % name, mine) \
        + "      %-26s %6d MB   (%d MB total, %d MB kept for the desktop)\n" % ("host envelope", budget, res.host_mem_mb(),
                                                                               res.reserve_mb())
    die("not enough memory to start '%s'.\n%s\n      podman machine stop %s\n          free the whole envelope (workspaces "
        "and their state survive)\n      wk stop <name>\n          free a running guest\n%s\n      WK_VM_SHARE=1, then retry\n"
        "          proceed anyway" % (name, rows, vm.podman_machine(), advice))


def podman_admit(vm):
    """The podman machine holds its whole size from the moment it starts, so it is refused beside running guests it does not fit with."""
    from wk.resources import Resources
    from wk.places import podman_vm
    name = Store(vm.env).podman_machine()
    pod = int(((podman_vm(vm.machine, name) or {}).get("Resources") or {}).get("Memory") or 0)
    guests = vm.committed_mem_mb(None)
    budget = Resources(vm.machine, vm.env, "macos").envelope_mem_mb()
    if not guests or pod + guests <= budget:
        return
    if vm_share(vm.env):
        warn("the podman machine '%s' (%dMB) on top of %dMB of running guests exceeds the %dMB envelope -- starting it "
             "because WK_VM_SHARE is set" % (name, pod, guests, budget))
        return
    die("not starting the podman machine '%s' (%dMB): %dMB of macOS guests are running, and the\n"
        "    host envelope is %dMB.\n      wk stop <name>\n          free a running guest ('wk ls' names them)\n"
        "      WK_VM_SHARE=1, then retry\n          start it anyway" % (name, pod, guests, budget))


def host_disk(host):
    """A guest's disk is sparse: every byte it writes comes from here, and running out fails a build as an I/O error."""
    from wk.resources import Budget
    free = Budget(host.machine, host.env).free_gb("/")
    if free is None:
        warn("cannot tell how much is free on the host, so its disk is not checked")
        return
    if free < HOST_FREE_MIN_GB:
        die("only %d GB free on the host.\n    A macOS guest believes it has a %d GB disk, but every byte it writes has to "
            "come\n    from here, and a build that runs out fails as an I/O error naming nothing useful.\n\n"
            "      wk ls                        what exists\n      wk rm <name>                 reclaim a workspace\n"
            "      tart prune --space-budget 0  drop the OCI image cache" % (free, vm_disk_gb(host.env)))
    if free < HOST_FREE_WARN_GB:
        warn("%d GB free on the host -- a Release build tree is ~39 GB and a Debug one ~78 GB, so this may not be enough "
             "to finish" % free)


def boot(host, ws, wait=BOOT_WAIT):
    """Windowed, from the .app's own binary: outside the bundle tart loses its virtualization entitlement."""
    vm, m = host.vm, host.machine
    m.mkdir(host.dir)
    runlog = host.path(ws + ".run.log")
    if vm.vm_state(ws) != "running":
        flags = host.softnet_flags()
        if flags:
            m.remove(host.path(ws + ".unfiltered"))
        else:
            m.write(host.path(ws + ".unfiltered"), "")
        path = "%s:%s" % (os.path.dirname(host.softnet()), host.env.get("PATH") or os.environ.get("PATH", ""))
        m.remove(runlog)
        m.spawn(["env", "PATH=" + path, vm.tart_or_die(), "run", *flags,
                 "--dir=%s:%s:ro,tag=%s" % (vm.mirror_share, vm.store.mirror_parent(), vm.mirror_tag), vm.vm(ws)], runlog)
        info("booting %s (log: %s)" % (vm.vm(ws), runlog))
        if act.dry_run():
            return ""   # a guest this run did not boot has no address, nor anything to converge
    # The dhcp resolver works behind Softnet; the arp one does not.
    r = m.run([vm.tart_or_die(), "ip", vm.vm(ws), "--wait", str(wait)], timeout=wait + 30)
    ip = r.out.strip() if r.ok else ""
    if not ip:
        die("%s did not come up within %ds. Its run log says:\n%s" % (vm.vm(ws), wait, runlog_tail(m, runlog)))
    host.start_proxy()
    g = vm.guest_of(vm.vm(ws))
    if not host.clock.wait_until(lambda: g.run(["true"]).ok, 120, 2):
        die("%s is up at %s but its tart guest agent never answered. Its run log says:\n%s" % (vm.vm(ws), ip, runlog_tail(m, runlog)))
    return ip


def start(vm, ws, clock=None):
    host = Host(vm, clock)
    with host.lock().held("guest-" + ws):
        state = vm.vm_state(ws)
        if state == "absent":
            die(no_such_workspace(ws))
        if state == "running":
            ip = vm.ip(ws)
            if not ip:
                die("'%s' is running but tart gives it no address yet; 'wk start %s' again in a moment" % (ws, ws))
            host.start_proxy()
        else:
            admit(host, vm.vm(ws), vm.mem_mb(ws))
            ip = boot(host, ws)
        if ip:
            Guest(host, ws, vm.guest_of(vm.vm(ws))).converge()
    login_note(vm.env)
    return ip


def stop(vm, ws, clock=None):
    """The forwards first: one left holding a socket in a guest that is gone is a process nothing would reap."""
    host = Host(vm, clock)
    with host.lock().held("guest-" + ws):
        for what in FORWARDS:
            host.forward_stop(ws, what)
        if vm.vm_state(ws) != "running":
            info("%s is not running" % ws)
            return True
        r = host.machine.act_run([vm.tart_or_die(), "stop", vm.vm(ws)])
        if not r.ok:
            warn("tart stop %s failed (exit %d): %s" % (vm.vm(ws), r.rc, (r.err or r.out).strip()))
            return False
    info("stopped %s" % ws)
    return True


def _vm(root, machine, env):
    from wk import places
    return places.Registry(root, env=os.environ if env is None else env, machine=machine).load("vm")


def credentials_converge(root, env, machine):
    vm = _vm(root, machine, env)
    return not Store(vm.env).macos_host or Host(vm).credentials_converge()


def inject_host(root, env):
    from wk import places
    return Host(places.Registry(root, env, Local()).load("vm"))
