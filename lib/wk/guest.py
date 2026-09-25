"""A macOS guest's start, stop and convergence, what its desktop and its load say about it, and the host daemons every
guest shares: the egress proxy, the credential injector behind it, and the ssh-agent a guest's push reaches. A pidfile
is a lock, never a record."""

import os
import re
import shlex
import signal
import sys
import time

from wk import act, git, secrets, shell, tools
from wk.act import Refused, debug, die, info, log, warn
from wk.clock import Clock
from wk.lock import Lock
from wk.store import Store

SUBNET = "192.168.2"   # Softnet's own network, not vmnet's 192.168.64
PROXY_PORT = "3128"
SOFTNET = "/usr/local/bin/softnet"
CLOCK_SKEW = "30"      # not zero: the reading is taken over ssh, so a round trip is in every compare
BOOT_WAIT = 180
FORWARD_WAIT = 4

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
PASSWORD = "admin"
DISPLAY = "1280x800"
BASE = "wk-base"
BASE_PROFILE = "macos-guest-base"
BASE_BUILD = "wk sysimage build " + BASE_PROFILE
RESTART = "wk stop <name> && wk start <name>"
REBUILD = BASE_BUILD + " --rebuild, then re-create this guest"
SHELLS_WARN, MEM_FREE_WARN_PCT, SWAP_WARN_MB = 12, 15, 1024   # twelve shells measured as nothing wrong; macOS pages below 15% free
HOST_FREE_WARN_GB, HOST_FREE_MIN_GB = 80, 25
DISK_GB = 320
SA_COUNT = "pgrep -f 'Setup Assistant.app/Contents/MacOS' | grep -c . || true"   # macOS pgrep has no -c
QUIET, WINDOWS, PYOBJC = "bench/mac-quiet-desktop.sh", "bench/mac-window-probe.sh", "bench/mac-pyobjc.sh"
LLDB_HEADER = "# wk: written by lib/wk/guest.py. See wk run --lldb, wk gui --lldb.\n"
CLAUDE_CONFIG = """[ -d "$1/claude" ] || exit 1
mkdir -p "$HOME/.claude"
for f in settings.json hooks CLAUDE.md skills; do ln -sfn "$1/claude/$f" "$HOME/.claude/$f"; done
"""

