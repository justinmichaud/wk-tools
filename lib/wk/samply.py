"""samply, one version and checksum set for the fleet; asked of the measured process's arch (a lib32 image has a 64-bit kernel)."""

import argparse
import os
import sys

from wk import act
from wk.sysimage.task import fetch_pinned

VERSION = "0.13.1"
TRIPLES = {("Darwin", "arm64"): "aarch64-apple-darwin", ("Darwin", "x86_64"): "x86_64-apple-darwin",
           ("Linux", "x86_64"): "x86_64-unknown-linux-gnu", ("Linux", "aarch64"): "aarch64-unknown-linux-gnu"}
SHA256 = {
    "x86_64-unknown-linux-gnu": "61875daad67888798690dea3cb2748279df6ac299c5c6a857d67eed7642473d9",
    "aarch64-unknown-linux-gnu": "aa465162b62830168775b7ff4804bc35049436dcbc29bb3d1ea9f580380ea06a",
    "aarch64-apple-darwin": "7597239aa3769e75058be5ed359dbfe067f5e7714a5f052c45dd81d509aec17f",
    "x86_64-apple-darwin": "a57f05f9162d06c36df6d51425d976ac774a8cd6dbd84050e6303fa8bf813998",
}


def triple(arch, system="Linux"):
    return TRIPLES.get(("Darwin" if system == "Darwin" else "Linux", arch), "")


def url(t):
    return "https://github.com/mstange/samply/releases/download/samply-v%s/samply-%s.tar.xz" % (VERSION, t)


def store_dir(cache, t):
    return os.path.join(cache, "samply", "%s-%s" % (VERSION, t))


def fetch(machine, cache, arch, system="Linux"):
    t = triple(arch, system)
    if not t:
        return ""
    d = store_dir(cache, t)
    binary = os.path.join(d, "samply")
    if machine.run(["test", "-x", binary]).ok:
        return binary
    tmp = machine.run(["mktemp", "-d"]).out.strip()
    if not tmp:
        return ""
    tar = os.path.join(tmp, "samply.tar.xz")
    try:
        why = fetch_pinned(machine, url(t), tar, SHA256[t])
        if why:
            act.warn("samply %s for %s: %s" % (VERSION, t, why))
            return ""
        if not machine.act_run(["tar", "-xJf", tar, "-C", tmp]).ok:
            act.warn("samply %s for %s would not unpack" % (VERSION, t))
            return ""
        machine.mkdir(d)
        if not machine.act_run(["install", "-m", "0755", os.path.join(tmp, "samply-" + t, "samply"), binary]).ok:
            return ""
        return binary
    finally:
        machine.run(["rm", "-rf", tmp])


def resolve(arch, have_sysprof, system="Linux"):
    if triple(arch, system):
        return "samply", "upstream publishes samply %s for %s" % (VERSION, arch)
    if have_sysprof:
        return "sysprof", "no samply release for %s; the image ships sysprof-cli" % arch
    return None, ("neither samply nor sysprof can profile a %s userspace: upstream publishes\n    no samply binary for it "
                  "(x86_64 and aarch64 only), and the image has no\n    sysprof-cli. Add sysprof-cli to the image, or build "
                  "samply %s for it." % (arch, VERSION))


def main(argv):
    parser = argparse.ArgumentParser(prog="python3 -m wk.samply")
    parser.add_subparsers(dest="verb", required=True).add_parser("release").add_argument("arch", help="uname -m")
    t = triple(parser.parse_args(argv).arch)
    if not t:
        return 1
    print(VERSION, t, SHA256[t], url(t))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
