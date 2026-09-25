"""The pmos builder's driving half: a postmarketOS system for a phone, built over ssh because pmbootstrap is
Linux-only, needs root (loop devices, chroots, kpartx), and never starts qemu on aarch64. The build itself is
lib/wk/sysimage/pmos_build.py, run on the build host from the copy of lib/wk this pushes there."""

import hashlib
import os
import re
import shlex

from wk import act, images, job, reach
from wk.act import die, info, log, warn
from wk.machine import Ssh
from wk.rubble import row
from wk.sysimage import pmos_build, task
from wk.sysimage.ls import human_bytes

RUNNING_PATTERN = "wk[.]sysimage[.]pmos_build"   # bracketed: pgrep -f would otherwise match the ssh line carrying this very check
KEEPALIVE_OPTS = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]   # the build host roams on WiFi
BUILD_USAGE = "usage: wk sysimage build %s [--dry-run|--detach|--resume]"
HASH_BLOCK = 1 << 20
PROVISION = "wk machine setup %s --disk <machine>:<device>"


def ssh_machine(fl, env, via, host):
    """The ssh destination for a fleet name (its NODE_SSH, if it has one) or a raw hostname passed through."""
    conf = fl.load(host)
    dest = (conf or {}).get("NODE_SSH") or host
    opts = list(KEEPALIVE_OPTS)
    if (conf or {}).get("NODE_ROLE") == "bench-device":
        opts += ["-l", "root"] + reach.UNPINNED
    return Ssh(dest, opts=opts, timeout=int(env.get("WK_SSH_TIMEOUT") or 10), via=via)


def host_for(p, env):
    h = env.get("WK_PMOS_HOST") or (p or {}).get("PMO_BUILD_HOST", "")
    if not h:
        die("this pmos profile sets no PMO_BUILD_HOST (image/profiles.sh)")
    return h


def sh(machine, text, timeout=None):
    return machine.run(["sh", "-c", text], timeout=timeout)


def act_sh(machine, text):
    return machine.act_run(["sh", "-c", text])


def ask(machine, text):
    """A best-effort remote question: ssh's own failure is not an error, only an empty answer."""
    return sh(machine, "%s 2>/dev/null || true" % text).out.replace("\r", "")


def home_dir(machine):
    h = sh(machine, "echo $HOME").out.strip()
    if not h:
        die("could not read $HOME on %s" % machine.name)
    return h


def root_dir(machine, env):
    return env.get("WK_PMOS_ROOT") or (home_dir(machine) + "/wk-pmos")


def out_dir(root, id_):
    return "%s/out/%s" % (root, id_)


def newest_out(machine, env, profile_name):
    """The newest build with a result block: "finished" is that block, not merely an rc file (a resumed build has an rc but no result yet)."""
    root = root_dir(machine, env)
    for d in ask(machine, "ls -1t %s" % shlex.quote(os.path.join(root, "out"))).splitlines():
        if d.startswith(profile_name + "-") and sh(machine, "test -f %s" % shlex.quote(os.path.join(root, "out", d, "result"))).ok:
            return d
    return None


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(HASH_BLOCK), b""):
            h.update(block)
    return h.hexdigest()


def fetch_out(machine, env, id_, dest, here=None):
    """The hash is the one the build host computed: the only check that works from a Mac, with no sfdisk."""
    here = here or machine.via
    out = out_dir(root_dir(machine, env), id_)
    result = sh(machine, "cat %s" % shlex.quote(out + "/result"))
    if not result.ok:
        die("the build on %s left no result block at %s.\n    It did not get as far as producing an image. "
            "The log is at %s/build.log\n    on that machine."
            % (machine.name, out, out))
    there = next((l.split("=", 1)[1] for l in result.out.splitlines() if l.startswith("raw_sha256=")), "")
    info("copying %s off %s" % (id_, machine.name))
    try:
        machine.copy_out(out + "/disk.wic.xz", dest + ".xz")
    except OSError as e:
        die("could not copy the image off %s: %s" % (machine.name, e))
    if not here.act_run(["xz", "-d", "-f", dest + ".xz"]).ok:
        die("could not decompress %s.xz" % dest)
    if act.dry_run():
        return dest
    got = file_sha256(dest)
    if there != got:
        die("the image does not survive the trip:\n    on %s: %s\n    here:            %s" % (machine.name, there, got))
    return dest


