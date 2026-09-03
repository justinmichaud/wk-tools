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
    def test_push_runs_on_the_machine_that_holds_the_keys(self):
        """`wk push` declares where=local: the keys are this device's own
        (wk_secrets_dir), and on macOS the podman machine reads them as a
        mount -- so nothing is forwarded and nothing has to be running"""
        self.assertEqual(decl("push")[1], "local")

    def test_nothing_forwards_it_into_the_podman_machine(self):
        """the hop is gone, not merely unused: no `--store` flag, no second
        `wk push` invocation of its own, no `podman machine ssh`"""
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("--store", "store_half", "podman machine ssh"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

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

    def test_the_fan_out_needs_no_flag_override(self):
        """--all/--target need the target confs and ssh reach to the fleet, and
        get them for free now that the whole command runs here: a `flag ...
        where=local` override would be a second copy of that decision"""
        src = (REPO / "cmd" / "push").read_text()
        self.assertNotIn("flag --all,--target", src)
        self.assertIn("# wk: where=local", src)


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

    def test_nothing_moves_between_the_two_directories_any_more(self):
        """The switch is what an ssh-agent holds, so the file walk that used to
        move keys is gone rather than unused: two positions on disk means a
        crash between them, and a key in the mounted directory is the position
        this command exists to prevent."""
        src = (REPO / "cmd" / "push").read_text()
        for gone in ("move_keys", "MOVED", "STUCK", "purge_copies"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, src)

    def test_on_without_an_agent_fails_and_claims_nothing(self):
        """The failure the old `mv` had, in its new shape: there is nowhere to
        put the keys, so `on` must not report a switch it did not throw."""
        secrets = self.tmp / "secrets"
        held = self.tmp / "push-keys"
        held.mkdir(parents=True)
        secrets.mkdir(parents=True)
        (held / "build_key_fork").write_text("placeholder-not-a-key\n")

        cp = self.run_wk("push", "on", env={
            "WK_STORE": str(self.tmp / "store"),
            "WK_HOST_SECRETS": str(secrets),
            "WK_PUSH_AGENT_SOCK": str(self.tmp / "no-agent.sock"),
            "WK_PUSH_PAT_FILE": str(self.tmp / "pat"),
            "WK_MACHINE": "wk-no-such-machine",
        })
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no ssh-agent answers", cp.stdout)
        self.assertNotIn("push is ON", cp.stdout)
        self.assertTrue((held / "build_key_fork").exists(),
                        "a private half left the directory nothing mounts")

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
