"""A macOS guest's start, stop and convergence, and the host daemons every guest shares: the egress proxy, the
credential injector behind it, and the ssh-agent a guest's push reaches. A pidfile is a lock, never a record."""

import os
import signal
import sys
import time

from wk import act, record, secrets, shell, tools
from wk.act import Refused, debug, die, info, warn
from wk.clock import Clock
from wk.lock import Lock

SUBNET = "192.168.2"   # Softnet's own network, not vmnet's 192.168.64
PROXY_PORT = "3128"
SOFTNET = "/usr/local/bin/softnet"
CLOCK_SKEW = "30"      # not zero: the reading is taken over ssh, so a round trip is in every compare
BOOT_WAIT = 180
FORWARD = "agent-forward"

# An `nc -z -U` answers 1 for a served socket on macOS, so the connect is made in python.
SOCKET_ANSWERS = """import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(2)
try:
    s.connect(sys.argv[1])
except OSError:
    sys.exit(1)
"""

# `tart clone` hands a clone the base's clock, and NTP is UDP, which the CONNECT proxy cannot carry.
CLOCK = """set -u
skew=$(( WK_NOW_EPOCH - $(date -u +%s) ))
[ "$skew" -ge 0 ] || skew=$(( - skew ))
[ "$skew" -gt "$WK_SKEW" ] || exit 0
sudo -n date -u "$WK_NOW_SET" >/dev/null || exit 1
echo "$skew"
"""

# WebKit's network process reads no http_proxy, so the system proxy is set too. The CA bundle is the system's
# plus the injector's: these variables replace the trust store outright.
EGRESS = """set -u
addr=$WK_ADDR port=$WK_PORT ghuser=$WK_GHUSER bzuser=$WK_BZUSER
if [ -z "$addr" ]; then
    rm -f "$HOME/.wk-egress"
else
    cat > "$HOME/.wk-egress" <<WKEGRESS
# wk: written by lib/wk/guest.py on every start; sourced by every shell
# (vm/shell-rc.sh). Softnet denies everything but this address.
export http_proxy=http://$addr:$port
export https_proxy=http://$addr:$port
export HTTP_PROXY=http://$addr:$port
export HTTPS_PROXY=http://$addr:$port
export no_proxy=localhost,127.0.0.1,::1
export NO_PROXY=localhost,127.0.0.1,::1
export PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
WKEGRESS
fi
if grep -q 'BEGIN CERTIFICATE' /tmp/.wk-github-ca.new 2>/dev/null; then
    mv /tmp/.wk-github-ca.new "$HOME/.wk-github-ca.pem"
    cat /etc/ssl/cert.pem "$HOME/.wk-github-ca.pem" > "$HOME/.wk-ca-bundle.pem"
    cat >> "$HOME/.wk-egress" <<WKCAENV
export REQUESTS_CA_BUNDLE=$HOME/.wk-ca-bundle.pem
export CURL_CA_BUNDLE=$HOME/.wk-ca-bundle.pem
export GIT_SSL_CAINFO=$HOME/.wk-ca-bundle.pem
export GITHUB_COM_USERNAME=$ghuser
export GITHUB_COM_TOKEN=wk-injects-this
export SSL_CERT_FILE=$HOME/.wk-ca-bundle.pem
export GH_TOKEN=wk-injects-this
WKCAENV
    [ -z "$bzuser" ] || cat >> "$HOME/.wk-egress" <<WKBZENV
export BUGS_WEBKIT_ORG_USERNAME=$bzuser
export BUGS_WEBKIT_ORG_PASSWORD=wk-injects-this
WKBZENV
else
    rm -f /tmp/.wk-github-ca.new "$HOME/.wk-github-ca.pem" "$HOME/.wk-ca-bundle.pem"
fi
dev=$(route -n get default 2>/dev/null | awk '/interface:/{print $2}')
[ -n "$dev" ] || { echo "no default route in the guest" >&2; exit 1; }
svc=$(networksetup -listnetworkserviceorder | awk -v d="$dev" '
    /^\\([0-9]+\\)/ { name = substr($0, index($0, ") ") + 2) }
    index($0, "Device: " d ")") { print name; exit }')
[ -n "$svc" ] || { echo "no network service owns $dev" >&2; exit 1; }
read_state() {
    networksetup -getsecurewebproxy "$svc" | awk '
        /^Enabled:/ { e = $2 } /^Server:/ { s = $2 } /^Port:/ { p = $2 }
        END { print e ":" s ":" p }'
}
if [ -z "$addr" ]; then
    [ "$(read_state)" = "No::0" ] && exit 0
    sudo -n networksetup -setwebproxystate "$svc" off &&
    sudo -n networksetup -setsecurewebproxystate "$svc" off
    exit
fi
[ "$(read_state)" = "Yes:$addr:$port" ] && exit 0
sudo -n networksetup -setwebproxy "$svc" "$addr" "$port" &&
sudo -n networksetup -setsecurewebproxy "$svc" "$addr" "$port" &&
sudo -n networksetup -setproxybypassdomains "$svc" localhost 127.0.0.1
"""

