"""A reporting command starts, boots or repairs nothing, here or on the far machine, and writes nothing locally."""
import os
import re
import shlex
import shutil
import tempfile
import unittest

from tests.support import BLIND_FLEET, REPO, run

REPORTS = (("status",), ("ls",), ("status", "nosuch", "--log"), ("doctor",))
PLACES = {"store": {"WK_IN_VM": "1"}, "workstation": {}}
TOOLS = ("ssh", "scp", "rsync", "podman", "tart", "tailscale", "sudo", "systemctl", "launchctl", "gh", "nmap")
# A tool followed by one of these words acts; the power verbs and the copies act whatever follows.
ACTS = {"podman": {"start", "run", "create", "rm", "rmi", "stop", "kill", "restart", "pull", "build", "init", "exec", "set"},
        "tart": {"run", "clone", "set", "stop", "delete", "pull", "push", "create", "import", "prune"},
        "tailscale": {"up", "down", "set", "login", "logout", "switch", "cert"},
        "systemctl": {"start", "stop", "restart", "enable", "disable", "kill", "reboot", "poweroff", "daemon-reload"},
        "launchctl": {"load", "unload", "bootstrap", "bootout", "kickstart", "start", "stop", "enable", "disable"}}
ALWAYS = {"reboot", "shutdown", "poweroff", "scp", "rsync"}
HELPERS = {"wk-boot-priv", "wk-card-priv", "wk-quiesce-priv"}   # the privileged helpers: `status` is their one read


def readonly_verbs():
    out = set()
    for p in (REPO / "cmd").iterdir():
        m = re.search(r"^# wk:.*\breadonly\b(?!=)", p.read_text(errors="replace"), re.M) if p.is_file() else None
        if m:
            out.add(p.name)
    return out


def acts_in(tokens, readonly):
    """The first call in `tokens` that acts, or "": a tool followed within two words by an acting verb, a wk that is
    not asked a readonly verb, or a privileged helper asked anything but its status."""
    for i, tok in enumerate(tokens):
        tool, rest = os.path.basename(tok), tokens[i + 1:]
        verbs = [a for a in rest if not a.startswith("-")][:2]
        if tool in ALWAYS or (ACTS.get(tool, set()) & set(verbs)):
            return " ".join(tokens[i:i + 4])
        if tok.endswith("/wk") and (verbs[:1] or [""])[0] not in readonly:
            return " ".join(tokens[i:i + 3])
        if tool in HELPERS and verbs[:1] != ["status"]:
            return " ".join(tokens[i:i + 3])
    return ""


def acts(argv, readonly):
    """What in one recorded call acts, or "": an ssh's far command line is read word by word, whatever quoting it is in."""
    if argv[0] == "ssh":
        return "" if "-G" in argv else acts_in(re.findall(r"[A-Za-z0-9_./:@=+-]+", " ".join(argv[1:])), readonly)
    return acts_in(argv, readonly)


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class TestTheClassifier(unittest.TestCase):
    def test_it_names_a_start_a_boot_and_a_far_mutation_and_passes_a_read(self):
        ro = {"status", "ls"}
        for argv in (["podman", "machine", "start", "wk"], ["tart", "run", "wk-a"], ["sudo", "reboot"], ["sudo", "-n", "/usr/local/sbin/wk-boot-priv", "reboot"],
                     ["ssh", "-o", "BatchMode=yes", "box", "cd /x && ./wk build a"], ["ssh", "box", "PATH=/x; tart clone a b"],
                     ["ssh", "box", "sh -c 'systemctl restart wk-proxy'"], ["tailscale", "up"]):
            self.assertTrue(acts(argv, ro), argv)
        for argv in (["podman", "machine", "inspect", "wk"], ["tart", "list", "--format", "json"], ["ssh", "-G", "box"],
                     ["ssh", "box", "cd /x && ./wk status --continued"], ["sudo", "-n", "-l"], ["tailscale", "status", "--json"],
                     ["ssh", "box", "sudo -n /usr/local/sbin/wk-bridge-healthcheck 2>&1 || true"]):
            self.assertFalse(acts(argv, ro), argv)


class TestReportsAreReadOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-readonly-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls")
        shims = os.path.join(self.tmp, "bin")
        os.makedirs(shims)
        for tool in TOOLS:
            p = os.path.join(shims, tool)
            with open(p, "w") as f:
                f.write('#!/bin/sh\n{ printf "%%s\\0" %s "$@"; printf "\\n"; } >> %s\nexit 97\n' % (tool, shlex.quote(self.log)))
            os.chmod(p, 0o755)
        fleet = os.path.join(self.tmp, "fleet")
        os.makedirs(fleet)
        for conf in os.listdir(BLIND_FLEET):
            os.symlink(os.path.join(BLIND_FLEET, conf), os.path.join(fleet, conf))
        with open(os.path.join(fleet, "farbox.conf"), "w") as f:
            f.write("kind=build\ndriver=remote\nhost=farbox\n")
        self.watched = {k: os.path.join(self.tmp, k) for k in ("home", "state", "store", "secrets")}
        for d in self.watched.values():
            os.makedirs(d)
        self.env = {"PATH": shims + os.pathsep + os.environ["PATH"], "WK_MACHINES_DIR": fleet, "HOME": self.watched["home"],
                    "XDG_STATE_HOME": self.watched["state"], "WK_STORE": self.watched["store"],
                    "WK_HOST_SECRETS": self.watched["secrets"]}
        self.readonly = readonly_verbs()

    def calls(self):
        try:
            with open(self.log) as f:
                return [line.rstrip("\0").split("\0") for line in f.read().split("\0\n") if line]
        except OSError:
            return []

    def test_every_report_is_declared_readonly(self):
        self.assertLessEqual({r[0] for r in REPORTS}, self.readonly)

    def test_a_report_starts_boots_and_repairs_nothing_here_or_on_the_far_machine(self):
        for place, extra in PLACES.items():
            for argv in REPORTS:
                with self.subTest(place=place, cmd=argv[0]):
                    open(self.log, "w").close()
                    before = {k: tree(d) for k, d in self.watched.items()}
                    run(*argv, env=dict(self.env, **extra), timeout=60)
                    after = {k: tree(d) for k, d in self.watched.items()}
                    self.assertEqual([a for a in map(lambda c: acts(c, self.readonly), self.calls()) if a], [])
                    self.assertEqual(before, after, "wk %s wrote under the scratch %s" % (argv[0], place))


if __name__ == "__main__":
    unittest.main()
