"""Image profiles (image/configs/<name>.conf), their workspaces `<builder>-<profile>[-<arm>]`, slots and PGO."""

import os
import re

from wk import act, kv
from wk.kv import ConfError
from wk.store import Store

FIELDS = {
    "IMG_BUILDER": "", "IMG_MACHINE": "", "IMG_ARCH": "", "IMG_HOSTNAME": "",
    "IMG_WATCHDOG": "300",
    "YOC_BRANCH": "", "YOC_TARGET": "", "YOC_IMAGE": "", "YOC_RM_WORK": "",
    "YOC_CHROMIUM": "1", "YOC_REMOTE": "origin", "YOC_LOCAL_LAYER": "1",
    "YOC_PORT_TARGET_FROM": "", "YOC_MACHINE": "", "YOC_MULTILIB": "", "YOC_MULTILIB_TUNE": "",
    "CFG_PROJECT": "", "CFG_RELEASE": "", "CFG_BRANCH": "", "CFG_REMOTE": "", "CFG_NEEDS": "",
    "BR_TREE_URL": "", "BR_TREE_BRANCH": "", "BR_TREE_COMMIT": "", "BR_DEFCONFIG": "",
    "BR_OVERLAY_TAILSCALE": "", "BR_EXTERNAL": "", "BR_IMAGE": "",
    "BR_KERNEL_DEB_URL": "", "BR_KERNEL_DEB_SHA256": "", "BR_KERNEL_RELEASE": "",
    "FET_URL": "", "FET_SHA256": "", "FET_XZ": "", "FET_NOTE": "", "FET_DEVICE": "",
    "PMO_DEVICE": "", "PMO_UI": "", "PMO_CHANNEL": "", "PMO_PMB_VERSION": "",
    "PMO_USER": "", "PMO_PASSWORD": "", "PMO_PACKAGES": "", "PMO_EXTRA_SPACE": "",
    "PMO_BRIDGE": "", "PMO_BUILD_HOST": "", "PMO_WIFI_BANDS": "",
    "PMO_KERNEL_APORT": "", "PMO_KCONFIG": "",
}
MARKER = "/etc/wk-image"
WS_BUILDERS = ("yocto", "buildroot")
PGO_FROM = (2, 52)   # upstream's cmake USE_PGO_PROFILE, 310954@main
PGO_SUBDIR = "wk-pgo"
INSTR_SUFFIX = "-instr"
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BLURB = re.compile(r"^# \S+ -- (.*)$")
SLOT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")

_PERF = """there is no profile '%s'. Use:
    rpi5-perf        -> webkit-2.52-yocto-rpi5-64
    rpi4-perf        -> webkit-2.52-yocto-rpi4-64
    rpi3-perf        -> webkit-2.52-yocto-rpi3-32
    rpi4-wpe-2.48    -> webkit-2.52-yocto-rpi4-64
    rpi4-wpe-2.48-32 -> webkit-2.52-yocto-rpi4-32
    rpi3-wpe-2.48-32 -> webkit-2.52-yocto-rpi3-32
    rpi3-wpe-2.48-64 -> rpi3 is 32-bit here; use webkit-2.52-yocto-rpi3-32
    rpi5-wpe-2.48    -> webkit-2.52-yocto-rpi5-64
    mac-bench        -> perf-macos-tolken"""
_DOWNSTREAM = """there is no profile '%s'. Configurations are named for the project
    they are built from -- WebKit or WPEWebKit -- and webkitglib/2.48 is a
    branch in WebKit/WebKit.

    WPEWebKit releases here are 2.38 and 2.46; WebKit's is 2.52. So:

        downstream-wpe-2.46-rpi4          -> wpewebkit-2.46-yocto-rpi4-64
        downstream-yocto-wpe-2.48-rpi4    -> webkit-2.52-yocto-rpi4-64
        downstream-yocto-wpe-2.48-rpi4-32 -> webkit-2.52-yocto-rpi4-32
        downstream-yocto-wpe-2.48-rpi3-32 -> webkit-2.52-yocto-rpi3-32
        downstream-yocto-wpe-2.48-rpi3-64 -> the rpi3 is 32-bit here
        downstream-yocto-wpe-2.48-rpi5    -> webkit-2.52-yocto-rpi5-64

    'wk sysimage configs' has all of them."""
