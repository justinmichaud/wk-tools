#!/usr/bin/env python3
"""Will the firmware find everything it needs in this TFTP root?

Why this exists, and why it is a hard refusal rather than a warning.

A netboot that never gets off the ground is *harmless*: the firmware's TFTP
attempt times out, BOOT_ORDER falls through to the local disk, and the board
comes up as it always did.  That fall-through is the safety property the whole
netboot design leans on -- boot/pi-netboot.sh calls it "benign" and it is.

A netboot that gets *partway* is not harmless.  Once the bootloader has pulled
start4.elf over the network and executed it, the second stage owns the boot and
BOOT_ORDER is spent.  If that stage cannot find the kernel its config.txt names,
Pi 4 firmware halts with an LED error pattern.  It does not retry, it does not
return to the bootloader, and it does not touch the SD card.  A headless board in
that state needs a hand on its power supply.

That happened here, on 2026-08-20, to the fleet's rpi4.  The cause was a bug in
wk-tftpd's serial-directory fallback: it retried a missed request under
`os.path.basename`, so `<serial>/current/vmlinuz` became `vmlinuz`, which is not
at the root of a flash-kernel image.  The firmware fetched start4.elf and
config.txt happily, then asked for the kernel, got NOT FOUND, and stopped.  The
files were all present in the served tree; they were simply unreachable through
the resolver.

So this check asks *the resolver* rather than the filesystem.  Reading the tree
with os.path.exists would have found vmlinuz exactly where it was supposed to be
and passed the board straight into the halt.  The only question worth asking is
the one the firmware asks: "if I request this name, do I get bytes?"
"""

import argparse
import importlib.util
import os
import sys


def load_resolver(script):
    spec = importlib.util.spec_from_file_location("wk_tftpd", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve


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
    bootcfg.txt -- and missing those is normal, which is why a log full of
    misses is not by itself a problem.
    """
    prefix = config.get("os_prefix", "")

    kernel = config.get("kernel")
    if not kernel:
        # The firmware's own default, which depends on the width it was told to
        # boot. Both spellings are checked by the caller as alternatives.
        kernel = "kernel8.img" if config.get("arm_64bit") == "1" else "kernel7l.img"

    files = [
        ("second-stage firmware", ["start4.elf"]),
        ("firmware fixup", ["fixup4.dat"]),
        ("kernel", [prefix + kernel]),
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
    ap.add_argument("--root", required=True, help="the TFTP root to check")
    ap.add_argument("--tftpd", required=True, help="path to wk-tftpd.py")
    ap.add_argument("--dtb", default="bcm2711-rpi-4-b.dtb",
                    help="the device tree the target board will ask for")
    ap.add_argument("--serial", default="0badc0de",
                    help="a stand-in serial directory, so the check goes through "
                         "the same prefixed path the firmware uses")
    args = ap.parse_args()

    root = os.path.realpath(args.root)
    resolve = load_resolver(args.tftpd)

    config_path = os.path.join(root, "config.txt")
    if not os.path.isfile(config_path):
        print("no config.txt in the served tree", file=sys.stderr)
        return 1

    with open(config_path, "r", errors="replace") as fh:
        config = parse_config(fh.read())

    missing = []
    for what, candidates in wanted_files(config, args.dtb):
        for name in candidates:
            path, _ = resolve(root, f"{args.serial}/{name}")
            if path is not None:
                break
        else:
            missing.append((what, candidates))

    for what, candidates in missing:
        print(f"{what}: {' or '.join(candidates)}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