DEPLOY_HEADER = "# wk: written by lib/wk/guest.py on every start. Whether the agent these name\n# holds a key at all is 'wk push'.\n"


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
        return bool(self.env.get("WK_VM_UNFILTERED"))

    def port(self):
        return self.env.get("WK_VM_PROXY_PORT") or PROXY_PORT

    def softnet(self):
        return self.env.get("WK_SOFTNET_BIN") or SOFTNET

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

    def listener(self, where):
        pids = [int(p) for p in self.machine.run(["lsof", "-t", *where]).out.split() if p.isdigit()]
        return pids[0] if pids else None

    def restart_if_stale(self, pidfile, what, where):
        """One older than its source is stopped, and so is one serving `where` that no pidfile names (a kill lost it)."""
        pid = self.daemon_pid(pidfile)
        src = os.path.join(self.root, "container", "proxy")
        if pid is not None:
            if not self.machine.run(["find", src, "-name", "*.py", "-newer", pidfile]).out.strip():
                return
            why = "container/proxy changed since it started"
        else:
            pid = self.listener(where)
            if pid is None:
                return
            why = "no pidfile names it"
        info("restarting the %s: %s" % (what, why))
        self.machine.kill(pid)
        if not act.dry_run() and not self.clock.wait_until(lambda: not self.machine.alive(pid), 5, 0.25):
            self.machine.kill(pid, signal.SIGKILL)
        self.machine.remove(pidfile)

    def proxy_where(self):
        return ["-nP", "-iTCP@%s:%s" % (self.proxy_addr(), self.port()), "-sTCP:LISTEN"]

    def proxy_running(self):
        return self.daemon_pid(self.path("proxy.pid")) is not None

    def start_proxy(self):
        """The injector first, ahead of the liveness check, so its read token converges on every start."""
        if self.unfiltered():
            return True
        with self.lock().held("vm-daemons"):
            return self._start_proxy()

    def _start_proxy(self):
        self.start_inject()
        pidfile, log = self.path("proxy.pid"), self.path("proxy.log")
        self.restart_if_stale(pidfile, "egress proxy", self.proxy_where())
        if self.proxy_running():
            debug("host proxy already running")
            return True
        self.machine.mkdir(self.dir)
        addr = self.proxy_addr()
        if not self.clock.wait_until(lambda: ("inet %s " % addr) in self.machine.run(["ifconfig"]).out, 15, 0.5):
            warn("the guest bridge never got address %s; not starting the proxy" % addr)
            return False
        pid = self.spawn(["env", "WK_PROXY_UNIX=0", "WK_PROXY_TCP=%s:%s" % (addr, self.port()),
                          "WK_STORE=" + self.vm.store.root(), "WK_INJECT_SOCK=" + self.path("github-inject.sock"),
                          "/usr/bin/python3", os.path.join(self.root, "container", "proxy", "wk-proxy.py")], log, pidfile)
        if act.dry_run():
            return True

        def said():
            try:
                return ("listening on " + addr) in self.machine.read(log)
            except OSError:
                return False
        self.clock.wait_until(lambda: said() or not self.machine.alive(pid), 10, 0.5)
        if said():
            info("egress proxy on %s:%s" % (addr, self.port()))
            return True
        warn("the host egress proxy did not start; the guest will have no egress at all\n"
             "  (Softnet denies everything except the proxy address). See %s" % log)
        return False

    def inject_running(self):
        sock = self.path("github-inject.sock")
        return self.machine.exists(sock) and self.machine.run(["/usr/bin/python3", "-c", SOCKET_ANSWERS, sock]).ok

    def pat_converge(self):
        """The one writer of the read token this injector serves every guest: each start and each `wk key set`."""
        self.machine.mkdir(self.dir)
        if self.secrets.cred_sync(self.path("read-github-pat"), "github-pat"):
            return True
        warn("could not converge %s; a read from a guest answers 401" % self.path("read-github-pat"))
        return False

    def start_inject(self):
        self.machine.mkdir(self.dir)
        self.pat_converge()
        pidfile, log, sock = self.path("github-inject.pid"), self.path("github-inject.log"), self.path("github-inject.sock")
        self.restart_if_stale(pidfile, "GitHub API injector", [sock])
        if self.inject_running():
            return True
        self.spawn(["env", "WK_INJECT_SOCK=" + sock, "WK_INJECT_DIR=" + self.path("github-inject"),
                    "WK_INJECT_CA_OUT=" + self.path("wk-github-ca.pem"), "WK_INJECT_PAT=" + self.path("push-github-pat"),
                    "WK_INJECT_READ_PAT=" + self.path("read-github-pat"),
                    "WK_INJECT_BUGZILLA_KEY=" + self.path("push-bugzilla-api-key"),
                    "/usr/bin/python3", os.path.join(self.root, "container", "proxy", "github-inject.py")], log, pidfile)
        if act.dry_run() or self.clock.wait_until(self.inject_running, 10, 0.25):
            info("GitHub API injector on %s" % sock)
            return True
        warn("the GitHub API injector did not start, so 'git-webkit pr' in a guest\n  will fail; see %s" % log)
        return False

    def agent_sock(self):
        return self.path("ssh-agent.sock")

    def start_agent(self):
        """The agent holding the private halves runs here: a guest cannot see a unix socket across the hypervisor."""
        with self.lock().held("vm-daemons"):
            return self._start_agent()

    def _start_agent(self):
        # One that answers is adopted, not replaced, which would drop the keys `wk push on` loaded.
        sock, pidfile = self.agent_sock(), self.path("ssh-agent.pid")
        if self.secrets.agent_answers(sock):
            pid = None if self.daemon_pid(pidfile) else self.listener([sock])
            if pid is not None:
                self.machine.write(pidfile, "%d\n" % pid)
            return True
        self.machine.mkdir(self.dir)
        self.machine.remove(sock)   # ssh-agent refuses to bind a path that exists
        self.spawn(["/usr/bin/ssh-agent", "-D", "-a", sock], self.path("ssh-agent.log"), pidfile)
        if act.dry_run() or self.clock.wait_until(lambda: self.secrets.agent_answers(sock), 4, 0.2):
            return True
        warn("the guests' ssh-agent did not start, so no guest can push;\n  see %s" % self.path("ssh-agent.log"))
        return False

    def records(self):
        return record.Records(self.vm.store.record_dir(), clock=self.clock, env=self.env, machine=self.machine)

    def lock(self):
        if self._lock is None:
            self._lock = Lock(self.vm.store, self.machine, self.clock)
        return self._lock

    def forward_start(self, ws, guest):
        with self.lock().held("vm-agent-forward-" + ws):
            recs = self.records()
            t = recs.find(FORWARD, ws)
            if t is not None and t.alive():
                return True
            if not guest.act_run(["rm", "-f", self.vm.agent_sock()]).ok:
                return False
            argv = ["ssh", *guest.opts, "-N", "-R", "%s:%s" % (self.vm.agent_sock(), self.agent_sock()), guest.dest]
            log = self.path(ws + ".agent-forward.log")
            if act.dry_run():
                self.machine.spawn(argv, log)
                return True
            # `wk push off` ends it whoever started it: it clears the agent, and a converge with none stops this.
            t = recs.begin(FORWARD, "here", ws, "wk push off", log, ["start forward", "verify"])
            t.step_named("start forward")
            t.pid(self.machine.spawn(argv, log))
            t.step_named("verify")
            self.clock.sleep(0.5)
            if t.alive():
                return True
            t.end("failed")
            return False

    def forward_stop(self, ws):
        with self.lock().held("vm-agent-forward-" + ws):
            t = self.records().find(FORWARD, ws)
            if t is None:
                return
            pid = t.field("pid")
            if pid.isdigit():
                self.machine.kill(int(pid))
            t.end("stopped")


