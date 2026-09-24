#!/usr/bin/env python3
"""One disk, firmware or display fact of a Mac, for the macOS boot and bench
drivers, and the one way to set a display's brightness or declare its mode;
exit 1, printing nothing, when the fact cannot be read or a set did not take."""
import argparse
import ctypes
import json
import plistlib
import subprocess
import sys

CG_FRAMEWORK = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
DS_FRAMEWORK = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
# CGDisplayCreateUUIDFromDisplayID is exported here and not by CoreGraphics, whose handle dlsym does not find it (26.6.2).
CS_FRAMEWORK = "/System/Library/Frameworks/ColorSync.framework/ColorSync"
CF_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
MAX_DISPLAYS = 16
SET_TOLERANCE = 0.01

# WindowServer reads the mode it comes up at from here, and it is the only way in: CGDisplayCopyAllDisplayModes lists the 1:1 modes alone on an Apple Silicon panel, with or without kCGDisplayShowDuplicateLowResolutionModes, so the scaled mode a bench install is measured at is not one CGDisplaySetDisplayMode can reach (measured 2026-09-08 on Mac16,12: five modes, none of them the running 1280x832@2).
WINDOWSERVER_CONFIG = "/Library/Preferences/com.apple.windowserver.displays.plist"
BUILTIN_SCALE = 2   # a scaled mode's backing store: 1280x832 points over the 2560x1664 panel
kCFStringEncodingUTF8 = 0x08000100

_FLAGS = {"builtin": "CGDisplayIsBuiltin", "main": "CGDisplayIsMain",
          "active": "CGDisplayIsActive", "online": "CGDisplayIsOnline",
          "mirrored": "CGDisplayIsInMirrorSet", "asleep": "CGDisplayIsAsleep"}
_NUMBERS = {"vendor": "CGDisplayVendorNumber", "model": "CGDisplayModelNumber",
            "unit": "CGDisplayUnitNumber"}


def _plist_of(argv):
    try:
        out = subprocess.run(argv, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return plistlib.loads(out)
    except Exception:
        return None


def cmd_volume_name(args):
    info = _plist_of(["diskutil", "info", "-plist", args.target])
    name = info.get("VolumeName") if info else None
    if not name:
        return 1
    print(name)
    return 0


def cmd_volume_group(args):
    info = _plist_of(["diskutil", "info", "-plist", args.target])
    grp = info.get("APFSVolumeGroupID") if info else None
    if not grp:
        return 1
    print(grp)
    return 0


def cmd_boot_volume(args):
    pl = _plist_of(["nvram", "-xp"])
    val = pl.get("boot-volume") if pl else None
    if isinstance(val, bytes):
        try:
            val = val.decode("utf-8")
        except UnicodeDecodeError:
            return 1
    if not val:
        return 1
    print(val)
    return 0


def cmd_physical_store(args):
    # Via `target`'s own container: a Mac can have several APFS containers.
    info = _plist_of(["diskutil", "info", "-plist", args.target])
    stores = info.get("APFSPhysicalStores") if info else None
    if not stores:
        return 1
    dev = stores[0].get("APFSPhysicalStore")
    if not dev:
        return 1
    print(dev)
    return 0


def _declare(lib, name, restype):
    fn = getattr(lib, name)
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = restype


def _coregraphics():
    # Every argtype and restype declared: an undeclared ctypes call on a display handle segfaults, which is why the CGDisplayMode API is not used here at all.
    try:
        cg = ctypes.CDLL(CG_FRAMEWORK)
        cg.CGGetOnlineDisplayList.argtypes = [ctypes.c_uint32,
                                              ctypes.POINTER(ctypes.c_uint32),
                                              ctypes.POINTER(ctypes.c_uint32)]
        cg.CGGetOnlineDisplayList.restype = ctypes.c_int32
        for name in _FLAGS.values():
            _declare(cg, name, ctypes.c_int32)
        for name in _NUMBERS.values():
            _declare(cg, name, ctypes.c_uint32)
        for name in ("CGDisplayPixelsWide", "CGDisplayPixelsHigh"):
            _declare(cg, name, ctypes.c_size_t)
    except (OSError, AttributeError):
        return None
    return cg


def _display_services():
    # macOS ships no brightness CLI; this private framework is the mechanism.
    try:
        ds = ctypes.CDLL(DS_FRAMEWORK)
        ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32,
                                                    ctypes.POINTER(ctypes.c_float)]
        ds.DisplayServicesGetBrightness.restype = ctypes.c_int32
        ds.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
        ds.DisplayServicesSetBrightness.restype = ctypes.c_int32
        _declare(ds, "DisplayServicesCanChangeBrightness", ctypes.c_bool)
        _declare(ds, "DisplayServicesHasAmbientLightCompensation", ctypes.c_bool)
        ds.DisplayServicesAmbientLightCompensationEnabled.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_bool)]
        ds.DisplayServicesAmbientLightCompensationEnabled.restype = ctypes.c_int32
        ds.DisplayServicesEnableAmbientLightCompensation.argtypes = [ctypes.c_uint32,
                                                                     ctypes.c_bool]
        ds.DisplayServicesEnableAmbientLightCompensation.restype = ctypes.c_int32
    except (OSError, AttributeError):
        return None
    return ds


