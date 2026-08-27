"""Un-managed clobbering is detected, not silently trusted (CLAUDE.md rule
5: when the record and the machine disagree, the machine wins and the
command says so). Both tests here drive the *real* driver code
(targets/container.sh's t_info/t_created, targets/vm.sh's t_info/t_created)
against a stub `podman`/`tart` on PATH -- not a stub of t_info itself, which
would only prove the contract lib/target.sh's ws_state already tests
(tests/test_state.py's TestWsStateWords) -- so what is new here is that the
*driver's own* translation of "the environment is gone" into `absent` is
exercised for real, for the one case a person can actually produce by hand:
`podman rm`/`tart delete` on a workspace whose creation had already
finished.

Run: python3 -m unittest tests.test_clobbering -v
"""
import os
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, stub_path, temp_store


class TestPodmanRmByHand(WkTest):
    def test_a_container_removed_by_hand_reads_broken(self):
        # `inspect` failing is exactly what `podman rm <container>` leaves
        # behind: the workspace directory and its finished-creation marker
        # survive (they are host-side files), only the container is gone.
        fake_podman = "case \"$*\" in *inspect*) exit 1 ;; *) exit 0 ;; esac\n"
        with temp_store() as store, stub_path({"podman": fake_podman}) as binp:
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            (ws / "home").mkdir(parents=True)
            (ws / "home" / ".wk-ready").write_text("")
            (ws / "base-id").write_text("deadbeef\n")

            script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/detach.sh"
load_target container >/dev/null 2>&1
echo "state:$(ws_state {name})"
echo "display:$(ws_display_state {name})"
'''
            env = {"WK_STORE": str(store["path"]), "PATH": f"{binp}:{os.environ['PATH']}"}
            cp = bash(script, env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(
                "state:broken", cp.stdout,
                f"a hand-removed container should read 'broken' (rule 5): {cp.stdout}",
            )
            self.assertIn(
                "display:broken", cp.stdout,
                f"'wk ls'/'wk status' (ws_display_state) should say 'broken' too: {cp.stdout}",
            )


class TestTartDeleteByHand(WkTest):
    def test_a_guest_deleted_by_hand_reads_broken(self):
        # `tart list --format json` returning nothing for this VM is exactly
        # what `tart delete wk-<name>` leaves behind: the host-side
        # workspace directory and its ready marker survive, the guest does
        # not.
        fake_tart = 'case "$1" in list) echo "[]" ;; *) exit 1 ;; esac\n'
        with temp_store() as store, stub_path({"tart": fake_tart}) as binp:
            name = f"demo-{rand_suffix()}"
            ws = store["path"] / "ws" / name
            ws.mkdir(parents=True)
            (ws / ".wk-ready").write_text("")

            script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/detach.sh"
load_target vm >/dev/null 2>&1
echo "state:$(ws_state {name})"
echo "display:$(ws_display_state {name})"
'''
            env = {"WK_VM_STORE": str(store["path"]), "PATH": f"{binp}:{os.environ['PATH']}"}
            cp = bash(script, env=env)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(
                "state:broken", cp.stdout,
                f"a hand-deleted guest should read 'broken' (rule 5): {cp.stdout}",
            )
            self.assertIn(
                "display:broken", cp.stdout,
                f"'wk ls'/'wk status' (ws_display_state) should say 'broken' too: {cp.stdout}",
            )


if __name__ == "__main__":
    unittest.main()