CHECKOUT = """set -u
git config --global --replace-all include.path "$WK_TOOLS/dotfiles/gitconfig"
if [ -d "$WK_SRC/.git" ]; then
    echo checkout=present
elif [ ! -d "$WK_MIRROR" ]; then
    echo "checkout=no-mirror: $WK_MIRROR is not there. The share is mounted at boot, so"
    echo "  'wk stop <name>', then 'wk start <name>' -- or the host has no mirror yet: wk sync"
    exit 1
elif git clone --quiet --shared --branch main "$WK_MIRROR" "$WK_SRC"; then
    echo checkout=cloned
else
    echo checkout=clone-failed; exit 1
fi
[ ! -r "$HOME/.wk-egress" ] || . "$HOME/.wk-egress"
"""

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

    def lock(self):
        if self._lock is None:
            self._lock = Lock(self.vm.store, self.machine, self.clock)
        return self._lock

    def forward_pidfile(self, ws):
        return self.path(ws + ".agent-forward.pid")

    def forward_start(self, ws, guest):
        """`wk push off` ends it whoever started it: it clears the agent, and a converge with none stops this."""
        with self.lock().held("vm-agent-forward-" + ws):
            pidfile = self.forward_pidfile(ws)
            if self.daemon_pid(pidfile) is not None:
                return True
            sock = self.vm.agent_sock()
            if not guest.act_run(["rm", "-f", sock]).ok:
                return False
            argv = ["ssh", *guest.opts, "-N", "-R", "%s:%s" % (sock, self.agent_sock()), guest.dest]
            log = self.path(ws + ".agent-forward.log")
            pid = self.spawn(argv, log, pidfile)
            if act.dry_run() or self.clock.wait_until(lambda: guest.run(["test", "-S", sock]).ok, FORWARD_WAIT, 0.2):
                return True
            self.machine.kill(pid)
            self.machine.remove(pidfile)
            warn("the agent forward into '%s' did not come up, so it cannot push;\n  see %s" % (ws, log))
            return False

    def forward_stop(self, ws):
        with self.lock().held("vm-agent-forward-" + ws):
            pidfile = self.forward_pidfile(ws)
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
    ("write_checkout", warn, "{ws}'s WebKit checkout is not wired and set up (above); 'wk sync {ws} --fix' once it is up"),
    ("install_claude_cli", warn, "could not install the Claude CLI in {ws}; 'wk ai claude {ws}' will not work there"),
    ("write_claude_config", warn, "could not link ~/.claude in {ws}; an agent in there would have no instructions"),
    ("write_agent_secrets", warn, "could not write the agent credentials into {ws}; an agent in there will ask you to log in"),
    ("write_deploy_keys", warn, "could not write {ws}'s ssh config and public key halves; a push from in there is refused ('wk push status')"),
    ("agent_converge_guest", warn, "could not converge {ws}'s ssh-agent forward; 'wk push status' says what it can reach"),
    ("settle_desktop", warn, "could not settle {ws}'s desktop; 'wk doctor {ws}' says what is in front of the window"),
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

    def _said(self, r):
        sys.stderr.write(r.err.replace("\r", ""))
        return r.ok

    def guest_tools_push(self):
        return tools.push(self.host.root, self.host.machine, self.m, self.vm.tools(self.ws), self.host.env)

    def write_marker(self):
        return self.vm.write_marker(self.ws, self.m)

    def write_shell_rc(self):
        return self._said(self.m.act_run(["bash", "-s", self.vm.tools(self.ws), self.vm.agent_rw_dir()],
                                         input=tree(self.host.root, "vm/shell-rc.sh")))

    def write_lldbinit(self):
        text = LLDB_HEADER + "command script import %s/Tools/lldb/lldb_webkit.py\n" % self.vm.src(self.ws) \
            + tree(self.host.root, "dotfiles/lldbinit")
        return self._said(self.m.act_run(["sh", "-c", 'cat > "$HOME/.lldbinit"'], input=text))

    def write_checkout(self):
        src, mirror, forks = self.vm.src(self.ws), self.vm.mirror_dir(), self.secrets.forks()
        script = CHECKOUT + git.wiring_script(src, mirror, forks, git.mirror_branches(self.host.env)) \
            + git.gitwebkit_setup_script(src, forks)
        t0 = self.host.clock.now()
        r = self.m.act_run(["env", "WK_SRC=" + src, "WK_MIRROR=" + mirror, "WK_TOOLS=" + self.vm.tools(self.ws), "bash", "-s"],
                           input=script)
        out = (r.out + r.err).replace("\r", "")
        if "checkout=cloned" in out:
            info("%s's WebKit checkout made from its mirror in %ds" % (self.ws, self.host.clock.now() - t0))
        if "setup=ok" in out:
            info("git-webkit is set up in %s" % self.ws)
        if not r.ok:
            sys.stderr.write("".join("    %s\n" % l for l in out.splitlines()[-5:]))
        return r.ok

    def install_claude_cli(self):
        script = self.host.machine.run(shell.argv(self.host.root, "wk_claude_cli_script"))
        if not script.ok:
            return False
        r = self.m.act_run(["sh", "-s"], input=script.out)
        if r.ok and "claude=installed" in r.out:
            info("Claude CLI installed in %s" % self.ws)
        return r.ok

    def write_claude_config(self):
        return self._said(self.m.act_run(["sh", "-c", CLAUDE_CONFIG, "sh", self.vm.tools(self.ws)]))

    def settle_desktop(self):
        """The password is the script's own first line: as an argument it would be in `ps` on both machines."""
        script = "WK_VM_PASSWORD=%s\n" % shlex.quote(password(self.host.env)) + quiet_script(self.host.root, self.host.machine) \
            + tree(self.host.root, PYOBJC, "vm/desktop.sh")
        return self._said(self.m.act_run(["bash", "-s"], input=script))

    def report_desktop(self):
        probe = desktop_probe(self.host.root, self.host.machine, self.m)
        if not probe:
            return True
        info("the guest's desktop, as it is now ('wk doctor %s' asks again):" % self.ws)
        d = Desktop(self.host.root, self.host.machine, probe)
        render(d.findings().replace("<name>", self.ws))
        blocked = d.blockers()
        if not blocked:
            return True
        lines = "".join("      %s\n" % b for b in blocked)
        if self.host.env.get("WK_VM_FORCE"):
            warn("WK_VM_FORCE=1 -- '%s' is handed over with this in front of its desktop:\n%s" % (self.ws, lines))
            return True
        die("'%s' is not usable: something is in front of its desktop.\n%s    A clone cannot clear this itself -- Setup "
            "Assistant's account pane needs\n    Apple's servers, and a guest's egress filter refuses them. It is cleared\n"
            "    once, on the base every guest is cloned from:\n      %s --rebuild     hours; then re-create this guest\n"
            "    WK_VM_FORCE=1 hands the guest over anyway." % (self.ws, lines, BASE_BUILD))

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
                         "    'wk stop %s', then 'wk start %s' boots it with the share"
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


