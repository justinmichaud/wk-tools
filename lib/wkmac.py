#!/usr/bin/env python3
"""One disk, firmware or display fact of a Mac, for the macOS boot and bench
drivers, and the one way to set a display's brightness; exit 1, printing
nothing, when the fact cannot be read or a set did not take."""
import argparse
import ctypes
import json
import plistlib
import subprocess
import sys

CG_FRAMEWORK = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
DS_FRAMEWORK = "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices"
MAX_DISPLAYS = 16
SET_TOLERANCE = 0.01

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


def _display(cg, ds, ident):
    row = {"id": ident, "brightness": _brightness_of(ds, ident),
           "points": [int(cg.CGDisplayPixelsWide(ident)),
                      int(cg.CGDisplayPixelsHigh(ident))]}
    for key, call in _FLAGS.items():
        row[key] = bool(getattr(cg, call)(ident))
    for key, call in _NUMBERS.items():
        row[key] = int(getattr(cg, call)(ident))
    return row


def _builtin_id(cg):
    ids = _online_ids(cg)
    if ids is None or len(ids) != 1 or not cg.CGDisplayIsBuiltin(ids[0]):
        return None
    return ids[0]


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

    sp = sub.add_parser("brightness", help="the built-in display's brightness, 0.0 to 1.0")
    sp.add_argument("--set", metavar="V", type=_fraction,
                    help="set it to V, then print the value read back; exit 1 printing "
                         "nothing when the read-back is not V")
    sp.set_defaults(func=cmd_brightness)

    sp = sub.add_parser("physical-store", help="device identifier of the physical store backing a volume's APFS container")
    sp.add_argument("target", nargs="?", default="/")
    sp.set_defaults(func=cmd_physical_store)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
