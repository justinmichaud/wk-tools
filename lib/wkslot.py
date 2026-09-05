#!/usr/bin/env python3
"""A WebKit *slot*: one built WebKit that sits beside others on a board. Its layout
is written by image/buildroot-webkit.sh and read by cmd/pi."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_id_of(path, readelf):
    cp = subprocess.run([readelf, "-n", path], capture_output=True, text=True)
    if cp.returncode != 0:
        sys.exit("%s -n %s failed: %s" % (readelf, path, cp.stderr.strip()))
    m = re.search(r"Build ID:\s*([0-9a-f]+)", cp.stdout)
    if not m:
        sys.exit("%s carries no build-id note; the image's BR2_TARGET_LDFLAGS sets --build-id" % path)
    return m.group(1)


def cmd_manifest(args):
    root = args.root.rstrip("/")
    doc = {}
    for field in args.fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("manifest: not a key=value: %s" % field)
        doc[key] = value
    files = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            files[os.path.relpath(full, root)] = _sha256(full)
    if not files:
        sys.exit("manifest: nothing under %s" % root)
    doc["files"] = files
    # The library that is the WebKit: what `wk pi bench` checks in the running process.
    libs = sorted(rel for rel in files
                  if os.path.dirname(rel) == doc.get("lib_dir", "usr/lib")
                  and os.path.basename(rel).startswith("libWPEWebKit-")
                  and ".so." in rel and not os.path.basename(rel).endswith(".so"))
    if len(libs) != 1:
        sys.exit("manifest: expected one libWPEWebKit-*.so.N.N.N under %s, found %s" % (doc.get("lib_dir", "usr/lib"), libs))
    doc["lib_file"] = libs[0]
    doc["build_id"] = build_id_of(os.path.join(root, libs[0]), args.readelf)
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, args.out)


def _load(path):
    with open(path) as f:
        return json.load(f)


def cmd_sums(args):
    doc = _load(args.slot_json)
    prefix = args.prefix.rstrip("/") + "/" if args.prefix else ""
    for rel, digest in sorted(doc["files"].items()):
        print("%s  %s%s" % (digest, prefix, rel))


def cmd_env(args):
    doc = _load(args.slot_json)
    p = args.prefix.rstrip("/")
    print("LD_LIBRARY_PATH=%s/%s" % (p, doc["lib_dir"]))
    print("WEBKIT_EXEC_PATH=%s/%s" % (p, doc["exec_dir"]))
    print("WEBKIT_INJECTED_BUNDLE_PATH=%s/%s" % (p, doc["bundle_dir"]))


# Read as WK_BOARD_EXPECT by bench/wk_board_driver.py.
def cmd_expect(args):
    doc = _load(args.slot_json)
    p = args.prefix.rstrip("/")
    print(json.dumps({
        "process": "WPEWebProcess",
        "exe": "%s/%s/WPEWebProcess" % (p, doc["exec_dir"]),
        "lib": "%s/%s" % (p, doc["lib_file"]),
        "lib_sha256": doc["files"][doc["lib_file"]],
        "build_id": doc["build_id"],
    }))


# The driver's evidence file: one JSON check per line, one line per iteration.
def cmd_verified(args):
    n = 0
    try:
        with open(args.evidence) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += 1
                if not json.loads(line).get("ok"):
                    print(n)
                    sys.exit(1)
    except FileNotFoundError:
        sys.exit(1)
    print(n)
    sys.exit(0 if n else 1)


def cmd_get(args):
    doc = _load(args.slot_json)
    value = doc.get(args.key, args.default)
    if isinstance(value, (dict, list)):
        print(json.dumps(value))
    else:
        print(value)


def main(argv):
    parser = argparse.ArgumentParser(prog="wkslot.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("manifest", help="write slot.json for an installed root")
    p.add_argument("root")
    p.add_argument("out")
    p.add_argument("fields", nargs="*", metavar="key=value")
    p.add_argument("--readelf", default="readelf", help="the readelf that reads this architecture's ELF")
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("sums", help="the manifest as `sha256sum -c` input")
    p.add_argument("slot_json")
    p.add_argument("--prefix", default="", help="directory the root was unpacked to")
    p.set_defaults(func=cmd_sums)

    p = sub.add_parser("env", help="KEY=VALUE lines a browser needs to run from the slot")
    p.add_argument("slot_json")
    p.add_argument("prefix", help="where root/ is on the machine that runs it")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("expect", help="the running-binary check for a deployed slot, as JSON")
    p.add_argument("slot_json")
    p.add_argument("prefix", help="where root/ is on the machine that runs it")
    p.set_defaults(func=cmd_expect)

    p = sub.add_parser("verified", help="did every recorded running-binary check pass (exit status), and how many")
    p.add_argument("evidence")
    p.set_defaults(func=cmd_verified)

    p = sub.add_parser("get", help="one field out of slot.json")
    p.add_argument("slot_json")
    p.add_argument("key")
    p.add_argument("--default", default="")
    p.set_defaults(func=cmd_get)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
