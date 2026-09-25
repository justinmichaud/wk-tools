"""The pmos build as it runs on the build host (lib/wk/sysimage/pmos.py is the driving half). The driver copies
lib/wk there and runs `PYTHONPATH=<root>/lib python3 -m wk.sysimage.pmos_build remote-build|wifi-ssid ...`, so both
halves are one tree. Its output is the build log the driver follows."""

import argparse
import glob
import os
import re
import sys

from wk.act import Refused, die, info, log, warn
from wk.clock import Clock
from wk.machine import here

MODULE = "wk.sysimage.pmos_build"
WORK_VERSION = "8"
TRIES = 3
PMAPORTS = "https://gitlab.postmarketos.org/postmarketOS/pmaports.git"
PMBOOTSTRAP = "git+https://gitlab.postmarketos.org/postmarketOS/pmbootstrap.git@%s"
UPLINK = ("[connection]\nid=wk-uplink\ntype=wifi\nautoconnect=true\nautoconnect-retries=0\n\n"
          "[wifi]\nmode=infrastructure\nssid=%s\ncloned-mac-address=permanent\n\n"
          "[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n[ipv4]\nmethod=auto\n\n"
          "[ipv6]\nmethod=auto\naddr-gen-mode=default\n")
POWER = "[connection]\nwifi.powersave=2\n\n[device]\nwifi.scan-rand-mac-address=no\n"
NOSLEEP = "[Login]\nHandlePowerKey=ignore\nHandleSuspendKey=ignore\nHandleHibernateKey=ignore\nHandleLidSwitch=ignore\nIdleAction=ignore\n"


def argv_for(root, verb, *args):
    """The far command line: the copied tree on PYTHONPATH, this module as the program."""
    return ["env", "PYTHONPATH=%s/lib" % root, "python3", "-m", MODULE, verb] + list(args)


