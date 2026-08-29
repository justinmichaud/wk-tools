"""`wk push` -- the deploy-key switch: where it runs, what `--all` covers, and
that it never reports a move it did not make.

Run: python3 -m unittest tests.test_push_switch -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WK, WkTest, bash


def decl(name):
    """The dispatcher's own declaration line for one command."""
    cp = subprocess.run([str(WK), "--declarations"], cwd=str(REPO),
                        stdout=subprocess.PIPE, text=True, timeout=60)
    for line in cp.stdout.splitlines():
        f = line.split("\t")
        if f and f[0] == name:
            return f
    raise AssertionError(f"{name} is not declared")


class TestWhere(WkTest):
    def test_push_acts_on_the_store_not_the_host(self):
        """`wk push` declares where=store: the keys are in the store, which on
        a macOS workstation is inside the podman VM"""
        self.assertEqual(decl("push")[1], "store")

    def _as_build_machine(self):
        """The environment of a shared build machine: the ~/.wk-remote marker
        the dispatcher reads, and a store of its own that is local (so the
        macOS-only forward into the podman VM does not apply, exactly as on
        the Linux machine this stands in for)."""
        marker = self.tmp / "wk-remote"
        marker.write_text("target=buildbox4\n")
        store = self.tmp / "store"
        store.mkdir()
        return {"WK_REMOTE_MARKER": str(marker), "WK_STORE": str(store)}

    def test_a_store_command_runs_on_a_build_machine(self):
        """a build machine holds its own store, so `wk push` is not refused
        there -- which is what `wk push --all` needs to reach it"""
        cp = self.run_wk("push", "status", env=self._as_build_machine())
        self.assertNotIn("acts on a workstation", cp.stdout)

    def test_a_host_command_is_still_refused_on_a_build_machine(self):
        """where=host keeps its refusal: `wk quiesce` acts on hardware here"""
        cp = self.run_wk("quiesce", "status", env=self._as_build_machine())
        self.assertIn("acts on a workstation", cp.stdout)

    def test_a_store_command_is_refused_inside_a_workspace(self):
        """the keys are on the host and a workspace cannot reach them"""
        marker = self.tmp / "wk-workspace"
        marker.write_text("name=probe\ntarget=container\n")
        cp = self.run_wk("push", "status", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("workspace", cp.stdout)

    def test_the_fan_out_flags_stay_on_this_machine(self):
        """--all/--target are declared where=local: the fan-out needs the
        target confs and ssh reach, which the podman VM has neither of"""
        self.assertIn("flag --all,--target where=local", (REPO / "cmd" / "push").read_text())


class TestAllCoversThisMachine(WkTest):
    def test_all_asks_this_machine_before_the_fleet(self):
        """`--all` means every machine: this one through its own `wk`, then
        for_each_machine for the rest"""
        src = (REPO / "cmd" / "push").read_text()
        body = src[src.index('if [ "$TARGET" = --all ]'):]
        self.assertLess(body.index("_here"), body.index("for_each_machine"),
                        "the fan-out runs before this machine's own row")


class TestNoFalseClaim(WkTest):
    def test_ensure_dir_dies_rather_than_claiming_a_dir_it_could_not_make(self):
        """ensure_dir reports an error, not '==> create', when mkdir fails"""
        d = self.tmp / "no-write" / "child"
        os.makedirs(self.tmp / "no-write")
        os.chmod(self.tmp / "no-write", 0o500)
        self.addCleanup(os.chmod, self.tmp / "no-write", 0o700)
        # ensure_dir narrates on stderr (info/die), like everything in lib.
        cp = bash(f'. "{REPO}/lib/common.sh"; ensure_dir "{d}" 0700')
        self.assertNotEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("cannot create", cp.stderr)
        self.assertNotIn("==> create", cp.stderr)
        self.assertFalse(d.exists())

    def test_move_keys_count_survives_a_store_it_cannot_create(self):
        """a `die` inside `n=$(...)` exits only the subshell, so the count
        comes back in MOVED and `wk push off` cannot report 'already OFF'
        about a store nothing looked at"""
        src = (REPO / "cmd" / "push").read_text()
        self.assertNotIn("n=$(move_keys", src)
        self.assertIn('move_keys "$LIVE" "$HELD"; n="$MOVED"', src)

    def test_the_subshell_trap_is_real(self):
        """the mechanism the two above defend against: bash applies neither
        errexit nor a die's exit across an assignment's command substitution"""
        cp = bash('f() { false; echo reached; }; n=$(f); printf %s "$n"')
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(cp.stdout.strip(), "reached")


class TestStatusIsReadOnly(WkTest):
    def test_status_is_declared_readonly(self):
        """`wk push status` changes nothing, so a stopped podman machine is
        reported rather than started"""
        self.assertIn("# wk: readonly status", (REPO / "cmd" / "push").read_text())
