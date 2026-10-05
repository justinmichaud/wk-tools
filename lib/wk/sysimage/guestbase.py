"""The `guest` builder: the golden macOS base every guest is a `tart clone` of, sealed only on a clear screen. Its
marker records the hash of its inputs, and staleness is that hash recomputed on every read."""

import hashlib
import json
import os

from wk import act, guest, job, tools
from wk.act import die, info, log, warn
from wk.clock import Clock
from wk.kv import kv
from wk.store import Store
from wk.sysimage import task

# macOS 26.6.2 with Xcode 27 beta 6. A Cirrus Labs `-xcode` tag names the Xcode, and the first Saturday of every month
# re-pushes it onto the newest macOS, so only a digest names one image.
IMAGE = "ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:f441eb487a18b4588c096adcff5eb48fddca550909e01c472580872b48c166b0"
INPUTS = ("vm/provision-base.sh", "vm/desktop.sh", "vm/mount-mirror.sh", "bench/mac-pyobjc.sh")
LOGIN_SETTLE = 45    # measured: Setup Assistant is up 4s after boot, and the guest answers before that
BOOT_WAIT = 300
PLOG, PRC = "/tmp/wk-base-provision.log", "/tmp/wk-base-provision.rc"
MODES = ("--refresh", "--rebuild", "--rm")
USAGE = "usage: wk sysimage build %s [--refresh|--rebuild|--rm] [--dry-run]"


def tart_home(env):
    return env.get("TART_HOME") or os.path.join(Store(env).home(), ".tart")


def image(env):
    return env.get("WK_VM_IMAGE") or IMAGE


def mirror_env_words():
    from wk.places import GUEST_MIRROR_MOUNT, GUEST_MOUNT_MIRROR, MIRROR_TAG
    return " ".join(("WK_MIRROR_TAG=" + MIRROR_TAG, "WK_MIRROR_MOUNT=" + GUEST_MIRROR_MOUNT, "WK_MOUNT_MIRROR=" + GUEST_MOUNT_MIRROR))


