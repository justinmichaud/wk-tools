#!/usr/bin/env python3
"""Will the firmware find everything it needs in this boot filesystem?

Asked of a TFTP root and of an image's boot partition alike, because it is the
same question either way: firmware reads config.txt, follows `os_prefix`, and
looks for a kernel.  The only difference is the spelling -- over TFTP every
request carries a serial-number directory, and on a disk none of them do, which
is what `--serial ""` is for.

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
import importlib.machinery
import importlib.util
import os
import sys


def load_resolver(script):
    """Import `resolve` out of the server that is actually going to run.

    An explicit SourceFileLoader rather than letting spec_from_file_location
    infer one: on the privileged path the server is
    /usr/local/libexec/wk-tftpd, a root-owned copy with no .py extension, and
    inference returns a spec with no loader for a name Python does not
    recognise as source.

    Pointing this at the repo's copy when the installed one will serve is the
    whole failure this argument exists to prevent -- see cmd/serve.
    """
    loader = importlib.machinery.SourceFileLoader("wk_tftpd", script)
    spec = importlib.util.spec_from_file_location("wk_tftpd", script, loader=loader)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        # The server called main() at import. Every copy new enough to be
        # importable guards that call, so this *is* the staleness signature --
        # and it is worth naming rather than letting argparse's usage message
        # be the whole of the diagnosis.
        raise SystemExit(
            f"{script} ran its main() on import, so it predates the importable "
            f"guard in boot/wk-tftpd.py. It is a stale copy: re-run "
            f"'./setup --stage quiesce' to reinstall it."
        )
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
    ap.add_argument("--root", required=True, help="the TFTP root to check")
    ap.add_argument("--tftpd", required=True,
                    help="the tftp server that will actually serve this root")
    ap.add_argument("--dtb", default="bcm2711-rpi-4-b.dtb",
                    help="the device tree the target board will ask for")
    ap.add_argument("--serial", default="0badc0de",
                    help="a stand-in serial directory, so the check goes through "
                         "the same prefixed path the firmware uses over TFTP. "
                         "Empty for a boot filesystem on a disk, where firmware "
                         "asks for the plain name and there is no prefix to "
                         "fall back from")
    ap.add_argument("--resolve", metavar="NAME",
                    help="instead of checking the tree, print what this one "
                         "request maps to, relative to the root, and nothing at "
                         "all if it maps to no file. For asking the resolver a "
                         "direct question -- wk selftest does exactly that.")
    args = ap.parse_args()

    root = os.path.realpath(args.root)
    resolve = load_resolver(args.tftpd)

    def ask(name):
        """One request, spelled the way the firmware would spell it here."""
        return resolve(root, f"{args.serial}/{name}" if args.serial else name)

    if args.resolve is not None:
        path, _ = resolve(root, args.resolve)
        print(os.path.relpath(path, root) if path else "")
        return 0

    config_path = os.path.join(root, "config.txt")
    if not os.path.isfile(config_path):
        print("no config.txt in the served tree", file=sys.stderr)
        return 1

    with open(config_path, "r", errors="replace") as fh:
        config = parse_config(fh.read())

    missing = []
    for what, candidates in wanted_files(config, args.dtb):
        for name in candidates:
            path, _ = ask(name)
            if path is not None:
                break
        else:
            missing.append((what, candidates))

    for what, candidates in missing:
        print(f"{what}: {' or '.join(candidates)}", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
