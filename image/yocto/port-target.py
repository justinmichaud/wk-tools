#!/usr/bin/env python3
"""Give a branch a cross-target it does not have, derived from one it does: the derived-from section with its conf_local_path changed, pointing at that target's local.conf with one MACHINE line changed.
Idempotent, and not a substitute for upstreaming the section."""

import argparse
import configparser
import os
import re
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yocto-dir", required=True, help="the checkout's Tools/yocto")
    ap.add_argument("--target", required=True, help="the cross-target to add")
    ap.add_argument("--from-target", required=True, help="the one to derive it from")
    ap.add_argument("--machine", required=True, help="the yocto MACHINE the new target selects")
    ap.add_argument("--image", help="the image recipe the new target builds, when it is not "
                                    "the one the derived-from target builds (a multilib variant)")
    args = ap.parse_args()

    conf_path = os.path.join(args.yocto_dir, "targets.conf")
    if not os.path.isfile(conf_path):
        sys.exit("no targets.conf in %s" % args.yocto_dir)

    cp = configparser.ConfigParser()
    cp.read(conf_path)

    if cp.has_section(args.target):
        print("targets.conf already has [%s]; nothing to port" % args.target)
        return 0
    if not cp.has_section(args.from_target):
        sys.exit("this branch has no [%s] to derive [%s] from; its sections are: %s"
                 % (args.from_target, args.target, ", ".join(cp.sections())))

    src = dict(cp.items(args.from_target))
    src_local = src.get("conf_local_path")
    if not src_local:
        sys.exit("[%s] names no conf_local_path, so there is nothing to derive" % args.from_target)

    src_local_abs = os.path.join(args.yocto_dir, src_local)
    if not os.path.isfile(src_local_abs):
        sys.exit("[%s] names %s, which is not in this checkout" % (args.from_target, src_local))

    new_local = os.path.join(os.path.dirname(src_local),
                             "local-%s.conf" % args.target)
    text = open(src_local_abs, errors="replace").read()
    swapped, n = re.subn(r'(?m)^\s*MACHINE\s*=.*$',
                         'MACHINE = "%s"' % args.machine, text)
    if n != 1:
        sys.exit("%s sets MACHINE %d times; expected exactly one line to change"
                 % (src_local, n))
    with open(os.path.join(args.yocto_dir, new_local), "w") as fh:
        fh.write(swapped)

    section = dict(src, conf_local_path=new_local)
    if args.image:
        section["image_basename"] = args.image
    cp[args.target] = section
    with open(conf_path, "w") as fh:
        cp.write(fh)

    print("ported [%s] from [%s]: MACHINE=%s, %s%s"
          % (args.target, args.from_target, args.machine, new_local,
             ", image %s" % args.image if args.image else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
