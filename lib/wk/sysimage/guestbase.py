"""The `guest` builder: the golden macOS base every guest is a `tart clone` of, sealed only on a clear screen. Its
marker records the hash of its inputs, and staleness is that hash recomputed on every read."""

import hashlib
import json
import os

from wk import act, guest, job, tools
from wk.act import debug, die, info, log, warn
from wk.clock import Clock
from wk.store import Store
from wk.sysimage import task

# macOS 26.6.2 with Xcode 27 beta 6. A Cirrus Labs `-xcode` tag names the Xcode, and the first Saturday of every month
# re-pushes it onto the newest macOS, so only a digest names one image.
IMAGE = "ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:f441eb487a18b4588c096adcff5eb48fddca550909e01c472580872b48c166b0"
INPUTS = ("vm/provision-base.sh", "vm/desktop.sh", "bench/mac-pyobjc.sh")
LOGIN_SETTLE = 45    # measured: Setup Assistant is up 4s after boot, and ssh answers before that
BOOT_WAIT = 300
PLOG, PRC = "/tmp/wk-base-provision.log", "/tmp/wk-base-provision.rc"
MODES = ("--refresh", "--rebuild", "--rm")
USAGE = "usage: wk sysimage build %s [--refresh|--rebuild|--rm] [--dry-run]"


def tart_home(env):
    return env.get("TART_HOME") or os.path.join(Store(env).home(), ".tart")


def image(env):
    return env.get("WK_VM_IMAGE") or IMAGE


def inputs_hash(root, env):
    h = hashlib.sha256()
    for rel in INPUTS:
        with open(os.path.join(root, rel), "rb") as f:
            h.update(f.read())
    h.update(("image=%s\nuser=%s\n" % (image(env), env.get("WK_VM_USER") or "admin")).encode())
    return h.hexdigest()[:16]


