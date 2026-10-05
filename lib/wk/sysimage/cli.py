"""`wk sysimage`'s verbs and the routing questions the dispatcher asks before it runs one."""

import re

from wk import act, build, fleet, images, pgo, record
from wk.kv import ConfError
from wk.sysimage import buildroot, disk, guestbase, macvolume, pmos, task, write as writemod, yocto
from wk.sysimage import ls as lsmod

SHA = re.compile(r"^[0-9a-f]{40}$")
UNKNOWN = "unknown profile '%s'.\n    'wk sysimage configs' has every configuration."
BUILDERS = ("yocto", "buildroot", "pmos", "fetch", "mac-volume", "guest")


def wsname(argv, env=None):
    return images.ws_arg(list(argv[1:]), env) if argv else ""


def where(argv, env=None):
    """`ls` walks from here and answers another's walk from the store; a verb naming an image workspace runs there."""
    if argv[:1] in (["ls"], ["list"]):
        return "store" if "--continued" in argv[1:] else "local"
    return "workspace" if wsname(argv, env) else "host"


def wsplace(argv, reg):
    m = images.spec_machine(argv[1]) if len(argv) > 1 else ""
    if not m or not wsname(argv, reg.env):
        return ""
    return images.spec_place(m, record.machine_name(reg.env), reg.default())


def grow(grows, keeps, order):
    """The last of --grow and --no-grow given, or None for neither."""
    if grows and keeps:
        return max(i for i, o in enumerate(order) if o == "--grow") > max(i for i, o in enumerate(order) if o == "--no-grow")
    return True if grows else False if keeps else None


