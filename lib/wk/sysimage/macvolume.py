"""The Mac's benchmark install as a `wk sysimage build` builder: a second APFS volume in the internal container,
macOS put on it by startosinstall with a provisioning package, and provisioned from inside it. It is built when
the install carries /etc/wk-image, the marker a board's image carries. Every step acts on the Mac it runs on."""

import json
import os
import plistlib
import shlex
import sys
import time
from xml.parsers.expat import ExpatError

from wk import act, fleet, images
from wk.act import Refused, die, info, log, warn
from wk.clock import Clock
from wk.quiet import DESKTOP, HOSTS, Quiesce, lib_argv
from wk.store import Store
from wk.sysimage import task
from wk.sysimage.mactailnet import Tailnet

NEED_GB = 120   # the room the host install keeps: both installs share the container, and a full one stops both
BENCH_PASSWORD = "benchbench"   # public, so anyone at the console has passwordless sudo: the install holds no user data
MARKER = "/etc/wk-image"
FIRSTBOOT = "/Library/LaunchDaemons/com.wk.bench-firstboot.plist"
FIRSTBOOT_LOG = "/var/log/wk-bench-firstboot.log"
PAYLOAD_DIR = "usr/local/share/wk-bench"
PAYLOAD = (("bench/mac-bench-firstboot.sh", "usr/local/libexec/wk-bench-firstboot.sh", "0755"),
           ("bench/mac-quiet-hosts.sh", "usr/local/libexec/wk-bench-quiet-hosts.sh", "0644"),
           ("bench/mac-quiet-desktop.sh", "usr/local/libexec/wk-bench-quiet-desktop.sh", "0644"),
           ("bench/quiet/macos.tsv", "usr/local/libexec/quiet/macos.tsv", "0644"),
           ("bench/quiet/macos-hosts.txt", "usr/local/libexec/quiet/macos-hosts.txt", "0644"),
           ("bench/mac-pyobjc.sh", "usr/local/libexec/wk-bench-pyobjc.sh", "0644"))
STALE = ("tailscale-authkey", "Tailscale-macos.pkg")   # tombstone: the packaged client and its key, removed on repair
SKIP_SETUP = ("private/var/db/.AppleSetupDone", "System/Library/User Template/English.lproj/.skipbuddy",
              "System/Library/User Template/Non_localized/.skipbuddy", "Library/User Template/English.lproj/.skipbuddy")
ACTIONS = ("--create", "--fetch", "--install", "--provision", "--repair", "--build-pkg", "--all")
USAGE = ("usage: wk sysimage build %s [--create | --fetch [--version <v>] | --install | --provision | --repair |"
         " --build-pkg | --all [--version <v>]] [--dry-run]")

# RunAtLoad and no KeepAlive, which would resurrect a job that deletes its own script; a daemon, since no user exists yet.
FIRSTBOOT_PLIST = plistlib.dumps({"Label": "com.wk.bench-firstboot",
                                  "ProgramArguments": ["/bin/bash", "/usr/local/libexec/wk-bench-firstboot.sh"],
                                  "RunAtLoad": True, "StandardOutPath": FIRSTBOOT_LOG,
                                  "StandardErrorPath": FIRSTBOOT_LOG}).decode()
# --installpackage lands the daemon after launchd scanned /Library/LaunchDaemons, so it is bootstrapped on this boot.
POSTINSTALL = """#!/bin/bash
PLIST=%s
[ -f "$PLIST" ] || exit 0
launchctl bootstrap system "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
exit 0
""" % FIRSTBOOT

BANNER = """
  ------------------------------------------------------------------
   NEXT IS THE PART THAT NEEDS YOU, AND IT REBOOTS THIS MACHINE.

   startosinstall erases '{v}' and will:
     - ask for your password (sudo), and then again (--passprompt)
       for the volume-owner authorisation: on Apple Silicon a volume
       is personalised only with an owner's credential, and without
       it the install fails with "failed to authorize for installation".
     - reboot into the installer and install macOS onto '{v}',
       about half an hour with the machine unusable.

   The provisioning package answers Setup Assistant: the new install
   gets one user, bench, no Apple ID, no FileVault, no Siri. Then, in it:
     wk sysimage build {p} --provision

   Installer: {app}
   Target:    {s}
  ------------------------------------------------------------------

"""


