"""The fleet exit code aggregates the worst state found anywhere -- owed"""
import os
import unittest

from tests.support import WkTest, rand_suffix, run, scratch_dir, stub_path
from tests.test_build_liveness import write_task


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''


def _records(xdg, machdir, **env):
    with stub_path({"ssh": _ANSWERING_SSH}) as binp:
        return run("status", "--records", timeout=45, env={
            "XDG_STATE_HOME": str(xdg), "WK_MACHINES_DIR": str(machdir), "WK_PLACE": "remote",
            "WK_REMOTE_HOST": "fake-reachable-machine", "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "WK_PROBE_SECONDS": "1", **env})


class TestFleetExitCodeIsTheWorst(WkTest):
    def _two_workspaces(self, xdg, remote_root, states):
        store = xdg / "wk" / "remote" / "remote"
        names = []
        for i, (state, extra) in enumerate(states):
            name = f"wsx{i}-{rand_suffix()}"
            names.append(name)
            write_task(store, name=name, end=state, **extra)
            (remote_root / "ws" / name).mkdir(parents=True)
            (remote_root / "ws" / name / ".wk-ready").touch()
        return names

    def _bare_status(self, xdg, remote_root, machdir):
        return _records(xdg, machdir, WK_REMOTE_ROOT=str(remote_root))

    def test_failed_and_stalled_together_report_the_worse_of_the_two(self):
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-remote-root-") as root, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            names = self._two_workspaces(xdg, root, [
                (1, {}),
                ("stalled", {}),
            ])
            cp = self._bare_status(xdg, root, machdir)
            for n in names:
                self.assertIn(n, cp.stdout, f"{n} missing from the records:\n{cp.stdout}")
            self.assertEqual(cp.returncode, 3, cp.stdout)

    def test_two_failed_workspaces_report_1_not_2(self):
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-remote-root-") as root, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            self._two_workspaces(xdg, root, [
                (1, {}),
                (1, {}),
            ])
            cp = self._bare_status(xdg, root, machdir)
            self.assertEqual(cp.returncode, 1, cp.stdout)


class TestAFailedRecordSurvivesAWorkspaceThatBumpedFourAlready(WkTest):
    def test_a_failed_record_is_emitted_beside_a_workspace_that_bumped_4(self):
        with scratch_dir(prefix="wk-test-xdg-") as xdg, \
             scratch_dir(prefix="wk-test-machines-") as machdir:
            store = xdg / "wk" / "remote" / "remote"
            name = f"wsxbug-{rand_suffix()}"
            (store / "ws" / name).mkdir(parents=True)
            write_task(store, name=name, end=1)
            cp = _records(xdg, machdir)
            self.assertIn(name, cp.stdout, cp.stdout)
            self.assertIn('"state":"failed"', cp.stdout, cp.stdout)
            self.assertIn('"kind":"workspace"', cp.stdout, cp.stdout)
            self.assertEqual(cp.returncode, 4, cp.stdout)


if __name__ == "__main__":
    unittest.main()
