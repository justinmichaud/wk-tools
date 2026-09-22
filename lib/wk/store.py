"""Where a machine keeps things: the store and the paths under it, the same
rules lib/store.sh spells, read from the same variables."""

import os
import re
import subprocess


def lock_holder_pid(path):
    """The pid in a lock's payload (a symlink target or a payload file), or None."""
    try:
        line = os.readlink(path)
    except OSError:
        try:
            with open(os.path.join(path, "payload")) as f:
                line = f.read()
        except OSError:
            return None
    m = re.search(r"pid=(\d+)", line)
    return int(m.group(1)) if m else None


class Store:
    def __init__(self, env=None):
        self.env = os.environ if env is None else env

    @property
    def macos_host(self):
        return os.uname().sysname == "Darwin" and not self.env.get("WK_IN_VM")

    def home(self):
        return self.env.get("HOME") or os.path.expanduser("~")

    def state_dir(self):
        return os.path.join(self.env.get("XDG_STATE_HOME") or os.path.join(self.home(), ".local", "state"), "wk")

    def lock_dir(self):
        return self.env.get("WK_LOCK_DIR") or os.path.join(self.state_dir(), "locks")

    def lock_path(self, resource):
        cp = subprocess.run(["hostname", "-s"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        host = cp.stdout.strip() or "local"
        return os.path.join(self.lock_dir(), "%s@%s.lock" % (resource, host))

    def default(self):
        """The store a machine uses when none is named."""
        if self.env.get("WK_IN_VM") or os.uname().sysname == "Darwin":
            return "/var/lib/wk"
        if os.path.isdir("/var/lib/wk") and os.access("/var/lib/wk", os.W_OK):
            return "/var/lib/wk"
        return os.path.join(self.env.get("XDG_DATA_HOME") or os.path.join(self.home(), ".local", "share"), "wk")

    def root(self):
        return self.env.get("WK_STORE") or self.default()

    def machine_store(self):
        return self.env.get("WK_STORE_DEFAULT") or self.root()

    def record_dir(self):
        if self.macos_host and self.root() == self.default():
            return self.state_dir()
        return self.root()

    def mirror(self):
        if self.macos_host:
            return os.path.join(self.state_dir(), "git", "WebKit.git")
        return os.path.join(self.root(), "git", "WebKit.git")

    def base_dir(self):
        return os.path.join(self.root(), "base")

    def base_path(self, base_id):
        return os.path.join(self.base_dir(), base_id, "WebKit")

    def base_sha_file(self, base_id):
        return os.path.join(self.base_dir(), base_id, "sha")

    def ws_dir(self, name):
        return os.path.join(self.root(), "ws", name)

    def ws_base_id(self, name):
        try:
            with open(os.path.join(self.ws_dir(name), "base-id")) as f:
                return f.read().strip()
        except OSError:
            return None

    def workspaces(self):
        try:
            return sorted(os.listdir(os.path.join(self.root(), "ws")))
        except OSError:
            return []

    def secrets_dir(self):
        if self.macos_host:
            return self.env.get("WK_HOST_SECRETS") or os.path.join(
                self.env.get("XDG_CONFIG_HOME") or os.path.join(self.home(), ".config"), "wk", "secrets")
        return os.path.join(self.machine_store(), "secrets")

    def secrets_view_dir(self, kind):
        return os.path.join(self.secrets_dir(), "view", kind)

    def agent_rw_dir(self):
        return os.path.join(os.path.dirname(self.secrets_dir()), "agent-rw")

    def bench_dir(self):
        return os.path.join(self.record_dir(), "bench")

    def artifact_dir(self):
        return os.path.join(self.record_dir(), "cache")
