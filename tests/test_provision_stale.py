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

Hermetic. The base half runs the real driver functions against a scratch
WK_VM_STORE and a stub `tart`; the build-machine half runs lib/wk/machine_cmd.py
against a fake machine holding the marker a provisioned one would have.

Run: python3 -m unittest tests.test_provision_stale -v
"""
import os
import platform
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, repo_files, WkTest, bash, rand_suffix, run, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import machine_cmd, targets  # noqa: E402
from wk.machine import Fake  # noqa: E402

# A base that exists and is stopped, and every other verb succeeding: what
# `tart` answers about a machine whose golden base has been built.
TART_WITH_BASE = '''#!/bin/sh
case "$1" in
  list) echo '[{"Name":"wk-base","Source":"local","State":"stopped"}]' ;;
  *)    exit 0 ;;
esac
'''

# Nothing local at all: a machine that has never run `wk vm base`.
TART_EMPTY = '''#!/bin/sh
case "$1" in
  list) echo '[]' ;;
  *)    exit 0 ;;
esac
'''


class TestTheBaseRecord(WkTest):
    """_base_mark_ready writes it, vm_base_stale reads it back, and nothing in
    between stores a verdict."""

    def _drive(self, body, tart=TART_WITH_BASE, env=None):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": tart}) as binp:
            e = {"WK_VM_STORE": str(store), "PATH": f"{binp}:{os.environ['PATH']}"}
            if env:
                e.update(env)
            cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
{body}
''', env=e)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp, store / "vm" / "base.ready"

    def test_mark_ready_records_the_inputs_and_reads_fresh(self):
        cp, marker = self._drive('''
_base_mark_ready
echo "hash=$(_base_inputs_hash)"
if why=$(vm_base_stale); then echo "stale=$why"; else echo "fresh"; fi
''')
        text = marker.read_text()
        self.assertIn("inputs=", text, text)
        self.assertIn("fresh", cp.stdout, cp.stdout)
        recorded = [l for l in text.splitlines() if l.startswith("inputs=")][0]
        self.assertIn(recorded.split("=", 1)[1],
                      cp.stdout.split("hash=", 1)[1], cp.stdout)

    def test_the_record_holds_no_password(self):
        """The value itself is measured in the guest (vm/desktop.sh
        authenticates it, `wk vm check` reports it), so the record carries only
        whether the change applies -- a short digest over public files and a
        trivial password is a password a reader could recover."""
        _, marker = self._drive("_base_mark_ready")
        text = marker.read_text()
        self.assertNotIn("password", text, text)

    def test_a_marker_without_the_field_is_stale_and_says_why(self):
        """Today's base on a real machine: provisioned before the record
        existed, so nothing in the marker can vouch for it."""
        cp, _ = self._drive('''
ensure_dir "$WK_VM_DIR" 0700 >/dev/null
printf 'image=x\\nfinished=2026-08-20T20:43:06Z\\n' > "$(_base_marker)"
if why=$(vm_base_stale); then echo "stale=$why"; else echo "fresh"; fi
vm_base_findings
''')
        self.assertIn("stale=provisioned before this record existed", cp.stdout)
        self.assertIn("wk vm base --rebuild", cp.stdout, cp.stdout)
        self.assertTrue(cp.stdout.splitlines()[-1].startswith("wrong\t"), cp.stdout)

    def test_a_changed_input_makes_it_stale(self):
        """Marked ready against one image, read back against another: the base
        on the disk was made from the first one and says so."""
        cp, _ = self._drive('''
_base_mark_ready
if why=$(WK_VM_IMAGE=ghcr.io/other/image:1 vm_base_stale); then echo "stale=$why"; else echo "fresh"; fi
''')
        self.assertIn("stale=", cp.stdout, cp.stdout)
        self.assertIn("WK_VM_IMAGE", cp.stdout, cp.stdout)

    def test_a_changed_provisioning_script_makes_it_stale(self):
        """The point of the record: the scripts are the inputs, and editing one
        makes every base built before the edit read stale at once."""
        cp, _ = self._drive('''
_base_mark_ready
before=$(_base_inputs_hash)
tmp=$(mktemp -d)
cp -R "$WK_ROOT/vm" "$tmp/vm"
printf '\\n# one more line\\n' >> "$tmp/vm/desktop.sh"
after=$(WK_ROOT="$tmp" _base_inputs_hash)
rm -rf "$tmp"
[ "$before" = "$after" ] && echo same || echo different
''')
        self.assertIn("different", cp.stdout, cp.stdout)

    def test_no_base_at_all_is_reported_as_nothing_to_clone(self):
        cp, _ = self._drive("vm_base_findings", tart=TART_EMPTY)
        state, what, remedy = cp.stdout.rstrip("\n").split("\t")
        self.assertEqual(state, "wrong")
        self.assertIn("no golden base", what)
        self.assertIn("wk vm base", remedy)

    def test_a_base_whose_provisioning_never_finished_is_reported_as_such(self):
        """Existing is not finished, and the two have different remedies: an
        unfinished base is refreshed, a stale one rebuilt."""
        cp, _ = self._drive("vm_base_findings")
        state, what, remedy = cp.stdout.rstrip("\n").split("\t")
        self.assertEqual(state, "wrong")
        self.assertIn("never finished", what)
        self.assertIn("--refresh", remedy)

    def test_a_fresh_base_is_one_ok_line(self):
        cp, _ = self._drive("_base_mark_ready\nvm_base_findings")
        state, what, _ = cp.stdout.rstrip("\n").split("\t")
        self.assertEqual(state, "ok")
        self.assertIn("matches its provisioning inputs", what)


class TestCloningAStaleBaseIsRefused(WkTest):
    """A rebuild is hours, so the choice stays the person's -- but it is made
    before the clone exists, not after. A guest cloned from a base known to be
    wrong comes up behind Setup Assistant, and nothing in the guest can clear
    it, so creating one silently is handing over work that cannot be finished."""

    def _create(self, mark_ready, force=False, rc=0):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        # A clone is refused outright without a mirror on this machine to
        # clone from ([ -d "$(wk_mirror)" ], targets/vm.sh), and on macOS the
        # mirror lives under XDG_STATE_HOME -- which the suite points at a
        # scratch directory so no test writes into the real one. What the
        # driver checks is that the directory is there; the clone itself is
        # the guest's, through the stubbed tart.
        state = self.tmp / "state"
        (state / "wk" / "git" / "WebKit.git").mkdir(parents=True, exist_ok=True)
        with stub_path({"tart": TART_WITH_BASE}) as binp:
            env = {"WK_VM_STORE": str(store),
                   "XDG_STATE_HOME": str(state),
                   "PATH": f"{binp}:{os.environ['PATH']}"}
            if force:
                env["WK_VM_FORCE"] = "1"
            marker = "_base_mark_ready" if mark_ready else '''
ensure_dir "$WK_VM_DIR" 0700 >/dev/null
printf 'image=x\\nfinished=old\\n' > "$(_base_marker)"
'''
            cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
{marker}
t_create demo-{rand_suffix()}
''', env=env)
        self.assertEqual(cp.returncode, rc, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_a_stale_base_is_refused_and_the_rebuild_named(self):
        out = self._create(mark_ready=False, rc=1)
        self.assertIn("predates its own provisioning inputs", out, out)
        self.assertIn("wk vm base --rebuild", out, out)
        # What a clone actually inherits from a stale base. Not the password:
        # the guest keeps the one its image ships, so it cannot go stale.
        self.assertIn("desktop settings", out, out)

    def test_the_refusal_names_what_crosses_it(self):
        out = self._create(mark_ready=False, rc=1)
        self.assertIn("WK_VM_FORCE=1", out, out)

    def test_a_forced_clone_still_says_what_it_is_cloning(self):
        """Crossing the barrier is a choice, not a way to stop being told."""
        out = self._create(mark_ready=False, force=True)
        self.assertIn("predates its own provisioning inputs", out, out)

    def test_a_current_base_clones_without_a_word(self):
        out = self._create(mark_ready=True)
        self.assertNotIn("predates", out, out)


@unittest.skipUnless(platform.system() == "Darwin", "wk vm needs a macOS host")
class TestTheListingSaysSo(WkTest):
    """`wk vm ls` is where the base's state is asked for, so that is where it
    is answered -- recomputed on every read, never remembered."""

    def _ls(self, mark_ready):
        store = self.tmp / "store"
        (store / "vm").mkdir(parents=True, exist_ok=True)
        if mark_ready:
            cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target vm >/dev/null 2>&1
_base_mark_ready
''', env={"WK_VM_STORE": str(store)})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        else:
            (store / "vm" / "base.ready").write_text("image=x\nfinished=old\n")
        with stub_path({"tart": TART_WITH_BASE}) as binp:
            return run("vm", "ls", env={"WK_VM_STORE": str(store),
                                        "PATH": f"{binp}:{os.environ['PATH']}"})

    def test_a_stale_base_gets_a_base_line_and_the_rebuild(self):
        cp = self._ls(mark_ready=False)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("BASE", cp.stdout, cp.stdout)
        self.assertIn("predates its own provisioning inputs", cp.stdout, cp.stdout)
        self.assertIn("wk vm base --rebuild", cp.stdout, cp.stdout)

    def test_a_current_base_gets_a_base_line_saying_so(self):
        cp = self._ls(mark_ready=True)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("BASE", cp.stdout, cp.stdout)
        self.assertIn("matches its provisioning inputs", cp.stdout, cp.stdout)


class TestTheBuildMachineRecord(unittest.TestCase):
    """The same shape one layer out: `wk machine setup` records the hash of what
    provisions a machine in that machine's own marker, and `wk doctor --all`
    recomputes it (lib/wk/machine_cmd.py's stale), against a fake machine."""

    def _stale(self, marker_text=None):
        far = Fake("box")
        if marker_text is None:
            far.answer(["sh", "-c"], rc=0, out="")
        else:
            far.answer(["sh", "-c"], out=marker_text)
        t = targets.Remote("box", str(REPO), {"HOME": "/tmp/wk-test-unused", "XDG_STATE_HOME": "/tmp/wk-test-unused/state"}, far)
        t.machine = far
        return machine_cmd.stale(t, REPO)

    def test_a_machine_provisioned_from_these_inputs_reads_fresh(self):
        self.assertIsNone(self._stale("target=otherbox\nroot=/home/x/wk\ninputs=%s\n" % machine_cmd.inputs_hash(REPO)))

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
        return machine_cmd.inputs_hash(root)

    def test_a_comment_or_a_layout_change_leaves_the_hash_alone(self):
        """Every provisioned box would otherwise read stale over prose nothing on it runs."""
        def edit(f, text):
            if f != "deps.sh":
                return text
            text = text.replace("wk_remote_deps() {   #", "\n\n# a new note\nwk_remote_deps() {  # reworded:")
            text = text.replace("    local w\n", "        local w\n\n")
            return "# a new header\n" + text + "\nwk_remote_driving_side_only() { true; }\n"
        self.assertEqual(self._copy(edit), machine_cmd.inputs_hash(REPO))

    def test_what_the_box_runs_is_in_it(self):
        for f, old, new in (("deps.sh", "ccache wanted", "ccache required"), ("probe.sh", "arch=%s", "machine=%s"),
                            ("provision.sh", "ensure_dir \"$ROOT/ws\"", "ensure_dir \"$ROOT/w\"")):
            with self.subTest(f=f):
                self.assertNotEqual(self._copy(lambda g, t: t.replace(old, new) if g == f else t), machine_cmd.inputs_hash(REPO))


class TestTheWriteSideRecordsIt(unittest.TestCase):
    """Source-level: the marker template and the one place the value is
    computed. Running remote/provision.sh needs a machine; what a test can hold
    it to is that the field is written and that its value comes from the
    driving side, so the two ends cannot hash differently."""

    def test_the_marker_carries_the_field(self):
        text = (REPO / "remote" / "provision.sh").read_text()
        self.assertIn("inputs=${WK_REMOTE_INPUTS:-}", text)

    def test_setup_computes_it_here_and_hands_it_over(self):
        text = (REPO / "lib" / "wk" / "machine_cmd.py").read_text()
        self.assertIn('"WK_REMOTE_INPUTS=" + inputs_hash(self.root)', text)

    def test_the_hash_is_computed_in_exactly_one_place_per_artifact(self):
        """One implementation per behaviour: the base's hash lives in the vm
        driver, the machine's in lib/wk/machine_cmd.py, and nothing else in the
        tree recomputes either."""
        defs = [f for f in repo_files() if f.suffix not in (".py",)
                and "_base_inputs_hash() {" in f.read_text(errors="replace")]
        self.assertEqual(defs, [REPO / "targets" / "vm.sh"])
        defs = [f for f in sorted((REPO / "lib").rglob("*.py")) if "def inputs_hash(" in f.read_text(errors="replace")]
        self.assertEqual(defs, [REPO / "lib" / "wk" / "machine_cmd.py"])

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
