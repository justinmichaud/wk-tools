"""An artifact that predates its own provisioning inputs, and how it says so.

Two artifacts are provisioned once and used for months -- the golden macOS base
VM every guest is a clone of, and a shared build machine -- and both are made by
scripts in this tree. Edit one of those scripts and the artifact does not
change: a base sealed before the password change still hands every clone the
image's password, and a build box provisioned before remote/provision.sh grew a
line still lacks it. So each records the hash of the inputs that produced it
(the base marker, `~/.wk-remote`) and every read recomputes that hash and
compares. A record of what produced an artifact, never a verdict about it: no
code here believes the record over the comparison.

Hermetic: lib/wk/machine_cmd/deps.py against a fake machine holding the marker a
provisioned one would have. The base's half is tests/test_vm_base.py's.

Run: python3 -m unittest tests.test_provision_stale -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, repo_files

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine_cmd import deps  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestTheBuildMachineRecord(unittest.TestCase):
    """The same shape one layer out: `wk machine setup` records the hash of what
    provisions a machine in that machine's own marker, and `wk doctor --all`
    recomputes it (lib/wk/machine_cmd/deps.py's stale), against a fake machine."""

    def _stale(self, marker_text=None):
        far = Fake("box")
        if marker_text is None:
            far.answer(["sh", "-c"], rc=0, out="")
        else:
            far.answer(["sh", "-c"], out=marker_text)
        t = targets.Remote("box", str(REPO), {"HOME": "/tmp/wk-test-unused", "XDG_STATE_HOME": "/tmp/wk-test-unused/state"}, far)
        t.machine = far
        return deps.stale(t, REPO)

    def test_a_machine_provisioned_from_these_inputs_reads_fresh(self):
        self.assertIsNone(self._stale("target=otherbox\nroot=/home/x/wk\ninputs=%s\n" % deps.inputs_hash(REPO)))

    def test_a_machine_provisioned_before_the_record_says_so(self):
        self.assertEqual(self._stale("target=otherbox\nroot=/home/x/wk\n"), "provisioned before this record existed")

    def test_a_changed_provisioning_script_makes_it_stale(self):
        self.assertIn("remote/provision.sh", self._stale("target=otherbox\nroot=/home/x/wk\ninputs=0000000000000000\n"))

    def test_a_machine_with_no_marker_is_not_provisioned_at_all(self):
        self.assertIn("nothing has provisioned it", self._stale(None))

    def _copy(self, edit=None):
        root = Path(tempfile.mkdtemp(prefix="wk-test-inputs-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "remote").mkdir()
        for f in ("provision.sh", "probe.sh", "deps.sh"):
            text = (REPO / "remote" / f).read_text()
            (root / "remote" / f).write_text(edit(f, text) if edit else text)
        return deps.inputs_hash(root)

    def test_a_comment_or_a_layout_change_leaves_the_hash_alone(self):
        """Every provisioned box would otherwise read stale over prose nothing on it runs."""
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


class TestTheWriteSideRecordsIt(unittest.TestCase):
    """Source-level: the marker template and the one place the value is
    computed. Running remote/provision.sh needs a machine; what a test can hold
    it to is that the field is written and that its value comes from the
    driving side, so the two ends cannot hash differently."""

    def test_the_marker_carries_the_field(self):
        text = (REPO / "remote" / "provision.sh").read_text()
        self.assertIn("inputs=${WK_REMOTE_INPUTS:-}", text)

    def test_setup_computes_it_here_and_hands_it_over(self):
        text = (REPO / "lib" / "wk" / "machine_cmd" / "build.py").read_text()
        self.assertIn('"WK_REMOTE_INPUTS=" + inputs_hash(self.root)', text)

    def test_the_hash_is_computed_in_exactly_one_place_per_artifact(self):
        """One implementation per behaviour: the base's hash lives in its builder, the machine's in
        lib/wk/machine_cmd/deps.py, and nothing else in the tree recomputes either."""
        self.assertEqual([], [f for f in repo_files() if f.suffix != ".py" and "inputs_hash() {" in f.read_text(errors="replace")])
        defs = [f for f in sorted((REPO / "lib").rglob("*.py")) if "def inputs_hash(" in f.read_text(errors="replace")]
        self.assertEqual(defs, [REPO / "lib" / "wk" / "machine_cmd" / "deps.py", REPO / "lib" / "wk" / "sysimage" / "guestbase.py"])

    def test_doctor_reports_the_machine_through_that_one_function(self):
        """One row per machine, from remote_provision_stale's answer alone: a
        reason is a miss naming the setup, none is the ok row."""
        from tests.test_doctor import MISS, OK, build_doctor
        asked = []

        def stale(target, root):
            asked.append(target.name)
            return "provisioned before this record existed" if target.name == "old" else None
        doc = build_doctor(probe=lambda t, root: "family=debian\n", stale=stale)
        self.assertEqual([(MISS, "provisioning on old predates its inputs: provisioned before this record existed", "wk machine setup old")],
                         list(doc.build_machine("old")))
        self.assertEqual([(OK, "provisioned from this tree's remote/provision.sh + remote/deps.sh", "")], list(doc.build_machine("fresh")))
        self.assertEqual(["old", "fresh"], asked)

if __name__ == "__main__":
    unittest.main()