class Sysimage:
    def __init__(self, reg, clock):
        self.reg, self.clock, self.env, self.machine = reg, clock, reg.env, reg.machine

    def building(self, ws):
        try:
            place = self.reg.load(self.reg.ws_place(ws))
            return build.busy_reason(place, build.records_of(place, self.clock, self.machine), ws) is not None
        except (LookupError, OSError):
            return None

    def profile(self, spec):
        name = images.spec_profile(spec)
        try:
            return images.load(name, self.env)
        except ConfError as e:
            act.die(str(e))
        except LookupError:
            act.die(UNKNOWN % name)

    def builder_outputs(self, p):
        return lsmod.builder_outputs(self.reg, self.clock, p)

    def image_path(self, ws, p=None):
        try:
            host = self.builder_outputs(p) if p is not None else None
        except lsmod.Unknown as e:
            act.die("cannot tell whether %s is built: %s" % (p["IMG_PROFILE"], e))
        found = host if host is not None else (lsmod.outputs(self.machine, self.reg.store, ws) if ws else [])
        return found[0] if found else None

    def buildable(self, spec):
        p = self.profile(spec)
        name = p["IMG_PROFILE"]
        if p["CFG_NEEDS"]:
            act.die("'%s' cannot be built yet:\n\n    %s\n\n    (declared in %s)" % (name, p["CFG_NEEDS"], images.conf_path(name, self.env)))
        if p["IMG_BUILDER"] not in BUILDERS:
            act.die("profile '%s' names builder '%s', which does not exist; there are: %s."
                    % (name, p["IMG_BUILDER"], ", ".join(BUILDERS)))
        return p

    def build(self, spec, rest):
        if not spec:
            act.die("usage: wk sysimage build <profile> [options]; see wk sysimage -h")
        p = self.buildable(spec)
        make = {"fetch": lambda: task.Fetch(self.machine, p, self.env),
                "mac-volume": lambda: macvolume.MacVolume(self.machine, p, self.env, self.clock),
                "guest": lambda: guestbase.Base(self.reg.load("vm"), self.clock),
                "buildroot": lambda: buildroot.Buildroot(self.reg, p, spec, self.clock),
                "yocto": lambda: yocto.Yocto(self.reg, p, spec, self.clock),
                "pmos": lambda: pmos.Pmos(self.reg, p, spec, self.clock)}
        return make[p["IMG_BUILDER"]]().build(rest)

    def webkit(self, spec, rest):
        if not spec:
            act.die("usage: wk sysimage webkit <profile> --commit <sha> --slot <name> [--detach]; see wk sysimage -h")
        p = self.profile(spec)
        if p["IMG_BUILDER"] == "buildroot":
            return buildroot.Buildroot(self.reg, p, spec, self.clock).webkit(rest)
        if p["IMG_BUILDER"] == "yocto" and images.pgo_wanted("yocto", p["CFG_RELEASE"]):
            return pgo.Cycle(self.reg, p, spec, self.clock).webkit(rest)
        if p["IMG_BUILDER"] == "yocto":
            return yocto.Yocto(self.reg, p, spec, self.clock).webkit(rest)
        act.die("'%s' is built by %s, and only a buildroot or yocto image takes WebKit slots"
                % (p["IMG_PROFILE"], p["IMG_BUILDER"] or "no builder"))

    def ls(self, continued):
        here = record.machine_name(self.env)
        rows = lsmod.Listing(self.reg, record.row_label(self.env), here, self.building, clock=self.clock).rows()
        if rows:
            if not continued:
                print(lsmod.ROW % lsmod.HEADER)
            print("\n".join(rows), flush=True)
        if continued:
            return 0
        n = sum(1 for r in rows if not r[:1].isspace())
        if n == 0:
            act.log("no workspace on any machine this one knows has built an image; 'wk sysimage build <profile>' builds one.")
            return 0
        act.log("\n%d image%s. To write one:  wk sysimage write --from <configuration> --disk <machine>:<device> [--rescue]"
                % (n, "" if n == 1 else "s"))
        return 0

    def holds(self, spec, ws, slot, commit, preset, toolchain):
        """The verdict is on stdout: a readonly command forwarded to a stopped podman machine exits 0 having said so."""
        if not spec:
            act.die("usage: wk sysimage holds <profile> [--toolchain|--slot <name> --commit <sha>]; see wk sysimage -h")
        if slot is not None:
            images.check_slot_name(slot)
        p = self.profile(spec)
        name = p["IMG_PROFILE"]
        ws = ws or images.image_ws(name, self.env)
        if toolchain:
            if slot is not None or commit or preset:
                act.die("usage: wk sysimage holds %s --toolchain   (it takes nothing else)" % name)
            if not p["YOC_TARGET"]:
                act.die("%s is built by %s, and only a yocto profile installs a cross toolchain (YOC_TARGET)"
                        % (name, p["IMG_BUILDER"] or "no builder"))
            return self.say(images.toolchain_holds(ws, p["YOC_TARGET"], self.env))
        if slot is None:
            if commit or preset:
                act.die("usage: wk sysimage holds %s --slot <name> --commit <sha>   (--commit and --preset need --slot)" % name)
            return self.say(self.image_path(ws, p) is not None)
        if not SHA.match(commit or ""):
            act.die("usage: wk sysimage holds %s --slot %s --commit <sha>   (a full 40-digit sha, got '%s')"
                    % (name, slot, commit or ""))
        if preset:
            return self.say(lsmod.slot_is(ws, slot, commit, preset, self.env))
        return self.say(lsmod.slot_holds(ws, slot, commit, self.env))

    @staticmethod
    def say(yes):
        print("yes" if yes else "no")
        return 0

    def path(self, spec, ws):
        if not spec:
            act.die("usage: wk sysimage path <profile>; see wk sysimage -h")
        p = self.profile(spec)
        ws = ws or images.image_ws(p["IMG_PROFILE"], self.env)
        found = self.image_path(ws, p)
        if found is None:
            return 1
        print(found)
        return 0

    def write(self, src, spec, profile, mach, rescue, grow):
        """`grow` None is the default: a second system fills the disk, a whole-disk write keeps its built size."""
        fl = fleet.Fleet(images.root(self.env), self.env)
        if mach is not None and not writemod.load_machine(fl, mach):
            act.die("unknown machine '%s' for --machine\n    machines:\n%s" % (mach, writemod.machine_list(fl)))
        if not src:
            act.die("usage: wk sysimage write --from <configuration|path|vm:path> --disk <machine>:<device>[@second|@third]\n"
                    "    'wk sysimage ls' lists every image with the path to pass to --from.")
        if not spec:
            act.die("usage: wk sysimage write --from %s --disk <machine>:<device>[@second|@third]\n"
                    "    'wk sysimage disks <machine>' lists what is attached where." % src)
        second = disk.is_second(spec.partition(":")[2])
        grow = second if grow is None else grow
        if second and rescue:
            act.die("a rescue is the system on partitions 1 and 2, and @second/@third the systems beside it:\n"
                    "    drop --rescue or the @-suffix.")
        w = writemod.Write(images.root(self.env), self.env, self.machine, self.reg.store)
        return w.run(src, spec, grow, profile, "rescue" if rescue else "bench", mach or "")

    def disks(self, name):
        fl = fleet.Fleet(images.root(self.env), self.env)
        if not name:
            act.die("usage: wk sysimage disks <machine>\n    machines:\n%s" % writemod.machine_list(fl))
        conf = writemod.load_machine(fl, name)
        if not conf:
            act.die("unknown machine '%s'" % name)
        w = writemod.Write(images.root(self.env), self.env, self.machine, self.reg.store)
        w.attach(conf)
        if not w.ssh("true").ok:
            act.die("%s is not reachable over ssh" % name)
        act.log("removable disks attached to %s:" % name)
        print(w.disks.listing())
        act.log("\n  write one with:  wk sysimage write --from <path> --disk %s:<device>" % name)
        return 0

