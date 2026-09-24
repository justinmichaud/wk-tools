"""`wk sysimage write`: an image's own bytes streamed onto a disk attached to a fleet machine, then made a fleet system
on the card by that machine's card helper (admin/wk-card-priv), so the driving machine never opens the image."""

import base64
import os
import re
import shlex
import sys
import tempfile

from wk import act, fleet, images, reach, record, shell
from wk.boot import driver_class
from wk.boot.driver import BashChannel, Driver, disk_of, kv, part
from wk.machine import is_macos
from wk.sysimage import disk
from wk.sysimage import ls as lsmod

CARD_PRIV = disk.CARD_PRIV
FILTERS = ((".xz", "xz -dc"), (".zst", "zstd -dc"), (".gz", "gzip -dc"))
ROOT_WORDS = {"mmc": "an SD card (/dev/mmcblk*)", "usb": "a USB or SCSI disk (/dev/sd*)", "nvme": "an NVMe disk",
              "portable": "any device it is written to", "network": "a network root, not a local device"}
OLD_HELPER = "usage: wk-card-priv"
UPDATE = ("Remedy, from a terminal on %s (its sudo asks for a password, which\n    is why this end cannot do it): "
          "update its wk-tools checkout, then\n        ./setup --stage quiesce")

# stdin to stdout unchanged; the byte count and sha256 go to fd 3 in one write, so they cannot interleave with the
# helper's own report on the fd they share.
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
    return device_class(spec) if spec else "unknown"


def device_class(dev):
    return disk.tran_of_name(dev) or "unknown"


def word(cls):
    return ROOT_WORDS.get(cls, "an unrecognised kind of device")


def check_root(spec, dev, what, env):
    """A system whose kernel looks for its root on another kind of device than the one it is on never boots."""
    cls, want = root_class(spec), device_class(dev)
    if cls in ("portable", "network", "unknown") or cls == want:
        return
    if env.get("WK_ANY_ROOT"):
        act.warn("this system expects %s and %s is %s;\n  left as written (WK_ANY_ROOT). It will not boot -- this proves "
                 "the transfer only." % (word(cls), dev, word(want)))
        return
    act.die("""the system on {dev} expects to boot from {c}, and {dev} is
    {w}.

    Its kernel command line says `root={spec}`. The firmware would load the
    kernel from {dev} and the kernel would then look for its root filesystem on
    {c} -- which is either absent or somebody else's
    disk. Nothing about the write failed; the board would.

    Either write it to {c} on {what}, or rebuild the
    image for this device -- a wic image's root device comes from the recipe's
    wks file, not from anything this repo sets. Set WK_ANY_ROOT=1 to write it
    anyway (for testing the transfer, which is all it can prove).""".format(
        dev=dev, c=word(cls), w=word(want), spec=spec, what=what))


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


def appended(root, machine, spec_dir, name):
    """The board's file first -- `os_check=0` is a Pi 5 firmware fact every image needs -- then the profile's."""
    text = ""
    for path in (os.path.join(str(root), "image", "boards", machine, name) if machine else "",
                 os.path.join(spec_dir, name) if spec_dir else ""):
        if path and os.path.isfile(path):
            with open(path) as f:
                text += f.read()
    return text


def cmdline_add(root, p):
    """One line: the helper refuses a second."""
    text = appended(root, p.get("IMG_MACHINE", ""), p.get("IMG_SPEC_DIR", ""), "cmdline.txt.append")
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return " ".join(" ".join(lines).split())


def config_add(root, p):
    return appended(root, p.get("IMG_MACHINE", ""), p.get("IMG_SPEC_DIR", ""), "config.txt.append")


def load_machine(fl, name):
    """A bench machine's NODE_*, as boot/machines.sh's machine_load reads it; None when it is not one."""
    try:
        conf = fl.load(name) if name else None
    except fleet.ConfError:
        return None
    if not conf or conf["KIND"] not in fleet.BENCH_KINDS or not (conf.get("NODE_DRIVER") and conf.get("NODE_NOTE")):
        return None
    out = {"NODE_ROLE": "workstation", "NODE_OS": "any"}
    out.update({k: v for k, v in conf.items() if k.startswith("NODE_")})
    out["NODE_NAME"] = name
    return out


def machine_list(fl):
    return "\n".join("      %-8s%s" % (n, c["NODE_NOTE"]) for n in fl.names(fleet.BENCH_KINDS)
                     for c in [load_machine(fl, n)] if c)


def wants_wifi(fl, name):
    """A board wk writes a card for that has no cable: NODE_DEVICE too, since a Mac reaches the bench over WiFi."""
    conf = load_machine(fl, name)
    return bool(conf and conf.get("NODE_DEVICE") and conf.get("NODE_NET") == "wifi")


def tailnet_name(fl, name, role):
    """A bench system joins as NODE_BENCH_SSH, a rescue as NODE_SSH: a second join under an existing name comes up renamed."""
    conf = load_machine(fl, name) or {}
    return conf.get("NODE_SSH" if role == "rescue" else "NODE_BENCH_SSH", "")


