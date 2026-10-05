"""Where a machine keeps things."""

import argparse
import os
import shlex
import sys

from wk import project, record

BROKER_SOCKET = "/run/wk/broker.sock"
GUEST_BROKER_SOCKET = ".wk-broker.sock"


def _env(env):
    return os.environ if env is None else env


def remote_marker_path(env):
    return env.get("WK_REMOTE_MARKER") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".wk-remote")


def in_vm(env=None):
    return bool(_env(env).get("WK_IN_VM"))


def ws_name(env=None, take=False):  # `take` removes the answer too
    env = _env(env)
    return env.pop("WK_NAME", "") if take else env.get("WK_NAME", "")


def no_such_workspace(name):
    return "no such workspace: %s -- 'wk ls' lists them" % name


def build_preset(env=None, take=False):  # the preset lifted out of argv, or None
    env = _env(env)
    return env.pop("WK_PRESET", None) if take else env.get("WK_PRESET")


def dispatch_place(env=None, default=None):
    return _env(env).get("WK_PLACE", default)


class Store:
    def __init__(self, env=None):
        self.env = _env(env)

    @property
    def macos_host(self):
        return os.uname().sysname == "Darwin" and not in_vm(self.env)

    def home(self):
        return self.env.get("HOME") or os.path.expanduser("~")

    def podman_machine(self):
        """The podman machine a macOS host's container workspaces run in."""
        return self.env.get("WK_MACHINE") or "wk"

    def state_dir(self):
        return os.path.join(self.env.get("XDG_STATE_HOME") or os.path.join(self.home(), ".local", "state"), "wk")

    def lock_dir(self):
        return self.env.get("WK_LOCK_DIR") or os.path.join(self.state_dir(), "locks")

    def lock_path(self, resource):
        return os.path.join(self.lock_dir(), "%s@%s.lock" % (resource, record.host_name() or "local"))

    def default_store_dir(self):
        if in_vm(self.env) or os.uname().sysname == "Darwin":
            return "/var/lib/wk"
        if os.path.isdir("/var/lib/wk") and os.access("/var/lib/wk", os.W_OK):
            return "/var/lib/wk"
        return os.path.join(self.env.get("XDG_DATA_HOME") or os.path.join(self.home(), ".local", "share"), "wk")

    def named_runtime_socket(self):
        return self.env.get("WK_BROKER_SOCKET")

    def runtime_socket(self):
        if self.env.get("XDG_RUNTIME_DIR"):
            default = os.path.join(self.env["XDG_RUNTIME_DIR"], "wk", "broker.sock")
        else:
            default = os.path.join(self.state_dir(), "broker.sock")
        return self.named_runtime_socket() or default

    def workspace_runtime_socket(self):
        return self.named_runtime_socket() or (os.path.join(self.home(), GUEST_BROKER_SOCKET) if self.macos_host else BROKER_SOCKET)

    def container_mirror_dir(self):
        return self.env.get("WK_MIRROR")

    def named_store_dir(self):
        return self.env.get("WK_STORE")

    def admission_dir(self):
        return self.named_store_dir() or self.home()

    def store_dir(self):
        return self.named_store_dir() or self.default_store_dir()

    def provisioned_store_dir(self):
        return self.named_store_dir() or "/var/lib/wk"

    def vm_store_dir(self):
        return self.env.get("WK_VM_STORE") or self.records_dir()

    def vm_store_apart(self):
        """Whether the vm place has a store of its own: only on a macOS host, and never the container's."""
        return self.macos_host and os.path.realpath(self.vm_store_dir()) != os.path.realpath(self.store_dir())

    def machine_store_dir(self):
        return self.env.get("WK_STORE_DEFAULT") or self.store_dir()

    def records_dir(self):
        if self.macos_host and self.store_dir() == self.default_store_dir():
            return self.state_dir()
        return self.store_dir()

    def mirror_parent(self):
        return os.path.join(self.state_dir() if self.macos_host else self.store_dir(), "git")

    def mirror_dir(self):
        return os.path.join(self.mirror_parent(), project.get("MIRROR"))

    def snapshots_dir(self):
        return os.path.join(self.store_dir(), "base")

    def snapshot_dir(self, bid):
        return os.path.join(self.snapshots_dir(), bid)

    def snapshot_tree(self, bid):
        return os.path.join(self.snapshot_dir(bid), project.get("CHECKOUT"))

    def snapshot_sha_file(self, bid):
        return os.path.join(self.snapshot_dir(bid), "sha")

    def ws_dir(self, name):
        return os.path.join(self.store_dir(), "ws", name)

    def ws_snapshot_id(self, name):
        try:
            with open(os.path.join(self.ws_dir(name), "base-id")) as f:
                return f.read().strip()
        except OSError:
            return None

    def workspaces(self):
        try:
            return sorted(os.listdir(os.path.join(self.store_dir(), "ws")))
        except OSError:
            return []

    def keyring_dir(self):
        if self.macos_host:
            return self.env.get("WK_HOST_SECRETS") or os.path.join(
                self.env.get("XDG_CONFIG_HOME") or os.path.join(self.home(), ".config"), "wk", "secrets")
        return os.path.join(self.machine_store_dir(), "secrets")

    def keyring_view_dir(self, kind):
        return os.path.join(self.keyring_dir(), "view", kind)

    def keyring_agent_rw_dir(self):
        return os.path.join(os.path.dirname(self.keyring_dir()), "agent-rw")

    def keyring_push_dir(self):
        return os.path.join(os.path.dirname(self.keyring_dir()), "push-keys")

    def keyring_ntfy_topic(self):
        return os.path.join(os.path.dirname(self.keyring_dir()), "notify", "ntfy-topic")

    def is_local(self):
        """Whether this process can write the store; on a macOS host the default one is the podman VM's."""
        if in_vm(self.env) or os.uname().sysname != "Darwin":
            return True
        return os.path.isdir(self.store_dir()) and os.access(self.store_dir(), os.W_OK)

    def cache_dir(self):
        return os.path.join(self.records_dir(), "cache")


