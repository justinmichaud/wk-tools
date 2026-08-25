#!/usr/bin/env python3
"""Will the firmware find everything it needs in this boot filesystem?

Asked by `wk sysimage write` of an image's boot partition before anything is
written to a disk.  Firmware reads config.txt, follows `os_prefix`, and looks
for a kernel -- and a boot that gets *partway* is not harmless.  Once the
bootloader has executed start4.elf, the second stage owns the boot and
BOOT_ORDER is spent: if that stage cannot find the kernel its config.txt
names, Pi 4 firmware halts with an LED error pattern.  It does not retry, it
does not return to the bootloader, and it does not touch the next device.  A
headless board in that state needs a hand on its power supply.

What matters is *how* the files go missing: they are all present in the tree,
and unreachable
through the name-resolution rules the firmware actually applies.  So this
check asks a resolver that models those rules, not os.path.exists -- the only
question worth asking is the one the firmware asks: "if I request this name,
do I get bytes?"
"""

import argparse
import os
import sys


def _inside(root, wanted):
    """Join onto the root, or None if the result escapes it.

    Path traversal is refused by construction: the result must still be inside
    the root after resolution.  A name like `../../etc/shadow` never resolves
    to a file outside the tree being checked, no matter how it is spelled.
    """
    path = os.path.realpath(os.path.join(root, wanted))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def resolve(root, filename):
    """Map a requested name onto a real file inside the tree, or refuse."""
    wanted = filename.lstrip("/").replace("\\", "/")
    return _inside(root, wanted)


def parse_config(text):
    """The assignments a Pi 4 will act on, from config.txt.

    Conditional filters are honoured by ignoring them: everything inside a
    section that is not `[all]` is skipped.  That is deliberately conservative
    rather than a partial reimplementation of the firmware's filter language --
    `[tryboot]` in particular sets a *different* os_prefix for a boot path this
    is not checking, and treating its value as live would look for files in a
    directory the normal boot never opens.

    `[pi4]` is skipped by the same rule.  Nothing in it has ever named a file;
    it carries tuning like arm_boost, and a filter this ignores can only make
    the check miss a problem, never invent one.
    """
    config = {}
    live = True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            live = line.lower() == "[all]"
            continue
        if not live:
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
        else:
            # `initramfs initrd.img followkernel` is the one directive with
            # this shape, and the filename is the first word after it.
            parts = line.split()
            if len(parts) >= 2:
                config[parts[0]] = parts[1]
    return config


def wanted_files(config, model_dtb):
    """What the firmware will ask for, in the order it becomes fatal.

    Only the files whose absence *stops* the boot. The firmware probes for a
    long tail of optional things on every boot -- recovery.elf, dt-blob.bin,
    bootcfg.txt -- and missing those is normal.
    """
    prefix = config.get("os_prefix", "")

    if "kernel" in config:
        # Named explicitly: that file and no other. The firmware will not look
        # for anything else, so neither should this.
        kernels = [prefix + config["kernel"]]
    else:
        # Not named, so the firmware picks from its own defaults by inspecting
        # what is there -- on BCM2711 a bare config.txt with a `kernel8.img`
        # next to it boots 64-bit without `arm_64bit=1` being set anywhere.
        # The WebKit Dev@CI Yocto image is exactly that shape, and deriving one
        # expected name from `arm_64bit` refused it as unbootable when it boots
        # this board every day.
        #
        # So: any of the default names satisfies this. The check's question is
        # "is there a kernel for the firmware to find", not "which one will it
        # pick" -- guessing the second wrongly costs a refused write, while the
        # failure being guarded against is *no kernel at all*, which this still
        # catches.
        kernels = [prefix + k for k in
                   ("kernel8.img", "kernel7l.img", "kernel7.img", "kernel.img")]

    files = [
        ("second-stage firmware", ["start4.elf"]),
        ("firmware fixup", ["fixup4.dat"]),
        ("kernel", kernels),
        ("device tree", [prefix + model_dtb]),
    ]
    if "initramfs" in config:
        files.append(("initramfs", [prefix + config["initramfs"]]))
    if "cmdline" in config:
        # The firmware looks for the cmdline under os_prefix and then at the
        # root, so either satisfies it.
        files.append(("kernel command line",
                      [prefix + config["cmdline"], config["cmdline"]]))
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="the boot filesystem to check")
    ap.add_argument("--dtb", default="bcm2711-rpi-4-b.dtb",
                    help="the device tree the target board will ask for")
    ap.add_argument("--resolve", metavar="NAME",
                    help="instead of checking the tree, print what this one "
                         "request maps to, relative to the root, and nothing at "
                         "all if it maps to no file. For asking the resolver a "
                         "direct question -- wk selftest does exactly that.")
    args = ap.parse_args()

    root = os.path.realpath(args.root)

    if args.resolve is not None:
        path = resolve(root, args.resolve)
        print(os.path.relpath(path, root) if path else "")
        return 0

    config_path = os.path.join(root, "config.txt")
    if not os.path.isfile(config_path):
        print("no config.txt in the boot filesystem", file=sys.stderr)
        return 1

    with open(config_path, "r", errors="replace") as fh:
        config = parse_config(fh.read())

    missing = []
    for what, candidates in wanted_files(config, args.dtb):
        for name in candidates:
            if resolve(root, name) is not None:
                break
        else:
            missing.append((what, candidates))

    for what, candidates in missing:
        print(f"{what}: {' or '.join(candidates)}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
