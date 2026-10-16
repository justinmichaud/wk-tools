"""Whether this machine's setup has converged on its tree: a whole `./setup` records the tree and the boot it ran in."""

import hashlib
import json
import os
import sys

from wk import images
from wk.machine import Local, is_macos
from wk.store import Store

RECORD = "setup-applied"
LAUNCH_AGENTS = ("com.wk.inject", "com.wk.broker")


def tree(root, machine):
    status = machine.run(["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"]).out
    h = hashlib.sha256((machine.run(["git", "-C", root, "rev-parse", "HEAD"]).out + "\0" + status).encode())
    for entry in status.split("\0"):
        path = os.path.join(root, entry[3:])
        if entry[3:] and machine.exists(path) and not machine.isdir(path):
            h.update(machine.read_bytes(path))
    return h.hexdigest()


def boot(machine):
    if is_macos():
        return machine.run(["sysctl", "-n", "kern.boottime"]).out.split(",")[0].strip()
    return machine.run(["cat", "/proc/sys/kernel/random/boot_id"]).out.strip()


def path(env):
    return os.path.join(Store(env).state_dir(), RECORD)


def read(env, machine):
    try:
        return json.loads(machine.read(path(env)))
    except (OSError, ValueError):
        return None


def write(root, env, machine):
    machine.write_own(path(env), json.dumps({"tree": tree(root, machine), "boot": boot(machine)}))


def unloaded_agents(env, machine):
    plists = os.path.join(Store(env).home(), "Library", "LaunchAgents")
    return [label for label in LAUNCH_AGENTS if machine.exists(os.path.join(plists, label + ".plist"))
            and not machine.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)]).ok]


def behind(root, env, machine=None):
    """Why setup should run before a command, or "": never where no person has set it up (no record)."""
    machine = machine or Local()
    rec = read(env, machine)
    if rec is None:
        return ""
    if rec.get("tree") != tree(root, machine):
        return "the tree changed since it last ran"
    if is_macos() and rec.get("boot") != boot(machine):
        gone = unloaded_agents(env, machine)
        if gone:
            return "launchd has not loaded %s since this boot" % ", ".join(gone)
    return ""


def main(argv):
    if argv == ["record"]:
        write(images.root(), os.environ, Local())
        return 0
    sys.stderr.write("usage: python3 -m wk.setupstate record\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
