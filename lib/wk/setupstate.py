"""Whether this machine's setup has converged on its tree: a whole `./setup` records the tree and the boot it ran in."""

import hashlib
import json
import os
import sys

from wk import images
from wk.machine import Local, is_macos
from wk.store import Store

RECORD = "setup-applied"
LAUNCH_AGENTS = ("com.wk.github-inject", "com.wk.broker")   # the agents ./setup installs (lib/wk/daemons.py)


def tree(root, machine):
    status = machine.run(["git", "-C", root, "status", "--porcelain=v1", "-z", "--untracked-files=all"]).out
    h = hashlib.sha256((machine.run(["git", "-C", root, "rev-parse", "HEAD"]).out + "\0" + status).encode())
    for entry in status.split("\0"):
        path = os.path.join(root, entry[3:])
        if entry[3:] and machine.exists(path) and not machine.isdir(path):
            try:
                h.update(machine.read_bytes(path))
            except OSError:
                pass
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


def write(env, machine, applied):
    machine.write_own(path(env), json.dumps({"tree": applied, "boot": boot(machine)}))


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
    if argv == ["tree"]:
        print(tree(images.root(), Local()))
        return 0
    if len(argv) == 2 and argv[0] == "record":
        write(os.environ, Local(), argv[1])
        return 0
    sys.stderr.write("usage: python3 -m wk.setupstate tree | record <tree>\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
