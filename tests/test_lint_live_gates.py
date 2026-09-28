"""lint.live_runs_on_the_container_target: the live tier runs against the
container target on Linux and macOS alike, so no test is gated on the podman
VM. A container test takes requires_container_target (tests/support.py), which
on macOS means the VM is up and on Linux means podman is; a test of the VM
itself names the platform as well.

Run: python3 tests/run.py --lint -k test_lint_live_gates
"""
TIER = "lint"
import re
import unittest

from tests.support import REPO

VM_GATES = re.compile(r"\b(requires_podman_vm|_needs_podman_vm|podman_vm_running|podman_vm_ssh)\b")


def gated_on_the_vm(text, support=False):
    """The lines naming a VM gate; support.py itself asks podman_vm_running inside container_target_missing only."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        m = VM_GATES.search(line)
        if m and not (support and m.group(1) == "podman_vm_running"):
            out.append("%d: %s" % (n, line.strip()))
    return out


class TestNoTestIsGatedOnThePodmanVm(unittest.TestCase):
    def test_the_rule_names_a_vm_gate(self):
        self.assertEqual(len(gated_on_the_vm("@requires_podman_vm()\nif not podman_vm_running(): skip\n")), 2)
        self.assertEqual(gated_on_the_vm("@requires_container_target()\n"), [])
        self.assertEqual(gated_on_the_vm("    return None if podman_vm_running('wk') else why\n", support=True), [])

    def test_no_test_names_a_podman_vm_gate(self):
        found = []
        for p in sorted((REPO / "tests").glob("*.py")):
            if p.name == "test_lint_live_gates.py":
                continue
            found += ["%s:%s" % (p.name, hit) for hit in gated_on_the_vm(p.read_text(), support=p.name == "support.py")]
        self.assertEqual(found, [], "gate on requires_container_target; a test of the VM itself also names darwin")


if __name__ == "__main__":
    unittest.main()