def unit(root, name, **lines):
    """A first-boot unit, boot/firstboot/<name> verbatim; its parameters are KEY=value lines in the file's last section."""
    with open(os.path.join(str(root), "boot", "firstboot", name)) as f:
        return f.read() + "".join("%s=%s\n" % kv for kv in lines.items())


def init_script(root, name, **params):
    with open(os.path.join(str(root), "boot", "onboard", name)) as f:
        return "#!/bin/sh\n" + "".join("%s=%s\n" % (k, shlex.quote(v)) for k, v in params.items()) + f.read()


def stage_units(root, watchdog, disarm, profile=""):
    """{archive path: text}. The two units that hand a machine back are gated at runtime on /etc/wk/rescue, so one
    artifact serves both roles; a timer, since a sleeping oneshot holds its target inactive for the whole watchdog."""
    out = {}
    if not watchdog:
        act.warn("%s sets no IMG_WATCHDOG, so this image will not hand its machine back" % (profile or "this image"))
    else:
        out["systemd/wk-self-return.timer"] = unit(root, "wk-self-return.timer", OnBootSec=watchdog)
        out["systemd/wk-self-return.service"] = unit(root, "wk-self-return.service")
        out["init.d/S99wk-self-return"] = init_script(root, "S99wk-self-return", WK_WATCHDOG=watchdog)
    if disarm:
        act.info("staging the self-disarm (the medium stops booting once this image is up)")
        out["systemd/wk-self-disarm.service"] = unit(root, "wk-self-disarm.service",
                                                     ExecStart="/bin/sh -c '%s'" % disarm.replace("$", "$$"))
        out["init.d/S11wk-self-disarm"] = init_script(root, "S11wk-self-disarm", WK_DISARM=disarm)
    out["sysctl.d/90-wk-perf.conf"] = unit(root, "90-wk-perf.conf")
    for name in ("wk-cpu-governor.service", "wk-no-swap.service", "wk-diag.service"):
        out["systemd/" + name] = unit(root, name)
    return out


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
        cls = driver_class(conf["NODE_DRIVER"])
        self.ch = BashChannel(self.root, conf, "none", bash_driver=cls is Driver, machine=self.machine)
        self.drv = cls(self.root, conf, self.ch)
        self.disks = disk.Disks(self.ch, conf)

    def card(self, *args, input=None, mutates=True):
        return self.ch.call("card_priv", *args, input=input, mutates=mutates)

    def piped(self, reader):
        return BashChannel(self.root, self.conf, self.ch.channel, bash_driver=self.ch.bash_driver,
                           machine=Piped(self.machine, reader))

    def ssh(self, command, mutates=False):
        return self.ch.call("m_ssh", command, input="", mutates=mutates)

    @staticmethod
    def said(r):
        return (r.out + r.err).replace("\r", "").rstrip("\n")

    @staticmethod
    def indent(text, by="    "):
        return "\n".join(by + l for l in text.splitlines())

    def resolve(self, spec):
        if spec.startswith(("vm:", "/", "./", "../")):
            return spec
        for img in lsmod.scan(self.machine, self.store):
            if img.path and images.ws_profile(img.ws, self.env) == spec:
                act.info("'%s' is a configuration; its image is at %s" % (spec, img.path))
                return img.path
        if images.quiet_load(spec, self.env):
            r = self.machine.run([os.path.join(self.root, "wk"), "sysimage", "path", spec])
            path = r.out.replace("\r", "").strip() if r.ok else ""
            if path:
                # The lane answers in its own spelling; on a macOS workstation that is the podman VM's filesystem.
                path = path if shell.store_is_local(self.root, self.machine) else "vm:" + path
                act.info("'%s' is a configuration; its image is at %s" % (spec, path))
                return path
            act.die("'%s' is a configuration this checkout defines, and the lane that would\n    build it holds no image:\n"
                    "        wk sysimage build %s\n    'wk sysimage ls' lists every image this fleet has built, with its path."
                    % (spec, spec))
        built = sorted({images.ws_profile(i.ws, self.env) for i in lsmod.scan(self.machine, self.store) if i.path} - {None})
        act.die("'%s' is neither a path nor a configuration this checkout defines.\n    Configurations with an image on "
                "this machine:\n%s\n    'wk sysimage ls' prints them with their paths and sizes."
                % (spec, "\n".join("      " + b for b in built)))

    def reader(self, src):
        if src.startswith("vm:"):
            if not is_macos():
                act.die("--from vm:<path> is for reading a container workspace's\n    output out of this machine's podman VM, "
                        "and this is not a macOS host. Give a\n    plain path.")
            return ["podman", "machine", "ssh", self.env.get("WK_MACHINE") or "wk", "--", "sudo", "cat", bare(src)]
        if not self.machine.exists(src):
            act.die("no image at %s\n    An image lives where the workspace that built it put it (wk help). If\n"
                    "    that is inside this machine's podman VM, say so: --from vm:%s" % (src, src))
        return ["cat", src]

    def profile(self, name, src):
        if not name:
            m = re.search(r"/ws/([^/]*)/", bare(src))
            name = (images.ws_profile(m.group(1), self.env) if m else None) or ""
            if name:
                act.debug("profile '%s' derived from the path" % name)
        if not name:
            return "", {}
        try:
            return name, images.load(name, self.env)
        except (LookupError, images.ConfError) as e:
            act.warn("this write cannot tell which machine the card is for, so there is no\n  firmware check and no "
                     "tailnet name to seed. Pass --profile with a\n  configuration this checkout defines.")
            if isinstance(e, (images.ConfError, images.Tombstone)):
                act.log(self.indent(str(e)))
            return name, {}

    def key_preflight(self, img_machine, role):
        if not tailnet_name(self.fleet, img_machine, role):
            act.die("this image joins the tailnet on first boot, and nothing here\n    knows what name it should answer to "
                    "-- the image records no machine, so the\n    card would join under the image's own hostname and come "
                    "up unreachable by\n    its fleet name.\n    Give it one:  --machine <name>")
        if not shell.tailnet_key_present(self.root, self.machine):
            act.die("there is no tailnet auth key on this machine, so the card this is about\n    to write would boot with no "
                    "tailnet identity -- reachable only over whatever\n    LAN it lands on, unreachable by its fleet name, "
                    "which is the state the fleet\n    rule exists to end.\n    Set one first:  wk key set tailnet")

    def wifi_preflight(self, img_machine):
        """No --force: a board with no uplink is unreachable, which is worse than refusing."""
        if not wants_wifi(self.fleet, img_machine):
            return
        name = self.c("NODE_NAME")
        r = self.card("wifi-host", mutates=False)
        if not r.ok:
            act.die("could not tell whether %s is on WiFi:\n%s\n    A board with no uplink is unreachable, which is worse "
                    "than refusing, so\n    there is no --force past this." % (name, self.indent(self.said(r))))
        if "wifi-host: yes" in r.out + r.err:
            return
        act.die("%s has no cable at the bench, and its rescue/bench images bring up WiFi\n    from a credential taken from "
                "%s's own connection -- %s\n    is not on WiFi. A board with no uplink is unreachable, which is worse than\n"
                "    refusing, so there is no --force past this.\n    Join %s to the WiFi the board will use; the card takes "
                "its\n    credential from that machine's own connection." % (img_machine, name, name, name))

    def name_preflight(self, name, role, img_machine):
        if not name:
            return
        peers = reach.Reach(self.machine, self.env).peers()
        if not peers:
            if self.step("read this machine's tailnet view and retire whatever node holds\n              '%s', so the "
                         "card can join under it (the real write refuses\n              when that view cannot be read)" % name):
                return
            act.die("could not read this machine's tailnet view (tailscale status --json\n    returned nothing -- no CLI, "
                    "not logged in, or the daemon did not answer), so\n    there is no way to tell whether '%s' is already "
                    "on the tailnet. This\n    check cannot be skipped: writing anyway could join renamed to '%s-1',\n"
                    "    and everything here reaches a board by its tailnet name." % (name, name))
        hit = collides(name, peers)
        if not hit:
            return
        if role == "rescue" and hit.startswith("exact:") and img_machine == self.c("NODE_NAME") and name == self.c("NODE_SSH"):
            act.barrier("'%s' is %s's running rescue -- the system this card replaces.\n    The card joins under that name "
                        "only if the old node is gone by its first\n    boot: after this write, remove '%s' from the tailnet "
                        "admin console\n    (https://login.tailscale.com/admin/machines -> %s -> Remove) before\n    rebooting; "
                        "a card that boots while the node exists joins renamed '%s-1'\n    and nothing here can find it."
                        % (name, self.c("NODE_NAME"), name, name, name))
            return
        if not shell.tailnet_api_present(self.root, self.machine):
            if self.step("retire the stale tailnet node '%s' -- which the real write\n              refuses to do without a "
                         "stored token (wk key set tailnet-api)" % name):
                return
            act.die("'%s' is already on the tailnet (%s), online or offline.\n    Joining under it again does not keep the "
                    "name -- Tailscale renames the\n    collision to '%s-1', and everything here reaches a board by its\n"
                    "    tailnet name, so a card that joins renamed is a card nothing here can find.\n    There is no --force: "
                    "the one exception is a board's own rescue replacing\n    itself, and this write is not that. Two "
                    "remedies:\n      wk key set tailnet-api  store a token, and this command retires the\n"
                    "                              stale node itself on the re-run\n      the admin console       remove '%s' "
                    "by hand at\n                              https://login.tailscale.com/admin/machines"
                    % (name, hit.split(":", 1)[1], name, name))
        if self.step("retire the stale tailnet node '%s' so this card can join under it" % name):
            return
        act.info("'%s' is held by a node this board is not running; retiring it so the card can join under it" % name)
        r = shell.tailnet_retire(self.root, self.machine, name)
        if not r.ok:
            act.die("could not retire the stale tailnet node '%s':\n%s\n    Nothing was written. A node that is online is a "
                    "running board, not a\n    leftover -- check what is answering to that name before writing this card."
                    % (name, self.indent(self.said(r))))
        act.log(self.indent(self.said(r), "    "))

    def unmount(self, dev):
        if self.step("unmount whatever is mounted from %s on %s" % (dev, self.c("NODE_NAME"))):
            return
        if not self.ch.call("disk_unmount", dev, mutates=True).ok:
            raise act.Refused(1)

    def tailnet_save(self, dev):
        """tailscaled's state on partition 4, kept aside by the helper and put back after, so the new system comes up
        as the node the old one was."""
        if self.step("keep %s's bench tailnet identity aside, if it holds one" % dev):
            return False
        name = self.c("NODE_NAME")
        if "tailnet-keep=yes" not in self.card("status", mutates=False).out:
            act.warn("%s's card helper cannot keep a node's tailnet identity across a rewrite,\n  so the new system joins "
                     "fresh; a stale node of the same name on the tailnet\n  refuses the write. The helper is the rescue "
                     "image's: a rebuilt rescue, written\n  from a reader, has the current one." % name)
            return False
        r = self.card("tailnet-save", dev)
        out = self.said(r)
        if not r.ok:
            act.die("could not look for a tailnet identity on %s's partition 4:\n%s" % (dev, self.indent(out)))
        if "kept=yes" in out:
            if "adopted=remembered" in out:
                act.info("this board remembers its bench tailnet node, so the card written now\n  rejoins as that node "
                         "rather than colliding with it -- nothing has to retire\n  a leftover, and no credential that can "
                         "administer the tailnet is needed")
            elif "adopted=" in out:
                act.info("taking the board's bench tailnet identity from the system beside this one:\n  the two bench "
                         "systems take turns being one node, so the new one is reachable\n  under the name the board's "
                         "bench role already holds")
            else:
                act.info("keeping the node's tailnet identity aside: the rewritten system comes back as the same node")
            return True
        if "kept=no" in out:
            act.debug("%s holds no bench tailnet identity yet; the new system joins fresh" % dev)
            return False
        act.die("%s's card helper did not say whether %s's partition 4 holds a\n    tailnet identity (it said: %s). Refusing "
                "to guess: a system that\n    joins under a name it already holds comes up renamed and unreachable."
                % (name, dev, out or "nothing"))

    def stream(self, dev, reader, filt):
        name = self.c("NODE_NAME")
        if self.step("stream the image onto %s on %s, and read it back to verify" % (dev, name)):
            return {}
        tool = filt.split()[0]
        if filt != "cat" and not self.ssh("command -v %s >/dev/null" % shlex.quote(tool)).ok:
            act.die("%s has no %s, and the image being sent to it is compressed\n    with it -- the card machine is what "
                    "decompresses the stream, so this end\n    never has to have the tool for a format it is only passing "
                    "through.\n    Remedy: install %s on %s (apt spells xz 'xz-utils')." % (name, tool, tool, name))
        act.info("writing to %s on %s (streamed; decompressed there with %s)" % (dev, name, filt))
        far = "exec 3>&1; %s | python3 -c %s | sudo -n %s write %s" % (filt, shlex.quote(METER), CARD_PRIV, shlex.quote(dev))
        r = self.piped(reader).call("m_ssh", far, mutates=True)
        if not r.ok:
            size = self.ssh("lsblk -dno SIZE %s" % shlex.quote(base(dev))).out.replace("\r", "").strip()
            act.die("could not write the image onto %s on %s.\n    It was read through:  %s\n    %s is %s; an image larger "
                    "than that runs out of space\n    part-written, and the read that fed it can fail on its own account."
                    % (dev, name, shlex.join(reader), dev, size))
        report = r.out.replace("\r", "")
        act.log(self.indent(report.rstrip("\n")))
        return {k: kv(report, k) for k in ("stream_bytes", "stream_sha", "boot_bytes", "boot_sha", "root_bytes", "root_sha")}

    def verify(self, dev, rep):
        if self.step("read %s back and compare it with the image streamed to it" % dev):
            return
        act.info("verifying %s against the image that was streamed to it" % dev)
        if disk.is_second(dev):
            if not rep.get("root_sha"):
                act.die("the write onto %s did not report what it split the image into,\n    so there is nothing to read the "
                        "card back against." % dev)
            if not self.card("verify", dev, rep["boot_bytes"], rep["boot_sha"], rep["root_bytes"], rep["root_sha"],
                             mutates=False).ok:
                act.die("%s does not read back as the image's boot and root." % dev)
            return
        lines = self.card("verify", dev, rep["stream_bytes"], mutates=False).out.replace("\r", "").split()
        got = lines[-1] if lines else ""
        if not got:
            act.die("could not read %s back on %s" % (dev, self.c("NODE_NAME")))
        if got != rep["stream_sha"]:
            act.die("%s does not match the image that was streamed to it\n    image: %s\n    disk:  %s"
                    % (dev, rep["stream_sha"], got))

    def parts_present(self, dev):
        """A card that took a stream with a shell banner ahead of it hashes perfectly and has no partition table."""
        if self.step("check that %s came out of this with a partition table" % dev):
            return
        r = self.card("parts", dev, mutates=False)
        if not r.ok:
            act.die("%s has no readable partition table after the write:\n%s\n    Something was written ahead of the image "
                    "bytes on the way out, or the\n    source is not a disk image at all." % (dev, self.indent(self.said(r))))

    def root_spec(self, dev):
        return kv(self.card("root-spec", dev, mutates=False).out, "root")

    def simple(self, sentence, *verb, why):
        if self.step(sentence):
            return
        if not self.card(*verb).ok:
            act.die(why)

    def retarget(self, dev):
        if self.step("retarget %s's root= to a PARTUUID of %s, so it boots from any device" % (dev, dev)):
            return
        spec = self.root_spec(dev)
        if not spec:
            act.die("%s has no cmdline.txt to read a root from, and this image was\n    written as one that boots by firmware "
                    "and cmdline.txt." % dev)
        # LABEL= and UUID= boot from any device, and name every disk written from this image: only a PARTUUID names one.
        if spec.startswith("PARTUUID="):
            return
        if root_class(spec) == "network":
            act.die("%s names a network root (%s). Nothing here boots that way." % (dev, spec))
        act.info("retargeting %s's root: %s -> a PARTUUID of this disk (this disk, from any device)" % (dev, spec))
        if not self.card("retarget", dev).ok:
            act.die("could not retarget %s's root.\n    The image is written; its kernel command line still says root=%s, "
                    "which\n    is a promise about a device rather than about a filesystem -- in another\n    reader it names "
                    "somebody else's disk." % (dev, spec))

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
        act.info("stamping a unique identity on %s (0x%s -> 0x%s), so it cannot be confused with another copy" % (dev, old, new))
        if not self.card("identity", dev, old, new).ok:
            act.die("could not stamp a unique identity on %s.\n    The image is written and verified, but its root is still "
                    "PARTUUID=%s-2 --\n    the same as any other disk written from this image. Booted next to one of\n"
                    "    them, the kernel may mount the wrong root." % (dev, old))
        got = self.ssh("lsblk -no PARTUUID %s" % shlex.quote(part(dev, 2))).out.replace("\r", "").split()
        if new + "-02" not in got:
            act.die("%s did not take the new identity; refusing to leave it ambiguous" % dev)

    def fleet_install(self, dev, marker, key):
        """root's authorized_keys: a Yocto image ships `PermitRootLogin yes` with an empty password, which BatchMode cannot use."""
        if self.step("install the identity marker and the driving ssh key on %s" % dev):
            return
        act.info("installing the identity marker and the driving key on %s" % dev)
        if not self.card("fleet", dev, b64(marker), b64(key)).ok:
            act.die("could not install the fleet integration on %s.\n    The image is written, but the board would boot with "
                    "no /etc/wk-image and no\n    key in root's authorized_keys: unreachable by anything here, and\n"
                    "    indistinguishable from the machine's host mode." % dev)

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
        out = self.said(r)
        if not r.ok:
            act.die("could not install the fleet units on %s:\n%s\n    The image is written; a run that wedges the board "
                    "would not hand it back." % (dev, self.indent(out)))
        if "no systemd on this disk; nothing installed" in out:
            act.die("%s's card helper predates BusyBox init scripts, so this image got\n    neither its self-disarm nor its "
                    "self-return: a board booted into it would not\n    hand itself back. The image is written. Update the "
                    "helper (on a workstation,\n    ./setup --stage quiesce from a terminal there; on a rescue, rebuild the\n"
                    "    rescue image) and write the card again." % self.c("NODE_NAME"))
        if "neither systemd nor /etc/init.d" in out:
            act.warn("this image has neither systemd nor a BusyBox init, so the self-return\n  watchdog and the self-disarm "
                     "were NOT installed. The card carries its identity\n  marker and the driving key and nothing else: a "
                     "run that wedges the board will\n  not hand it back, and on a medium-armed machine the medium stays "
                     "armed until\n  something disarms it.")

    def check_boot_files(self, dev, machine, dtb):
        """Firmware that cannot find a kernel halts: no retry, no fall-through, no way back over the wire."""
        if self.step("check that every file a %s's firmware asks for resolves on %s" % (machine, dev)):
            return
        r = self.card("boot-check", dev, dtb, mutates=False)
        out = self.said(r)
        if r.ok:
            return
        if "no boot-file checker" in out:
            act.warn("%s's boot files were NOT checked: %s's card helper has no boot-file\n  checker beside it. If the "
                     "firmware cannot find a kernel it halts, and that costs\n  a trip to the board. The checker is "
                     "installed with the helper (./setup --stage\n  quiesce on a workstation; a rebuilt rescue image "
                     "carries it)." % (dev, self.c("NODE_NAME")))
            return
        act.die("%s is missing files a %s needs to reach its kernel:\n\n%s\n\n    Firmware that cannot find a kernel halts. "
                "It does not move on to the next\n    BOOT_ORDER entry and it does not come back, so booting this card would "
                "cost\n    a trip to the board rather than a reboot.\n\n    The image is the problem, not the disk: rebuild "
                "it, or check what its\n    config.txt names against what its boot partition holds, and write again."
                % (dev, machine, self.indent(out, "      ")))

    def check_root(self, dev, what):
        if self.step("check that the system on %s names a root it can find on %s" % (dev, dev)):
            return
        check_root(self.root_spec(dev), base(dev), what, self.env)

    def old_helper(self, out, verb, what):
        if OLD_HELPER in out:
            act.die("%s's card helper is older than this checkout: it has no\n    '%s' verb, so %s.\n    %s"
                    % (self.c("NODE_NAME"), verb, what, UPDATE % self.c("NODE_NAME")))

    def seed_role(self, dev, role):
        """The only difference between a rescue and a bench system: every unit checks `ConditionPathExists=!/etc/wk/rescue`."""
        if self.step("mark %s a %s system" % (dev, role)):
            return
        if role == "rescue":
            act.info("marking %s a rescue system -- no self-return watchdog, no self-disarm" % dev)
            act.log("  it is what a board falls back to, so there is nothing to hand it back to")
        r = self.card("role", dev, role)
        if r.ok:
            return
        self.old_helper(self.said(r), "role", "the rescue marker cannot be written and this card would boot\n    carrying a "
                        "live self-return watchdog.\n    The image is written; the role is not set")
        act.die("could not set the role on %s:\n%s\n    The image is written, and the role decides whether this system "
                "reboots\n    itself every few minutes. Refusing to leave that unknown: a rescue that\n    carries a live "
                "self-return watchdog reboots the helper in the middle of\n    whatever card it is writing."
                % (dev, self.indent(self.said(r))))

    def install_helper(self, dev):
        """Onto every system, so a board whose arming is an edit to the card can arm the next system where it stands."""
        if self.step("put this machine's card helper on %s" % dev):
            return
        r = self.card("helper", dev)
        out = self.said(r)
        if r.ok:
            act.log("  " + "".join(l[len("wk-card-priv: helper: "):] for l in out.splitlines()
                                   if l.startswith("wk-card-priv: helper: ")))
            return
        self.old_helper(out, "helper", "the system being written would carry whatever its image\n    was built with, and a "
                        "fix made here would never reach the board.\n    The image is written; the helper is not")
        act.die("could not put the card helper on %s:\n%s" % (dev, out))

    def install_autoboot(self, dev):
        """Without the firmware's own selector the tryboot flag is ignored and the first pair boots."""
        if self.step("write the firmware's two-system selector (autoboot.txt) onto %s" % dev):
            return
        r = self.card("autoboot", dev)
        if r.ok:
            return
        self.old_helper(self.said(r), "autoboot", "this medium would hold two systems with no way for the firmware to\n"
                        "    choose the second")
        act.die("could not write the two-system selector onto %s:\n%s" % (dev, self.said(r)))

    def joins(self, dev, verb, what, yes, no):
        """Whether the image carries a first-boot joiner: a guess either way strands a card or a credential."""
        r = self.card(verb, dev, mutates=False)
        out = self.said(r)
        if not r.ok:
            act.die("could not tell whether %s %s on first boot:\n%s\n    The image is written. Refusing to guess."
                    % (dev, what, self.indent(out)))
        if yes in out:
            return True
        if no in out:
            return False
        act.die("%s's card helper did not say whether %s %s\n    (it said: %s). Refusing to guess."
                % (self.c("NODE_NAME"), dev, what, out or "nothing"))

    def seed_tailnet(self, dev, name):
        """Onto the card just written, never baked into the image: wk-tailnet-join deletes it once spent."""
        tag = self.env.get("WK_TAILNET_TAG") or "tag:wk"
        if self.step("seed the tailnet identity on %s (it would join as '%s', %s)" % (dev, name, tag)):
            return
        if not self.joins(dev, "joins", "joins the tailnet", "tailnet-join: yes", "tailnet-join: no"):
            return
        keyfile = shell.tailnet_authkey(self.root, self.machine)
        if not keyfile:
            act.die("the tailnet auth key present moments ago at the write preflight is\n    gone now, and %s is already "
                    "erased. Set one and retry:  wk key set tailnet" % dev)
        act.info("seeding the tailnet identity onto %s -- it joins as '%s' (%s) on first boot" % (dev, name, tag))
        if not self.card("tailnet", dev, name, tag, input=self.machine.read(keyfile)).ok:
            act.die("could not seed the tailnet identity onto %s.\n    The image is written; it would boot with no tailnet "
                    "identity and be\n    reachable only over whatever LAN it lands on." % dev)

    def seed_wifi(self, dev, img_machine):
        """The card takes its credential from the disk machine's own WiFi connection, read by the card helper as root."""
        name = self.c("NODE_NAME")
        if self.step("seed %s's own WiFi credential on %s, for a board with no cable" % (name, dev)):
            return
        if not wants_wifi(self.fleet, img_machine):
            return
        if not self.joins(dev, "wifi-joins", "brings up WiFi", "wifi-join: yes", "wifi-join: no"):
            return
        act.info("seeding %s's own WiFi credential onto %s" % (name, dev))
        if not self.card("wifi-from-host", dev).ok:
            act.die("could not seed WiFi credentials onto %s.\n    The image is written; %s has no cable at the bench, so it "
                    "would boot with\n    no way to reach a network at all." % (dev, img_machine))

    def eject(self, dev):
        name = self.c("NODE_NAME")
        if self.step("flush and power off %s" % dev):
            return
        if not self.ssh("command -v udisksctl >/dev/null").ok:
            act.warn("%s has no udisksctl, so %s is left powered on. The write is\n  complete and the card is synced -- it is "
                     "safe to pull. To have the card\n  powered off instead, install udisks2 on %s ('./setup' does, on a "
                     "wk host)." % (name, dev, name))
            return
        if self.ssh("udisksctl power-off -b %s" % shlex.quote(dev), mutates=True).ok:
            act.info("powered off %s -- safe to remove" % dev)
        else:
            act.log("  (could not power off %s; it is synced, so it is safe to pull anyway)" % dev)

    def run(self, src, spec, grow, profile, role, mach):
        src = self.resolve(src)
        reader, filt = self.reader(src), from_filter(bare(src))
        profile, p = self.profile(profile, src)
        img_machine = mach or p.get("IMG_MACHINE", "")
        disk_machine, dev = disk.parse_spec(spec)
        asked_dev = dev
        conf = load_machine(self.fleet, disk_machine)
        if not conf:
            act.die("unknown machine '%s'\n    machines:\n%s" % (disk_machine, machine_list(self.fleet)))
        self.attach(conf)
        if not self.ssh("true").ok:
            act.die("%s is not reachable over ssh, and the disk is attached to it." % disk_machine)
        self.drv.armed_barrier("Writing a disk now would overwrite the medium that boot is aimed at,\n    and the machine "
                               "would come up on whatever this write left behind.")
        if not dev and img_machine:
            dev = self.disks.for_machine(img_machine)
            if dev:
                act.info("%s's medium is %s on %s (matched by serial, not by name)" % (img_machine, dev, disk_machine))
        if not dev:
            act.log("disks attached to %s:" % disk_machine)
            act.log(self.disks.listing())
            act.die("say which one: --disk %s:<device>" % disk_machine)
        fleet_edit = p.get("IMG_BUILDER") in images.WS_BUILDERS
        cmdline, config = (cmdline_add(self.root, p), config_add(self.root, p)) if fleet_edit else ("", "")
        name = os.path.basename(bare(src))
        for ext in (".xz", ".zst", ".gz", ".wic", ".img"):
            name = name[:-len(ext)] if name.endswith(ext) else name
        name = profile or name
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
        kept = role != "rescue" and fleet_edit and (disk.is_second(dev) or base(dev) == self.c("NODE_DEVICE")) \
            and self.tailnet_save(dev)
        if kept:
            act.log("  '%s' on the tailnet is this system's own node, kept across the rewrite" % tailnet)
        else:
            self.name_preflight(tailnet, role, img_machine)

        rep = self.stream(dev, reader, filt)
        if not act.dry_run() and int(rep.get("stream_bytes") or 0) <= 0:
            act.die("%s read as 0 bytes through: %s" % (src, shlex.join(reader)))
        if not self.env.get("WK_NO_VERIFY"):
            self.verify(dev, rep)
        self.parts_present(dev)
        if kept:
            self.simple("put the kept tailnet identity back on %s's partition 4" % dev, "tailnet-restore", dev,
                        why="could not put the kept tailnet identity back on %s.\n    The image is written; booted, it would "
                        "join as a new node under a name the\n    old one still holds, and come up renamed." % dev)

        ident = image_id(name, rep.get("stream_sha", ""))
        wk_tools = self.machine.run(["git", "-C", self.root, "rev-parse", "--short", "HEAD"])
        marker = "\n".join(["id=" + ident, "profile=" + (profile or "unknown"), "machine=" + img_machine,
                            "builder=" + p.get("IMG_BUILDER", ""), "role=" + role,
                            "built_by=" + record.host_name(self.machine),
                            "wk_tools=" + (wk_tools.out.strip() if wk_tools.ok else "unknown"), "source=" + src])
        if fleet_edit:
            self.retarget(dev)
            if cmdline:
                self.simple("append to %s's kernel command line: %s" % (dev, cmdline), "cmdline-append", dev, b64(cmdline),
                            why="could not append to %s's kernel command line (%s).\n    The image is written; the board "
                            "would boot without what this profile asks\n    its kernel for." % (dev, cmdline))
            if config:
                # A firmware setting that fails to land fails nothing and makes every number worse.
                self.simple("append this profile's firmware block to %s's config.txt" % dev, "config-append", dev,
                            b64(config), why="could not append the firmware block to %s's config.txt.\n    The image is "
                            "written; the board would come up at whatever clock it felt\n    like, and nothing later would "
                            "say so." % dev)
            self.simple("name the system on %s's boot partition by its image id" % dev, "boot-id", dev, ident,
                        why="could not write the identity onto %s's boot partition.\n    The image is written, but 'wk boot' "
                        "refuses a disk it cannot name." % dev)
        self.unique_identity(dev)
        self.fleet_install(dev, marker, self.driving_key())
        if fleet_edit:
            self.put_units(dev, stage_units(self.root, p.get("IMG_WATCHDOG", ""), self.self_disarm(img_machine), profile))
            if img_machine and img_machine == disk_machine:
                if not self.c("NODE_DTB"):
                    act.die("'%s' (machines/%s.conf) sets no NODE_DTB" % (disk_machine, disk_machine))
                self.check_boot_files(dev, disk_machine, self.c("NODE_DTB"))
            elif img_machine:
                act.log("  (not checking %s's boot files: this is %s's image, so this card goes elsewhere)"
                        % (disk_machine, img_machine))
            else:
                act.log("  (not checking %s's boot files: no machine is known for this image)" % disk_machine)
            self.check_root(dev, disk_machine)
        self.seed_role(dev, role)
        self.install_helper(dev)
        if disk.is_second(asked_dev) and self.selects_by_partition(img_machine):
            self.install_autoboot(dev)
        self.seed_tailnet(dev, tailnet)
        self.seed_wifi(dev, img_machine)
        if grow:
            self.simple("grow the last partition to fill %s" % dev, "grow", dev,
                        why="could not grow the root partition on %s" % dev)
        else:
            act.log("  the root partition is left at its built size, so the rest of the card\n"
                    "  is free for a second system. --grow fills it instead.")
        self.eject(dev)
        if act.dry_run():
            act.log("dry run -- nothing was written.")
            return 0
        act.info("%s on %s now holds %s" % (dev, disk_machine, ident))
        self.after(dev, disk_machine)
        return 0

    def dry_preamble(self, src, dev, name, fleet_edit, p, img_machine):
        disk_machine = self.c("NODE_NAME")
        if wants_wifi(self.fleet, img_machine):
            wifi = ("%s is on WiFi -- the card brings up WiFi on every boot, from its credential" % disk_machine
                    if "wifi-host: yes" in self.said(self.card("wifi-host", mutates=False))
                    else "NO -- %s is not on WiFi; the real write refuses here (no --force)" % disk_machine)
        else:
            wifi = "not needed -- this board has a cable"
        act.log("would write\n  image     %s\n            streamed as it is read: the card takes the image's own bytes, "
                "and\n            every edit is made afterwards, on the card\n  onto      %s attached to %s\n  identity  %s\n"
                "  as        %s\n  tailnet   %s\n  wifi      %s" % (
                    src, dev, disk_machine, image_id(name, ""),
                    "a fleet system: identity marker, driving key, systemd units, retargeted root" if fleet_edit else
                    "written as built -- not a fleet board build (%s); identity marker and driving key only"
                    % (p.get("IMG_BUILDER") or "unknown profile"),
                    "auth key present -- the card joins as this board on first boot"
                    if shell.tailnet_key_present(self.root, self.machine)
                    else "NO auth key -- the real write refuses here (wk key set tailnet)", wifi))
        if dev == self.c("NODE_DEVICE"):
            act.log("  note      %s is configured to boot from this disk (wk boot %s)" % (disk_machine, disk_machine))
        act.log("then, in order:")

    def after(self, dev, disk_machine):
        if base(dev) == self.c("NODE_DEVICE"):
            # A medium-armed machine's arming is firmware the image brings its own copy of, so it would boot it next.
            if self.drv.arming == "medium":
                self.drv.disarm()
                act.debug("%s's %s was left disarmed" % (disk_machine, dev))
            act.log("  %s is configured to boot from this disk, but writing it\n  did not arm anything. To boot it -- once, "
                    "reverting by itself:\n      wk boot %s" % (disk_machine, disk_machine))
        elif self.c("NODE_ROOT") and dev == disk_of(self.c("NODE_ROOT")):
            act.log("  this is %s's rescue medium: it boots whenever %s is\n  disarmed. To boot it now:  wk boot %s --disarm "
                    "  then power-cycle the board\n  (if this write was forced past the running rescue's name, remove that "
                    "node\n  from the admin console between the two)." % (
                        disk_machine, self.c("NODE_DEVICE") or "the bench medium", disk_machine))
        else:
            act.log("  nothing boots this yet. Move it to the board it is for, or point a\n  machine at it; 'wk boot "
                    "<machine>' is the one-shot.")

    def image_driver(self, name):
        conf = load_machine(self.fleet, name)
        return driver_class(conf["NODE_DRIVER"])(self.root, conf, None) if conf else None

    def self_disarm(self, name):
        d = self.image_driver(name)
        return (d.self_disarm_sh() or "") if d else ""

    def selects_by_partition(self, name):
        """On a board that selects some other way, autoboot.txt changes which system a flag boots."""
        d = self.image_driver(name)
        return bool(d and d.selects_by_partition)

    def driving_key(self):
        """Tailscale SSH needs no key at all, so a working session is not evidence of the right one."""
        path = self.env.get("WK_IMAGE_KEY") or os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519.pub")
        try:
            return self.machine.read(path)
        except OSError:
            act.die("no public key at %s\n    The image has to accept an ssh key on first boot, or it comes up\n"
                    "    unreachable. Set WK_IMAGE_KEY to the one this machine should use." % path)


def b64(text):
    """The helper checks a value against a character set before decoding it."""
    return base64.b64encode(text.encode()).decode()


def main(argv, env=None):
    env = os.environ if env is None else env
    if argv[:1] != ["wants-wifi"] or len(argv) != 2:
        sys.stderr.write("usage: python3 -m wk.sysimage.write wants-wifi <machine>\n")
        return 2
    return 0 if wants_wifi(fleet.Fleet(images.root(env), env), argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