def _online_ids(cg):
    ids = (ctypes.c_uint32 * MAX_DISPLAYS)()
    count = ctypes.c_uint32(0)
    if cg.CGGetOnlineDisplayList(MAX_DISPLAYS, ids, ctypes.pointer(count)) != 0:
        return None
    return [int(ids[i]) for i in range(count.value)]


def _brightness_of(ds, ident):
    value = ctypes.c_float(0.0)
    if ds is None or ds.DisplayServicesGetBrightness(ident, ctypes.pointer(value)) != 0:
        return None
    return round(value.value, 4)


# DisplayServices, the same private framework the brightness itself goes through: `DisplayServicesAmbientLightCompensationEnabled` reads it and `DisplayServicesEnableAmbientLightCompensation` sets it, both measured on tolken (26.6.2, `Mac16,12`) against `dyld_info -exports`. None where a panel has no sensor or the call did not answer -- absent is not off.
def _auto_brightness(ds, ident):
    if ds is None or ident is None:
        return None
    if not ds.DisplayServicesHasAmbientLightCompensation(ident):
        return None
    value = ctypes.c_bool(False)
    if ds.DisplayServicesAmbientLightCompensationEnabled(ident, ctypes.pointer(value)) != 0:
        return None
    return bool(value.value)


def _display(cg, ds, ident):
    row = {"id": ident, "brightness": _brightness_of(ds, ident),
           "points": [int(cg.CGDisplayPixelsWide(ident)),
                      int(cg.CGDisplayPixelsHigh(ident))]}
    for key, call in _FLAGS.items():
        row[key] = bool(getattr(cg, call)(ident))
    for key, call in _NUMBERS.items():
        row[key] = int(getattr(cg, call)(ident))
    row["auto_brightness"] = _auto_brightness(ds, ident)
    return row


def _builtin_id(cg):
    ids = _online_ids(cg)
    if ids is None or len(ids) != 1 or not cg.CGDisplayIsBuiltin(ids[0]):
        return None
    return ids[0]


def _colorsync():
    try:
        cs = ctypes.CDLL(CS_FRAMEWORK)
        cs.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
        cs.CGDisplayCreateUUIDFromDisplayID.restype = ctypes.c_void_p
        cf = ctypes.CDLL(CF_FRAMEWORK)
        cf.CFUUIDCreateString.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFUUIDCreateString.restype = ctypes.c_void_p
        cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_bool
    except (OSError, AttributeError):
        return None, None
    return cs, cf


def _builtin_uuid(cs, cf, ident):
    # What keys the panel's row in the WindowServer configuration: one string per panel, the same on either install of one machine.
    handle = cs.CGDisplayCreateUUIDFromDisplayID(ident)
    if not handle:
        return None
    text = cf.CFUUIDCreateString(None, handle)
    if not text:
        return None
    buf = ctypes.create_string_buffer(64)
    if not cf.CFStringGetCString(text, buf, 64, kCFStringEncodingUTF8):
        return None
    return buf.value.decode()


def _mode_rows(node, uuid):
    """Every declared mode of one panel, wherever WindowServer keeps it. Walked rather than reached by a fixed key path, so a shape that gains a level of nesting is converged rather than silently half-written."""
    rows = []
    if isinstance(node, dict):
        if node.get("UUID") == uuid:
            rows += [node[k] for k in ("CurrentInfo", "UnmirrorInfo")
                     if isinstance(node.get(k), dict)]
        for value in node.values():
            rows += _mode_rows(value, uuid)
    elif isinstance(node, list):
        for value in node:
            rows += _mode_rows(value, uuid)
    return rows


def _points(text):
    try:
        wide, high = text.lower().split("x")
        return int(wide), int(high)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text} is not '<wide>x<high>', as in 1280x832")


