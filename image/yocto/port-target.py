#!/usr/bin/env python3
"""Give a branch a cross-target it does not have, derived from one it does.

WebKit's `Tools/yocto/targets.conf` gained its rpi5 section after the 2.4x
releases branched, so those branches can build for every Pi but that one --
even though the layers they pin already support the machine (wpe-2.46's
meta-raspberrypi has conf/machine/raspberrypi5.conf).  What is missing is
WebKit's own glue: a section, and the local.conf it points at.

Both are *derived* from a target the branch does have, not vendored: the
local.conf is that target's with one MACHINE line changed, and the section is
its section with one path changed.  So there is no copy of somebody else's
200-line local.conf here to drift from theirs.

Idempotent: a checkout that already has the section is left alone, so a
re-run after a killed build converges rather than appending a second section.

Not a substitute for upstreaming the section.  The caller says, every run,
that it ported a target.
"""

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

    # The local.conf: that target's, with the machine changed and nothing else.
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

    # The section: that target's, pointing at the local.conf just written.
    cp[args.target] = dict(src, conf_local_path=new_local)
    with open(conf_path, "w") as fh:
        cp.write(fh)

    print("ported [%s] from [%s]: MACHINE=%s, %s"
          % (args.target, args.from_target, args.machine, new_local))
    return 0


if __name__ == "__main__":
    sys.exit(main())
