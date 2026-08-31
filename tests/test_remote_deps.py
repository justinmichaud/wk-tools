"""What a shared build machine needs, and the one root command that installs it
(remote/deps.sh, remote/probe.sh).

wk installs nothing on a build box -- provisioning never takes root
(remote/provision.sh) -- so the whole of the help it can give is naming the
exact command to run there or to hand to that machine's administrators. Three
places ask: `wk remote setup`, provisioning itself, and `wk doctor --all`. One
list answers all three, and these tests pin the list, the package names, the
per-distro command, and what the findings say about a machine.

The probe is exercised against a captured sample rather than a real machine:
what it says about *this* fleet is a fact about the fleet, not about the code.

Run: python3 -m unittest tests.test_remote_deps -v
"""
import unittest

from tests.support import REPO, WkTest, bash

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


def findings(probe):
    # The probe arrives as a heredoc, not as a quoted argument: it is many
    # lines, and a repr()'d one would reach the shell with literal backslash-n.
    cp = bash(f'''
set -euo pipefail
WK_ROOT={str(REPO)!r}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/remote/deps.sh"
probe=$(cat <<'PROBE_EOF'
{probe}
PROBE_EOF
)
wk_remote_findings "$probe"
''')
    assert cp.returncode == 0, cp.stdout + cp.stderr
    out = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append(tuple(parts))
    return out


class TestTheList(WkTest):
    def test_every_dep_is_tool_need_and_a_reason(self):
        cp = self.bash(f'. "{DEPS}"\nwk_remote_deps\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        rows = [l.split(None, 2) for l in cp.stdout.strip().splitlines()]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(len(row), 3, row)
            self.assertIn(row[1], ("required", "wanted"), row)
        names = [r[0] for r in rows]
        self.assertEqual(len(names), len(set(names)), f"a tool listed twice: {names}")
        # The four a CMake build cannot start without, and the two that only
        # make it slower or less pleasant.
        need = {r[0] for r in rows if r[1] == "required"}
        self.assertEqual(need, {"git", "cmake", "ninja", "clang", "python3"})
        self.assertEqual({r[0] for r in rows if r[1] == "wanted"}, {"ccache", "zsh"})

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
        cp = self.bash(f'. "{DEPS}"\n'
                       'printf "%s %s %s\\n" "$(wk_remote_package ninja debian)" '
                       '"$(wk_remote_package ccache debian)" "$(wk_remote_package ninja fedora)"\n')
        self.assertEqual(cp.stdout.split(), ["ninja-build", "ccache", "ninja-build"])

    def test_one_command_installs_the_whole_set(self):
        cp = self.bash(f'. "{DEPS}"\n'
                       'wk_remote_install_cmd debian ccache zsh; echo\n'
                       'wk_remote_install_cmd fedora ccache; echo\n'
                       'wk_remote_install_cmd arch ccache; echo\n'
                       'wk_remote_install_cmd unknown ccache || echo REFUSED\n')
        lines = cp.stdout.strip().splitlines()
        self.assertEqual(lines[0], "sudo apt-get update && sudo apt-get install -y ccache zsh")
        self.assertEqual(lines[1], "sudo dnf install -y ccache")
        self.assertEqual(lines[2], "sudo pacman -S --needed ccache")
        self.assertEqual(lines[3], "REFUSED",
                         "an unknown package manager got a command invented for it")

    def test_nothing_to_install_is_not_a_command(self):
        cp = self.bash(f'. "{DEPS}"\nwk_remote_install_cmd debian || echo REFUSED\n')
        self.assertIn("REFUSED", cp.stdout)


class TestTheFindings(WkTest):
    def test_a_complete_machine_reports_only_ok(self):
        states = {f[0] for f in findings(FULL)}
        self.assertEqual(states, {"ok"}, findings(FULL))

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
        tools = self.bash(f'. "{DEPS}"\nwk_remote_deps | awk "{{print \\$1}}"\n').stdout.split()
        for t in tools:
            self.assertIn(f"tool.{t}", keys, f"the probe said nothing about {t}")

    def test_it_sources_nothing(self):
        """A machine that has never been provisioned has no wk-tools to source."""
        text = PROBE.read_text()
        for bad in ("lib/common.sh", "$WK_ROOT", "wk_state_dir"):
            self.assertNotIn(bad, text, f"remote/probe.sh reaches for {bad}")
