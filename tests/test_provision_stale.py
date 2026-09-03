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
WK_VM_STORE and a stub `tart`; the build-machine half runs the real driver
functions against a stub `ssh` that executes the command locally, over a scratch
HOME holding the marker a provisioned machine would have.

Run: python3 -m unittest tests.test_provision_stale -v
"""
import os
import platform
import unittest

from tests.support import (REPO, WkTest, bash, rand_suffix, run, scratch_dir,
                           stub_path)

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

# Stand in for a live build machine: run the ssh'd command locally.
ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
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
printf 'image=x\\nprebuild=mac-release\\nfinished=2026-08-20T20:43:06Z\\n' > "$(_base_marker)"
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


class TestCloningAStaleBaseWarns(WkTest):
    """t_create warns and clones anyway: a rebuild is hours, so the choice is
    the person's -- but it is made with the consequence in front of them."""

    def _create(self, mark_ready):
        store = self.tmp / "store"
        store.mkdir(exist_ok=True)
        with stub_path({"tart": TART_WITH_BASE}) as binp:
            env = {"WK_VM_STORE": str(store),
                   "PATH": f"{binp}:{os.environ['PATH']}"}
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
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_a_stale_base_warns_and_names_the_rebuild(self):
        out = self._create(mark_ready=False)
        self.assertIn("predates its own provisioning inputs", out, out)
        self.assertIn("wk vm base --rebuild", out, out)
        self.assertIn("password", out, out)

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


class TestTheBuildMachineRecord(WkTest):
    """The same shape one layer out: `wk remote setup` records the hash of what
    provisions a machine in that machine's own marker, and `wk doctor --all`
    recomputes it. Driven over a stub ssh against a scratch HOME."""

    def _stale(self, marker_text=None):
        with scratch_dir(prefix="wk-test-remote-home-") as home, \
             stub_path({"ssh": ANSWERING_SSH}) as binp:
            if marker_text is not None:
                (home / ".wk-remote").write_text(marker_text)
            cp = bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
load_target remote >/dev/null 2>&1
echo "hash=$(remote_provision_inputs_hash)"
if why=$(remote_provision_stale); then echo "stale=$why"; else echo "fresh"; fi
''', env={
                # target= names another machine, so this run is a workstation
                # driving one over ssh rather than the machine itself.
                "HOME": str(home),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-build-machine",
                "WK_REMOTE_ROOT": str(home / "wk"),
                "PATH": f"{binp}:{os.environ['PATH']}",
            })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def _hash(self, out):
        return out.split("hash=", 1)[1].splitlines()[0]

    def test_a_machine_provisioned_from_these_inputs_reads_fresh(self):
        out = self._stale("target=otherbox\nroot=/home/x/wk\ninputs=PLACEHOLDER\n")
        h = self._hash(out)
        out = self._stale(f"target=otherbox\nroot=/home/x/wk\ninputs={h}\n")
        self.assertIn("fresh", out, out)

    def test_a_machine_provisioned_before_the_record_says_so(self):
        out = self._stale("target=otherbox\nroot=/home/x/wk\n")
        self.assertIn("stale=provisioned before this record existed", out, out)

    def test_a_changed_provisioning_script_makes_it_stale(self):
        out = self._stale("target=otherbox\nroot=/home/x/wk\ninputs=0000000000000000\n")
        self.assertIn("stale=", out, out)
        self.assertIn("remote/provision.sh", out, out)

    def test_a_machine_with_no_marker_is_not_provisioned_at_all(self):
        out = self._stale(None)
        self.assertIn("nothing has provisioned it", out, out)


class TestTheWriteSideRecordsIt(unittest.TestCase):
    """Source-level: the marker template and the one place the value is
    computed. Running remote/provision.sh needs a machine; what a test can hold
    it to is that the field is written and that its value comes from the
    driving side, so the two ends cannot hash differently."""

    def test_the_marker_carries_the_field(self):
        text = (REPO / "remote" / "provision.sh").read_text()
        self.assertIn("inputs=${WK_REMOTE_INPUTS:-}", text)

    def test_setup_computes_it_here_and_hands_it_over(self):
        text = (REPO / "cmd" / "remote").read_text()
        self.assertIn("WK_REMOTE_INPUTS=$(sh_quote \"$(remote_provision_inputs_hash)\")",
                      text)

    def test_the_hash_is_computed_in_exactly_one_place_per_artifact(self):
        """One implementation per behaviour: the base's hash lives in the vm
        driver, the machine's in the remote driver, and nothing else in the
        tree recomputes either."""
        for func, owner in (("_base_inputs_hash", REPO / "targets" / "vm.sh"),
                            ("remote_provision_inputs_hash",
                             REPO / "targets" / "remote.sh")):
            defs = [f for f in REPO.rglob("*")
                    if f.is_file() and ".git" not in f.parts
                    and f.suffix not in (".py",)
                    and f"{func}() {{" in f.read_text(errors="replace")]
            self.assertEqual(defs, [owner], f"{func}: {defs}")

    def test_doctor_reports_the_machine_through_that_one_function(self):
        text = (REPO / "cmd" / "doctor").read_text()
        self.assertIn("remote_provision_stale", text)
        self.assertIn("wk remote setup $_t", text)


if __name__ == "__main__":
    unittest.main()
