"""What a shared build machine needs, and the one root command that installs it
(remote/deps.sh's table, remote/probe.sh, lib/wk/machine_cmd/deps.py's Deps).

wk installs nothing on a build box -- provisioning never takes root
(remote/provision.sh) -- so the whole of the help it can give is naming the
exact command to run there or to hand to that machine's administrators. Three
places ask: `wk machine setup`, provisioning itself, and `wk doctor --all`. One
list answers all three, and these tests pin the list, the package names, the
per-distro command, and what the findings say about a machine.

The probe is exercised against a captured sample rather than a real machine:
what it says about *this* fleet is a fact about the fleet, not about the code.

Run: python3 -m unittest tests.test_remote_deps -v
"""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash

sys.path.insert(0, str(REPO / "lib"))
from wk.machine_cmd import deps as machine_deps  # noqa: E402

DEPS = REPO / "remote" / "deps.sh"
PROBE = REPO / "remote" / "probe.sh"

# A machine with everything (moose's shape), and one missing ccache with a
# junk git identity (buildbox4/devbox-arm64-2's, measured 2026-08-31).
FULL = """host=fullbox
os=Ubuntu 24.04
family=debian
arch=aarch64
cores=80
tool.git=/usr/bin/git
tool.cmake=/usr/bin/cmake
tool.ninja=/usr/bin/ninja
tool.clang=/usr/bin/clang
tool.python3=/usr/bin/python3
tool.ccache=/usr/bin/ccache
tool.zsh=/usr/bin/zsh
git.name=Justin Michaud
git.email=jmichaud@igalia.com
git.fsmonitor=true
git.manyfiles=true
marker=yes
root=/home/x/wk
target=fullbox
cred..wk-agent-token=__TOKEN__
cred..wk-litellm-key=__LITELLM__
"""

THIN = """host=thinbox
os=Debian GNU/Linux 13 (trixie)
family=debian
arch=x86_64
cores=128
tool.git=/usr/bin/git
tool.cmake=/usr/bin/cmake
tool.ninja=/usr/bin/ninja
tool.clang=/usr/bin/clang
tool.python3=/usr/bin/python3
tool.ccache=
tool.zsh=/usr/bin/zsh
env.CC=gcc-13
git.name=no
git.email=no
git.fsmonitor=
git.manyfiles=
marker=yes
root=/home/x/wk
target=thinbox
"""

NO_GIT = FULL.replace("tool.git=/usr/bin/git", "tool.git=")


def _digest(value):
    return hashlib.sha256((value + "\n").encode()).hexdigest()[:16]


# The store the findings compare a machine's credential copies with: a scratch one holding the two values FULL's digests are of.
TOKEN, LITELLM = "sk-ant-oat01-placeholder", "sk-litellm-placeholder"
FULL = FULL.replace("__TOKEN__", _digest(TOKEN)).replace("__LITELLM__", _digest(LITELLM))


def store_with(**values):
    tmp = Path(tempfile.mkdtemp(prefix="wk-test-deps-store-"))
    (tmp / "secrets").mkdir()
    for name, value in values.items():
        (tmp / "secrets" / name).write_text(value + "\n")
    return {"WK_STORE": str(tmp), "WK_HOST_SECRETS": str(tmp / "secrets")}


def findings(probe, env=None):
    env = env if env is not None else store_with(**{"claude-token": TOKEN, "litellm-key": LITELLM})
    return machine_deps.Deps(REPO, env=dict(os.environ, **env)).findings(probe)


