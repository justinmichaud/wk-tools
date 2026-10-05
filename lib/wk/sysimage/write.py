"""`wk sysimage write`: an image's own bytes streamed onto a disk attached to a fleet machine, then made a fleet system
on the card by that machine's card helper (admin/wk-card-priv), so the driving machine never opens the image."""

import base64
import os
import re
import shlex
import tempfile

from wk import act, fleet, images, reach, record, tailnet
from wk.boot import cli as bootcli, driver_class
from wk.boot.driver import Channel, disk_of, part
from wk.kv import ConfError, kv
from wk.machine import HAVE, is_macos
from wk.store import Store
from wk.sysimage import disk
from wk.sysimage import ls as lsmod

CARD_PRIV = disk.CARD_PRIV
FILTERS = ((".xz", "xz -dc"), (".zst", "zstd -dc"), (".gz", "gzip -dc"))
ROOT_WORDS = {"mmc": "an SD card (/dev/mmcblk*)", "usb": "a USB or SCSI disk (/dev/sd*)", "nvme": "an NVMe disk"}
UPDATE = "Remedy, from a terminal on %s: update its wk-tools checkout, then  ./setup --stage quiesce"
REPORT = ("stream_bytes", "stream_sha", "boot_bytes", "boot_sha", "root_bytes", "root_sha")

# stdin to stdout unchanged; the count and sha256 go to fd 3 in one write, so they cannot interleave with the helper's.
METER = """import hashlib, os, sys
out, digest, n = sys.stdout.buffer, hashlib.sha256(), 0
while True:
    chunk = sys.stdin.buffer.read(1 << 20)
    if not chunk:
        break
    n += len(chunk)
    digest.update(chunk)
    out.write(chunk)
out.flush()
os.write(3, ("stream_bytes=%d\\nstream_sha=%s\\n" % (n, digest.hexdigest())).encode())
"""


def base(dev):
    return dev.split("@")[0]


def bare(src):
    return src[3:] if src.startswith("vm:") else src


def from_filter(path):
    """A compressed stream written straight to a disk makes a card that is not bootable, silently."""
    return next((f for ext, f in FILTERS if path.endswith(ext)), "cat")


def image_id(name, sha):
    return "%s-%s" % (name, sha[:12]) if sha else "%s-<the written image sha256>" % name


def root_class(spec):
    if spec.startswith(("LABEL=", "UUID=", "PARTUUID=")):
        return "portable"
    if spec == "/dev/nfs" or "nfsroot" in spec:
        return "network"
    return (disk.tran_of_name(spec) or "unknown") if spec else "unknown"


def check_root(spec, dev, what, env):
    """A system whose kernel looks for its root on another kind of device than the one it is on never boots."""
    cls, want = root_class(spec), disk.tran_of_name(dev) or "unknown"
    if cls not in ROOT_WORDS or cls == want:
        return
    c, w = ROOT_WORDS[cls], ROOT_WORDS.get(want, "an unrecognised kind of device")
    if env.get("WK_ANY_ROOT"):
        act.warn("this system expects %s and %s is %s;\n  left as written (WK_ANY_ROOT): it proves the transfer only."
                 % (c, dev, w))
        return
    act.die("the system on %s expects to boot from %s, and %s is %s (its cmdline says root=%s).\n"
            "    Write it to %s on %s, or rebuild the image for this device (a wic image's root comes from its wks file);\n"
            "    WK_ANY_ROOT=1 writes it anyway, to test the transfer." % (dev, c, dev, w, spec, c, what))


def collides(name, peers):
    """"exact:<peer>" or "suffixed:<peer>" (a `<name>-N` is the trace of an earlier rename), or "" when it is free."""
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9-]*$", name or ""):
        return ""
    want = name.lower()
    for row in peers:
        n = row[0].lower()
        if n == want:
            return "exact:" + row[0]
        if re.match("^%s-[0-9]+$" % re.escape(want), n):
            return "suffixed:" + row[0]
    return ""


def appended(root, p, name):
    """The board's file first -- `os_check=0` is a Pi 5 firmware fact every image needs -- then the preset's."""
    machine, spec_dir = p.get("IMG_MACHINE", ""), p.get("IMG_SPEC_DIR", "")
    text = ""
    for path in (os.path.join(str(root), "image", "boards", machine, name) if machine else "",
                 os.path.join(spec_dir, name) if spec_dir else ""):
        if path and os.path.isfile(path):
            with open(path) as f:
                text += f.read()
    return text