class Base:
    def __init__(self, vm, clock=None):
        self.vm, self.machine, self.env, self.root = vm, vm.machine, vm.env, str(vm.root)
        self.clock = clock or Clock()
        self.name = vm.base()
        self.host = guest.Host(vm, self.clock)

    def marker(self):
        return os.path.join(self.vm.vm_dir(), "base.ready")

    def exists(self):
        return self.vm.state_of(self.name) != "absent"

    def ready(self):
        return self.exists() and self.machine.exists(self.marker())

    def outputs(self):
        return [self.marker()] if self.ready() else []

    def field(self, key):
        try:
            text = self.machine.read(self.marker())
        except OSError:
            return ""
        return next((l.split("=", 1)[1] for l in text.splitlines() if l.startswith(key + "=")), "")

    def stale(self):
        rec = self.field("inputs")
        if not rec:
            return "provisioned before this record existed"
        if rec == inputs_hash(self.root, self.env):
            return ""
        return "%s, WK_VM_IMAGE or WK_VM_USER has changed since it was built" % ", ".join(INPUTS)

    def findings(self):
        if not self.exists():
            return ("wrong\tno golden base VM '%s' -- there is nothing for a guest to be cloned from\t"
                    "%s   (hours: the image pull, Xcode's first launch)\n" % (self.name, guest.BASE_BUILD))
        if not self.machine.exists(self.marker()):
            return ("wrong\t'%s' exists but provisioning never finished in it\t%s --refresh   (re-runs provisioning; "
                    "nothing is re-downloaded)\n" % (self.name, guest.BASE_BUILD))
        why = self.stale()
        if why:
            return ("wrong\t'%s' predates its own provisioning inputs: %s -- every guest cloned from it carries what that "
                    "base was built with\t%s --rebuild   (hours; existing guests are unaffected)\n" % (self.name, why, guest.BASE_BUILD))
        return "ok\tgolden base '%s' matches its provisioning inputs\t\n" % self.name

    def build(self, rest):
        got = task.options(rest, ("--dry-run",) + MODES, (), USAGE % guest.BASE_PROFILE)
        if len(got) > 1:
            die("%s -- one of them" % (USAGE % guest.BASE_PROFILE))
        if not Store(self.env).macos_host:
            die("a macOS guest needs a macOS host (Virtualization.framework)")
        mode = next(iter(got), "")
        with self.host.lock().held("guest-base"):
            if mode == "--rm":
                return self.erase()
            if mode == "--rebuild":
                self.rebuild()
            elif mode == "--refresh":
                if not self.exists():
                    die("no golden base yet -- run '%s'" % guest.BASE_BUILD)
                self.provision()
            else:
                self.ensure()
        info("golden base '%s' is ready" % self.name)
        return 0

    def rebuild(self):
        """The tree is asked first: refusing it after the delete would cost a clone, a grow and a boot for the same verdict."""
        why = tools.committed(self.root, self.machine)
        if why:
            die("the golden base is given a commit, so it cannot be built from this tree:\n%s\n    Nothing has been deleted." % why)
        if not act.confirm("delete and rebuild the golden base VM '%s'?" % self.name):
            die("aborted")
        self.vm.delete_vm(self.name)
        self.machine.remove(self.marker())
        self.ensure()

    def erase(self):
        if not self.exists():
            die("no golden base '%s' on this machine -- nothing to erase.\n    '%s' builds one; 'wk disk' says what "
                "everything here costs." % (self.name, guest.BASE_BUILD))
        home = tart_home(self.env)
        if not act.confirm("delete the golden base VM '%s' (%s)? rebuilding it is hours"
                           % (self.name, self.size(os.path.join(home, "vms", self.name)))):
            die("aborted -- nothing was changed")
        self.vm.delete_vm(self.name)
        self.machine.remove(self.marker())
        info("deleted '%s' -- existing vm workspaces are unaffected" % self.name)
        log("  '%s' builds it again; until then 'wk new --target vm' has nothing to clone" % guest.BASE_BUILD)
        cache = os.path.join(home, "cache")
        try:
            cached = self.machine.isdir(cache) and bool(self.machine.listdir(cache))
        except OSError:
            cached = False
        if not cached:
            return 0
        if act.confirm("also drop the pulled image cache (%s)? it is re-downloadable" % self.size(cache)):
            self.tart_or_die(["prune", "--space-budget", "0"])
            info("pruned the image cache")
        else:
            log("  kept: %s" % cache)
        return 0

    def size(self, path):
        words = self.machine.run(["du", "-sh", path]).out.split()
        return words[0] if words else "?"

    def tart_or_die(self, args, what=None):
        r = self.machine.act_run([self.vm.tart_or_die()] + args)
        if not r.ok:
            die("tart %s failed (exit %d): %s" % (what or args[0], r.rc, (r.err or r.out).strip()), r.rc or 1)
        return r

    def cached(self):
        r = self.machine.run([self.vm.tart_or_die(), "list", "--format", "json", "--source", "oci"])
        try:
            return any(v.get("Name") == image(self.env) for v in json.loads(r.out)) if r.ok else False
        except ValueError:
            return False

    def sizing(self):
        from wk.resources import Resources
        res = Resources(self.machine, self.env, "macos")
        return (self.env.get("WK_VM_BASE_CPUS") or str(res.envelope_cores()),
                self.env.get("WK_VM_BASE_MEM_MB") or str(res.envelope_mem_mb()))

    def ensure(self):
        if self.ready():
            return
        if self.exists():
            warn("'%s' exists but was never finished (no completion marker)" % self.name)
            log("  destroying it and starting again")
            self.vm.delete_vm(self.name)
        self.machine.remove(self.marker())
        if not self.cached():
            info("pulling %s -- tens of GB, once only" % image(self.env))
            self.tart_or_die(["pull", image(self.env)])
        info("creating the golden base VM '%s'" % self.name)
        cpus, mem = self.sizing()
        self.tart_or_die(["clone", image(self.env), self.name])
        self.tart_or_die(["set", self.name, "--cpu", cpus, "--memory", mem])
        self.provision()

    def runlog(self):
        return self.host.path("base.run.log")

    def start(self):
        """Always with the open network, never through guest.boot's Softnet: provisioning installs from PyPI and the
        account pane needs Apple's servers, and a base booted both ways gets a lease `tart ip` does not follow."""
        if self.vm.state_of(self.name) != "running":
            self.machine.remove(self.runlog())
            self.machine.spawn([self.vm.tart_or_die(), "run", "--no-graphics", self.name], self.runlog())
            info("booting the base VM (log: %s)" % self.runlog())
        r = self.machine.run([self.vm.tart_or_die(), "ip", self.name, "--wait", str(BOOT_WAIT)], timeout=BOOT_WAIT + 30)
        ip = r.out.strip() if r.ok else ""
        if not ip:
            die("the base VM did not boot. Its run log says:\n%s" % guest.runlog_tail(self.machine, self.runlog()))
        return ip

    def wait_ssh(self, g):
        return self.clock.wait_until(lambda: g.run(["true"]).ok, 120, 2)

    def install_key(self, g):
        if self.wait_ssh(g):
            debug("ssh already works in '%s'; no key to install" % self.name)
            return
        info("installing the wk ssh key over the guest agent")
        pub = self.machine.read(self.vm.key() + ".pub").strip()
        r = self.machine.act_run([self.vm.tart_or_die(), "exec", self.name, "/bin/sh", "-c",
                                  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qxF '%s' ~/.ssh/authorized_keys 2>/dev/null "
                                  "|| echo '%s' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" % (pub, pub)])
        if not r.ok:
            die("could not reach the guest agent in '%s'.\n    `tart exec` needs the Tart guest agent, which the Cirrus Labs "
                "images ship\n    but a vanilla macOS image does not: log in once with the image's own credentials,\n"
                "    append %s.pub to ~/.ssh/authorized_keys, then re-run." % (self.name, self.vm.key()))
        if not self.wait_ssh(g):
            die("ssh key was installed but ssh still refuses. Its run log says:\n%s" % guest.runlog_tail(self.machine, self.runlog()))

    def provision(self):
        v = self.vm
        cpus, mem = self.sizing()
        guest.admit(self.host, self.name, int(mem))
        v.ensure_dir_mode(v.vm_dir(), "0700")
        cur, want = v.configured(self.name, "Disk"), int(self.env.get("WK_VM_DISK_GB") or guest.DISK_GB)
        if cur is not None and cur < want:   # tart grows a disk only while the VM is off
            info("growing the base disk %dGB -> %dGB" % (cur, want))
            self.tart_or_die(["set", self.name, "--disk-size", str(want)])
        self.tart_or_die(["set", self.name, "--cpu", cpus, "--memory", mem])
        guest.host_disk(self.host)
        if not self.machine.exists(v.key()):
            if not self.machine.act_run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wk-vm", "-f", v.key()]).ok:
                die("could not generate the macOS VM ssh key %s" % v.key())
            info("generated the macOS VM ssh key")
        if act.dry_run():
            log("would boot '%s', provision it (vm/provision-base.sh), drive Setup Assistant off its screen, reboot it\n"
                "  and seal it only on a clear screen -- nothing past here can be shown without a running guest" % self.name)
            return
        ip = self.start()
        g = v.guest_at(ip)
        self.install_key(g)
        # A stale clock fails provisioning's first HTTPS clone as a not-yet-valid certificate, and a base hands it to every clone.
        if not guest.Guest(self.host, self.name, ip).set_guest_clock():
            die("could not set the clock in '%s'. Passwordless sudo is what it needs, and the base is\n    built from the image "
                "WK_VM_IMAGE names -- check that image rather than patching the guest" % self.name)
        info("provisioning the base VM (Xcode licence, disk, desktop)")
        if not tools.push(self.root, self.machine, g, v.tools(self.name), self.env):
            die("the base cannot be provisioned without wk-tools in it (see above)")
        guest.login_note(self.env)
        self.run_provisioning(g)
        if not guest.unblock_desktop(self.root, g):
            die("Setup Assistant is still on '%s''s screen, and a base is not sealed behind a pane:\n    every guest cloned "
                "from it would come up behind one too. It is running at %s --\n    answer it at its own window, then  %s --refresh"
                % (self.name, ip, guest.BASE_BUILD))
        if not guest.Guest(self.host, self.name, ip).settle_desktop():
            warn("could not re-settle the base's desktop after Setup Assistant")
        info("rebooting the base to prove its screen comes up clear")   # a dismissed pane comes back at the next login
        self.tart_or_die(["stop", self.name])
        ip = self.start()
        g = v.guest_at(ip)
        if not self.wait_ssh(g):
            die("'%s' rebooted to %s but ssh never answered, so the screen it came up with\n    cannot be read. Its run log says:\n%s"
                % (self.name, ip, guest.runlog_tail(self.machine, self.runlog())))
        if not self.login_settled(g):
            die("Setup Assistant came back at '%s''s next login, so the flow that answered it did\n    not finish. The base is "
                "running at %s: answer it at its own window, then  %s --refresh" % (self.name, ip, guest.BASE_BUILD))
        self.check_screen(g, ip)
        info("shutting the base VM down")
        self.tart_or_die(["stop", self.name])
        self.machine.write(self.marker(), "image=%s\ninputs=%s\nfinished=%s\n"
                           % (image(self.env), inputs_hash(self.root, self.env), self.clock.iso()))
        info("golden base VM '%s' is sealed" % self.name)

    def run_provisioning(self, g):
        """Detached and polled: provisioning is minutes, and a dropped connection takes a foreground ssh with it."""
        argv = ["env", "WK_VM_DISPLAY=" + guest.display(self.env), "WK_VM_USER=" + self.vm.user(),
                "WK_VM_PASSWORD=" + guest.password(self.env), "bash", self.vm.tools(self.name) + "/vm/provision-base.sh"]
        if not g.act_run(["sh", "-c", job.remote_line(argv, PLOG, PRC)]).ok:
            die("could not start base provisioning in '%s'" % self.name)
        word, _ = job.wait_remote(lambda line: g.run(["sh", "-c", line]), PLOG, PRC, self.clock, env=self.env)
        saved = self.host.path("base-provision.log")
        try:
            self.machine.write(saved, g.read(PLOG))
        except OSError:
            pass
        if word != "0":
            die("base provisioning failed (%s).\n    What it printed is in %s; the base is rubble until this finishes,\n"
                "    and a re-run starts it again:  %s --refresh" % ("rc=" + word if word.isdigit() else word, saved, guest.BASE_BUILD))

    def login_settled(self, g):
        """ssh answers before the login has drawn anything, so a read straight after boot reads clear whatever is coming."""
        settle = int(self.env.get("WK_VM_LOGIN_SETTLE") or LOGIN_SETTLE)
        for _ in range(0, settle, 3):
            if guest.setup_assistant(g) == "up":
                return False
            self.clock.sleep(3)
        return True

    def check_screen(self, g, ip):
        reading = guest.window_reading(self.root, self.machine, g)
        if not reading or reading == "?":
            die("could not ask '%s' what is on its screen, and a base is not sealed unread: every\n    guest cloned from it "
                "would come up behind whatever is there. The base is still\n    running at %s -- '%s --refresh' re-runs this."
                % (self.name, ip, guest.BASE_BUILD))
        uninvited = guest.unexpected(self.root, self.machine, reading)
        if not uninvited:
            info("the base's screen is clear, so a clone's will be too")
            return
        die("on the base's screen, and nothing wk put there: %s\n    Every guest cloned from this base comes up behind it, and "
            "a clone cannot clear it\n    itself. The base is running now, at %s: answer it at its own window, then  %s --refresh"
            % (uninvited.rstrip(";"), ip, guest.BASE_BUILD))


def rubble(vm):
    """tart's pulled-image cache down to its budget by tart's own default, caches and never a VM; the golden base, named."""
    from wk.rubble import du_kb, row
    tart = vm.tart()
    if not tart:
        return []
    home = tart_home(vm.env)
    budget = vm.env.get("WK_TART_CACHE_GB") or "20"
    rows, cache = [], os.path.join(home, "cache")
    if vm.machine.isdir(cache):
        rows.append(row("tart-cache", "tart's pulled-image cache, down to %s GB" % budget, du_kb(vm.machine, cache),
                        take=lambda: vm.machine.act_run([tart, "prune", "--space-budget", budget]).ok))
    if vm.state_of(vm.base()) != "absent":
        rows.append(row("guest-base", "golden base '%s'" % vm.base(), du_kb(vm.machine, os.path.join(home, "vms", vm.base())),
                        guest.BASE_BUILD + " --rm"))
    return rows