class TestTheList(WkTest):
    def test_every_dep_is_tool_need_and_a_reason(self):
        rows = machine_deps.deps(REPO)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 3, row)
            self.assertIn(row[1], ("required", "wanted"), row)
        names = [r[0] for r in rows]
        self.assertEqual(len(names), len(set(names)), f"a tool listed twice: {names}")
        # The ones a CMake build cannot start without, and the two that only
        # make it slower or less pleasant.
        need = {r[0] for r in rows if r[1] == "required"}
        self.assertEqual(need, {"git", "cmake", "ninja", "clang", "python3"})
        self.assertEqual({r[0] for r in rows if r[1] == "wanted"}, {"ccache", "zsh"})

    def test_the_machine_reads_the_same_table(self):
        """remote/probe.sh, on the machine, asks deps.sh's own function for the list the Python parses."""
        cp = self.bash(f'. "{DEPS}"\nwk_remote_deps\n')
        self.assertEqual([tuple(l.split(None, 2)) for l in cp.stdout.strip().splitlines()], machine_deps.deps(REPO))

    def test_a_derivative_resolves_to_its_parent_family(self):
        """ID first, then ID_LIKE -- so Mint, Raspberry Pi OS and Rocky resolve
        to the parent they declare without being named in the list."""
        cases = [
            ("debian", "", "debian"),
            ("ubuntu", "", "debian"),
            ("raspbian", "debian", "debian"),
            ("linuxmint", "ubuntu debian", "debian"),
            ("fedora", "", "fedora"),
            ("rocky", "rhel centos fedora", "fedora"),
            ("arch", "", "arch"),
            ("plan9", "", "unknown"),
        ]
        for idv, like, want in cases:
            cp = self.bash(f'. "{DEPS}"\nwk_remote_family {idv!r} {like!r}\n')
            with self.subTest(id=idv, id_like=like):
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(cp.stdout.strip(), want)

    def test_the_package_name_is_the_tool_unless_it_differs(self):
        self.assertEqual([machine_deps.package("ninja", "debian"), machine_deps.package("ccache", "debian"),
                          machine_deps.package("ninja", "fedora")], ["ninja-build", "ccache", "ninja-build"])

    def test_one_command_installs_the_whole_set(self):
        self.assertEqual(machine_deps.install_cmd("debian", ["ccache", "zsh"]),
                         "sudo apt-get update && sudo apt-get install -y ccache zsh")
        self.assertEqual(machine_deps.install_cmd("fedora", ["ccache"]), "sudo dnf install -y ccache")
        self.assertEqual(machine_deps.install_cmd("arch", ["ccache"]), "sudo pacman -S --needed ccache")
        self.assertIsNone(machine_deps.install_cmd("unknown", ["ccache"]),
                          "an unknown package manager got a command invented for it")

    def test_nothing_to_install_is_not_a_command(self):
        self.assertIsNone(machine_deps.install_cmd("debian", []))