def display(env):
    return env.get("WK_VM_DISPLAY") or DISPLAY


def base_name(env):
    """The golden base every guest is cloned from."""
    return env.get("WK_VM_BASE") or BASE


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


def password(env):
    return env.get("WK_VM_PASSWORD") or PASSWORD


def login_note(env):
    log("  the guest's own window logs in as %s / %s" % (env.get("WK_VM_USER") or "admin", password(env)))
    log("  (wk itself uses an ssh key; this is for a prompt on the screen)")
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


def row(state, what, remedy=""):
    return "%s\t%s\t%s\n" % (state, what, remedy)


def render(text):
    from wk import doctor
    doctor.Report(sys.stderr).rows(doctor.findings(text))


class Desktop:

    def __init__(self, root, machine, probe):
        self.probe = probe
        windows = self.v("windows")
        lib = bench(root, machine, READINGS, probe, RESTART, "" if windows in ("", "?") else windows)
        self.pin = value(lib, "pin")
        self.uninvited = value(lib, "unexpected").rstrip(";")
        self.quiet = "".join(l[4:] + "\n" for l in lib.splitlines() if l.startswith("row="))

    def v(self, key):
        return value(self.probe, key)

    def blockers(self):
        out = ["a window nothing here put there: " + self.uninvited] if self.uninvited else []
        if self.v("securityagent") == "up":
            out.append("an authentication sheet (SecurityAgent) is up")
        if self.v("console_user") in ("root", "", "?"):
            out.append("nobody is logged in at the window, so there is no desktop")
        if self.v("screenlock") == "on":
            out.append("the screen lock is on, so the guest comes up asking for a password")
        return out

    def findings(self):
        v, out = self.v, []
        cu = v("console_user")
        out.append(row("wrong", "nobody is logged in at the window (console user '%s') -- there is no desktop to draw on" % cu,
                       RESTART + "  (auto-login logs it back in; the account is a base setting)")
                   if cu in ("root", "", "?") else row("ok", "logged in at the window as " + cu))
        py = v("pyobjc")
        if py and py == self.pin:
            out.append(row("ok", "pyobjc %s: a browser can be driven and held in front here" % py))
        elif py in ("", "?"):
            out.append(row("wrong", "no pyobjc: run-benchmark cannot size the screen and nothing can keep MiniBrowser frontmost, "
                           "so a benchmark here measures a throttled browser", RESTART + "  (the settle installs it)"))
        else:
            out.append(row("wrong", "pyobjc here is %s and this fleet measures with %s" % (py, self.pin), RESTART))
        lock = v("screenlock")
        out.append(row("ok", "screen lock off") if lock == "off" else
                   row("wrong", "the screen lock is on, so this guest comes up asking for a password", REBUILD
                       + "  -- vm/desktop.sh leaves the lock alone unless the account's password is the one wk set") if lock == "on"
                   else row("note", "screen lock could not be read (sysadminctl needs passwordless sudo in there)"))
        out.append(self.quiet)
        pending = v("setupassistant_pending")
        out.append(row("wrong", "Setup Assistant will put a modal pane on the desktop: " + pending, REBUILD) if pending
                   else row("ok", "Setup Assistant already clicked through"))
        out += self.updates()
        out.append(self.screen())
        out.append(row("note", "%s has the focus" % v("frontapp"),
                       "an unfocused window is a throttled window: a benchmark measured behind one measures the throttle"))
        # SecurityAgent is up for a moment at every login, and this is read seconds after one.
        out.append(row("ok", "no authentication sheet is up") if v("securityagent") == "down" else
                   row("note", "SecurityAgent is up, which the frontmost-application reading above cannot see. Every login has "
                       "one for a moment", "wk doctor <name>  -- still up means something in there is waiting for a password"))
        out.append(row("note", "the guest's own window logs in as %s" % v("user"),
                       "wk itself uses an ssh key; 'wk start' and 'wk enter' state that account's password"))
        return "".join(out)

    def updates(self):
        v, out = self.v, []
        auto = v("update_autoinstall_system")
        out.append(row("ok", "macOS updates will not install themselves") if auto == "0" else
                   row("note", "the guest did not answer about Software Update, so whether it installs one under a build is "
                       "unknown", RESTART + "  -- a start re-runs this probe") if auto == "" else
                   row("wrong", "macOS updates are set to install themselves in there (AutomaticallyInstallMacOSUpdates=%s), "
                       "which reboots the guest -- mid-build, if that is when one lands" % auto, REBUILD))
        dl = v("update_download_system")
        if dl == "0":
            out.append(row("ok", "no update downloads itself in there"))
        elif dl:
            out.append(row("wrong", "updates download themselves in there (AutomaticDownload=%s), which takes the host's disk "
                           "and the guest's bandwidth mid-build" % dl, REBUILD))
        check, down = v("update_check"), v("update_download")
        out.append(row("ok", "Software Update offers off in the login account too") if (check, down) == ("0", "0") else
                   row("note", "the account's own Software Update settings read check=%s, download=%s -- what System Settings "
                       "shows at that window, not what softwareupdated obeys" % (check, down), REBUILD))
        # Buddy shows its "what is new in macOS" pane whenever these keys do not name the running system.
        os_v, seen = v("os_product"), v("setupassistant_seen_product")
        out.append(row("note", "the guest did not say which macOS it runs, so Setup Assistant's 'what is new in macOS' pane "
                       "cannot be judged from here", RESTART + "  -- a start re-runs this probe") if os_v in ("", "?") else
                   row("ok", "Setup Assistant has already seen macOS " + os_v) if seen == os_v else
                   row("wrong", "Setup Assistant will show its 'what is new in macOS' pane (it last saw %s, this guest runs %s)"
                       % (seen, os_v), REBUILD))
        return out

    def screen(self):
        w = self.v("windows")
        if w in ("", "?"):
            return row("note", "the window server was not asked what is on that screen (no compiler in there to build the "
                       "probe with)", RESTART + "  -- a start builds and runs it again")
        if self.uninvited:
            return row("wrong", "on that screen right now, and nothing wk runs put it there: " + self.uninvited,
                       "it comes back on every boot and no setting a guest can write stops it: clear it on the base, once -- "
                       + REBUILD)
        return row("ok", "nothing on that screen but %d window(s) wk put there" % sum(":0:" in e for e in w.split(";")))