def gb(n):
    return int(n or 0) // 1000000000


def stage_payload(m, tools, root, sudo, env):
    """The files a benchmark install needs, from the host install over a mounted volume or from the install itself."""
    pre = ["sudo"] if sudo else []
    base = root.rstrip("/")

    def run(argv, what):
        if not m.act_run(pre + argv).ok:
            die(what)

    lib = base + "/usr/local/libexec"
    run(["install", "-d", "-m", "0755", lib, lib + "/quiet", "%s/%s" % (base, PAYLOAD_DIR)], "could not make %s" % lib)
    for src, dest, mode in PAYLOAD:
        run(["install", "-m", mode, os.path.join(tools, src), "%s/%s" % (base, dest)], "could not stage %s" % src)
    keys = os.path.join(Store(env).home(), ".ssh", "authorized_keys")
    if m.exists(keys):
        run(["install", "-m", "0644", keys, "%s/%s/authorized_keys" % (base, PAYLOAD_DIR)], "could not stage %s" % keys)
        log("  authorized_keys: %d key(s) from this install" % len([l for l in m.read(keys).splitlines() if l.strip()]))
    else:
        warn("  no ~/.ssh/authorized_keys here, so the bench install will have none")
        warn("  -- it will boot, and nothing will be able to drive it")
    run(["rsync", "-a", "--chmod=go-w", "--delete", "--exclude", ".git/", "--exclude", "__pycache__/", "--exclude", "*.pyc",
         tools.rstrip("/") + "/", "%s/%s/wk-tools/" % (base, PAYLOAD_DIR)], "could not stage wk-tools onto '%s'" % root)


def load_plist(text):
    try:
        return plistlib.loads(text.encode())
    except (ValueError, ExpatError):
        return {}


