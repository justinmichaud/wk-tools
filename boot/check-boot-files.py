#!/usr/bin/env python3
"""Will the firmware find everything it needs in this boot filesystem? Once start4.elf runs, BOOT_ORDER is spent: a
missing kernel halts a Pi 4 rather than trying the next device."""

import argparse
import os
import sys


def resolve(root, filename):
    path = os.path.realpath(os.path.join(root, filename.lstrip("/").replace("\\", "/")))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def parse_config(text):
    """The assignments a Pi 4 acts on: only `[all]`'s; `[tryboot]`'s belong to a boot path this is not checking."""
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
        # The firmware takes whichever is present; kernel_2712.img is the only name meta-raspberrypi gives a Pi 5's.
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
                    help="print the file this name resolves to, relative to the root, or nothing")
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