# In order; a failed step is named and passed over, except the last, whose refusal is the start's.
STEPS = (
    ("guest_tools_push", warn, "wk-tools in {ws} is not this tree's commit; 'wk sync --tools' puts it there once it is committed"),
    ("write_marker", debug, "could not settle {ws}'s workspace marker"),
    ("write_shell_rc", warn, "could not wire {ws}'s shell; 'wk' will not be on PATH in there"),
    ("write_lldbinit", debug, "could not write .lldbinit in {ws}"),
    ("set_guest_clock", warn, "could not set {ws}'s clock; TLS in there will fail as CERT_NOT_YET_VALID"),
    ("set_guest_egress", warn, "could not set {ws}'s egress; nothing in there will reach the outside"),
    ("write_checkout", warn, "{ws}'s WebKit checkout is not wired and set up (above); 'wk sync {ws} --fix' once it is up"),
    ("install_claude_cli", warn, "could not install the Claude CLI in {ws}; 'wk ai claude {ws}' will not work there"),
    ("write_claude_config", warn, "could not link ~/.claude in {ws}; an agent in there would have no instructions"),
    ("write_agent_secrets", warn, "could not write the agent credentials into {ws}; an agent in there will ask you to log in"),
    ("write_deploy_keys", warn, "could not write {ws}'s ssh config and public key halves; a push from in there is refused ('wk push status')"),
    ("agent_converge_guest", warn, "could not converge {ws}'s ssh-agent forward; 'wk push status' says what it can reach"),
    ("settle_desktop", warn, "could not settle {ws}'s desktop; 'wk vm check {ws}' says what is in front of the window"),
    ("report_desktop", None, ""),
)