def build_hosts(env):
    hosts = set()
    for name in images.names(env):
        p = images.quiet_load(name, env)
        if p and p["IMG_BUILDER"] == "pmos":
            h = env.get("WK_PMOS_HOST") or p["PMO_BUILD_HOST"]
            if h:
                hosts.add(h)
    return sorted(hosts)


def cache_probe(machine, env):
    """{"work": kb, "out": kb} of what exists; None when the host did not answer, since {} would be a measurement."""
    root_expr = env.get("WK_PMOS_ROOT") or "$HOME/wk-pmos"
    out = sh(machine, "du -sk %s/work %s/out 2>/dev/null || true" % (root_expr, root_expr))
    if not out.ok:
        return None
    kb = {}
    for line in out.out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            kb[os.path.basename(parts[1].rstrip("/"))] = int(parts[0])
    return {k: v for k, v in kb.items() if k in ("work", "out")}


def prune(machine, env, host):
    """No rc file is an interrupted build that cannot be resumed, so it goes; a finished one is kept, newest per profile."""
    root = root_dir(machine, env)
    seen = set()
    for d in ask(machine, "ls -1t %s" % shlex.quote(os.path.join(root, "out"))).splitlines():
        if not d or d.startswith("probe-"):
            continue
        rc = ask(machine, "cat %s" % shlex.quote(os.path.join(root, "out", d, "build.rc"))).strip()
        entry = os.path.join(root, "out", d)
        if not rc:
            info("removing an interrupted build on %s: %s" % (host, d))
            act_sh(machine, "rm -rf %s" % shlex.quote(entry))
            continue
        profile = d.rsplit("-", 1)[0]
        if profile in seen:
            size = ask(machine, "du -sh %s | cut -f1" % shlex.quote(entry)).strip()
            info("removing an old build on %s: %s (%s)" % (host, d, size))
            act_sh(machine, "rm -rf %s" % shlex.quote(entry))
        else:
            seen.add(profile)


def purge_work(machine, env, host, kb):
    """Each chroot is shut down first, or its bind mounts follow the rm into the host's own /proc and /dev."""
    root = root_dir(machine, env)
    shutdown = ("for c in %s/*.cfg; do [ -f \"$c\" ] || continue; "
                "%s/venv/bin/pmbootstrap -c \"$c\" -y shutdown >/dev/null 2>&1 || true; done; rm -rf %s"
                % (root, root, shlex.quote(os.path.join(root, "work"))))
    if not act_sh(machine, shutdown).ok:
        warn("could not erase the work folder on %s" % host)
        return False
    info("erased %s of pmbootstrap chroots on %s" % (human_bytes(kb * 1024), host))
    return True


def rubble(hosts, machine_for, env):
    """Per build host: its builds (a plain `wk gc` prunes all but the newest finished one per profile) and its chroots."""
    rows = []
    for host in hosts:
        m = machine_for(host)
        kb = cache_probe(m, env)
        if kb is None:
            rows.append(row("pmos", "%s: pmos build host" % host, None, why="not looked at -- %s did not answer" % host))
            continue
        if "out" in kb:
            rows.append(row("pmos-builds", "%s: pmos builds, the newest per profile stays" % host, kb["out"],
                            take=lambda m=m, h=host: prune(m, env, h)))
        if kb.get("work"):
            rows.append(row("pmos-work", "%s: pmbootstrap chroots, refetched by the next build" % host, kb["work"], "--purge-pmos",
                            lambda m=m, h=host, k=kb["work"]: purge_work(m, env, h, k)))
    return rows


