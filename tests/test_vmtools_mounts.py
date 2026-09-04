"""host/macos/vmtools.sh's `_verify_mounts`: the podman machine's three mounts,
asked of the machine itself rather than of the config file that created it.

host/macos/machine.sh holds the machine to the mounts it asked podman for
(tests/test_machine_mounts.py); that is a different question from whether the
machine actually has them, and only this one can answer it -- a podman that
takes an option and drops it, or a directory of the VM's own at the same path,
both pass the first check and fail this one. Every step of the vmtools stage
names a path in one of the three, so the stage fails here, once, with the
remedy, rather than one step at a time with three different messages.

`_verify_mounts` is lifted and run against a fake `_rsh` that answers the
questions it asks, so each verdict is driven with no machine.

Run: python3 -m unittest tests.test_vmtools_mounts -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash

VMTOOLS = REPO / "host" / "macos" / "vmtools.sh"

# What _verify_mounts asks the machine, answered from the case under test:
# whether the tooling is executable, whether each store directory is a mount,
# whether the writable one is writable, and whether the other two are actually
# read-only -- `-O ro` being the question findmnt is asked for that last one.
FAKE_RSH = '''
_rsh() {
    case "$*" in
        *"test -x /opt/wk-tools/wk"*)   [ "$WANT_TOOLS" = 1 ] ;;
        *"-O ro"*"/opt/wk-tools"*)      [ "$WANT_TOOLS_RO" = 1 ] && echo /var/opt/wk-tools ;;
        *"-O ro"*"/secrets"*)           [ "$WANT_SECRETS_RO" = 1 ] && echo /var/lib/wk/secrets ;;
        *"findmnt"*"/secrets"*)         [ "$WANT_SECRETS" = 1 ] && echo /var/lib/wk/secrets ;;
        *"findmnt"*"/agent-rw"*)        [ "$WANT_RW_MOUNT" = 1 ] && echo /var/lib/wk/agent-rw ;;
        *"test -w"*"/agent-rw"*)        [ "$WANT_RW_WRITABLE" = 1 ] ;;
        *) return 1 ;;
    esac
}
'''


class TestVerifyMounts(WkTest):
    def _run(self, tools=1, secrets=1, rw_mount=1, rw_writable=1,
             tools_ro=1, secrets_ro=1):
        lifted = subprocess.run(
            ["sed", "-n", "/^_verify_mounts()/,/^}/p", str(VMTOOLS)],
            capture_output=True, text=True).stdout
        self.assertTrue(lifted.strip(), "_verify_mounts not found in host/macos/vmtools.sh")
        return bash(f'''
set -uo pipefail
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
WK_MACHINE=wk
WANT_TOOLS={tools} WANT_SECRETS={secrets}
WANT_RW_MOUNT={rw_mount} WANT_RW_WRITABLE={rw_writable}
WANT_TOOLS_RO={tools_ro} WANT_SECRETS_RO={secrets_ro}
{FAKE_RSH}
{lifted}
_verify_mounts && echo VERIFIED
''', env={"WK_STORE": "/var/lib/wk",
          "WK_HOST_SECRETS": str(self.tmp / "secrets"),
          "WK_DEBUG": "1"})

    def test_all_three_there_and_the_writable_one_writable(self):
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("VERIFIED", cp.stdout)
        for phrase in ("mounted at /opt/wk-tools",
                       "mounted at /var/lib/wk/secrets",
                       "read-write at /var/lib/wk/agent-rw",
                       "/opt/wk-tools is mounted read-only",
                       "/var/lib/wk/secrets is mounted read-only"):
            self.assertIn(phrase, out)

    def test_no_tooling_mount_names_the_stage_that_makes_one(self):
        cp = self._run(tools=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("nothing in", out)
        self.assertIn("./setup --stage machine", out)
        self.assertNotIn("VERIFIED", cp.stdout)

    def test_a_secrets_directory_that_is_not_a_mount_is_refused(self):
        """A directory of the VM's own at that path passes every `test -d`:
        `wk key set` would write on this host and every container read
        nothing."""
        cp = self._run(secrets=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("/var/lib/wk/secrets is not a mount", out)
        self.assertIn("reach no workspace", out)

    def test_no_agent_rw_mount_at_all_is_refused(self):
        """A machine created before the writable mount existed: `wk ai claude
        --rc` in every workspace would ask for a login that is already here."""
        cp = self._run(rw_mount=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("/var/lib/wk/agent-rw is not a mount", out)
        self.assertIn("claude.ai", out)

    def test_an_agent_rw_mount_that_is_read_only_is_refused_for_its_own_reason(self):
        """Present is not enough, and the difference matters: the Claude CLI
        rewrites the credential in place, so a read-only mount logs every
        workspace out on the first refresh instead of never logging one in."""
        cp = self._run(rw_writable=0)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("mounted read-only", out)
        self.assertIn("logged", out)
        self.assertNotIn("is not a mount", out)

    def test_a_read_only_mount_that_is_writable_is_refused(self):
        """podman takes `--volume src:target:ro` and mounts it read-write
        anyway (measured on podman 5.4 + applehv), so this is the check that
        turns the machine's provisioning into a guarantee. A workspace that
        can write here rewrites the tooling it runs and the deploy keys it
        pushes with."""
        for case in ("tools_ro", "secrets_ro"):
            with self.subTest(case=case):
                cp = self._run(**{case: 0})
                out = cp.stdout + cp.stderr
                self.assertNotEqual(cp.returncode, 0, out)
                self.assertIn("is writable inside", out)
                self.assertIn("host/macos/playbook.yaml", out)
                self.assertNotIn("VERIFIED", cp.stdout)

    def test_it_runs_before_anything_that_names_a_path_in_them(self):
        """The whole point of failing once: the proxy, the skills and the
        SDK all live in one of the three."""
        text = VMTOOLS.read_text()
        self.assertLess(text.index("\n_verify_mounts\n"),
                        text.index("unit_start wk-proxy.service"),
                        "the mounts are verified after a step that needs them")

    def test_the_provisioning_runs_before_it(self):
        """The one step that must not wait for the verify: it is what holds
        the mounts read-only, so verifying first would refuse a machine this
        stage is about to fix -- and send the reader back to a stage that
        reports the invariant held."""
        text = VMTOOLS.read_text()
        self.assertLess(text.index("ansible-playbook /home/core/playbook.yaml"),
                        text.index("\n_verify_mounts\n"),
                        "the mounts are verified before the step that sets their mode")


if __name__ == "__main__":
    unittest.main()