class Guest:

    def __init__(self, host, ws, ip):
        self.host, self.vm, self.ws, self.ip = host, host.vm, ws, ip
        self.m = host.vm.guest_at(ip)
        self.secrets = host.secrets

    def converge(self):
        for name, level, why in STEPS:
            if getattr(self, name)():
                continue
            if level is None:
                raise Refused(1)
            level(why.format(ws=self.ws))

    def _bash(self, fn):
        return shell.guest_step(self.host.root, fn, self.ws, self.ip, env=dict(self.host.env))

    def guest_tools_push(self):
        return tools.push(self.host.root, self.host.machine, self.m, self.vm.tools(self.ws), self.host.env)

    def write_marker(self):
        return self.vm.write_marker(self.ws, self.m)

    def write_shell_rc(self):
        return self._bash("_write_shell_rc")

    def write_lldbinit(self):
        return self._bash("_write_lldbinit")

    def write_checkout(self):
        return self._bash("_write_checkout")

    def install_claude_cli(self):
        return self._bash("_install_claude_cli")

    def write_claude_config(self):
        return self._bash("_write_claude_config")

    def settle_desktop(self):
        return self._bash("_settle_desktop")

    def report_desktop(self):
        return self._bash("_report_desktop")

    def set_guest_clock(self):
        """Idempotent by measurement: a guest within WK_VM_CLOCK_SKEW costs no sudo."""
        now = int(self.host.clock.now())
        r = self.m.act_run(["env", "WK_NOW_EPOCH=%d" % now, "WK_NOW_SET=" + time.strftime("%m%d%H%M%Y.%S", time.gmtime(now)),
                            "WK_SKEW=" + (self.host.env.get("WK_VM_CLOCK_SKEW") or CLOCK_SKEW), "bash", "-s"], input=CLOCK)
        if not r.ok:
            return False
        if r.out.strip():
            info("%s's clock was %ss out; set from this host" % (self.ws, r.out.strip()))
        return True

    def set_guest_egress(self):
        h = self.host
        addr = "" if h.unfiltered() else h.proxy_addr()
        ca = ""
        if addr:
            try:
                ca = h.machine.read(h.path("wk-github-ca.pem"))
            except OSError:
                ca = ""
        debug("guest egress in %s: %s" % (self.ws, addr or "off"))
        script = "cat > /tmp/.wk-github-ca.new <<'WKCA'\n%s\nWKCA\n" % ca.rstrip("\n") + EGRESS
        return self.m.act_run(["env", "WK_ADDR=" + addr, "WK_PORT=" + h.port(), "WK_GHUSER=" + self.secrets.github_user(),
                               "WK_BZUSER=" + (self.secrets.bugzilla_user() or ""), "bash", "-s"], input=script).ok

    def write_agent_secrets(self):
        """Rewritten every start, so a withdrawn one goes; a file row is never copied, only read on the share."""
        n = 0
        for name, _file, home_path, _var, kind, delivery in self.secrets.agent_secrets():
            if kind == "file":
                if not self.m.act_run(["sh", "-c", 'rm -f "$HOME/$1" && bash -lc \'test -d "$CLAUDE_SECURESTORAGE_CONFIG_DIR"\'',
                                       "sh", home_path]).ok:
                    warn("the %s share is not mounted in %s, so it has no claude.ai login:\n"
                         "    'wk vm stop %s', then 'wk vm start %s' boots it with the share"
                         % (self.vm.agent_rw_share, self.ws, self.ws, self.ws))
                continue
            here = self.secrets.cred_stored(name) if "vm" in delivery.split(",") else False
            if here is None:
                return False
            if not here:
                if not self.m.act_run(["sh", "-c", 'rm -f "$HOME/$1"', "sh", home_path]).ok:
                    return False
                continue
            value = secrets.first_line(self.secrets.cred_read(name))
            if not self.m.act_run(["sh", "-c", 'umask 077 && cat > "$HOME/$1"', "sh", home_path], input=value + "\n").ok:
                return False
            n += 1
        debug("agent credentials in %s: %d" % (self.ws, n))
        return True

    def write_deploy_keys(self):
        """Never a private half. Port 22 is reached by CONNECT through the one address Softnet allows."""
        proxy = "/usr/bin/nc -X connect -x %s:%s %%h %%p" % (self.host.proxy_addr(), self.host.port())
        forks = self.secrets.forks()
        cfg = DEPLOY_HEADER + secrets.alias_blocks(forks, self.vm.home() + "/.ssh", "id_", self.vm.agent_sock(), proxy)
        if not self.m.act_run(["sh", "-c", 'umask 077 && mkdir -p "$HOME/.ssh" && cat > "$HOME/.ssh/config"'], input=cfg).ok:
            return False
        n = 0
        for fork, _repo, _alias in forks:
            pub = (self.secrets.read(self.secrets.pub_path(fork)) or "").strip()
            idf = ".ssh/id_%s.pub" % fork
            if pub:
                ok = self.m.act_run(["sh", "-c", 'cat > "$HOME/$1"', "sh", idf], input=pub + "\n").ok
                n += 1
            else:
                ok = self.m.act_run(["sh", "-c", 'rm -f "$HOME/$1"', "sh", idf]).ok
            if not ok:
                return False
        debug("public deploy halves in %s: %d of %d" % (self.ws, n, len(forks)))
        return True

    def agent_converge_guest(self):
        if self.secrets.agent_list(self.host.agent_sock()):
            return self.host.forward_start(self.ws, self.m)
        self.host.forward_stop(self.ws)
        return self.m.act_run(["rm", "-f", self.vm.agent_sock()]).ok