class MacVolume:
    kind = "mac-volume"

    def __init__(self, machine, profile, env, clock=None, root=None):
        self.m, self.p, self.env = machine, profile, env
        self.clock = clock or Clock()
        self.root = str(root or images.root(env))
        self.name, self.machine = profile["IMG_PROFILE"], profile["IMG_MACHINE"]
        self.volume = (fleet.Fleet(self.root, env).load(self.machine) or {}).get("NODE_VOLUME", "")
        self.tailnet = Tailnet(machine, env, self.root)
        self.need_gb = int(env.get("WK_BENCH_NEED_GB") or NEED_GB)

    @property
    def s(self):
        return "/Volumes/" + self.volume

    @property
    def d(self):
        return "/Volumes/%s - Data" % self.volume

    def act(self, argv, what, input=None):
        if not self.m.act_run(argv, input=input).ok:
            die(what)

    def sudo_write(self, path, text, mode=None, append=False):
        self.act(["sudo", "tee"] + (["-a"] if append else []) + [path], "could not write %s" % path, input=text)
        if mode:
            self.act(["sudo", "chmod", mode, path], "could not set %s's mode" % path)

    def tty(self, argv, what):
        """A command whose prompt and progress are the person's to see."""
        if act.dry_run():
            log("would run: %s" % " ".join(shlex.quote(a) for a in argv))
            return
        if not self.m.run_tty(argv).ok:
            die(what)

    def disk(self, target):
        r = self.m.run(["diskutil", "info", "-plist", target])
        return load_plist(r.out) if r.ok else {}

    def container(self):
        c = self.disk("/").get("APFSContainerReference")
        if not c:
            die("the boot volume is not on an APFS container -- this shape does not apply")
        return c

    def present(self):
        return self.m.run(["diskutil", "info", self.volume]).ok

    def installed(self):
        """An empty formatted volume with the right name mounts perfectly and boots nothing."""
        core = self.s + "/System/Library/CoreServices"
        return self.m.isdir(core) and self.m.exists(core + "/SystemVersion.plist")

    def outputs(self):
        """The builder's done marker: an installed volume carrying the marker a board's image carries."""
        return [self.s + MARKER] if self.installed() and self.m.exists(self.s + MARKER) else []

    def marker_id(self, path):
        for line in self.m.read(path).splitlines():
            if line.startswith("id="):
                return line[3:]
        return "?"

    def build(self, rest):
        o = task.options(rest, ACTIONS + ("--dry-run",), ("--version",), USAGE % self.name)
        actions = [a for a in ACTIONS if o.get(a)]
        if len(actions) > 1:
            die("one action at a time (got %s)" % " and ".join(actions))
        if self.m.run(["uname", "-s"]).out.strip() != "Darwin":
            die("%s is built on the Mac itself -- it acts on the machine's own disk.\n"
                "    From another machine, the lane that drives it is:  wk bench mac <ws>" % self.name)
        if not self.volume:
            die("machines/%s.conf declares no NODE_VOLUME, so there is no volume to build %s on" % (self.machine, self.name))
        if act.dry_run():
            info("--dry-run: nothing on this machine will be changed")
        version = o.get("--version") or ""
        action = actions[0] if actions else ""
        if action == "--create":
            return self.create()
        if action == "--fetch":
            return self.fetch(version)
        if action == "--install":
            return self.install()
        if action == "--provision":
            return self.provision()
        if action == "--build-pkg":
            self.build_pkg()
            return 0
        if action == "--repair":
            return self.repair()
        if action == "--all":
            return self.all(version)
        return self.report()

    def report(self):
        cont, free = self.container(), self.disk("/").get("APFSContainerFree")
        info("the benchmark volume on this Mac")
        log("  container:      %s  (the same one the running system is on)" % cont)
        log("  free in it:     %d GB   (need %d GB to proceed)" % (gb(free), self.need_gb))
        log("  volume name:    %s" % self.volume)
        if not self.present():
            log("  state:          absent -- --create makes it")
        elif not self.installed():
            warn("  state:          the volume exists but has no macOS on it")
            log("                  --fetch then --install, or delete it and start again:")
            log("                    sudo diskutil apfs deleteVolume '%s'" % self.volume)
        else:
            try:
                v = load_plist(self.m.read(self.s + "/System/Library/CoreServices/SystemVersion.plist")).get(
                    "ProductUserVisibleVersion", "?")
            except OSError:
                v = "?"
            log("  state:          a macOS %s system volume -- installed" % v)
            if self.outputs():
                log("  marker:         %s" % self.marker_id(self.s + MARKER))
            else:
                warn("  marker:         MISSING -- bench mode would report itself as host mode")
                log("                  --provision writes it (run it in bench mode)")
        log("")
        log("  the way back, whole:  sudo diskutil apfs deleteVolume '%s'" % self.volume)
        return 0

    def create(self):
        cont, free = self.container(), self.disk("/").get("APFSContainerFree")
        if self.present():
            info("'%s' already exists -- nothing to create" % self.volume)
            return 0
        if free is None:
            die("could not read the container's free space -- refusing to add a volume blind")
        if int(free) < self.need_gb * 1000000000:
            die("only %d GB free in %s, and this needs %d GB.\n  Both installs share this container, so filling it stops "
                "the machine you work\n  on, not just the one you measure on. Free space first, or set\n"
                "  WK_BENCH_NEED_GB deliberately lower if you have costed it." % (gb(free), cont, self.need_gb))
        info("adding APFS volume '%s' to %s" % (self.volume, cont))   # no quota: an upgrade that outgrows one fails like a disk fault
        self.act(["sudo", "diskutil", "apfs", "addVolume", cont, "APFS", self.volume], "could not add '%s'" % self.volume)
        if not act.dry_run():
            info("created. It is empty and boots nothing yet -- --fetch, then --install")
        return 0

    def fetch(self, version):
        if not version:
            info("full installers this Mac is offered")
            r = self.m.run(["softwareupdate", "--list-full-installers"])
            for line in (r.out + r.err).splitlines():
                log("  " + line)
            log("")
            log("  pick one:  wk sysimage build %s --fetch --version <version>" % self.name)
            log("  match the host install's major version unless you mean not to --")
            log("  two different macOS versions is a second variable in every number.")
            return 0
        info("fetching the macOS %s installer (this is tens of GB)" % version)   # the one source Apple personalises
        self.tty(["softwareupdate", "--fetch-full-installer", "--full-installer-version", version],
                 "softwareupdate could not fetch the macOS %s installer" % version)
        return 0

    def find_installer(self):
        try:
            names = self.m.listdir("/Applications")
        except OSError:
            return ""
        apps = ["/Applications/" + n for n in names if n.startswith("Install macOS") and n.endswith(".app")
                and self.m.exists("/Applications/%s/Contents/Resources/startosinstall" % n)]
        return apps[-1] if apps else ""

    def install(self):
        if not self.present():
            die("'%s' does not exist yet -- --create first" % self.volume)
        if self.installed():
            act.nothing_to_ask()
            info("'%s' already has macOS on it -- nothing to install" % self.volume)
            return 0
        app = self.find_installer()
        if not app:
            die("no 'Install macOS *.app' in /Applications -- --fetch first")
        sys.stderr.write(BANNER.format(v=self.volume, p=self.name, app=app, s=self.s))
        if not act.asked() and not act.confirm("erase '%s' and install macOS onto it now?" % self.volume):
            log("nothing done")
            return 0
        try:
            self.tailnet.remember(self.s, self.machine, sudo=True)
        except Refused:
            warn("  could not take the bench install's tailnet identity aside -- it will rejoin as a new node")
        admin = self.env.get("WK_BENCH_ADMIN") or self.m.run(["id", "-un"]).out.strip()
        log("  authorising as '%s' -- it must be a volume owner on this Mac" % admin)
        info("building the provisioning package (so Setup Assistant never runs)")
        pkg = self.build_pkg()
        self.tty(["sudo", app + "/Contents/Resources/startosinstall", "--volume", self.s, "--agreetolicense",
                  "--user", admin, "--passprompt", "--installpackage", pkg], "startosinstall did not install onto '%s'" % self.volume)
        return 0

    def password(self, root, sudo):
        state = Store(self.env).state_dir()
        pwfile = os.path.join(state, "bench-password")
        self.m.mkdir(state)
        self.m.write(pwfile, BENCH_PASSWORD)
        self.act(["chmod", "0600", pwfile], "could not keep %s private" % pwfile)
        dest = "%s/%s/password" % (root.rstrip("/"), PAYLOAD_DIR)
        if sudo:
            self.sudo_write(dest, BENCH_PASSWORD, "0600")
        else:
            self.m.write(dest, BENCH_PASSWORD)
            self.act(["chmod", "0600", dest], "could not keep %s private" % dest)
        log("  bench account password: '%s' (constant; also at %s)" % (BENCH_PASSWORD, pwfile))

    def build_pkg(self):
        """`startosinstall --installpackage` takes a productbuild package, laid down before the first boot."""
        tmp = self.env.get("TMPDIR") or "/tmp"
        out, root = os.path.join(tmp, "wk-bench-provision.pkg"), os.path.join(tmp, "wk-bench-pkgroot")
        comp, scripts = os.path.join(tmp, "wk-bench-component.pkg"), os.path.join(tmp, "wk-bench-pkgscripts")
        self.m.act_run(["rm", "-rf", root, scripts, comp, out])
        for f in SKIP_SETUP:
            self.m.mkdir(os.path.dirname(os.path.join(root, f)))
            self.m.write(os.path.join(root, f), "")
        self.m.mkdir(root + os.path.dirname(FIRSTBOOT))
        stage_payload(self.m, self.root, root, False, self.env)
        self.tailnet.stage(root, self.machine)
        self.m.write(root + FIRSTBOOT, FIRSTBOOT_PLIST)
        self.password(root, sudo=False)
        self.m.mkdir(scripts)
        self.m.write(scripts + "/postinstall", POSTINSTALL)
        self.act(["chmod", "0755", scripts + "/postinstall"], "could not mark the postinstall executable")
        self.act(["pkgbuild", "--root", root, "--scripts", scripts, "--identifier", "com.wk.bench-provision", "--version", "1",
                  "--install-location", "/", comp], "pkgbuild failed (it comes with the Command Line Tools)")
        self.act(["productbuild", "--package", comp, out], "productbuild failed (it comes with the Command Line Tools)")
        self.m.act_run(["rm", "-rf", root, comp, scripts])
        if not act.dry_run():
            sig = [l.split(":", 1)[1].strip() for l in self.m.run(["pkgutil", "--check-signature", out]).out.splitlines()
                   if l.strip().startswith("Status:")]
            info("built %s" % out)
            log("  signature: %s" % (sig[0] if sig else "?"))
        return out

    def wifi_conf(self, dest):
        """The first preferred network whose passphrase the System keychain holds, which is what macOS joins from."""
        if self.env.get("WK_BENCH_WIRED"):
            info("  WK_BENCH_WIRED: that install is on ethernet, so no Wi-Fi is copied")
            return
        lines = self.m.run(["networksetup", "-listallhardwareports"]).out.splitlines()
        dev = next((lines[i + 1].split(":", 1)[1].strip() for i, l in enumerate(lines[:-1])
                    if l.strip() == "Hardware Port: Wi-Fi" and ":" in lines[i + 1]), "")
        if not dev:
            warn("  no Wi-Fi interface here; assuming the bench install has wired network")
            return
        nets = [l[1:] for l in self.m.run(["networksetup", "-listpreferredwirelessnetworks", dev]).out.splitlines()
                if l.startswith("\t") and l[1:]]
        for ssid in nets:
            r = self.m.run(["sudo", "security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w",
                            "/Library/Keychains/System.keychain"])
            psk = r.out.rstrip("\n") if r.ok else ""
            if psk:
                self.sudo_write(dest, "WIFI_SSID=%s\nWIFI_PSK=%s\n" % (shlex.quote(ssid), shlex.quote(psk)), "0600")
                info("  wifi: '%s' written into the bench payload" % ssid)
                return
        die("this Mac has Wi-Fi (%s) and no preferred network whose passphrase is\n    in its System keychain, so there is "
            "nothing to give the bench install -- and\n    without a network that install joins no tailnet, installs no "
            "pyobjc and\n    cannot take the Command Line Tools.\n      networksetup -listpreferredwirelessnetworks %s   "
            "lists what was looked for\n    Join the network on this install first, or put the bench install on ethernet\n"
            "    and re-run with WK_BENCH_WIRED=1." % (dev, dev))

    def sshd_on(self, dis):
        """Remote Login is a launchd override, so false is "not disabled"."""
        if not self.m.exists(dis):
            warn("  no launchd override file at %s -- leaving ssh to the first-boot script" % dis)
            return

        def overrides():
            r = self.m.run(["plutil", "-convert", "json", "-o", "-", dis])
            try:
                return json.loads(r.out) if r.ok else {}
            except ValueError:
                return {}

        had = overrides()
        if had.get("com.openssh.sshd") is False:
            return
        verb = "Set :com.openssh.sshd false" if "com.openssh.sshd" in had else "Add :com.openssh.sshd bool false"
        self.m.act_run(["sudo", "/usr/libexec/PlistBuddy", "-c", verb, dis])
        if act.dry_run():
            return
        if overrides().get("com.openssh.sshd") is False:
            info("remote login enabled on the bench volume")
        else:
            warn("  the sshd override did not take -- the volume will have no ssh")

    def repair(self):
        """Re-arms first boot on an installed volume, which `--install` refuses once it carries macOS."""
        if not self.present():
            die("'%s' is not attached" % self.volume)
        if not self.installed():
            die("'%s' has no macOS on it -- --install first, not --repair" % self.volume)
        if not self.m.isdir(self.d):
            die("no data volume at '%s'; is this a volume group?" % self.d)
        info("re-arming first-boot provisioning on '%s'" % self.volume)
        self.sshd_on(self.d + "/private/var/db/com.apple.xpc.launchd/disabled.plist")
        stage_payload(self.m, self.root, self.s, True, self.env)
        self.tailnet.stage(self.s, self.machine, sudo=True)
        self.password(self.s, sudo=True)
        sroot = self.d + "/private/var/wk"
        if self.m.act_run(["sudo", "install", "-d", "-o", "501", "-g", "20", "-m", "0755", sroot]).ok:
            info("staging root ready: %s (uid 501, the first account on both installs)" % sroot)
        else:
            warn("  could not create %s -- 'wk bench stage' will fail" % sroot)
        if act.dry_run():
            log("  would copy this Mac's Wi-Fi identity into the bench payload")
        else:
            self.wifi_conf("%s/%s/wifi.conf" % (self.s, PAYLOAD_DIR))
        for f in STALE:
            stale = "%s/%s/%s" % (self.s, PAYLOAD_DIR, f)
            if self.m.exists(stale):
                self.act(["sudo", "rm", "-f", stale], "could not remove %s" % stale)
                info("removed %s from the payload" % f)
        self.sudo_write(self.s + FIRSTBOOT, FIRSTBOOT_PLIST, "0644")
        self.act(["sudo", "chown", "root:wheel", self.s + FIRSTBOOT], "could not give the first-boot daemon to root")
        info("re-armed the first-boot daemon")
        log("")
        info("boot '%s' once more; it will finish provisioning and reboot itself" % self.volume)
        return 0

    def provision(self):
        here = self.disk("/").get("VolumeName", "?")
        if not self.m.isdir("/System/Volumes/Data") or here != self.volume:
            die("this is running on '%s', not on '%s'.\n  --provision writes the bench-mode marker, and writing it on the "
                "workstation\n  would make host mode claim to be bench mode. Boot '%s' first." % (here, self.volume, self.volume))
        info("provisioning '%s' as the benchmark install" % self.volume)
        if self.m.exists(MARKER):
            act.debug("ok: marker %s: %s" % (MARKER, self.marker_id(MARKER)))
        else:
            month = time.strftime("%Y-%m", time.gmtime(self.clock.now()))
            self.sudo_write(MARKER, "id=%s-%s\nprofile=%s\n" % (self.name, month, self.name))
            info("wrote %s" % MARKER)
        quiet_ok = self.quiet()
        hosts = lib_argv(self.root, HOSTS, "wk_bench_hosts_present", "/etc/hosts")
        if self.m.run(hosts).ok:
            act.debug("ok: hosts: update endpoints already denied")
        elif self.m.act_run(["sudo"] + lib_argv(self.root, HOSTS, "wk_bench_hosts_apply", "/etc/hosts")).ok:
            info("hosts: update endpoints denied")
        else:
            warn("  hosts: could not deny update endpoints in /etc/hosts (see above)")
        if not self.m.run(["/usr/bin/python3", "-c", "import objc"]).ok:   # Apple's python3 has pyobjc; a Homebrew one has not
            warn("/usr/bin/python3 cannot 'import objc' -- run-benchmark's prepare_env will fail")
            log("  xcode-select --install   (Command Line Tools; it is a GUI prompt)")
        if not self.m.run(["/usr/bin/python3", "-c", "import scipy"]).ok:
            log("scipy absent -- optional: /usr/bin/python3 -m pip install --user scipy")
            log("  (only needed to run 'wk bench compare' in bench mode)")
        self.record(quiet_ok)
        log("")
        info("still yours to check, and each is a run that otherwise looks like a hang:")
        log("  * one user, logged in AT THE CONSOLE. A browser driven over ssh with")
        log("    nobody at the screen has nowhere to draw.")
        log("  * this install's own ~/.ssh/authorized_keys -- two installs, two files.")
        log("  then, from the driving machine:  wk bench mac <ws> --preflight")
        return 0

    def quiet(self):
        """The permanent half of quieting, read back from the machine rather than trusted."""
        info("quieting the install permanently")
        if not self.m.act_run(["sudo"] + lib_argv(self.root, DESKTOP, "wk_quiet_desktop_system")).ok:
            warn("  the machine-wide half did not fully take (above)")
        if not self.m.act_run(lib_argv(self.root, DESKTOP, "wk_quiet_desktop_user")).ok:
            warn("  this account's desktop is not fully quiet (above)")
        log("  read back, every setting, from the machine:")
        probe = self.m.run(lib_argv(self.root, DESKTOP, "wk_quiet_desktop_probe")).out
        findings = (self.m.run(lib_argv(self.root, DESKTOP, "wk_quiet_desktop_findings", probe,
                                        "re-run: wk sysimage build %s --provision" % self.name)).out
                    + self.m.run(lib_argv(self.root, DESKTOP, "wk_quiet_cpu_findings", probe)).out)
        ok = Quiesce(self.root, self.m, self.clock, self.env, macos=True).render(findings) == 0
        if not ok:
            warn("  the '--' lines above are what this install still is not")
        log("    filevault: %s" % (self.m.run(["fdesetup", "status"]).out.splitlines() or [""])[0])
        log("    timemachine destinations: %d" % len([l for l in self.m.run(["tmutil", "destinationinfo"]).out.splitlines()
                                                      if l.startswith("Name")]))
        return ok

    def record(self, quiet_ok):
        """The first-boot daemon's log line, written only behind a clean readback: the A/B plants a job on its strength."""
        if act.dry_run():
            log("  would record 'provisioning complete' in %s -- but only on a readback" % FIRSTBOOT_LOG)
            log("    with no '--' line above, and this one has %s" % ("none" if quiet_ok else "some"))
        elif quiet_ok:
            self.sudo_write(FIRSTBOOT_LOG, "=== provisioning complete (wk sysimage build %s --provision, %s) ===\n"
                            % (self.name, self.clock.iso()), append=True)
            info("recorded 'provisioning complete' in %s" % FIRSTBOOT_LOG)
        else:
            warn("  not recorded as provisioned: the settings above are not a measured Mac's,")
            warn("  and that record is what 'wk bench ab --devices <mac>' plants a job on the strength of")

    def all(self, version):
        """Three steps that each say "nothing to do" can still leave no tailnet identity, so an installed volume is re-armed."""
        if not self.find_installer() and not version:
            die("no installer downloaded and no --version given.\n  'wk sysimage build %s --fetch' lists what this Mac "
                "is offered; then\n  'wk sysimage build %s --all --version <v>' does the rest in one go." % (self.name, self.name))
        if self.present() and self.installed():
            act.nothing_to_ask()
        elif not act.confirm("add '%s' if it is absent, then erase it and install macOS onto it -- this reboots the machine?"
                             % self.volume):
            log("nothing done")
            return 0
        self.create()
        if self.find_installer():
            info("installer already downloaded: %s" % self.find_installer())
        else:
            self.fetch(version)
        self.install()
        if self.present() and self.installed():
            info("the volume is installed; completing provisioning through first boot")
            self.repair()
        return 0


def main(argv, env=None, machine=None):
    """`stage-payload <root>`: the bench install converging itself, as root (lib/wk/bench/autorun.py)."""
    from wk.machine import here
    env = os.environ if env is None else env
    try:
        if argv[:1] == ["stage-payload"] and len(argv) == 2:
            stage_payload(machine or here(), images.root(env), argv[1], False, env)
        else:
            die("usage: python3 -m wk.sysimage.macvolume stage-payload <root>", 2)
    except Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
