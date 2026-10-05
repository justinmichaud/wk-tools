"""A build machine records the hash of the inputs that provisioned it (`~/.wk-remote`), and every read recomputes it:
lib/wk/machine_cmd/deps.py against a fake machine. The base VM's half is tests/test_vm_base.py's."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine_cmd import deps  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestTheBuildMachineRecord(unittest.TestCase):

    def _stale(self, marker_text):
        far = Fake("box")
        far.answer(["sh", "-c"], out=marker_text)
        t = places.Remote("box", str(REPO), {"HOME": "/tmp/wk-test-unused", "XDG_STATE_HOME": "/tmp/wk-test-unused/state"}, far)
        t.machine = far
        return deps.stale(t, REPO)

    def test_a_machine_provisioned_from_these_inputs_reads_fresh(self):
        self.assertIsNone(self._stale("place=otherbox\nroot=/home/x/wk\ninputs=%s\n" % deps.inputs_hash(REPO)))

    def test_a_machine_provisioned_before_the_record_or_not_at_all_says_so(self):
        for marker in ("place=otherbox\nroot=/home/x/wk\n", ""):
            with self.subTest(marker=marker):
                self.assertTrue(self._stale(marker))

    def test_a_changed_provisioning_script_makes_it_stale(self):
        self.assertIn("remote/provision.sh", self._stale("place=otherbox\nroot=/home/x/wk\ninputs=0000000000000000\n"))

    def _copy(self, edit=None):
        root = Path(tempfile.mkdtemp(prefix="wk-test-inputs-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "remote").mkdir()
        for f in ("provision.sh", "probe.sh", "deps.sh"):
            text = (REPO / "remote" / f).read_text()
            (root / "remote" / f).write_text(edit(f, text) if edit else text)
        return deps.inputs_hash(root)

    def test_a_comment_or_a_layout_change_leaves_the_hash_alone(self):
        def edit(f, text):
            if f != "deps.sh":
                return text
            text = text.replace("wk_remote_deps() {   #", "\n\n# a new note\nwk_remote_deps() {  # reworded:")
            text = text.replace("    local w\n", "        local w\n\n")
            return "# a new header\n" + text + "\nwk_remote_driving_side_only() { true; }\n"
        self.assertEqual(self._copy(edit), deps.inputs_hash(REPO))

    def test_what_the_box_runs_is_in_it(self):
        for f, old, new in (("deps.sh", "ccache wanted", "ccache required"), ("probe.sh", "arch=%s", "machine=%s"),
                            ("provision.sh", "ensure_dir \"$ROOT/ws\"", "ensure_dir \"$ROOT/w\"")):
            with self.subTest(f=f):
                self.assertNotEqual(self._copy(lambda g, t: t.replace(old, new) if g == f else t), deps.inputs_hash(REPO))


class TestDoctorReportsIt(unittest.TestCase):
    def test_doctor_reports_the_machine_through_that_one_function(self):
        from tests.test_doctor import MISS, OK, build_doctor
        asked = []

        def stale(driver, root):
            asked.append(driver.name)
            return "provisioned before this record existed" if driver.name == "old" else None
        doc = build_doctor(probe=lambda t, root: "family=debian\n", stale=stale)
        (old,), (fresh,) = list(doc.build_machine("old")), list(doc.build_machine("fresh"))
        self.assertEqual((old[0], old[2]), (MISS, "wk machine setup old"))
        self.assertEqual(fresh[0], OK)
        self.assertEqual(["old", "fresh"], asked)


if __name__ == "__main__":
    unittest.main()