def cmdline_add(root, p):
    lines = [l for l in appended(root, p, "cmdline.txt.append").splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return " ".join(" ".join(lines).split())


def config_add(root, p):
    return appended(root, p, "config.txt.append")


def load_machine(fl, name):
    return bootcli.load_conf(fl.root, name, fl.env) if name else None


def machine_list(fl):
    return "\n".join("      " + line for line in bootcli.listing(fl.root, fl.env).splitlines())


def wants_wifi(fl, name):
    conf = load_machine(fl, name)
    return bool(conf and conf.get("device") and conf.get("net") == "wifi")


def tailnet_name(fl, name, role):
    """A bench system joins as bench_ssh, a rescue as ssh: a second join under an existing name comes up renamed."""
    return (load_machine(fl, name) or {}).get("ssh" if role == "rescue" else "bench_ssh", "")


def unit(root, name, **lines):
    """boot/firstboot/<name> verbatim; its parameters are KEY=value lines in the file's last section."""
    with open(os.path.join(str(root), "boot", "firstboot", name)) as f:
        return f.read() + "".join("%s=%s\n" % kv for kv in lines.items())


def init_script(root, name, **params):
    with open(os.path.join(str(root), "boot", "onboard", name)) as f:
        return "#!/bin/sh\n" + "".join("%s=%s\n" % (k, shlex.quote(v)) for k, v in params.items()) + f.read()


def stage_units(root, watchdog, disarm, preset=""):
    """{archive path: text}. The units that hand a machine back are gated at runtime on /etc/wk/rescue, so one artifact
    serves both roles; a timer, since a sleeping oneshot holds its systemd target inactive for the whole watchdog."""
    out = {}
    if not watchdog:
        act.warn("%s sets no IMG_WATCHDOG, so this image will not hand its machine back" % (preset or "this image"))
    else:
        out["systemd/wk-self-return.timer"] = unit(root, "wk-self-return.timer", OnBootSec=watchdog)
        out["systemd/wk-self-return.service"] = unit(root, "wk-self-return.service")
        out["init.d/S99wk-self-return"] = init_script(root, "S99wk-self-return", WK_WATCHDOG=watchdog)
    if disarm:
        out["systemd/wk-self-disarm.service"] = unit(root, "wk-self-disarm.service",
                                                     ExecStart="/bin/sh -c '%s'" % disarm.replace("$", "$$"))
        out["init.d/S11wk-self-disarm"] = init_script(root, "S11wk-self-disarm", WK_DISARM=disarm)
    out["sysctl.d/90-wk-perf.conf"] = unit(root, "90-wk-perf.conf")
    for name in ("wk-cpu-governor.service", "wk-no-swap.service", "wk-diag.service"):
        out["systemd/" + name] = unit(root, name)
    return out


def b64(text):
    return base64.b64encode(text.encode()).decode()


def said(r):
    return (r.out + r.err).replace("\r", "").rstrip("\n")


def indent(text, by="    "):
    return "\n".join(by + l for l in text.splitlines())


class Piped:
    """A Machine whose every command reads `reader`'s stdout: bytes too large for a Result reach the card machine this way."""

    def __init__(self, machine, reader):
        self.machine, self.reader = machine, reader

    def _argv(self, argv):
        return ["bash", "-o", "pipefail", "-c", "%s | %s" % (shlex.join(self.reader), shlex.join(argv))]

    def run(self, argv, input=None, timeout=None):
        return self.machine.run(self._argv(argv))

    def act_run(self, argv, **kw):
        return self.machine.act_run(self._argv(argv))


class Write:
    def __init__(self, root, env, machine, store, rand=None):
        self.root, self.env, self.machine, self.store = str(root), env, machine, store
        self.fleet = fleet.Fleet(self.root, env)
        self.rand = rand or (lambda: os.urandom(4).hex())
        self.plan = []
        self.conf = self.ch = self.drv = self.disks = None

    def step(self, sentence):
        self.plan.append(sentence)
        if act.dry_run():
            act.log("  would " + sentence)
            return True
        return False

    def c(self, key):
        return self.conf.get(key, "")

    def attach(self, conf):
        self.conf = conf
        self.ch = Channel(self.root, conf, "none", env=self.env, via=self.machine)
        self.drv = driver_class(conf["driver"])(self.root, conf, self.ch)
        self.disks = disk.Disks(self.ch, conf)

    def card(self, *args, input=None, mutates=True):
        return self.ch.call("card_priv", *args, input=input, mutates=mutates)

    def piped(self, reader):
        return self.ch.through(Piped(self.machine, reader))

    def ssh(self, command, mutates=False):
        return self.ch.call("m_ssh", command, input="", mutates=mutates)

    def resolve(self, spec):
        if spec.startswith(("vm:", "/", "./", "../")):
            return spec
        found = lsmod.scan(self.machine, self.store)
        path = next((i.path for i in found if i.path and images.ws_preset(i.ws, self.env) == spec), "")
        if not path and images.quiet_load(spec, self.env):
            r = self.machine.run([os.path.join(self.root, "wk"), "sysimage", "path", spec])
            path = r.out.replace("\r", "").strip() if r.ok else ""
            if not path:
                act.die("'%s' is an image preset whose image workspace holds no image:\n        wk sysimage build %s"
                        % (spec, spec))
            # The image workspace's machine answers in its own spelling; on a macOS workstation, the podman VM's.
            path = path if Store(self.env).is_local() else "vm:" + path
        if path:
            act.info("'%s' is an image preset; its image is at %s" % (spec, path))
            return path
        built = sorted({images.ws_preset(i.ws, self.env) for i in found if i.path} - {None})
        act.die("'%s' is neither a path nor an image preset this checkout defines.\n    Image presets with an image here:\n%s"
                % (spec, "\n".join("      " + b for b in built)))

    def reader(self, src):
        if src.startswith("vm:"):
            if not is_macos():
                act.die("--from vm:<path> reads out of a macOS host's podman VM; give a plain path here.")
            return ["podman", "machine", "ssh", Store(self.env).podman_machine(), "--", "sudo", "cat", bare(src)]
        if not self.machine.exists(src):
            act.die("no image at %s\n    An image inside this machine's podman VM is --from vm:%s" % (src, src))
        return ["cat", src]

    def image_preset(self, name, src):
        if not name:
            m = re.search(r"/ws/([^/]*)/", bare(src))
            name = (images.ws_preset(m.group(1), self.env) if m else None) or ""
        if not name:
            return "", {}
        try:
            return name, images.load(name, self.env)
        except ConfError as e:
            act.die(str(e))
        except LookupError:
            act.warn("no image preset '%s', so there is no firmware check and no tailnet name to seed; pass --image-preset"
                     % name)
            return name, {}

    def key_preflight(self, img_machine, role):
        if not tailnet_name(self.fleet, img_machine, role):
            act.die("this image joins the tailnet on first boot, and the image records no machine to name it after:\n"
                    "    it would come up unreachable by its fleet name. Give it one:  --machine <name>")
        if not tailnet.Fleet(self.root, self.env, self.machine).key_present():
            act.die("there is no tailnet auth key here, so the card would boot with no tailnet identity.\n"
                    "    Set one first:  wk key set tailnet")

    def wifi_preflight(self, img_machine):
        """No --force: a board with no uplink is unreachable, which is worse than refusing."""
        if not wants_wifi(self.fleet, img_machine):
            return
        name = self.c("name")
        r = self.card("wifi-host", mutates=False)
        if r.ok and "wifi-host: yes" in r.out + r.err:
            return
        act.die("%s has no cable, and its card takes its WiFi credential from %s's own connection, which %s.\n"
                "    A board with no uplink is unreachable, so there is no --force: join %s to the board's WiFi."
                % (img_machine, name, "is not WiFi" if r.ok else "could not be read:\n" + indent(said(r)), name))

    def name_preflight(self, name, role, img_machine):
        if not name:
            return
        peers = reach.Reach(self.machine, self.env).peers()
        if not peers:
            if self.step("read this machine's tailnet view and retire whatever node holds '%s'" % name):
                return
            act.die("could not read this machine's tailnet view (tailscale status --json returned nothing), so whether\n"
                    "    '%s' is taken is unknown. This check cannot be skipped: a collision joins renamed '%s-1'."
                    % (name, name))
        hit = collides(name, peers)
        if not hit:
            return
        if role == "rescue" and hit.startswith("exact:") and img_machine == self.c("name") and name == self.c("ssh"):
            act.barrier("'%s' is %s's running rescue, the system this card replaces. Remove '%s' at\n"
                        "    https://login.tailscale.com/admin/machines before rebooting, or the card joins renamed '%s-1'."
                        % (name, self.c("name"), name, name))
            return
        fl = tailnet.Fleet(self.root, self.env, self.machine)
        if not fl.api_present():
            if self.step("retire the stale tailnet node '%s' (the real write needs wk key set tailnet-api)" % name):
                return
            act.die("'%s' is already on the tailnet (%s); a card joining under it comes up renamed '%s-1',\n"
                    "    and nothing here finds it. There is no --force. Remedy: wk key set tailnet-api (this command\n"
                    "    then retires the node), or remove '%s' at the admin console."
                    % (name, hit.split(":", 1)[1], name, name))
        if self.step("retire the stale tailnet node '%s' so this card can join under it" % name):
            return
        act.info("retiring the stale tailnet node '%s' so the card can join under it" % name)
        r = fl.retire(name)
        if not r.ok:
            act.die("could not retire the stale tailnet node '%s':\n%s\n    Nothing was written." % (name, indent(said(r))))
        act.log(indent(said(r)))

    def unmount(self, dev):
        if self.step("unmount whatever is mounted from %s on %s" % (dev, self.c("name"))):
            return
        if not self.ch.call("disk_unmount", dev, mutates=True).ok:
            raise act.Refused(1)

    def tailnet_save(self, dev):
        """tailscaled's state, kept aside by the helper and put back after: the new system comes up as the old node."""
        if self.step("keep %s's bench tailnet identity aside, if it holds one" % dev):
            return False
        if "tailnet-keep=yes" not in self.card("status", mutates=False).out:
            act.warn("%s's card helper cannot keep a node's tailnet identity across a rewrite, so the new system\n"
                     "  joins fresh. A rebuilt rescue has the current helper." % self.c("name"))
            return False
        r = self.card("tailnet-save", dev)
        out = said(r)
        if not r.ok:
            act.die("could not look for a tailnet identity on %s:\n%s" % (dev, indent(out)))
        if "kept=yes" in out:
            act.info("keeping the board's bench tailnet identity%s: the rewritten system comes back as that node"
                     % (" from the system beside this one" if "adopted=" in out and "remembered" not in out else ""))
            return True
        if "kept=no" in out:
            return False
        act.die("%s's card helper did not say whether %s holds a tailnet identity (it said: %s). Refusing to guess."
                % (self.c("name"), dev, out or "nothing"))

    def stream(self, dev, reader, filt):
        name = self.c("name")
        if self.step("stream the image onto %s on %s, and read it back to verify" % (dev, name)):
            return {}
        tool = filt.split()[0]
        if filt != "cat" and not self.ssh(shlex.join(HAVE + (tool,))).ok:
            act.die("%s has no %s, and it decompresses the stream it is sent.\n    Remedy: install %s on %s "
                    "(apt spells xz 'xz-utils')." % (name, tool, tool, name))
        act.info("writing to %s on %s (streamed; decompressed there with %s)" % (dev, name, filt))
        far = "exec 3>&1; %s | python3 -c %s | sudo -n %s write %s" % (filt, shlex.quote(METER), CARD_PRIV, shlex.quote(dev))
        r = self.piped(reader).call("m_ssh", far, mutates=True)
        if not r.ok:
            size = self.ssh("lsblk -dno SIZE %s" % shlex.quote(base(dev))).out.replace("\r", "").strip()
            act.die("could not write the image onto %s (%s) on %s; it was read through:  %s"
                    % (dev, size, name, shlex.join(reader)))
        report = r.out.replace("\r", "")
        act.log(indent(report.rstrip("\n")))
        d = kv(report)
        return {k: d.get(k, "") for k in REPORT}

    def verify(self, dev, rep):
        if self.step("read %s back and compare it with the image streamed to it" % dev):
            return
        act.info("verifying %s against the image that was streamed to it" % dev)
        if disk.is_second(dev):
            if not rep.get("root_sha"):
                act.die("the write onto %s did not report what it split the image into, so nothing can verify it." % dev)
            if not self.card("verify", dev, *[rep[k] for k in REPORT[2:]], mutates=False).ok:
                act.die("%s does not read back as the image's boot and root." % dev)
            return
        lines = self.card("verify", dev, rep["stream_bytes"], mutates=False).out.replace("\r", "").split()
        got = lines[-1] if lines else ""
        if got != rep["stream_sha"]:
            act.die("%s does not read back as the image streamed to it\n    image: %s\n    disk:  %s"
                    % (dev, rep["stream_sha"], got or "(unreadable)"))

    def root_spec(self, dev):
        return kv(self.card("root-spec", dev, mutates=False).out).get("root", "")

    def simple(self, sentence, *verb, why, mutates=True):
        if self.step(sentence):
            return
        r = self.card(*verb, mutates=mutates)
        if r.ok:
            return
        if "usage: wk-card-priv" in r.out + r.err:
            act.die("%s's card helper is older than this checkout: it has no '%s' verb.\n    %s"
                    % (self.c("name"), verb[0], UPDATE % self.c("name")))
        act.die("%s\n%s" % (why, indent(said(r))))

    def retarget(self, dev):
        if self.step("retarget %s's root= to a PARTUUID of %s, so it boots from any device" % (dev, dev)):
            return
        spec = self.root_spec(dev)
        if not spec:
            act.die("%s has no cmdline.txt to read a root from." % dev)
        # LABEL= and UUID= name every disk written from this image: only a PARTUUID names one.
        if spec.startswith("PARTUUID="):
            return
        if root_class(spec) == "network":
            act.die("%s names a network root (%s). Nothing here boots that way." % (dev, spec))
        act.info("retargeting %s's root: %s -> a PARTUUID of this disk" % (dev, spec))
        if not self.card("retarget", dev).ok:
            act.die("could not retarget %s's root; its cmdline still says root=%s, which names a device, not this disk."
                    % (dev, spec))

    def unique_identity(self, dev):
        """The old identity is read off the card: every reference to it there is rewritten from it."""
        if disk.is_second(dev):
            act.log("  %s keeps the rescue disk's identity; the second system names its partitions by it" % dev)
            return
        if self.step("stamp a unique disk identity on %s, so two cards written from one image cannot be confused" % dev):
            return
        spec = self.root_spec(dev)
        if not spec.startswith("PARTUUID="):
            return
        old, new = spec[len("PARTUUID="):].rsplit("-", 1)[0], self.rand()
        act.info("stamping a unique identity on %s (0x%s -> 0x%s)" % (dev, old, new))
        if not self.card("identity", dev, old, new).ok:
            act.die("could not stamp a unique identity on %s: its root is still PARTUUID=%s-2, like every other card\n"
                    "    written from this image, and booted beside one the kernel may mount the wrong root." % (dev, old))
        got = self.ssh("lsblk -no PARTUUID %s" % shlex.quote(part(dev, 2))).out.replace("\r", "").split()
        if new + "-02" not in got:
            act.die("%s did not take the new identity; refusing to leave it ambiguous" % dev)

    def put_units(self, dev, staged):
        if self.step("install the fleet units and the profiling knobs into %s's rootfs" % dev):
            return
        seed = os.path.join(tempfile.gettempdir(), "wk-units.%d" % os.getpid())
        try:
            for d in ("systemd", "sysctl.d", "init.d"):
                self.machine.mkdir(os.path.join(seed, d))
            for rel, text in sorted(staged.items()):
                self.machine.write(os.path.join(seed, rel), text)
            r = self.piped(["tar", "-cf", "-", "-C", seed, "systemd", "sysctl.d", "init.d"]).call(
                "card_priv", "units", dev, mutates=True)
        finally:
            self.machine.remove(seed)
        out = said(r)
        if not r.ok:
            act.die("could not install the fleet units on %s:\n%s\n    A run that wedges the board would not hand it back."
                    % (dev, indent(out)))
        if "no systemd on this disk; nothing installed" in out:
            act.die("%s's card helper predates BusyBox init scripts, so this card has no self-return or self-disarm.\n"
                    "    %s (on a rescue, rebuild the rescue image), and write again." % (self.c("name"), UPDATE % self.c("name")))
        if "neither systemd nor /etc/init.d" in out:
            act.warn("this image has neither systemd nor a BusyBox init, so the self-return watchdog and the self-disarm\n"
                     "  were NOT installed: a run that wedges the board will not hand it back.")

    def check_boot_files(self, dev, machine, dtb):
        """Firmware that cannot find a kernel halts: no retry, no fall-through, no way back over the wire."""
        if self.step("check that every file a %s's firmware asks for resolves on %s" % (machine, dev)):
            return
        r = self.card("boot-check", dev, dtb, mutates=False)
        if r.ok:
            return
        out = said(r)
        if "no boot-file checker" in out:
            act.warn("%s's boot files were NOT checked: %s's card helper has no boot-file checker beside it\n"
                     "  (./setup --stage quiesce installs it; a rebuilt rescue carries it)." % (dev, self.c("name")))
            return
        act.die("%s is missing files a %s needs to reach its kernel:\n\n%s\n\n    Firmware that cannot find a kernel halts "
                "and does not come back. Rebuild the image, or check its\n    config.txt against its boot partition."
                % (dev, machine, indent(out, "      ")))

    def joins(self, dev, verb, what, yes, no):
        """Whether the image carries a first-boot joiner: a guess either way strands a card or a credential."""
        r = self.card(verb, dev, mutates=False)
        out = said(r)
        if r.ok and yes in out:
            return True
        if r.ok and no in out:
            return False
        act.die("could not tell whether %s %s on first boot (%s's helper said: %s). Refusing to guess."
                % (dev, what, self.c("name"), out or "nothing"))

    def seed_tailnet(self, dev, name):
        """Onto the card just written, never baked into the image: wk-tailnet-join deletes it once spent."""
        tag = tailnet.fleet_tag(self.env)
        if self.step("seed the tailnet identity on %s (it would join as '%s', %s)" % (dev, name, tag)):
            return
        if not self.joins(dev, "joins", "joins the tailnet", "tailnet-join: yes", "tailnet-join: no"):
            return
        keyfile = tailnet.Fleet(self.root, self.env, self.machine).authkey()
        if not keyfile:
            act.die("the tailnet auth key present at the preflight is gone, and %s is already erased.\n"
                    "    Set one and retry:  wk key set tailnet" % dev)
        act.info("seeding the tailnet identity onto %s -- it joins as '%s' (%s) on first boot" % (dev, name, tag))
        if not self.card("tailnet", dev, name, tag, input=self.machine.read(keyfile)).ok:
            act.die("could not seed the tailnet identity onto %s; it would be reachable only over its LAN." % dev)

    def seed_wifi(self, dev, img_machine):
        name = self.c("name")
        if self.step("seed %s's own WiFi credential on %s, for a board with no cable" % (name, dev)):
            return
        if not wants_wifi(self.fleet, img_machine):
            return
        if not self.joins(dev, "wifi-joins", "brings up WiFi", "wifi-join: yes", "wifi-join: no"):
            return
        act.info("seeding %s's own WiFi credential onto %s" % (name, dev))
        if not self.card("wifi-from-host", dev).ok:
            act.die("could not seed WiFi credentials onto %s; %s has no cable, so it would reach no network." % (dev, img_machine))

    def eject(self, dev):
        name = self.c("name")
        if self.step("flush and power off %s" % dev):
            return
        if not self.ssh(shlex.join(HAVE + ("udisksctl",))).ok:
            act.warn("%s has no udisksctl, so %s is left powered on (synced, safe to pull).\n  To power it off, install "
                     "udisks2 on %s." % (name, dev, name))
            return
        if self.ssh("udisksctl power-off -b %s" % shlex.quote(dev), mutates=True).ok:
            act.info("powered off %s -- safe to remove" % dev)
        else:
            act.log("  (could not power off %s; it is synced, so it is safe to pull anyway)" % dev)

    def run(self, src, spec, grow, preset, role, mach):
        src = self.resolve(src)
        reader, filt = self.reader(src), from_filter(bare(src))
        preset, p = self.image_preset(preset, src)
        img_machine = mach or p.get("IMG_MACHINE", "")
        disk_machine, dev = disk.parse_spec(spec)
        asked_dev = dev
        conf = load_machine(self.fleet, disk_machine)
        if not conf:
            act.die("unknown machine '%s'\n    machines:\n%s" % (disk_machine, machine_list(self.fleet)))
        self.attach(conf)
        if not self.ssh("true").ok:
            act.die("%s is not reachable over ssh, and the disk is attached to it." % disk_machine)
        self.drv.armed_barrier("Writing a disk now would overwrite the medium that boot is aimed at.")
        if not dev and img_machine:
            dev = self.disks.for_machine(img_machine)
            if dev:
                act.info("%s's medium is %s on %s (matched by its marker, not by name)" % (img_machine, dev, disk_machine))
        if not dev:
            act.log("disks attached to %s:\n%s" % (disk_machine, self.disks.listing()))
            act.die("say which one: --disk %s:<device>" % disk_machine)
        fleet_edit = p.get("IMG_BUILDER") in images.WS_BUILDERS
        name = os.path.basename(bare(src))
        for ext in (".xz", ".zst", ".gz", ".wic", ".img"):
            name = name[:-len(ext)] if name.endswith(ext) else name
        name = preset or name
        tailnet = tailnet_name(self.fleet, img_machine, role)

        if act.dry_run():
            self.dry_preamble(src, dev, name, fleet_edit, p, img_machine)
        else:
            self.key_preflight(img_machine, role)
            self.wifi_preflight(img_machine)
            act.warn("this ERASES the %s system on %s on %s (the system on partitions 1-2 stays)."
                     % (dev.split("@")[1], base(dev), disk_machine) if disk.is_second(dev)
                     else "this ERASES %s on %s, whatever is on it now." % (dev, disk_machine))
        if not act.confirm("write %s onto %s attached to %s?" % (src, dev, disk_machine)):
            act.die("not written")
        self.unmount(dev)
        self.disks.refuse_unless_safe(dev)
        # A rewritten bench system keeps its own tailnet node; never a rescue, whose identity is on the medium replaced.
        kept = role != "rescue" and fleet_edit and (disk.is_second(dev) or base(dev) == self.c("device")) \
            and self.tailnet_save(dev)
        if kept:
            act.log("  '%s' on the tailnet is this system's own node, kept across the rewrite" % tailnet)
        else:
            self.name_preflight(tailnet, role, img_machine)

        rep = self.stream(dev, reader, filt)
        if not act.dry_run() and int(rep.get("stream_bytes") or 0) <= 0:
            act.die("%s read as 0 bytes through: %s" % (src, shlex.join(reader)))
        self.verify(dev, rep)
        # A stream with a shell banner ahead of it hashes perfectly and has no partition table.
        self.simple("check that %s came out of this with a partition table" % dev, "parts", dev, mutates=False,
                    why="%s has no readable partition table after the write: the source is not a disk image." % dev)
        if kept:
            self.simple("put the kept tailnet identity back on %s" % dev, "tailnet-restore", dev,
                        why="could not put the kept tailnet identity back on %s; it would come up renamed." % dev)

        ident = image_id(name, rep.get("stream_sha", ""))
        wk_tools = self.machine.run(["git", "-C", self.root, "rev-parse", "--short", "HEAD"])
        marker = "\n".join(["id=" + ident, "profile=" + (preset or "unknown"), "machine=" + img_machine,
                            "builder=" + p.get("IMG_BUILDER", ""), "role=" + role,
                            "built_by=" + record.host_name(self.machine),
                            "wk_tools=" + (wk_tools.out.strip() if wk_tools.ok else "unknown"), "source=" + src])
        if fleet_edit:
            self.retarget(dev)
            cmdline, config = cmdline_add(self.root, p), config_add(self.root, p)
            if cmdline:
                self.simple("append to %s's kernel command line: %s" % (dev, cmdline), "cmdline-append", dev, b64(cmdline),
                            why="could not append to %s's kernel command line (%s)." % (dev, cmdline))
            if config:
                # A firmware setting that fails to land fails nothing and makes every number worse.
                self.simple("append this preset's firmware block to %s's config.txt" % dev, "config-append", dev,
                            b64(config), why="could not append the firmware block to %s's config.txt." % dev)
            self.simple("name the system on %s's boot partition by its image id" % dev, "boot-id", dev, ident,
                        why="could not name the system on %s's boot partition; 'wk boot' refuses a disk it cannot name." % dev)
        self.unique_identity(dev)
        # root's authorized_keys: a Yocto image ships `PermitRootLogin yes` with an empty password, which BatchMode cannot use.
        self.simple("install the identity marker and the driving ssh key on %s" % dev,
                    "fleet", dev, b64(marker), b64(self.driving_key()),
                    why="could not install the identity marker and driving key on %s; nothing here could reach it." % dev)
        if fleet_edit:
            self.put_units(dev, stage_units(self.root, p.get("IMG_WATCHDOG", ""), self.self_disarm(img_machine), preset))
            if img_machine == disk_machine:
                if not self.c("dtb"):
                    act.die("'%s' (machines/%s.conf) sets no dtb" % (disk_machine, disk_machine))
                self.check_boot_files(dev, disk_machine, self.c("dtb"))
            else:
                act.log("  (not checking %s's boot files: this is %s's image, so this card goes elsewhere)"
                        % (disk_machine, img_machine or "an unknown machine"))
            if not self.step("check that the system on %s names a root it can find on %s" % (dev, dev)):
                check_root(self.root_spec(dev), base(dev), disk_machine, self.env)
        # The only difference between a rescue and a bench system: every unit checks `ConditionPathExists=!/etc/wk/rescue`.
        self.simple("mark %s a %s system" % (dev, role), "role", dev, role,
                    why="could not set the role on %s; a rescue carrying a live self-return watchdog reboots mid-write." % dev)
        # Onto every system, so a board whose arming is an edit to the card can arm the next system where it stands.
        self.simple("put this machine's card helper on %s" % dev, "helper", dev,
                    why="could not put the card helper on %s." % dev)
        if disk.is_second(asked_dev) and self.selects_by_partition(img_machine):
            self.simple("write the firmware's two-system selector (autoboot.txt) onto %s" % dev, "autoboot", dev,
                        why="could not write the two-system selector onto %s." % dev)
        self.seed_tailnet(dev, tailnet)
        self.seed_wifi(dev, img_machine)
        if grow:
            self.simple("grow the last partition to fill %s" % dev, "grow", dev, why="could not grow the root partition on %s" % dev)
        else:
            act.log("  the root partition is left at its built size, leaving room for a second system (--grow fills it).")
        self.eject(dev)
        if act.dry_run():
            act.log("dry run -- nothing was written.")
            return 0
        act.info("%s on %s now holds %s" % (dev, disk_machine, ident))
        self.after(dev, disk_machine)
        return 0

    def dry_preamble(self, src, dev, name, fleet_edit, p, img_machine):
        disk_machine = self.c("name")
        if not wants_wifi(self.fleet, img_machine):
            wifi = "not needed -- this board has a cable"
        elif "wifi-host: yes" in said(self.card("wifi-host", mutates=False)):
            wifi = "%s is on WiFi -- the card brings up WiFi on every boot, from its credential" % disk_machine
        else:
            wifi = "NO -- %s is not on WiFi; the real write refuses here (no --force)" % disk_machine
        key = tailnet.Fleet(self.root, self.env, self.machine).key_present()
        act.log("would write\n  image     %s\n  onto      %s attached to %s\n  identity  %s\n  as        %s\n  tailnet   %s\n"
                "  wifi      %s" % (
                    src, dev, disk_machine, image_id(name, ""),
                    "a fleet system: identity marker, driving key, units, retargeted root" if fleet_edit else
                    "as built (%s); identity marker and driving key only" % (p.get("IMG_BUILDER") or "unknown image preset"),
                    "auth key present" if key else "NO auth key -- the real write refuses here (wk key set tailnet)", wifi))
        act.log("then, in order:")

    def after(self, dev, disk_machine):
        if base(dev) == self.c("device"):
            # A medium-armed machine's arming is firmware the image brings its own copy of, so it would boot it next.
            if self.drv.arming == "medium":
                self.drv.disarm()
            act.log("  %s is configured to boot from this disk; writing it armed nothing. To boot it once:  wk boot %s"
                    % (disk_machine, disk_machine))
        elif self.c("root") and dev == disk_of(self.c("root")):
            act.log("  this is %s's rescue medium: it boots whenever %s is disarmed.\n  To boot it now:  wk boot %s --disarm"
                    "   then power-cycle the board." % (disk_machine, self.c("device") or "the bench medium", disk_machine))
        else:
            act.log("  nothing boots this yet: move it to its board; 'wk boot <machine>' is the one-shot.")

    def image_driver(self, name):
        conf = load_machine(self.fleet, name)
        return driver_class(conf["driver"])(self.root, conf, None) if conf else None

    def self_disarm(self, name):
        d = self.image_driver(name)
        return (d.self_disarm_sh() or "") if d else ""

    def selects_by_partition(self, name):
        """On a board that selects some other way, autoboot.txt changes which system a flag boots."""
        d = self.image_driver(name)
        return bool(d and d.selects_by_partition)

    def driving_key(self):
        """Tailscale SSH needs no key at all, so a working session is not evidence of the right one."""
        path = images.driving_key_path(self.env)
        try:
            return self.machine.read(path)
        except OSError:
            act.die("no public key at %s; the image would come up unreachable. Set WK_IMAGE_KEY to this machine's." % path)
