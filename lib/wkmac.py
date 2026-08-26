#!/usr/bin/env python3
"""Answers the macOS boot/bench drivers' diskutil and nvram questions.

Each subcommand reads `diskutil ... -plist` or `nvram -xp`, prints one
value to stdout, and exits 1 with nothing printed when it is absent.
"""
import argparse
import plistlib
import subprocess
import sys


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
    # The physical store(s) backing the container that carries `target`
    # (default `/`) -- not "the first container `diskutil apfs list`
    # happens to print", which on a Mac with more than one APFS container
    # (an internal-recovery container alongside the boot one) is not
    # necessarily the same container at all.
    info = _plist_of(["diskutil", "info", "-plist", args.target])
    stores = info.get("APFSPhysicalStores") if info else None
    if not stores:
        return 1
    dev = stores[0].get("APFSPhysicalStore")
    if not dev:
        return 1
    print(dev)
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

    sp = sub.add_parser("physical-store", help="device identifier of the physical store backing a volume's APFS container")
    sp.add_argument("target", nargs="?", default="/")
    sp.set_defaults(func=cmd_physical_store)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