def cmd_display_mode(args):
    cg = _coregraphics()
    if cg is None:
        return 1
    ident = _builtin_id(cg)
    if ident is None:
        return 1
    if args.declare is None:
        print("%dx%d" % (int(cg.CGDisplayPixelsWide(ident)),
                         int(cg.CGDisplayPixelsHigh(ident))))
        return 0

    cs, cf = _colorsync()
    if cs is None:
        return 1
    uuid = _builtin_uuid(cs, cf, ident)
    if uuid is None:
        return 1
    try:
        with open(WINDOWSERVER_CONFIG, "rb") as handle:
            doc = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return 1
    rows = _mode_rows(doc, uuid)
    if not rows:
        return 1
    wide, high = args.declare
    for row in rows:
        row["Wide"], row["High"], row["Scale"] = wide, high, BUILTIN_SCALE
    try:
        with open(WINDOWSERVER_CONFIG, "wb") as handle:
            plistlib.dump(doc, handle)
        with open(WINDOWSERVER_CONFIG, "rb") as handle:
            back = plistlib.load(handle)
    except OSError:
        return 1
    rows = _mode_rows(back, uuid)
    if not rows or any((row.get("Wide"), row.get("High"), row.get("Scale"))
                       != (wide, high, BUILTIN_SCALE) for row in rows):
        return 1
    print("%dx%d" % (wide, high))
    return 0


def _fraction(text):
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(f"{text} is not a fraction from 0.0 to 1.0")
    return value


def cmd_displays(args):
    cg = _coregraphics()
    if cg is None:
        return 1
    ids = _online_ids(cg)
    if ids is None:
        return 1
    ds = _display_services()
    rows = [_display(cg, ds, ident) for ident in ids]
    print(json.dumps({"count": len(rows), "displays": rows}, sort_keys=True))
    return 0


def cmd_brightness(args):
    cg = _coregraphics()
    ds = _display_services()
    if cg is None or ds is None:
        return 1
    ident = _builtin_id(cg)
    if ident is None:
        return 1
    if args.set is not None:
        if not ds.DisplayServicesCanChangeBrightness(ident):
            return 1
        if ds.DisplayServicesSetBrightness(ident, args.set) != 0:
            return 1
    value = _brightness_of(ds, ident)
    if value is None:
        return 1
    if args.set is not None and abs(value - args.set) > SET_TOLERANCE:
        return 1
    print(value)
    return 0


# Held rather than declined: a brightness ambient light can raise again is a load that varies, and the gate that refuses a run under it is the same rule either way -- this is what lets a machine pass it instead of being sent away.
def cmd_auto_brightness(args):
    cg = _coregraphics()
    ds = _display_services()
    if cg is None or ds is None:
        return 1
    ident = _builtin_id(cg)
    if ident is None:
        return 1
    if not ds.DisplayServicesHasAmbientLightCompensation(ident):
        print("none")   # no sensor to hold: not a refusal, and not "off" either
        return 0
    if args.off and ds.DisplayServicesEnableAmbientLightCompensation(ident, False) != 0:
        return 1
    value = _auto_brightness(ds, ident)
    if value is None:
        return 1
    print("on" if value else "off")
    return 1 if (args.off and value) else 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("volume-name", help="VolumeName of a mounted APFS volume")
    sp.add_argument("target", help="mount point, device node, or disk identifier")
    sp.set_defaults(func=cmd_volume_name)

    sp = sub.add_parser("volume-group", help="APFS volume group UUID of a mounted volume")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_volume_group)

    sp = sub.add_parser("boot-volume", help="the firmware's boot-volume NVRAM value (colon-separated UUIDs)")
    sp.set_defaults(func=cmd_boot_volume)

    sp = sub.add_parser("displays", help="every online display as JSON: id, builtin, "
                                        "main, active, online, mirrored, asleep, "
                                        "points, vendor, model, unit, brightness")
    sp.set_defaults(func=cmd_displays)

    sp = sub.add_parser("display-mode", help="the built-in display's mode in points, "
                                            "as <wide>x<high>")
    sp.add_argument("--declare", metavar="WxH", type=_points,
                    help="declare WxH as the mode WindowServer comes up at, by rewriting "
                         "every row of the built-in panel in "
                         + WINDOWSERVER_CONFIG + " (root, and it takes effect at the next "
                         "boot -- what is printed is what the file now declares, not the "
                         "running mode); exit 1 printing nothing when the file does not "
                         "read back as asked")
    sp.set_defaults(func=cmd_display_mode)

    sp = sub.add_parser("brightness", help="the built-in display's brightness, 0.0 to 1.0")
    sp.add_argument("--set", metavar="V", type=_fraction,
                    help="set it to V, then print the value read back; exit 1 printing "
                         "nothing when the read-back is not V")
    sp.set_defaults(func=cmd_brightness)

    sp = sub.add_parser("auto-brightness", help="whether the built-in panel is under "
                                                "ambient-light control: on, off, or none "
                                                "for a panel with no sensor")
    sp.add_argument("--off", action="store_true",
                    help="turn it off first, then print what it reads back; exit 1 "
                         "when it still reads on")
    sp.set_defaults(func=cmd_auto_brightness)

    sp = sub.add_parser("physical-store", help="device identifier of the physical store backing a volume's APFS container")
    sp.add_argument("target", nargs="?", default="/")
    sp.set_defaults(func=cmd_physical_store)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