def runlog_tail(machine, path):
    """The only place a `tart run` says why it died ("The number of VMs exceeds the system limit")."""
    try:
        lines = machine.read(path).splitlines()
    except OSError:
        lines = []
    if not lines:
        return "    nothing -- %s is empty" % path
    return "\n".join("      " + line for line in lines[-5:]) + "\n    (%s)" % path


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
        agent_rw = host.secrets.store.agent_rw_dir()
        host.secrets.ensure_dir(agent_rw, "0700")
        path = "%s:%s" % (os.path.dirname(host.softnet()), host.env.get("PATH") or os.environ.get("PATH", ""))
        m.remove(runlog)
        m.spawn(["env", "PATH=" + path, vm.tart_or_die(), "run", *flags, "--dir=%s:%s" % (vm.agent_rw_share, agent_rw),
                 "--dir=%s:%s:ro" % (vm.mirror_share, os.path.dirname(vm.store.mirror())), vm.vm(ws)], runlog)
        info("booting %s (log: %s)" % (vm.vm(ws), runlog))
    # The dhcp resolver works behind Softnet; the arp one does not.
    r = m.run([vm.tart_or_die(), "ip", vm.vm(ws), "--wait", str(wait)], timeout=wait + 30)
    ip = r.out.strip() if r.ok else ""
    if not ip:
        die("%s did not come up within %ds. Its run log says:\n%s" % (vm.vm(ws), wait, runlog_tail(m, runlog)))
    host.start_proxy()
    guest = vm.guest_at(ip)
    if not host.clock.wait_until(lambda: guest.run(["true"]).ok, 120, 2):
        die("%s is up at %s but ssh never answered. Its run log says:\n%s" % (vm.vm(ws), ip, runlog_tail(m, runlog)))
    return ip


def start(vm, ws, clock=None):
    host = Host(vm, clock)
    with host.lock().held("guest-" + ws):
        state = vm.vm_state(ws)
        if state == "absent":
            die("no such workspace: %s" % ws)
        if state == "running":
            ip = vm.ip(ws)
            if not ip:
                die("'%s' is running but tart gives it no address yet; 'wk vm start %s' again in a moment" % (ws, ws))
            host.start_proxy()
        else:
            rc = shell.guest_admit(vm.root, ws, env=dict(vm.env))
            if rc:
                raise Refused(rc)
            ip = boot(host, ws)
        Guest(host, ws, ip).converge()
    shell.vm_login_note(vm.root, env=dict(vm.env))
    return ip