class Pmos:
    def __init__(self, reg, profile, spec, clock):
        self.reg, self.p, self.spec, self.clock = reg, profile, spec, clock
        self.name = profile["IMG_PROFILE"]
        self.here, self.env, self.fleet = reg.machine, reg.env, reg.fleet

    def host(self):
        return host_for(self.p, self.env)

    def machine(self):
        return ssh_machine(self.fleet, self.env, self.here, self.host())

    def key_path(self):
        return self.env.get("WK_IMAGE_KEY") or os.path.join(self.env.get("HOME") or os.path.expanduser("~"), ".ssh", "id_ed25519.pub")

    def dry_run(self):
        p, machine = self.p, self.machine()
        log("would build image %s" % self.name)
        log("  profile     %s (pmos builder)" % self.name)
        log("  device      %s (%s), pmOS channel %s, UI %s" % (p["PMO_DEVICE"], p["IMG_ARCH"], p["PMO_CHANNEL"], p["PMO_UI"]))
        log("  for bridge  %s" % (p["PMO_BRIDGE"] or "none"))
        log("  hostname    %s" % p["IMG_HOSTNAME"])
        log("  build on    %s (%s), pmbootstrap %s in a venv there" % (self.host(), machine.name, p["PMO_PMB_VERSION"]))
        log("  packages    %s (on top of postmarketos-base and the UI)" % (p["PMO_PACKAGES"] or "none"))
        log("  ssh key     %s" % self.key_path())
        log("  uplink      %s's own WiFi credential, copied on %s (the PSK never leaves it)" % (machine.name, machine.name))
        log("  radio       %s GHz -- the build refuses if that SSID is not on the air in a" % (p["PMO_WIFI_BANDS"].replace(" ", "/") or "unknown"))
        log("              band this phone has, because such an image boots into isolation")
        log("  console     user '%s', password '%s' (the phone's screen; ssh is key-only)" % (p["PMO_USER"], p["PMO_PASSWORD"]))
        log("  into        %s/out/<id> on %s" % (self.env.get("WK_PMOS_ROOT") or "$HOME/wk-pmos", machine.name))
        log("  writes to   a card -- '%s' copies it off and writes it," % (PROVISION % (p["PMO_BRIDGE"] or "<bridge>")))
        log("              or by hand once it is local: wk sysimage write --from <path> --disk <machine>:<device>")
        log("dry run -- nothing was built.")
        return 0

    def ensure_packages(self, machine):
        missing = []

        def need(prog, pkg):
            if not sh(machine, "command -v %s >/dev/null 2>&1" % shlex.quote(prog)).ok:
                missing.append(pkg)
        need("kpartx", "multipath-tools")
        need("xz", "xz-utils")
        need("rsync", "rsync")
        if not sh(machine, "python3 -c 'import ensurepip'").ok:
            missing.append("python3-venv")
        if not sh(machine, "python3 -c 'import yaml'").ok:
            missing.append("python3-yaml")
        if missing:
            die("%s is missing what pmbootstrap needs: %s\n    './setup' on that machine installs them (host/linux/apt.txt); by hand:\n"
                "        ssh %s sudo apt install -y %s" % (machine.name, " ".join(missing), machine.name, " ".join(missing)))

    def push_tree(self, machine, root):
        machine.mkdir(os.path.join(root, "lib"))
        try:
            machine.copy_tree_in(os.path.join(images.root(self.env), "lib", "wk"), os.path.join(root, "lib", "wk"))
        except OSError as e:
            die("could not copy lib/wk to %s: %s" % (machine.name, e))

    def check_uplink_band(self, machine, root):
        bands = self.p["PMO_WIFI_BANDS"]
        if not bands:
            act.debug("profile declares no PMO_WIFI_BANDS -- not checking the uplink band")
            return
        want24, want5 = "2.4" in bands.split(), "5" in bands.split()
        ssid = machine.run(pmos_build.argv_for(root, "wifi-ssid")).out.strip()
        if not ssid:
            warn("%s cannot currently read its own WiFi SSID -- leaving the band unchecked" % machine.name)
            return
        freqs = self._ssid_freqs(machine, ssid)
        if not freqs:
            warn("%s cannot currently see '%s' on the air, so which bands it" % (machine.name, ssid))
            warn("  offers could not be checked. %s's radio is %s GHz." % (self.p["PMO_DEVICE"], bands))
            return
        seen, hit = set(), False
        for f in freqs:
            if f < 3000:
                seen.add("2.4")
                hit = hit or want24
            else:
                seen.add("5")
                hit = hit or want5
        if hit:
            act.debug("'%s' is on the air in a band %s supports" % (ssid, self.p["PMO_DEVICE"]))
            return
        die("'%s' is only being broadcast on %s GHz, and %s's radio is %s GHz only.\n    The image copies its WiFi credential "
            "from %s's own association, so it\n    would be built with a valid PSK for a network the phone's hardware cannot\n"
            "    see -- and a phone with no uplink has no way in at all.\n    The fix is on the access point, not here: broadcast "
            "'%s' on %s GHz as well." % (ssid, "/".join(sorted(seen)), self.p["PMO_DEVICE"], bands.replace(" ", "/"),
                                        machine.name, ssid, bands.replace(" ", "/")))

    @staticmethod
    def _ssid_freqs(machine, ssid):
        freqs = set()
        ifaces = re.findall(r"Interface (\S+)", ask(machine, "iw dev"))
        for i in ifaces:
            for how in ("scan", "scan dump"):
                out = ask(machine, "sudo -n iw dev %s %s" % (shlex.quote(i), how))
                f = None
                for line in out.splitlines():
                    if line.startswith("BSS"):
                        f = None
                    elif "freq:" in line:
                        f = line.split("freq:", 1)[1].split()[0]
                    elif line.strip().startswith("SSID:") and f:
                        if line.strip()[len("SSID:"):].strip() == ssid:
                            try:
                                freqs.add(int(float(f)))
                            except ValueError:
                                pass
        return freqs

    def build(self, rest):
        o = task.options(rest, ("--dry-run", "--detach", "--resume"), (), BUILD_USAGE % self.name)
        p = self.p
        if act.dry_run():
            return self.dry_run()
        machine = self.machine()
        if not sh(machine, "true").ok:
            die("cannot ssh to %s -- that is the build host for this profile.\n"
                "    pmbootstrap is Linux-only and needs root, so the build happens there.\n"
                "    Another machine: WK_PMOS_HOST=<name> wk sysimage build %s" % (machine.name, self.name))
        root = root_dir(machine, self.env)
        if o.get("--resume"):
            id_ = self._find_build(machine, root)
            if not id_:
                die("no build to resume on %s for '%s'.\n    'wk sysimage build %s' starts one." % (machine.name, self.name, self.name))
            info("resuming %s on %s" % (id_, machine.name))
            if self._running(machine, root, id_):
                self._follow(machine, root, id_)
            else:
                rc = ask(machine, "cat %s" % shlex.quote(out_dir(root, id_) + "/build.rc")).strip()
                if rc != "0":
                    die("that build failed (exit %s); its log is %s/build.log on %s" % (rc, out_dir(root, id_), machine.name))
                info("it has already finished")
        else:
            self.ensure_packages(machine)
            self._refuse_if_running(machine, root)
            self.push_tree(machine, root)
            self.check_uplink_band(machine, root)
            id_ = "%s-%s" % (self.name, self.clock.stamp())
            self._spawn(machine, root, id_)
            if o.get("--detach"):
                info("building %s on %s; this connection is not involved" % (id_, machine.name))
                log("  follow:  ssh %s tail -f %s" % (machine.name, out_dir(root, id_) + "/build.log"))
                log("  finish:  wk sysimage build %s --resume   (waits for it, then reports where it is)" % self.name)
                return 0
            self._follow(machine, root, id_)

        out = out_dir(root, id_)
        if not sh(machine, "test -f %s" % shlex.quote(out + "/result")).ok:
            die("the build on %s left no result block at %s.\n    It did not get as far as producing an image. The log is at %s/build.log\n"
                "    on that machine." % (machine.name, out, out))
        info("built %s on %s -- %s/disk.wic.xz" % (id_, machine.name, out))
        log("  the rest of the way:  " + PROVISION % (p["PMO_BRIDGE"] or "<bridge>"))
        log("  ...or by hand: copy disk.wic.xz off %s, then" % machine.name)
        log("             wk sysimage write --from <path> --disk <machine>:<device>")
        return 0

    def _find_build(self, machine, root):
        prefix = self.name + "-"
        for d in ask(machine, "ls -1t %s" % shlex.quote(os.path.join(root, "out"))).splitlines():
            if d.startswith(prefix):
                return d
        return None

    @staticmethod
    def _running(machine, root, id_):
        return not sh(machine, "test -f %s" % shlex.quote(out_dir(root, id_) + "/build.rc")).ok

    def _spawn(self, machine, root, id_):
        prune(machine, self.env, self.host())
        out = out_dir(root, id_)
        machine.mkdir(out)
        keyfile = self.key_path()
        if not os.path.isfile(keyfile):
            die("no public key at %s\n    The image has to accept an ssh key on first boot. Set WK_IMAGE_KEY." % keyfile)
        with open(keyfile) as f:
            machine.write(os.path.join(root, "driving-key.pub"), f.read())
        info("starting the build on %s (it survives this connection)" % machine.name)
        p = self.p
        argv = pmos_build.argv_for(root, "remote-build",
                "--id", id_, "--device", p["PMO_DEVICE"], "--channel", p["PMO_CHANNEL"], "--pmb-version", p["PMO_PMB_VERSION"],
                "--hostname", p["IMG_HOSTNAME"], "--keyfile", os.path.join(root, "driving-key.pub"), "--root", root,
                "--ui", p["PMO_UI"] or "phosh", "--user", p["PMO_USER"] or "user", "--password", p["PMO_PASSWORD"] or "147147",
                "--extra-space", p["PMO_EXTRA_SPACE"] or "512")
        if p["PMO_PACKAGES"]:
            argv += ["--packages", p["PMO_PACKAGES"]]
        if p["PMO_KERNEL_APORT"]:
            argv += ["--kernel-aport", p["PMO_KERNEL_APORT"]]
        if p["PMO_KCONFIG"]:
            argv += ["--kconfig", p["PMO_KCONFIG"]]
        line = job.remote_line(argv, out + "/build.log", out + "/build.rc")
        act_sh(machine, line)
        self.clock.sleep(3)

    @staticmethod
    def _refuse_if_running(machine, root):
        running = ask(machine, "pgrep -f %s >/dev/null && echo yes" % shlex.quote(RUNNING_PATTERN)).strip()
        if running == "yes":
            die("a pmos build is already running on %s.\n    Two at once would fight over the same chroots and loop devices. Wait for it,\n"
                "    or watch it:  ssh %s tail -f %s/out/*/build.log" % (machine.name, machine.name, root))

    def _follow(self, machine, root, id_):
        info("following the build on %s -- ^C stops watching, not building" % machine.name)
        out = out_dir(root, id_)

        def ask_line(line):
            return sh(machine, line)
        word, ok = job.wait_remote(ask_line, out + "/build.log", out + "/build.rc", self.clock, interval=5, stream=True, env=self.env)
        self._report_follow(machine, out, word, ok)

    def _report_follow(self, machine, out, word, ok):
        """`ok` false is `wait_remote` giving up (a timeout or an abort pattern), not a status the far side wrote."""
        if not ok:
            die("lost track of the build on %s.\n    It may still be running. 'wk sysimage build %s --resume' picks it\n"
                "    back up; the log is %s/build.log." % (machine.name, self.name, out))
        if word != "0":
            die("the build failed on %s (exit %s). Its own output is above, and the whole log is %s/build.log there." % (machine.name, word, out))
