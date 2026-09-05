#!/usr/bin/env python3
"""Will the firmware find everything it needs in this boot filesystem?
Once the bootloader has executed start4.elf, BOOT_ORDER is spent: a second stage that cannot find the kernel its config.txt names halts a Pi 4 with an LED pattern rather than retrying or moving to the next device. So this asks a resolver that models the firmware's name resolution, not os.path.exists."""

import argparse
import os
import sys


def _inside(root, wanted):
    path = os.path.realpath(os.path.join(root, wanted))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def resolve(root, filename):
    wanted = filename.lstrip("/").replace("\\", "/")
    return _inside(root, wanted)


def parse_config(text):
    """The assignments a Pi 4 acts on: everything outside `[all]` is skipped, `[tryboot]` above all, its os_prefix belonging to a boot path this is not checking."""
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
            words = line.split()
            if len(words) >= 2:
                directive, filename = words[0], words[1]
                config[directive] = filename
    return config


def wanted_files(config, model_dtb):
    prefix = config.get("os_prefix", "")

    if "kernel" in config:
        kernels = [prefix + config["kernel"]]
    else:
        # Unnamed, so the firmware picks among these by what is present and any of them answers the question. kernel_2712.img is the Pi 5's, the only name meta-raspberrypi's raspberrypi5.conf puts on a correct image for that board.
        kernels = [prefix + k for k in
                   ("kernel8.img", "kernel_2712.img",
                    "kernel7l.img", "kernel7.img", "kernel.img")]

    files = [
        ("second-stage firmware", ["start4.elf"]),
        ("firmware fixup", ["fixup4.dat"]),
        ("kernel", kernels),
        ("device tree", [prefix + model_dtb]),
    ]
    if "initramfs" in config:
        files.append(("initramfs", [prefix + config["initramfs"]]))
    if "cmdline" in config:
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