def read_wifi_credential(m):
    """The one WiFi reader for a phone's uplink: this host's own netplan association. PyYAML is netplan's own
    dependency (host/linux/apt.txt), and there is no stdlib YAML reader."""
    import yaml
    text = m.run(["sudo", "-n", "sh", "-c", "cat /etc/netplan/*.yaml 2>/dev/null"]).out
    if not text.strip():
        return None
    try:
        docs = [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError:
        return None
    for doc in docs:
        for _, dev in (doc.get("network", {}).get("wifis") or {}).items():
            for ssid, ap in (dev.get("access-points") or {}).items():
                psk = ((ap.get("auth") or {}).get("password") or ap.get("password") or "")
                if psk:
                    return {"ssid": ssid, "psk": psk}
    return None


def kconfig_delta(lines, opts):
    """Each `NAME=value` replaces NAME's line, set or `# NAME is not set`, or is appended."""
    out = list(lines)
    for opt in opts:
        name = opt.split("=", 1)[0]
        at = next((i for i, l in enumerate(out) if l.startswith(name + "=") or l == "# %s is not set" % name), None)
        if at is None:
            out.append(opt)
        else:
            out[at] = opt
    return out


def parser():
    ap = argparse.ArgumentParser(prog="python3 -m %s remote-build" % MODULE)
    for flag in ("--id", "--device", "--channel", "--pmb-version", "--hostname", "--keyfile", "--root"):
        ap.add_argument(flag, required=True)
    for flag, default in (("--ui", "phosh"), ("--user", "user"), ("--password", "147147"), ("--packages", ""),
                          ("--extra-space", "512"), ("--kernel-aport", ""), ("--kconfig", "")):
        ap.add_argument(flag, default=default)
    return ap


class Build:
    def __init__(self, a, m, clock):
        self.a, self.m, self.clock = a, m, clock
        self.venv, self.work = os.path.join(a.root, "venv"), os.path.join(a.root, "work")
        self.aports = os.path.join(self.work, "cache_git", "pmaports")
        self.cfg = os.path.join(a.root, a.device + ".cfg")
        self.out = os.path.join(a.root, "out", a.id)
        self.pmb = os.path.join(self.venv, "bin", "pmbootstrap")

    def ok(self, argv, why):
        if not self.m.run_tty(argv).ok:
            die(why)

    def sudo(self, *argv, **kw):
        r = self.m.run(["sudo", "-n"] + list(argv), **kw)
        if not r.ok:
            die("sudo %s failed: %s" % (" ".join(argv), r.err.strip()))
        return r

    def retried(self, argv, why):
        for n in range(TRIES):
            if self.m.run_tty(argv).ok:
                return
            if n + 1 < TRIES:
                warn("failed -- retrying in %ds" % ((n + 1) * 5))
                self.clock.sleep((n + 1) * 5)
        die(why)

    def has(self, prog):
        return self.m.run(["sh", "-c", 'command -v "$1" >/dev/null', "sh", prog]).ok

    def size(self, path):
        return self.m.run(["stat", "-c", "%s", path]).out.strip()

    def preflight(self):
        info("Preflight")
        u = os.uname()
        if u.sysname != "Linux":
            die("this is not Linux; pmbootstrap cannot run here")
        if u.machine != "aarch64":
            die("this host is %s and the phones are aarch64.\n    pmbootstrap would emulate the whole build with qemu -- hours instead of\n"
                "    minutes -- so this refuses. Build on an aarch64 machine." % u.machine)
        missing = [t for t in ("git", "xz") if not self.has(t)]
        missing += ["%s(%s)" % t for t in (("kpartx", "multipath-tools"), ("losetup", "util-linux")) if not self.has(t[0])]
        missing += [pkg for mod, pkg in (("ensurepip", "python3-venv"), ("yaml", "python3-yaml"))
                    if not self.m.run(["python3", "-c", "import " + mod]).ok]
        if missing:
            die("missing on this host: %s\n    sudo apt install -y multipath-tools python3-venv python3-yaml xz-utils git" % " ".join(missing))
        if not self.m.run(["sudo", "-n", "true"]).ok:
            die("sudo needs a password here.\n    pmbootstrap mounts chroots and loop devices; it cannot do that\n"
                "    non-interactively without passwordless sudo.")
        if not self.m.exists(self.a.keyfile):
            die("no public key at %s" % self.a.keyfile)

    def pmbootstrap(self):
        a = self.a
        info("pmbootstrap %s" % a.pmb_version)
        have = self.m.run([self.pmb, "--version"]).out.strip() if self.m.exists(self.pmb) else ""
        if have == a.pmb_version:
            log("    already installed")
            return have
        if not self.m.isdir(self.venv):
            self.ok([sys.executable, "-m", "venv", self.venv], "could not make a venv at %s" % self.venv)
        pip = os.path.join(self.venv, "bin", "pip")
        self.ok([pip, "install", "-q", "--upgrade", "pip"], "could not upgrade pip in %s" % self.venv)
        self.ok([pip, "install", "-q", PMBOOTSTRAP % a.pmb_version], "could not install pmbootstrap %s" % a.pmb_version)
        have = self.m.run([self.pmb, "--version"]).out.strip()
        log("    installed %s" % have)
        return have

    def work_folder(self):
        info("Work folder")
        stamp = os.path.join(self.work, "version")
        if not self.m.exists(stamp):
            self.m.mkdir(self.work)
            self.m.write(stamp, WORK_VERSION + "\n")
            log("    %s is version %s" % (self.work, WORK_VERSION))

    def pmaports(self):
        a, git = self.a, ["git", "-C", self.aports]
        info("pmaports (%s)" % a.channel)
        if not self.m.isdir(os.path.join(self.aports, ".git")):
            self.m.mkdir(os.path.dirname(self.aports))
            self.retried(["git", "clone", "-q", "--depth", "1", PMAPORTS, self.aports], "could not clone pmaports")
        self.retried(git + ["fetch", "-q", "--depth", "1", "origin", "+refs/heads/master:refs/remotes/origin/master"],
                     "could not fetch pmaports' master ref (channels.cfg lives there)")
        self.retried(git + ["fetch", "-q", "--depth", "1", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (a.channel, a.channel)],
                     "could not fetch the %s branch of pmaports" % a.channel)
        self.ok(git + ["checkout", "-q", "-B", a.channel, "refs/remotes/origin/%s" % a.channel], "could not check out %s" % a.channel)
        channels = self.m.run(git + ["show", "origin/master:channels.cfg"]).out
        if ("[%s]" % a.channel) not in channels:
            chans = "\n".join("      " + c for c in re.findall(r"^\[(v[0-9.]*|edge)\]$", channels, re.M))
            die("'%s' is not a channel in pmaports' channels.cfg.\n    Channels are the released ones:\n%s" % (a.channel, chans))
        rev = self.m.run(git + ["rev-parse", "--short", "HEAD"]).out.strip()
        log("    pmaports %s @ %s" % (a.channel, rev))
        return rev

    def config(self):
        a = self.a
        info("Config")
        self.m.write(self.cfg, "# Written by %s for %s. Edit the profile, not this.\n[pmbootstrap]\n"
                               "device = %s\nui = %s\nuser = %s\nhostname = %s\nwork = %s\naports = %s\n"
                               "ssh_keys = True\nssh_key_glob = %s\nsystemd = never\n"
                     % (MODULE, a.id, a.device, a.ui, a.user, a.hostname, self.work, self.aports, a.keyfile))
        log("    " + self.cfg)

    def kernel_delta(self):
        """pkgrel is bumped by 100 so the cached stock package cannot win; abuild refuses stale checksums."""
        a = self.a
        info("Kernel config delta (%s)" % a.kernel_aport)
        if not a.kernel_aport:
            die("--kconfig was given with no --kernel-aport to apply it to")
        kdir = os.path.join(self.aports, a.kernel_aport)
        if not self.m.isdir(kdir):
            die("no such aport in pmaports %s: %s" % (a.channel, a.kernel_aport))
        kcfg = next(iter(sorted(glob.glob(os.path.join(kdir, "config-*.aarch64")))), "")
        if not kcfg:
            die("no config-*.aarch64 in %s" % kdir)
        self.m.write(kcfg, "\n".join(kconfig_delta(self.m.read(kcfg).splitlines(), a.kconfig.split())) + "\n")
        log("".join("    %s\n" % o for o in a.kconfig.split()).rstrip("\n"))
        apk = os.path.join(kdir, "APKBUILD")
        text = self.m.read(apk)
        rel = re.search(r"^pkgrel=([0-9]+)$", text, re.M)
        if not rel:
            die("could not read pkgrel from %s" % apk)
        krel = int(rel.group(1))
        self.m.write(apk, re.sub(r"^pkgrel=%d$" % krel, "pkgrel=%d" % (krel + 100), text, flags=re.M))
        log("    pkgrel %d -> %d" % (krel, krel + 100))
        self.ok([self.pmb, "-c", self.cfg, "checksum", os.path.basename(a.kernel_aport)],
                "could not regenerate checksums for %s\n    The config was edited, so abuild will refuse the build without this." % a.kernel_aport)

    def install(self):
        a = self.a
        info("pmbootstrap install (%s, %s, +%sM)" % (a.device, a.ui, a.extra_space))
        self.m.run([self.pmb, "-c", self.cfg, "-y", "shutdown"])
        self.m.mkdir(self.out)
        for n in ("disk.wic.xz", "disk.bmap", "result"):
            self.m.remove(os.path.join(self.out, n))
        argv = [self.pmb, "-c", self.cfg, "-y", "-E", a.extra_space, "--details-to-stdout", "install", "--no-split", "--no-firewall",
                "--password", a.password] + (["--add", a.packages] if a.packages else [])
        if not self.m.run_tty(argv).ok:
            log_path = os.path.join(self.work, "log.txt")
            if self.m.exists(log_path):
                warn("pmbootstrap install failed; the last 40 lines of its own log:")
                log("".join("    | %s\n" % l for l in self.m.read(log_path).splitlines()[-40:]).rstrip("\n"))
            die("pmbootstrap install failed (its log: %s on this host)" % log_path)
        self.m.run([self.pmb, "-c", self.cfg, "-y", "shutdown"])
        img = next(iter(glob.glob(os.path.join(self.work, "chroot_native", "home", "pmos", "rootfs", a.device + ".img"))
                        + glob.glob(os.path.join(self.work, "**", a.device + ".img"), recursive=True)), "")
        if not img:
            die("pmbootstrap reported success but no %s.img is under %s" % (a.device, self.work))
        log("    image: %s" % img)
        return img

    def bootloader(self, img):
        """A card boots from the firmware pmbootstrap embeds at deviceinfo's offsets; all zeros there is no bootloader."""
        a = self.a
        info("Checking the bootloader pmbootstrap embedded")
        deviceinfo = next(iter(glob.glob(os.path.join(self.aports, "device", "*", "device-" + a.device, "deviceinfo"))), "")
        if not deviceinfo:
            die("no deviceinfo for %s under %s/device" % (a.device, self.aports))
        text = self.m.read(deviceinfo)
        embed = re.search(r'^deviceinfo_sd_embed_firmware="(.*)"$', text, re.M)
        step = re.search(r'^deviceinfo_sd_embed_firmware_step_size="?([0-9]*)', text, re.M)
        fw_step = int(step.group(1)) if step and step.group(1) else 1024
        if not (embed and embed.group(1)):
            warn("%s declares no sd_embed_firmware: this image carries no bootloader of its own,\n"
                 "  and a card written from it boots only if the device finds firmware somewhere else." % a.device)
            return
        for entry in embed.group(1).split():
            name, _, off = entry.rpartition(":")
            byte = int(off) * fw_step
            chunk = self.m.run(["sh", "-c", 'dd if="$1" bs=512 skip="$2" count=1 iflag=skip_bytes 2>/dev/null | od -An -v -tx1',
                                "sh", img, str(byte)]).out.split()
            nonzero = sum(1 for b in chunk if b != "00")
            if not nonzero:
                die("%s should be at %d KiB and that byte range is\n    all zeros. pmbootstrap did not embed it, and a card written from this "
                    "image\n    would not boot -- the phone would come up on its internal storage instead." % (os.path.basename(name), byte // 1024))
            log("    %s is at %d KiB (%d non-zero bytes in the first 512)" % (os.path.basename(name), byte // 1024, nonzero))

    def seed(self, img):
        a = self.a
        info("Seeding the image")
        cred = read_wifi_credential(self.m)
        if not cred:
            die("no WiFi credential found in this host's netplan.\n    The image needs one: the phone has no cable, so an image without it boots\n"
                "    into isolation. Build on a host that is on the WiFi the phone will use.")
        loop = self.sudo("losetup", "--show", "-P", "-f", img).out.strip()
        mnt = os.path.join(self.out, "mnt")
        try:
            self.m.mkdir(mnt)
            self.sudo("mount", loop + "p2", mnt)
            self.into(mnt, cred, a)
            self.sudo("sync")
        finally:
            self.m.run(["sudo", "-n", "umount", mnt])
            self.m.run(["sudo", "-n", "losetup", "-d", loop])
        if not self.m.run(["rmdir", mnt]).ok:   # rmdir, not a tree removal: a mount left behind is the image's rootfs
            die("%s is still mounted or not empty after the unmount; 'sudo umount %s' and re-run" % (mnt, mnt))
        return cred["ssid"]

    def put(self, path, text, mode):
        """Written root-owned at `mode` from stdin, so a secret never lands in a file anyone else can read first."""
        self.sudo("sh", "-c", 'umask 077; cat > "$1" && chmod "$2" "$1"', "sh", path, mode, input=text)

    def into(self, mnt, cred, a):
        nm = os.path.join(mnt, "etc", "NetworkManager")
        if not self.m.isdir(nm):
            die("the image's second partition has no /etc/NetworkManager -- that is not the rootfs")
        self.put(os.path.join(nm, "system-connections", "wk-uplink.nmconnection"), UPLINK % (cred["ssid"], cred["psk"]), "0600")
        log("    uplink: %s (the PSK stayed on this host)" % cred["ssid"])
        user_keys = os.path.join(mnt, "home", a.user, ".ssh", "authorized_keys")
        if self.m.run(["sudo", "-n", "test", "-f", user_keys]).ok:
            self.sudo("install", "-d", "-o", "0", "-g", "0", "-m", "0700", os.path.join(mnt, "root", ".ssh"))
            self.sudo("install", "-o", "0", "-g", "0", "-m", "0600", user_keys, os.path.join(mnt, "root", ".ssh", "authorized_keys"))
            log("    root: the same ssh key, so provisioning needs no password on the phone")
        else:
            warn("no authorized_keys for '%s' in the image -- root gets no key either." % a.user)
        self.sudo("install", "-d", "-o", "0", "-g", "0", "-m", "0755", os.path.join(nm, "conf.d"))
        self.put(os.path.join(nm, "conf.d", "99-wk-bridge-reachable.conf"), POWER, "0644")
        log("    power save off and scan MAC pinned, so the phone answers before it is provisioned")
        elogind = os.path.join(mnt, "etc", "elogind")
        if self.m.isdir(elogind):
            self.sudo("install", "-d", "-o", "0", "-g", "0", "-m", "0755", os.path.join(elogind, "logind.conf.d"))
            self.put(os.path.join(elogind, "logind.conf.d", "10-wk-bridge-nosleep.conf"), NOSLEEP, "0644")
            log("    suspend disabled in the image, so the first provision is not a race")
        else:
            warn("no /etc/elogind in the image -- cannot disable suspend before first boot")

    def compress(self, img, fields):
        info("Compression")
        xz = os.path.join(self.out, "disk.wic.xz")
        words = self.m.run(["sha256sum", img]).out.split()
        if not words:
            die("could not hash %s" % img)
        if not self.m.run(["sh", "-c", 'xz -T0 -3 -c "$1" > "$2"', "sh", img, xz]).ok:
            die("could not compress %s" % img)
        fields.update(raw_bytes=self.size(img), raw_sha256=words[0], wic_xz_bytes=self.size(xz))
        result = "".join("%s=%s\n" % kv for kv in fields.items())
        self.m.write(os.path.join(self.out, "result"), result)
        info("Done")
        log("    " + xz)
        log(result.rstrip("\n"))

    def run(self):
        a = self.a
        self.preflight()
        have = self.pmbootstrap()
        self.work_folder()
        rev = self.pmaports()
        self.config()
        if a.kconfig:
            self.kernel_delta()
        img = self.install()
        self.bootloader(img)
        ssid = self.seed(img)
        self.compress(img, dict(device=a.device, ui=a.ui, channel=a.channel, pmaports_rev=rev, pmbootstrap=have or a.pmb_version,
                                user=a.user, hostname=a.hostname, uplink_ssid=ssid))
        return 0


def main(argv):
    verb, rest = (argv[0], argv[1:]) if argv else ("", [])
    m = here()
    try:
        if verb == "remote-build":
            return Build(parser().parse_args(rest), m, Clock()).run()
        if verb == "wifi-ssid" and not rest:
            cred = read_wifi_credential(m)
            sys.stdout.write((cred["ssid"] if cred else "") + "\n")
            return 0
        die("usage: python3 -m %s remote-build <options> | wifi-ssid" % MODULE, 2)
    except Refused as e:
        return e.status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
