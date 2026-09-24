"""`wk sysimage`'s verbs -- ls, holds, path, write, --list, build and webkit's dispatch on the builder, and the rm
and flash tombstones -- and the questions the dispatcher asks before it routes one. The yocto and pmos builders
and `disks` are lib/sysimage-arms.sh."""

import re

from wk import act, build, fleet, images, record, shell
from wk.sysimage import buildroot, disk, task, write as writemod
from wk.sysimage import ls as lsmod

SHA = re.compile(r"^[0-9a-f]{40}$")
UNKNOWN = "unknown profile '%s'.\n    'wk sysimage --list' has every configuration."
BUILDERS = ("yocto", "buildroot", "pmos", "fetch")


def lane(argv, env=None):
    return images.ws_arg(list(argv[1:]), env) if argv else ""


def where(argv, env=None):
    """`ls` walks from here and answers another's walk from the store; a verb naming an image workspace runs there."""
    if argv[:1] in (["ls"], ["list"]):
        return "store" if "--continued" in argv[1:] else "local"
    return "workspace" if lane(argv, env) else "host"


def wstarget(argv, reg):
    m = images.spec_machine(argv[1]) if len(argv) > 1 else ""
    if not m or not lane(argv, reg.env):
        return ""
    return images.spec_target(m, record.machine_name(reg.env), reg.default())


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
            target = self.reg.load(self.reg.ws_target(ws))
            return build.busy_reason(target, build.records_of(target, self.clock, self.machine), ws) is not None
        except (LookupError, OSError):
            return False

    def profile(self, spec):
        name = images.spec_profile(spec)
        try:
            return images.load(name, self.env)
        except images.Tombstone as e:
            act.die(str(e))
        except (LookupError, images.ConfError):
            act.die(UNKNOWN % name)

    def image_path(self, ws):
        found = lsmod.outputs(self.machine, self.reg.store, ws)
        return found[0] if found else None

    def buildable(self, spec):
        p = self.profile(spec)
        name = p["IMG_PROFILE"]
        if p["CFG_NEEDS"]:
            act.die("'%s' cannot be built yet:\n\n    %s\n\n    The configuration is declared in %s, which is\n"
                    "    where the missing piece goes once it exists." % (name, p["CFG_NEEDS"], images.conf_path(name, self.env)))
        if not p["IMG_BUILDER"]:
            act.die("profile '%s' names no builder.\n    Every profile declares IMG_BUILDER and there is no default (wk help)." % name)
        if p["IMG_BUILDER"] not in BUILDERS:
            act.die("profile '%s' names builder '%s', which does not exist.\n    There are four: %s."
                    % (name, p["IMG_BUILDER"], ", ".join(BUILDERS)))
        return p

    def build(self, spec, rest):
        if not spec:
            act.die("usage: wk sysimage <sub> [args]; see wk sysimage -h")
        p = self.buildable(spec)
        if p["IMG_BUILDER"] == "fetch":
            return task.Fetch(self.machine, p, self.env).build(rest)
        if p["IMG_BUILDER"] == "buildroot":
            return buildroot.Buildroot(self.reg, p, spec, self.clock).build(rest)
        return shell.sysimage_arms(self.reg.root, p["IMG_BUILDER"], spec, *rest)

    def webkit(self, spec, rest):
        if not spec:
            act.die("usage: wk sysimage webkit <profile> --commit <sha> --slot <name> [--detach]; see wk sysimage -h")
        p = self.profile(spec)
        if p["IMG_BUILDER"] == "buildroot":
            return buildroot.Buildroot(self.reg, p, spec, self.clock).webkit(rest)
        if p["IMG_BUILDER"] == "yocto":
            return shell.sysimage_arms(self.reg.root, "yocto-webkit", spec, *rest)
        act.die("'%s' is built by %s, and only a buildroot or yocto image takes WebKit slots"
                % (p["IMG_PROFILE"], p["IMG_BUILDER"] or "no builder"))

    def ls(self, continued):
        here = record.machine_name(self.env)
        rows = lsmod.Listing(self.reg, self.env.get("WK_ROW_LABEL", ""), here, self.building).rows()
        if rows:
            if not continued:
                print(lsmod.ROW % lsmod.HEADER)
            print("\n".join(rows), flush=True)
        if continued:
            return 0
        n = sum(1 for r in rows if not r[:1].isspace())
        if n == 0:
            act.log("no workspace on any machine this one knows has built an image.")
            act.log("  'wk sysimage build <profile>' builds one; it stays in the workspace")
            act.log("  that built it (wk help), on the machine that built it.")
            return 0
        act.log("")
        act.log("%d image%s, each in the workspace that built it." % (n, "" if n == 1 else "s"))
        act.log("  There is no image store: the workspace is the name (wk help).")
        act.log("  WHERE is the machine holding that workspace, blank for this one; BOARD is")
        act.log("  the machine the image is for.")
        act.log("  To write one:  wk sysimage write --from <configuration above> --disk <machine>:<device>")
        act.log("                 add --rescue for the system a board falls back to")
        return 0

    def holds(self, spec, ws, slot, commit, config, toolchain):
        """A step's done predicate, asked of the machine holding the workspace (lib/sched.py). The verdict is
        on stdout: a readonly command forwarded to a stopped podman machine exits 0 having said so."""
        if not spec:
            act.die("usage: wk sysimage holds <profile> [--toolchain|--slot <name> --commit <sha>]; see wk sysimage -h")
        if slot is not None:
            images.check_slot_name(slot)
        p = self.profile(spec)
        name = p["IMG_PROFILE"]
        ws = ws or images.image_ws(name, self.env)
        if toolchain:
            if slot is not None or commit or config:
                act.die("usage: wk sysimage holds %s --toolchain\n"
                        "    --toolchain asks whether the lane has the cross SDK the webkit stage builds\n"
                        "    against, which is one question about the lane and takes nothing else." % name)
            if not p["YOC_TARGET"]:
                act.die("%s is built by %s, which has no cross toolchain\n"
                        "    of its own to ask about -- only a yocto profile installs one (YOC_TARGET)."
                        % (name, p["IMG_BUILDER"] or "no builder"))
            return self.say(images.toolchain_holds(ws, p["YOC_TARGET"], self.env))
        if slot is None:
            if commit or config:
                act.die("usage: wk sysimage holds %s --slot <name> --commit <sha>\n"
                        "    --commit and --config ask about a slot, so they need --slot; without one\n"
                        "    the question is whether the image itself is built." % name)
            return self.say(bool(ws) and self.image_path(ws) is not None)
        if not SHA.match(commit or ""):
            act.die("usage: wk sysimage holds %s --slot %s --commit <sha>\n"
                    "    --commit takes the full 40-digit sha the slot would hold, got '%s'" % (name, slot, commit or ""))
        if config:
            return self.say(lsmod.slot_is(ws, slot, commit, config, self.env))
        return self.say(lsmod.slot_holds(ws, slot, commit, self.env))

    @staticmethod
    def say(yes):
        print("yes" if yes else "no")
        return 0

    def path(self, spec, ws):
        if not spec:
            act.die("usage: wk sysimage path <profile>; see wk sysimage -h")
        ws = ws or images.image_ws(self.profile(spec)["IMG_PROFILE"], self.env)
        found = self.image_path(ws) if ws else None
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
                    "    An image is bytes a workspace produced, not an id in a catalogue\n    (wk help), so what identifies "
                    "one is its path.\n    'wk sysimage ls' lists every image a workspace here has built, with the\n"
                    "    path to pass to --from.")
        if not spec:
            act.die("usage: wk sysimage write --from %s --disk <machine>:<device>[@second|@third]\n"
                    "    'wk sysimage disks <machine>' lists what is attached where." % src)
        second = disk.is_second(spec.partition(":")[2])
        grow = second if grow is None else grow
        if second and rescue:
            act.die("a rescue is the system on partitions 1 and 2; '@second' and '@third'\n    name the bench systems "
                    "beside it. Drop --rescue, or drop the @-suffix.")
        w = writemod.Write(images.root(self.env), self.env, self.machine, self.reg.store)
        return w.run(src, spec, grow, profile, "rescue" if rescue else "bench", mach or "")

    @staticmethod
    def rm():
        act.die("""'wk sysimage rm' does not exist -- there is no image store to remove from
    (wk help). An image lives in the workspace that built it, so removing it
    is removing that workspace:

        wk rm <workspace>                 (buildroot, or a yocto image workspace)

    'wk sysimage ls' names the workspace beside each image it lists.""")

    @staticmethod
    def flash(machine):
        act.die("""'wk sysimage flash' does not exist -- it named the wrong thing twice.

    'flash <machine>' reads as reflashing {m}. That never happened: the
    machine's own system disk is refused, and what gets written is a removable
    disk plugged into it. And nothing here is permanent -- a machine boots such
    a disk once, by a firmware one-shot, and returns to host mode by itself.

    So the disk is named, and the verb says what it does to it:

        wk sysimage disks {m}                  what is attached over there
        wk sysimage write --from <path> --disk {m}:<device>

    Booting it is still a separate step, and still one-shot:  wk boot {m}""".format(m=machine or "<machine>"))