class Snapshots:
    def __init__(self, store, machine):
        self.store = store
        self.machine = machine

    def _read(self, path):
        try:
            return self.machine.read(path)
        except OSError:
            return None

    def _ls(self, d):
        try:
            return sorted(self.machine.listdir(d))
        except OSError:
            return []

    def _git(self, bid, *args):
        r = self.machine.run(["git", "-C", self.store.snapshot_tree(bid)] + list(args))
        return r.out.strip() if r.ok else ""

    def ids(self):
        return self._ls(self.store.snapshots_dir())

    # `wk sync` publishes into a hardlinked copy of the last snapshot, so a kill mid-publish leaves a newer directory than any good one; the sha lands last.
    def complete(self, bid):
        return bool(self._read(self.store.snapshot_sha_file(bid)))

    def recorded_branch(self, bid):
        return (self._read(os.path.join(self.store.snapshot_dir(bid), "branch")) or "").strip()

    def verify(self, bid):
        if not self.machine.isdir(self.store.snapshot_dir(bid)):
            return "snapshot %s does not exist" % bid
        if not self.complete(bid):
            return ("snapshot %s was never finished publishing (no completion marker).\n"
                    "    An interrupted 'wk sync' leaves one; the next 'wk gc' removes it." % bid)
        if not self.machine.isdir(os.path.join(self.store.snapshot_tree(bid), ".git")):
            return "snapshot %s is not a git checkout" % bid
        want = (self._read(self.store.snapshot_sha_file(bid)) or "").strip()
        got = self._git(bid, "rev-parse", "HEAD")
        if not got:
            return "snapshot %s has no readable HEAD" % bid
        if want != got:
            return ("snapshot %s no longer matches what was published:\n    recorded %s\n    tree     %s\n"
                    "    Something fetched or checked out inside a snapshot. Snapshots are\n"
                    "    immutable by design -- publish a new one with 'wk sync'." % (bid, want, got))
        return self.head_verify(bid)

    # Every workspace overlays this tree and inherits its HEAD, so a snapshot left on a raw sha is a detached `git status` in every workspace made from it.
    def head_verify(self, bid):
        branch = self.recorded_branch(bid)
        if not branch:
            return ("snapshot %s does not record the branch it was published from,\n"
                    "    so whether its HEAD is that branch cannot be known:  wk sync    publishes\n"
                    "    one that does, for the next 'wk new'." % bid)
        head = self._git(bid, "symbolic-ref", "--quiet", "HEAD")
        up = self._git(bid, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        local = branch.split("/", 1)[-1]
        if head == "refs/heads/" + local and up == branch:
            return ""
        return ("snapshot %s is not on branch %s tracking %s (HEAD is\n    %s%s), so every workspace overlaid on it starts\n"
                "    detached:  wk sync    publishes one that is." % (bid, local, branch, head or "a raw sha",
                                                                      ", tracking " + up if up else ""))

    def current(self):
        return next((b for b in reversed(self.ids()) if not self.verify(b)), "")

    # What `wk gc` keeps: weaker than `current` on purpose, since a snapshot published before the branch record is still the only one a machine may have.
    def newest_complete(self):
        return next((b for b in reversed(self.ids()) if self.complete(b)), "")

    def workspaces(self):
        return self._ls(os.path.join(self.store.store_dir(), "ws"))

    def pin(self, ws):
        text = self._read(os.path.join(self.store.ws_dir(ws), "base-id"))
        return None if text is None else text.strip()

    def unpinned(self):
        return [w for w in self.workspaces() if self.pin(w) is None]

    def unreferenced(self):
        """The snapshots `wk gc` may take; none while a workspace's pin is unknown."""
        if self.unpinned():
            return []
        used = {self.pin(w) for w in self.workspaces()}
        keep = self.newest_complete()
        return [b for b in self.ids() if b != keep and b not in used]


def rubble(store, machine, mirror_here):
    """A snapshot no workspace is on goes with a plain `wk gc`; the mirror and every snapshot only with --purge-mirror, and never under a workspace."""
    from wk.rubble import du_kb, remover, row
    snaps = Snapshots(store, machine)
    rows, ws = [], snaps.workspaces()
    unpinned = snaps.unpinned()
    if unpinned and [b for b in snaps.ids() if b != snaps.newest_complete()]:
        rows.append(row("snapshot", "snapshots", du_kb(machine, store.snapshots_dir()), take=remover(machine),
                        why="kept -- %s never recorded a snapshot, so any snapshot may be under it: 'wk new <name>' remakes "
                            "one, 'wk rm <name>' removes it" % " ".join(unpinned)))
    for b in snaps.unreferenced():
        d = store.snapshot_dir(b)
        rows.append(row("snapshot", "snapshot %s, no workspace on it" % b, du_kb(machine, d), take=remover(machine, d)))
    paths = [p for p in [store.snapshots_dir()] + ([store.mirror_dir()] if mirror_here else []) if machine.isdir(p)]
    if paths:
        kbs = [du_kb(machine, p) for p in paths]
        rows.append(row("mirror", "the mirror and every snapshot" if store.mirror_dir() in paths else "every snapshot",
                        None if None in kbs else sum(kbs), "--purge-mirror", remover(machine, *paths),
                        "kept -- every workspace is overlaid on a snapshot here: 'wk rm' %s first" % " ".join(ws) if ws else ""))
    from wk.bench import record as bench_record
    stray = bench_record.outside(store)
    for t in bench_record.tasks(stray, machine):
        d = os.path.join(stray, t)
        dest = os.path.join(store.ws_dir(bench_record.workspace_of(d, machine) or "<workspace>"), "bench")
        rows.append(row("bench", "%s: a task outside any workspace" % d, du_kb(machine, d),
                        why="kept -- a measurement is never rubble; once moved into its workspace every command reads it:"
                            "  mkdir -p %s && mv %s %s/" % (dest, d, dest)))
    return rows


def main(argv):
    p = argparse.ArgumentParser(prog="python3 -m wk.store")
    p.add_argument("verb", choices=("paths",), help="the store's directories, as shell assignments")
    p.parse_args(argv)
    s = Store()
    for k, v in (("WK_STORE", s.store_dir()), ("keyring_dir", s.keyring_dir()), ("keyring_agent_rw_dir", s.keyring_agent_rw_dir()),
                 ("keyring_push_dir", s.keyring_push_dir()), ("mirror_parent", s.mirror_parent())):
        print("%s=%s" % (k, shlex.quote(v)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
