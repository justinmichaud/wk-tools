#!/usr/bin/env python3
"""Does this boot filesystem hold every file its config.txt's `[all]` asks for? A missing kernel halts a Pi 4."""

import argparse
import os
import sys


def resolve(root, filename):
    path = os.path.realpath(os.path.join(root, filename.lstrip("/").replace("\\", "/")))
    if path != root and not path.startswith(root + os.sep):
        return None
    return path if os.path.isfile(path) else None


def parse_config(text):
    config, live = {}, True
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            live = line.lower() == "[all]"
        elif live and "=" in line:
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip()
        elif live and len(line.split()) >= 2:
            config[line.split()[0]] = line.split()[1]
    return config


def wanted_files(config, model_dtb):
    prefix = config.get("os_prefix", "")
    # The firmware takes whichever is present; kernel_2712.img is the only name meta-raspberrypi gives a Pi 5's.
    kernels = [config["kernel"]] if "kernel" in config else \
        ["kernel8.img", "kernel_2712.img", "kernel7l.img", "kernel7.img", "kernel.img"]
    files = [("second-stage firmware", ["start4.elf"]), ("firmware fixup", ["fixup4.dat"]),
             ("kernel", [prefix + k for k in kernels]), ("device tree", [prefix + model_dtb])]
    if "initramfs" in config:
        files.append(("initramfs", [prefix + config["initramfs"]]))
    if "cmdline" in config:
        files.append(("kernel command line", [prefix + config["cmdline"], config["cmdline"]]))
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True, help="the boot filesystem to check")
    ap.add_argument("--dtb", default="bcm2711-rpi-4-b.dtb", help="the device tree the board will ask for")
    args = ap.parse_args()
    root = os.path.realpath(args.root)
    try:
        with open(os.path.join(root, "config.txt"), errors="replace") as fh:
            config = parse_config(fh.read())
    except FileNotFoundError:
        print("no config.txt in the boot filesystem", file=sys.stderr)
        return 1
    missing = [(what, names) for what, names in wanted_files(config, args.dtb)
               if not any(resolve(root, n) for n in names)]
    for what, names in missing:
        print("%s: %s" % (what, " or ".join(names)), file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