def stop(vm, ws, clock=None):
    """The forward first: one left holding a socket in a guest that is gone is a process nothing would reap."""
    host = Host(vm, clock)
    with host.lock().held("guest-" + ws):
        host.forward_stop(ws)
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
    from wk import targets   # targets drives a guest through this module
    return targets.Registry(root, env=os.environ if env is None else env, machine=machine).load("vm")


def _guests(root, machine, env):
    vm = _vm(root, machine, env)
    return vm if vm.vm_store() else None


def pat_converge(root, env, machine):
    vm = _guests(root, machine, env)
    return vm is None or Host(vm).pat_converge()


def vm_push_keys_converge(root, machine, action, env=None):
    """`wk push on|off` for the guests; each running guest that did not converge is named, and fails it."""
    vm = _guests(root, machine, env)
    if vm is None:
        return True
    host = Host(vm)
    sec, sock, ok = host.secrets, host.agent_sock(), True
    creds = ((host.path("push-github-pat"), "github-pat"), (host.path("push-bugzilla-api-key"), "bugzilla-api-key"))
    if action == "on":
        if not host.start_agent():
            return False
        ok = all(state != "FAILED" for _, state in sec.agent_load(sock))
        for path, name in creds:
            if not sec.cred_write(path, name):
                sec.cred_clear(path)
    else:
        sec.agent_clear(sock)
        for path, _ in creds:
            sec.cred_clear(path)
        left = len(sec.agent_list(sock))
        if left:
            sys.stderr.write("  %-24s still holds %d identity/identities at %s\n" % ("the guests' agent", left, sock))
            ok = False
    for g in vm.workspaces():
        if vm.info(g) != "running":
            continue
        ip = vm.ip(g)
        if not ip:
            sys.stderr.write("  %-24s running, no address yet -- not converged\n" % g)
            ok = False
            continue
        guest = Guest(host, g, ip)
        if not guest.write_deploy_keys():
            sys.stderr.write("  %-24s FAILED -- its ssh config was not rewritten\n" % g)
            ok = False
        elif not guest.agent_converge_guest():
            sys.stderr.write("  %-24s FAILED -- it may still reach the agent\n" % g)
            ok = False
        else:
            sys.stderr.write("  %-24s %s\n" % (g, "reaches the agent on this host" if action == "on"
                                                   else "no agent socket -- a push in there is refused"))
    return ok


def vm_push_agent_keys(root, machine, env=None):
    vm = _guests(root, machine, env)
    if vm is None:
        return None
    host = Host(vm)
    return len(host.secrets.agent_list(host.agent_sock()))


def vm_push_keys_state(root, machine, env=None):
    """(guest, state, what it reaches) per guest; a stopped one is reported, never started."""
    vm = _guests(root, machine, env)
    if vm is None:
        return []
    host = Host(vm)
    n = len(host.secrets.agent_list(host.agent_sock()))
    rows = []
    for g in vm.workspaces():
        state = vm.info(g) or "unknown"
        ip = vm.ip(g) if state == "running" else None
        if not ip:
            rows.append((g, state, ""))
        elif n and vm.guest_at(ip).run(["test", "-S", vm.agent_sock()]).ok:
            rows.append((g, "running", "%d key(s) through the agent on this host" % n))
        else:
            rows.append((g, "running", ""))
    return rows


def main(argv, env=None):
    """cmd/vm's `start <ws>` (prints its address) and `stop <ws>`, and the base build's `clock <name> <ip>`."""
    env = os.environ if env is None else env
    verbs = {"start": 1, "stop": 1, "clock": 2}
    if not argv or verbs.get(argv[0]) != len(argv) - 1:
        sys.stderr.write("usage: python3 -m wk.guest start|stop <ws> | clock <name> <ip>\n")
        return 2
    vm = _vm(os.environ.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
             None, env)
    if argv[0] == "start":
        print(start(vm, argv[1]))
        return 0
    if argv[0] == "stop":
        return 0 if stop(vm, argv[1]) else 1
    return 0 if Guest(Host(vm), argv[1], argv[2]).set_guest_clock() else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Refused as e:
        sys.exit(e.status)