_PERF_LINUX = """there is no '%s'. A perf system is built by yocto or buildroot, or
    it is macOS (wk help). For this board:

        perf-linux-rpi3  -> webkit-2.52-yocto-rpi3-32   (32-bit, its native width)
        perf-linux-rpi4  -> webkit-2.52-yocto-rpi4-64
        perf-linux-rpi5  -> webkit-2.52-yocto-rpi5-64"""
TOMBSTONES = dict(
    [(n, _PERF) for n in ("rpi5-perf", "rpi4-perf", "rpi3-perf", "rpi4-wpe-2.48", "rpi4-wpe-2.48-32",
                          "rpi3-wpe-2.48-32", "rpi3-wpe-2.48-64", "rpi5-wpe-2.48", "mac-bench")]
    + [(n, _DOWNSTREAM) for n in ("downstream-wpe-2.46-rpi4", "downstream-yocto-wpe-2.48-rpi4",
                                  "downstream-yocto-wpe-2.48-rpi4-32", "downstream-yocto-wpe-2.48-rpi3-32",
                                  "downstream-yocto-wpe-2.48-rpi3-64", "downstream-yocto-wpe-2.48-rpi5")]
    + [(n, _PERF_LINUX) for n in ("perf-linux-rpi3", "perf-linux-rpi4", "perf-linux-rpi5")])


class Tombstone(LookupError):
    pass


def root(env=None):
    env = os.environ if env is None else env
    return env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def driving_key_path(env):
    return env.get("WK_IMAGE_KEY") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".ssh", "id_ed25519.pub")


def marker(env=None):
    env = os.environ if env is None else env
    return env.get("WK_IMAGE_MARKER") or MARKER


def config_dir(env=None):
    return os.path.join(root(env), "image", "configs")


def conf_path(name, env=None):
    return os.path.join(config_dir(env), name + ".conf")


def names(env=None):
    try:
        return sorted(f[:-5] for f in os.listdir(config_dir(env)) if f.endswith(".conf"))
    except OSError:
        return []


def _field(key):
    return None if key in FIELDS else "%s is not a profile field" % key


def parse(path):
    return kv.conf_file(path, _field)


def load(name, env=None):
    if name in TOMBSTONES:
        raise Tombstone(TOMBSTONES[name] % name)
    path = conf_path(name, env)
    if not NAME.match(name or "") or not os.path.isfile(path):
        raise LookupError(name)
    profile = dict(FIELDS)
    profile.update(parse(path))
    profile["IMG_PROFILE"] = name
    profile["IMG_SPEC_DIR"] = os.path.join(root(env), "image", name)
    return profile


def mac_profile(env=None):
    return next((n for n in names(env) if (quiet_load(n, env) or {}).get("IMG_BUILDER") == "mac-volume"), "")


def quiet_load(name, env=None):
    try:
        return load(name, env)
    except (LookupError, ConfError):
        return None


def blurb(name, env=None):
    with open(conf_path(name, env), errors="replace") as f:
        m = BLURB.match(f.readline().rstrip("\n"))
    return m.group(1) if m else ""


def listing(env=None):
    out = []
    for n in names(env):
        out += [n, "            " + blurb(n, env)]
        p = quiet_load(n, env)
        if p and p["CFG_NEEDS"]:
            out.append("            -- not buildable yet; 'wk sysimage build %s' says what it needs" % n)
    return "".join(line + "\n" for line in out)


def origin_branches(env=None):
    """origin's branches a profile tracks: a mirror carries these and main, and every other upstream whole."""
    got = {p["CFG_BRANCH"] for p in map(lambda n: quiet_load(n, env), names(env))
           if p and p["CFG_REMOTE"] == "origin" and p["CFG_BRANCH"]}
    return sorted(got)


def version(text):
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))


def pgo_wanted(builder, release):
    return builder == "yocto" and bool(release) and version(release) >= PGO_FROM


def spec_profile(spec):
    return spec.split("@", 1)[0]