def load_findings(probe, env):
    shells_warn = int(env.get("WK_VM_SHELLS_WARN") or SHELLS_WARN)
    free_warn = int(env.get("WK_VM_MEM_FREE_WARN_PCT") or MEM_FREE_WARN_PCT)
    swap_warn = int(env.get("WK_VM_SWAP_WARN_MB") or SWAP_WARN_MB)
    procs, vals, groups, out = [], {}, {}, []
    for line in probe.splitlines():
        key, _, val = line.partition("=")
        if key == "proc":
            rss, _, comm = val.strip().partition(" ")
            if rss.isdigit():
                procs.append((int(rss), comm.strip()))
        elif key:
            vals[key] = val.strip()
    for rss, comm in procs:
        fam = next((f for f, pat in LOAD_FAMILIES if re.search(pat, comm)), None)
        if fam:
            n, kb = groups.get(fam, (0, 0))
            groups[fam] = (n + 1, kb + rss)
    shells, shell_kb = groups.get("shell", (0, 0))
    if shells > shells_warn:
        holders = ["%d %s process(es)" % (groups[f][0], f) for f in ("editor remote server", "agent", "ssh session") if f in groups]
        out.append(row("wrong", "%d shells are resident in there, holding %d MB%s" % (
            shells, shell_kb // 1024, (" -- alongside " + ", ".join(holders)) if holders else ""),
            RESTART + " takes them all with it; closing the editor window does not, since its remote server outlives it"))
    else:
        out.append(row("ok", "%d shells resident (%d MB)" % (shells, shell_kb // 1024)))
    n, kb = groups.get("editor remote server", (0, 0))
    if n:
        out.append(row("note", "an editor remote server is running in there: %d process(es), %d MB" % (n, kb // 1024),
                       "it outlives the editor window, and every terminal pane in it leaves a shell behind; a guest restart "
                       "is what clears both"))
    n, kb = groups.get("agent", (0, 0))
    if n:
        out.append(row("note", "%d agent process(es) in there, %d MB" % (n, kb // 1024),
                       "each `wk ai claude` session in a guest is one of these"))
    free, total = vals.get("mem_free_pct", ""), vals.get("mem_total_mb", "?")
    if not free.isdigit():
        out.append(row("note", "memory pressure could not be read in there (memory_pressure said nothing)"))
    elif int(free) < free_warn:
        top = ", ".join("%s (%d MB)" % (c.rsplit("/", 1)[-1], r // 1024) for r, c in sorted(procs, reverse=True)[:3])
        out.append(row("wrong", "%s%% of the %s MB in that guest is free, and macOS calls that pressure: the biggest resident "
                       "processes are %s" % (free, total, top),
                       RESTART + "; a build in there is otherwise paging, and every number it produces is about the paging"))
    else:
        out.append(row("ok", "%s%% of the %s MB in that guest is free" % (free, total)))
    m = re.search(r"used = ([0-9.]+)M", vals.get("swapusage", ""))   # sysctl vm.swapusage, raw
    if m and float(m.group(1)) > swap_warn:
        out.append(row("note", "the guest is using %d MB of swap" % float(m.group(1)),
                       "it holds a fixed allocation, so this is the guest paging inside itself: a build here is slower "
                       "than its numbers say"))
    return "".join(out)


def check_rows(vm, ws):
    from wk import doctor
    from wk.sysimage import guestbase
    rows = doctor.findings(guestbase.Base(vm).findings())
    ip = vm.ip(ws)
    if not ip:
        return rows + [doctor.unk("'%s' is not running, so its desktop and its load cannot be read" % ws, "wk start %s" % ws)]
    g = vm.guest_at(ip)
    probe = desktop_probe(vm.root, vm.machine, g)
    rows += doctor.findings(Desktop(vm.root, vm.machine, probe).findings().replace("<name>", ws)) if probe else \
        [doctor.unk("'%s' did not answer the desktop probe" % ws, "wk doctor %s  -- again, once it is reachable" % ws)]
    load = load_probe(vm.root, g)
    rows += doctor.findings(load_findings(load, vm.env).replace("<name>", ws)) if load else \
        [doctor.unk("'%s' did not answer the load probe, so what is resident in there is unknown" % ws, "wk doctor %s" % ws)]
    return rows


def setup_assistant(g):
    r = g.run(["sh", "-c", SA_COUNT])
    n = r.out.replace("\r", "").strip() if r.ok else ""
    return "unreachable" if not n else "gone" if n == "0" else "up"


def unblock_desktop(root, g):
    """Driven over the Accessibility API, which answers a plain ssh session because the guest runs with SIP disabled:
    no preference the guest can write stops the pane (vm/desktop.sh)."""
    if setup_assistant(g) != "up":
        return True
    info("driving Setup Assistant off the screen over the Accessibility API")
    r = g.act_run(["/usr/bin/python3", "-"], input=tree(root, "vm/desktop-unblock.py"))
    sys.stderr.write((r.out + r.err).replace("\r", ""))
    return r.ok and setup_assistant(g) == "gone"


def running_rows(vm):
    return "".join("      %s\n" % n for n in vm.running_vms())


def admit(host, name, mine):
    """Virtualization.framework counts every VM on the host, the podman machine too, against one limit."""
    vm, env = host.vm, host.env
    running, most = vm.running_vms(), int(env.get("WK_VM_MAX") or 2)
    if len(running) >= most:
        die("%d VM(s) are already running on this host:\n%s    Virtualization.framework permits %d and refuses the next one "
            "with\n    VZErrorDomain code 6, in that guest's run log and nowhere else. Free a slot\n    with 'wk stop <name>', "
            "or with 'podman machine stop %s' -- that machine\n    carries the container workspaces, which survive it being down."
            % (len(running), running_rows(vm), most, vm.podman_machine()))
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
    if env.get("WK_VM_SHARE"):
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


def host_disk(host):
    """A guest's disk is sparse: every byte it writes comes from here, and running out fails a build as an I/O error."""
    from wk.resources import Budget
    free = Budget(host.machine, host.env).free_gb("/")
    if free is None:
        return
    if free < int(host.env.get("WK_HOST_FREE_MIN_GB") or HOST_FREE_MIN_GB):
        die("only %d GB free on the host.\n    A macOS guest believes it has a %d GB disk, but every byte it writes has to "
            "come\n    from here, and a build that runs out fails as an I/O error naming nothing useful.\n\n"
            "      wk ls                        what exists\n      wk rm <name>                 reclaim a workspace\n"
            "      tart prune --space-budget 0  drop the OCI image cache" % (free, int(host.env.get("WK_VM_DISK_GB") or DISK_GB)))
    if free < int(host.env.get("WK_HOST_FREE_WARN_GB") or HOST_FREE_WARN_GB):
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
                die("'%s' is running but tart gives it no address yet; 'wk start %s' again in a moment" % (ws, ws))
            host.start_proxy()
        else:
            admit(host, vm.vm(ws), vm.mem_mb(ws))
            ip = boot(host, ws)
        Guest(host, ws, ip).converge()
    login_note(vm.env)
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
    """The guests' injector serves every guest on a macOS host, wherever the vm target's store is."""
    vm = _vm(root, machine, env)
    return not Store(vm.env).macos_host or Host(vm).pat_converge()


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