class TestTheFindings(WkTest):
    def test_a_complete_machine_reports_only_ok(self):
        states = {f[0] for f in findings(FULL)}
        self.assertEqual(states, {"ok"}, findings(FULL))

    def _cred(self, probe, name, env=None):
        rows = [f for f in findings(probe, env) if f[1].startswith(name + " credential")]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def test_a_copy_that_is_this_machines_credential_is_ok(self):
        self.assertEqual("ok", self._cred(FULL, "claude")[0])
        self.assertEqual("ok", self._cred(FULL, "litellm")[0])

    def test_a_copy_rotated_out_from_under_is_wanted_with_the_setup_that_rewrites_it(self):
        state, what, remedy = self._cred(FULL, "claude", store_with(**{"claude-token": "sk-ant-oat01-rotated", "litellm-key": LITELLM}))
        self.assertEqual("wanted", state)
        self.assertIn("rotated since", what)
        self.assertIn("wk machine setup", remedy)

    def test_a_machine_without_the_copy_is_wanted(self):
        state, what, remedy = self._cred(FULL.replace("cred..wk-agent-token=", "cred..wk-other="), "claude")
        self.assertEqual("wanted", state)
        self.assertIn("not on the machine", what)
        self.assertIn("wk machine setup", remedy)

    def test_a_copy_of_a_credential_no_longer_stored_here_is_wanted_gone(self):
        state, what, _r = self._cred(FULL, "claude", store_with(**{"litellm-key": LITELLM}))
        self.assertEqual("wanted", state)
        self.assertIn("no longer stored here", what)

    def test_no_credential_on_either_side_is_a_note(self):
        state, what, remedy = self._cred(THIN, "claude", store_with())
        self.assertEqual("note", state)
        self.assertIn("wk key set claude", remedy)

    def test_a_machine_that_could_not_digest_its_copy_is_a_note(self):
        state, what, _r = self._cred(FULL.replace(_digest(TOKEN), "?"), "claude")
        self.assertEqual("note", state)
        self.assertIn("sha256sum", what)

    def test_the_probe_reports_every_copy_by_digest_and_never_by_value(self):
        text = PROBE.read_text()
        self.assertIn('for _f in "$HOME"/.wk-*', text)
        self.assertIn("sha256sum", text)
        self.assertNotIn("cat \"$_f\"", text)

    def test_a_missing_wanted_tool_is_reported_with_one_root_command(self):
        f = findings(THIN)
        self.assertIn("wanted", [x[0] for x in f])
        ccache = [x for x in f if "ccache" in x[1]]
        self.assertTrue(ccache, f)
        self.assertEqual(ccache[0][0], "wanted")
        notes = [x for x in f if x[0] == "note" and "apt-get" in x[2]]
        self.assertEqual(len(notes), 1,
                         "the root command is not stated exactly once: " + repr(f))
        self.assertIn("install -y ccache", notes[0][2])
        self.assertIn("thinbox", notes[0][1], "the command does not say which machine")

    def test_a_missing_required_tool_is_a_different_state(self):
        """`required` is what stops provisioning; `wanted` never does."""
        f = findings(NO_GIT)
        self.assertIn(("required"), [x[0] for x in f])
        self.assertTrue(any("git --" in x[1] for x in f), f)

    def test_a_wrong_git_identity_is_named_with_both_values(self):
        f = findings(THIN)
        ident = [x for x in f if "user.name" in x[1]]
        self.assertTrue(ident, f)
        self.assertEqual(ident[0][0], "wanted")
        self.assertIn("'no'", ident[0][1])
        self.assertIn("Justin Michaud", ident[0][1])

    def test_git_speed_settings_are_a_finding_of_their_own(self):
        self.assertTrue(any("big checkout" in x[1] for x in findings(THIN)))
        self.assertFalse(any("big checkout" in x[1] for x in findings(FULL)))

    def test_a_build_variable_the_machine_presets_is_said_out_loud(self):
        """wk's build sets its own CC and ignores the machine's, which is a
        surprise worth printing rather than a silence."""
        f = findings(THIN)
        cc = [x for x in f if x[1].startswith("CC is set")]
        self.assertTrue(cc, f)
        self.assertEqual(cc[0][0], "note")
        self.assertIn("gcc-13", cc[0][1])
        self.assertNotIn("CC is set", " ".join(x[1] for x in findings(FULL)))

    def test_an_unknown_distro_names_the_packages_instead_of_a_command(self):
        f = findings(THIN.replace("family=debian", "family=unknown"))
        notes = [x for x in f if x[0] == "note" and "by hand" in x[2]]
        self.assertTrue(notes, f)
        self.assertIn("ccache", notes[0][2])


class TestTheProbeItself(WkTest):
    def test_it_runs_against_this_machine_and_answers_every_key(self):
        """The probe is self-contained: deps.sh then probe.sh, into a bare
        shell, with no wk-tools on the far side. Run here, where 'the far side'
        is this machine -- what it *says* about a build box is a fact about
        that box, not about this code."""
        cp = bash(f'cat "{DEPS}" "{PROBE}" | bash -s')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        keys = {l.split("=", 1)[0] for l in cp.stdout.splitlines() if "=" in l}
        for want in ("host", "os", "family", "arch", "cores", "marker"):
            self.assertIn(want, keys, cp.stdout)
        # One line per declared tool, present or not, so a reader never has to
        # know the list to notice one missing.
        for t in (row[0] for row in machine_deps.deps(REPO)):
            self.assertIn(f"tool.{t}", keys, f"the probe said nothing about {t}")

    def test_it_sources_nothing(self):
        """A machine that has never been provisioned has no wk-tools to source."""
        text = PROBE.read_text()
        for bad in ("lib/common.sh", "$WK_ROOT", "wk_state_dir"):
            self.assertNotIn(bad, text, f"remote/probe.sh reaches for {bad}")