def spec_machine(spec):
    return spec.split("@", 1)[1] if "@" in spec else ""


def spec_target(machine, here, default):
    return default if machine == here else machine


def image_ws(spec, env=None):
    """`<builder>-<profile>`, or "" for a builder that builds on the host, or no profile at all."""
    profile = spec_profile(spec)
    try:
        builder = load(profile, env)["IMG_BUILDER"]
    except LookupError:
        return ""
    except ConfError as e:
        act.die(str(e))
    return "%s-%s" % (builder, profile) if builder in WS_BUILDERS else ""


def ws_profile(ws, env=None):
    """The profile an image workspace builds, by longest match, so an arm's `-<arm>` still names it."""
    builder = next((b for b in WS_BUILDERS if ws.startswith(b + "-")), None)
    if builder is None:
        return None
    rest = ws[len(builder) + 1:]
    hits = [n for n in names(env) if rest == n or rest.startswith(n + "-")]
    return max(hits, key=len) if hits else None


def ws_arg(args, env=None):
    """`--workspace <name>` when the arguments give one, else the profile's own image workspace."""
    if not args or not args[0] or args[0].startswith("-"):
        return ""
    for prev, a in zip([""] + args[1:], args[1:]):
        if prev == "--workspace":
            return a
        if a.startswith("--workspace=") and len(a) > len("--workspace="):
            return a.split("=", 1)[1]
    return image_ws(args[0], env)


def slot_dir(ws, slot, env=None):
    """A slot is one WebKit built beside the image in the workspace that built it, placed where its builder puts output."""
    profile = ws_profile(ws, env)
    if profile is None:
        return None
    d = Store(env).ws_dir(ws)
    if ws.startswith("buildroot-"):
        return os.path.join(d, "build", "buildroot", profile, "output", "wk-slots", slot)
    return os.path.join(d, "build", "wk-slots", slot)


def toolchain_holds(ws, cross_target, env=None):
    """cross-toolchain-helper's own test of an installed SDK, which lib/wk/sysimage/yocto_target.py reads too."""
    d = os.path.join(Store(env).ws_dir(ws), "build", "CrossToolChains", cross_target, "build", "toolchain")
    if not os.path.isfile(os.path.join(d, ".toolchain_path_configured")):
        return False
    try:
        return any(f.startswith("environment-setup-") and os.path.isfile(os.path.join(d, f)) for f in os.listdir(d))
    except OSError:
        return False


def pgo_dir(ws, slot, env=None):
    return os.path.join(Store(env).ws_dir(ws), "build", PGO_SUBDIR, slot)


def pgo_dir_in(slot):
    return "/src/WebKit/WebKitBuild/%s/%s" % (PGO_SUBDIR, slot)


def instr_slot(slot):
    return slot + INSTR_SUFFIX


def measured_slot(slot):
    return slot[:-len(INSTR_SUFFIX)] if slot.endswith(INSTR_SUFFIX) else slot


def ws_machine(named, target, here):
    """The machine holding an image workspace: the one a spec named, else its target's, `container` and `local` being here."""
    if named:
        return named
    return here if target in ("container", "local") else target


def build_resource(machine):
    """One machine builds one image at a time, whichever workspace it is for."""
    return "machine:" + machine


CONFIG_WORDS = {
    "wpe-cross-pgo-collect": "instrumented, to collect a profile from -- not a measurement",
    "wpe-cross-pgo-use": "the measured build, against the mixed profile",
    "wpe-cross": "built without a profile",
    "": "the image itself",
}


def build_subject(ws, stage, slot, commit, cross_config):
    if stage == "webkit":
        return "slot %s in %s at %.12s -- %s" % (slot, ws, commit, CONFIG_WORDS.get(cross_config, cross_config))
    if stage == "pgo-mix":
        return "mixing slot %s's collection in %s" % (slot, ws)
    return "%s stage of %s" % (stage or "build", ws)


def check_slot_name(slot):
    if not SLOT.match(slot or ""):
        act.die("""slot '%s' is not usable: letters, digits, '_', '.' and '-',
    not starting with '-' or '.'. It names a directory here and on the board.""" % slot)
