"""host/macos/vmtools.sh's `_verify_mounts`, lifted and run against a fake `_rsh`: the mounts the podman machine
actually has, which is not what its config asked for (tests/test_machine_mounts.py)."""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash

VMTOOLS = REPO / "host" / "macos" / "vmtools.sh"

# Answers each question _verify_mounts asks from the WANT_* flags of the case under test.
FAKE_RSH = '''
_rsh() {
    case "$*" in
        *"test -x /opt/wk-tools/wk"*)   [ "$WANT_TOOLS" = 1 ] ;;
        *"-O ro"*"/opt/wk-tools"*)      [ "$WANT_TOOLS_RO" = 1 ] && echo /var/opt/wk-tools ;;
        *"-O ro"*"/secrets"*)           [ "$WANT_SECRETS_RO" = 1 ] && echo /var/lib/wk/secrets ;;
        *"-O ro"*"/git"*)               [ "$WANT_GIT_RO" = 1 ] && echo /var/lib/wk/git ;;
        *"findmnt"*"/secrets"*)         [ "$WANT_SECRETS" = 1 ] && echo /var/lib/wk/secrets ;;
        *"findmnt"*"/git"*)             [ "$WANT_GIT" = 1 ] && echo /var/lib/wk/git ;;
        *"findmnt"*"/agent-rw"*)        [ "$WANT_RW_MOUNT" = 1 ] && echo /var/lib/wk/agent-rw ;;
        *"test -w"*"/agent-rw"*)        [ "$WANT_RW_WRITABLE" = 1 ] ;;
        *) return 1 ;;
    esac
}
'''


class TestVerifyMounts(WkTest):
    def _run(self, tools=1, secrets=1, rw_mount=1, rw_writable=1,
             tools_ro=1, secrets_ro=1, git=1, git_ro=1):
        lifted = subprocess.run(
            ["sed", "-n", "/^_verify_mounts()/,/^}/p", str(VMTOOLS)],
            capture_output=True, text=True).stdout
        self.assertTrue(lifted.strip(), "_verify_mounts not found in host/macos/vmtools.sh")
        return bash(f'''
set -uo pipefail
. "$WK_ROOT/lib/common.sh"
eval "$(wk_py wk.store paths)"
WK_MACHINE=wk
WANT_TOOLS={tools} WANT_SECRETS={secrets}
WANT_RW_MOUNT={rw_mount} WANT_RW_WRITABLE={rw_writable}
WANT_TOOLS_RO={tools_ro} WANT_SECRETS_RO={secrets_ro}
WANT_GIT={git} WANT_GIT_RO={git_ro}
{FAKE_RSH}
{lifted}
_verify_mounts && echo VERIFIED
''', env={"WK_STORE": "/var/lib/wk",
          "WK_HOST_SECRETS": str(self.tmp / "secrets"),
          "WK_DEBUG": "1"})

    def test_all_four_there_and_the_writable_one_writable(self):
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("VERIFIED", cp.stdout)

    def test_a_mirror_directory_that_is_not_a_mount_is_refused(self):
        cp = self._run(git=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("/var/lib/wk/git is not a mount", out)
        self.assertIn("reaches no snapshot", out)

    def test_no_tooling_mount_names_the_stage_that_makes_one(self):
        cp = self._run(tools=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("nothing in", out)
        self.assertIn("./setup --stage machine", out)
        self.assertNotIn("VERIFIED", cp.stdout)

    def test_a_secrets_directory_that_is_not_a_mount_is_refused(self):
        cp = self._run(secrets=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("/var/lib/wk/secrets is not a mount", out)
        self.assertIn("reach no workspace", out)

    def test_no_agent_rw_mount_at_all_is_refused(self):
        cp = self._run(rw_mount=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("/var/lib/wk/agent-rw is not a mount", out)
        self.assertIn("claude.ai", out)

    def test_an_agent_rw_mount_that_is_read_only_is_refused_for_its_own_reason(self):
        cp = self._run(rw_writable=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("mounted read-only", out)
        self.assertIn("logged", out)
        self.assertNotIn("is not a mount", out)

    def test_a_read_only_mount_that_is_writable_is_refused(self):
        """podman 5.4 + applehv mounts a `:ro` volume read-write (measured)."""
        for case in ("tools_ro", "secrets_ro", "git_ro"):
            with self.subTest(case=case):
                cp = self._run(**{case: 0})
                out = cp.stdout + cp.stderr
                self.assertNotEqual(cp.returncode, 0, out)
                self.assertIn("is writable inside", out)
                self.assertIn("host/macos/playbook.yaml", out)
                self.assertNotIn("VERIFIED", cp.stdout)

if __name__ == "__main__":
    unittest.main()
