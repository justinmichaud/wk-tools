"""t_egress_filtered (lib/target.sh): does everything this workspace reaches go
through wk's allowlisting proxy?

`wk verify` asks the driver rather than deciding per target, because the two
sandboxes apply one allowlist two ways -- nftables in the podman machine,
Softnet on this host -- and promise the same thing. Asking per command is how a
guest went a session with its egress unmeasured while `wk verify` printed
"sandbox intact".

Run: python3 -m unittest tests.test_target_egress -v
"""
import unittest

from tests.support import REPO, WkTest, bash


def _filtered(target, name="demo", env=None):
    cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target {target} >/dev/null 2>&1
t_egress_filtered {name} && echo FILTERED || echo OPEN
''', env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


class TestEveryDriverAnswers(WkTest):
    def test_a_container_is_filtered(self):
        """It has no network interface but loopback; everything is the proxy."""
        self.assertEqual("FILTERED", _filtered("container"))

    def test_a_guest_is_filtered(self):
        self.assertEqual("FILTERED", _filtered("vm", env={"WK_VM_STORE": str(self.tmp)}))

    def test_a_guest_booted_unfiltered_says_so(self):
        """Softnet is applied at `tart run` and cannot be added later, so a
        guest booted with WK_VM_UNFILTERED has the open network for its whole
        life. _boot records that beside the run log; the environment this is
        read in, hours later and in another shell, says nothing about it."""
        vmdir = self.tmp / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        (vmdir / "demo.unfiltered").write_text("")
        self.assertEqual("OPEN", _filtered("vm", env={"WK_VM_STORE": str(self.tmp)}))

    def test_a_target_with_no_filter_at_all_says_open(self):
        """remote is a plain checkout on a shared machine: `wk verify` refuses
        it outright, and the default must not claim otherwise."""
        self.assertEqual("OPEN", _filtered("remote"))


class TestVerifyAsksTheDriver(unittest.TestCase):
    def test_the_egress_checks_are_gated_on_the_contract(self):
        """Not on the target's name, and not on WK_SANDBOX: a fifth sandbox
        would then be verified as though it had no network at all."""
        src = (REPO / "cmd" / "verify").read_text()
        head = src[:src.index("github reachable through the proxy")]
        self.assertIn('if t_egress_filtered "$NAME"; then', head)
        self.assertNotIn('WK_TARGET_KIND" = vm', head)

    def test_an_unfiltered_guest_is_a_failure_not_a_skip(self):
        """`wk verify` proves a sandbox holds. A guest with the open network
        has none, and saying nothing about it is how one was reported intact."""
        src = (REPO / "cmd" / "verify").read_text()
        branch = src[src.index('elif [ "$WK_TARGET_KIND" = vm ]; then'):]
        branch = branch[:branch.index("\nfi\n")]
        self.assertIn("fail ", branch)
        self.assertIn("WK_VM_UNFILTERED", branch)


if __name__ == "__main__":
    unittest.main()
