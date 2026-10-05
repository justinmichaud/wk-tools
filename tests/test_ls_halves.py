"""A macOS `wk ls` is one table from two halves, and the "(no workspaces ...)" note appears only when both are empty:
the dispatcher hands the halves --more-follows and --empty-so-far."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, fake_workspace, temp_store
from tests.test_options import run_impl

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402


class TestTheEmptyNoteIsDecidedOnce(unittest.TestCase):
    def _ls(self, *flags):
        with temp_store() as store:
            # A podman that lists nothing, wherever the real one is installed.
            binp = store["path"] / "bin"
            binp.mkdir(parents=True, exist_ok=True)
            (binp / "podman").write_text("#!/bin/sh\n[ \"$1 $2\" = 'machine inspect' ] && echo '[{\"Name\": \"wk\", \"State\": \"stopped\"}]'\nexit 0\n")
            (binp / "podman").chmod(0o755)
            return run_impl("ls", *flags, env={
                "WK_STORE": store["WK_STORE"],
                "XDG_STATE_HOME": str(store["path"] / "state"),
                "WK_PLACE": "container",
                "PATH": f"{binp}:/usr/bin:/bin",
            })

    def test_the_first_half_never_prints_the_note(self):
        cp = self._ls("--more-follows")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("NAME", cp.stdout)
        self.assertNotIn("no workspaces", cp.stdout)

    def test_the_second_half_prints_it_only_when_nothing_came_before(self):
        cp = self._ls("--continued", "--empty-so-far")
        self.assertIn("no workspaces", cp.stdout)
        cp = self._ls("--continued")
        self.assertNotIn("no workspaces", cp.stdout)
        self.assertNotIn("NAME", cp.stdout)

    def test_a_bare_ls_still_prints_the_note_alone(self):
        cp = self._ls()
        self.assertIn("no workspaces", cp.stdout)

    def _halves(self, first_half_lines):
        """The arguments `bare_report` hands each half when the first prints `first_half_lines`."""
        with tempfile.TemporaryDirectory(prefix="wk-test-halves-") as tmp:
            stub, argv = Path(tmp) / "ls", Path(tmp) / "argv"
            stub.write_text("#!/bin/sh\n# wk ls -- a stub\n# wk: where=workspace name=none bare=merged readonly\n"
                            f'echo "$@" > {argv}\n'
                            + "".join(f"echo '{l}'\n" for l in first_half_lines))
            stub.chmod(0o755)
            inv = dispatch.Invocation("ls", D.Decl(stub), [])
            forwarded = mock.Mock(return_value=0)
            with mock.patch.object(dispatch, "registry", return_value=mock.Mock(all=mock.Mock(return_value=["container", "fakelocal"]))), \
                 mock.patch.object(dispatch, "machine_running", return_value=True), \
                 mock.patch.object(dispatch, "forward_status", forwarded), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(dispatch.Exit):
                    dispatch.bare_report(inv, "ls", [])
            return argv.read_text().split(), forwarded.call_args[0][2]

    def test_the_dispatcher_owns_the_flags(self):
        first, second = self._halves(["NAME"])
        self.assertEqual(first, ["--more-follows"])
        self.assertEqual(second, ["--continued", "--empty-so-far"])
        first, second = self._halves(["NAME", "a-workspace"])
        self.assertEqual(first, ["--more-follows"])
        self.assertEqual(second, ["--continued"])


class TestInsideAWorkspace(unittest.TestCase):
    """From inside a workspace BASE and CHANGES are not applicable: the host holds them."""

    def test_base_and_changes_are_not_applicable(self):
        with fake_workspace() as ws:
            table = run_impl("ls", env=ws.env()).stdout.splitlines()
            doc = json.loads(run_impl("ls", "--json", env=ws.env(), split=True).stdout)
        self.assertEqual(table[1].split(), ["selftest-ws", "local", "running", "n/a", "-", "native", "n/a"])
        (row,) = doc["workspaces"]
        self.assertEqual((row["base"], row["changes"]), ("n/a", "n/a"))


if __name__ == "__main__":
    unittest.main()