def inputs_hash(root, env):
    h = hashlib.sha256()
    for rel in INPUTS:
        with open(os.path.join(root, rel), "rb") as f:
            h.update(f.read())
    h.update(("image=%s\nuser=%s\n" % (image(env), guest.vm_user(env))).encode() + mirror_env_words().encode())
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
        return kv(text).get(key, "")

    def stale(self):
        rec = self.field("inputs")
        if not rec:
            return "provisioned before this record existed"
        if rec == inputs_hash(self.root, self.env):
            return ""
        return "%s, WK_VM_IMAGE or WK_VM_USER has changed since it was built" % ", ".join(INPUTS)

    def findings(self):
        if not self.exists():
            return "wrong\tno golden base VM '%s' to clone a guest from\t%s   (hours)\n" % (self.name, guest.BASE_BUILD)
        if not self.machine.exists(self.marker()):
            return "wrong\t'%s' exists but provisioning never finished in it\t%s --refresh\n" % (self.name, guest.BASE_BUILD)
        why = self.stale()
        if why:
            return ("wrong\t'%s' predates its own provisioning inputs: %s\t%s --rebuild   (hours; existing guests are "
                    "unaffected)\n" % (self.name, why, guest.BASE_BUILD))
        return "ok\tgolden base '%s' matches its provisioning inputs\t\n" % self.name

    def build(self, rest):
        got = task.options(rest, MODES, (), USAGE % guest.BASE_PRESET)
        if len(got) > 1:
            die("%s -- one of them" % (USAGE % guest.BASE_PRESET))
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
            die("no golden base '%s' on this machine -- nothing to erase" % self.name)
        home = tart_home(self.env)
        if not act.confirm("delete the golden base VM '%s' (%s)? rebuilding it is hours"
                           % (self.name, self.size(os.path.join(home, "vms", self.name)))):
            die("aborted -- nothing was changed")
        self.vm.delete_vm(self.name)
        self.machine.remove(self.marker())
        info("deleted '%s' -- existing vm workspaces are unaffected; '%s' builds it again" % (self.name, guest.BASE_BUILD))
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

    def tart_or_die(self, args, stream=False):
        argv = [self.vm.tart_or_die()] + args
        r = self.machine.act_run(argv, stream=True) if stream else self.machine.act_run(argv)
        if not r.ok:
            die("tart %s failed (exit %d): %s" % (args[0], r.rc, (r.err or r.out).strip() or "what it said is above"),
                r.rc or 1)
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
            warn("'%s' exists but was never finished -- destroying it and starting again" % self.name)
            self.vm.delete_vm(self.name)
        self.machine.remove(self.marker())
        if not self.cached():
            info("pulling %s -- tens of GB, once only" % image(self.env))
            self.tart_or_die(["pull", image(self.env)], stream=True)
        info("creating the golden base VM '%s'" % self.name)
        cpus, mem = self.sizing()
        self.tart_or_die(["clone", image(self.env), self.name], stream=True)
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

    def wait_agent(self, g):
        return self.clock.wait_until(lambda: g.run(["true"]).ok, 120, 2)

    def install_key(self, g):
        if not self.wait_agent(g):
            die("the tart guest agent in '%s' never answered (a Cirrus Labs image ships it; a vanilla macOS image does not).\n"
                "    Its run log says:\n%s" % (self.name, guest.runlog_tail(self.machine, self.runlog())))
        pub = self.machine.read(self.vm.key() + ".pub").strip()
        if not g.act_run(["sh", "-c", 'umask 077 && mkdir -p ~/.ssh && { grep -qxF "$1" ~/.ssh/authorized_keys 2>/dev/null || '
                          'echo "$1" >> ~/.ssh/authorized_keys; }', "sh", pub]).ok:
            die("could not authorise the wk ssh key in '%s'" % self.name)

    def provision(self):
        v = self.vm
        cpus, mem = self.sizing()
        guest.admit(self.host, self.name, int(mem))
        v.ensure_dir_mode(v.vm_dir(), "0700")
        cur, want = v.configured(self.name, "Disk"), guest.vm_disk_gb(self.env)
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
            log("would boot '%s', provision it, reboot it and seal it on a clear screen" % self.name)
            return
        ip = self.start()
        g = v.guest_of(self.name)
        self.install_key(g)
        # A stale clock fails provisioning's first HTTPS clone as a not-yet-valid certificate, and a base hands it to every clone.
        if not guest.Guest(self.host, self.name, g).set_guest_clock():
            die("could not set the clock in '%s', which needs passwordless sudo in the image WK_VM_IMAGE names" % self.name)
        info("provisioning the base VM (Xcode licence, disk, desktop)")
        if not tools.push(self.root, self.machine, g, v.tools(self.name), self.env):
            die("the base cannot be provisioned without wk-tools in it (see above)")
        guest.login_note(self.env)
        self.run_provisioning(g)
        if not guest.unblock_desktop(self.root, g):
            die("Setup Assistant is still on '%s''s screen, and a base is not sealed behind a pane.\n    It is running at %s: "
                "answer it at its own window, then  %s --refresh" % (self.name, ip, guest.BASE_BUILD))
        if not guest.Guest(self.host, self.name, g).settle_desktop():
            warn("could not re-settle the base's desktop after Setup Assistant")
        info("rebooting the base to prove its screen comes up clear")   # a dismissed pane comes back at the next login
        self.tart_or_die(["stop", self.name])
        ip = self.start()
        if not self.wait_agent(g):
            die("'%s' rebooted to %s but its guest agent never answered. Its run log says:\n%s"
                % (self.name, ip, guest.runlog_tail(self.machine, self.runlog())))
        if not self.login_settled(g):
            die("Setup Assistant came back at '%s''s next login. The base is running at %s: answer it at its own window,\n"
                "    then  %s --refresh" % (self.name, ip, guest.BASE_BUILD))
        self.check_screen(g, ip)
        info("shutting the base VM down")
        self.tart_or_die(["stop", self.name])
        self.machine.write(self.marker(), "image=%s\ninputs=%s\nfinished=%s\n"
                           % (image(self.env), inputs_hash(self.root, self.env), self.clock.iso()))
        info("golden base VM '%s' is sealed" % self.name)

    def run_provisioning(self, g):
        """Detached and polled: provisioning is minutes, and a dropped connection takes a foreground exec with it."""
        argv = ["env", "WK_VM_DISPLAY=" + guest.display(self.env), "WK_VM_USER=" + self.vm.user(),
                "WK_VM_PASSWORD=" + guest.password(self.env), *mirror_env_words().split(), "bash",
                self.vm.tools(self.name) + "/vm/provision-base.sh"]
        if not g.act_run(["sh", "-c", job.remote_line(argv, PLOG, PRC)]).ok:
            die("could not start base provisioning in '%s'" % self.name)
        word, _ = job.wait_remote(lambda line: g.run(["sh", "-c", line]), PLOG, PRC, self.clock, env=self.env)
        saved = self.host.path("base-provision.log")
        try:
            self.machine.write(saved, g.read(PLOG))
        except OSError:
            pass
        if word != "0":
            die("base provisioning failed (%s). What it printed is in %s; a re-run:  %s --refresh" % ("rc=" + word if word.isdigit() else word, saved, guest.BASE_BUILD))

    def login_settled(self, g):
        # The guest answers before the login has drawn anything, so one read straight after boot reads clear.
        return not self.clock.wait_until(lambda: guest.setup_assistant(g) == "up", LOGIN_SETTLE, 3)

    def check_screen(self, g, ip):
        reading = guest.window_reading(self.root, self.machine, g)
        if not reading or reading == "?":
            die("could not ask '%s' what is on its screen, and a base is not sealed unread.\n    It is running at %s; "
                "'%s --refresh' re-runs this." % (self.name, ip, guest.BASE_BUILD))
        uninvited = guest.unexpected(self.root, self.machine, reading)
        if not uninvited:
            info("the base's screen is clear, so a clone's will be too")
            return
        die("on the base's screen, and nothing wk put there: %s\n    Every clone would come up behind it. The base is running at "
            "%s: answer it at its own window, then  %s --refresh" % (uninvited.rstrip(";"), ip, guest.BASE_BUILD))


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
