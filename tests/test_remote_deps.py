"""What a shared build machine needs and the one root command that installs it (remote/deps.sh, remote/probe.sh,
lib/wk/machine_cmd/deps.py), with findings driven from captured probe samples."""
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

# A machine with everything, and one missing ccache with a junk git identity (both captured 2026-08-31).
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
place=fullbox
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
place=thinbox
"""

NO_GIT = FULL.replace("tool.git=/usr/bin/git", "tool.git=")


def _digest(value):
    return hashlib.sha256((value + "\n").encode()).hexdigest()[:16]


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
    def test_the_machine_reads_the_same_table(self):
        cp = self.bash(f'. "{DEPS}"\nwk_remote_deps\n')
        self.assertEqual([tuple(l.split(None, 2)) for l in cp.stdout.strip().splitlines()], machine_deps.deps(REPO))

    def test_a_derivative_resolves_to_its_parent_family(self):
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
        home = Path(tempfile.mkdtemp(prefix="wk-test-probe-home-"))
        self.addCleanup(bash, 'rm -rf "%s"' % home)
        (home / ".wk-agent-token").write_text("sk-the-value\n")
        cp = bash(f'cat "{DEPS}" "{PROBE}" | bash -s', env={"HOME": str(home)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        keys = {l.split("=", 1)[0] for l in cp.stdout.splitlines() if "=" in l}
        for want in ("host", "os", "family", "arch", "cores", "marker"):
            self.assertIn(want, keys, cp.stdout)
        for t in (row[0] for row in machine_deps.deps(REPO)):
            self.assertIn(f"tool.{t}", keys, f"the probe said nothing about {t}")
        self.assertIn("cred..wk-agent-token", keys, "a credential copy is reported by digest")
        self.assertNotIn("sk-the-value", cp.stdout)
