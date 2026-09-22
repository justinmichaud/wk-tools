"""A macOS `wk ls` is one table from two halves -- this host's targets, then
the podman VM's containers -- and the "(no workspaces ...)" note may appear
only when both halves are empty. cmd/ls carries the two dispatcher-only flags
that make that one decision: --more-follows (first half: never call the
listing empty) and --empty-so-far (second half: the note is yours if you are
empty too).

Run: python3 -m unittest tests.test_ls_halves -v
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, temp_store
from tests.test_options import run_impl

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402


class TestTheEmptyNoteIsDecidedOnce(unittest.TestCase):
    def _ls(self, *flags):
        with temp_store() as store:
            # The container target lists real containers through podman, and
            # this table must be empty by construction. Trimming PATH does not
            # do that -- podman lives in /usr/bin on a Linux host, so the real
            # workspaces came back and every assertion here read the machine it
            # ran on. A stub that lists nothing is the empty table, wherever
            # podman is installed.
            binp = store["path"] / "bin"
            binp.mkdir(parents=True, exist_ok=True)
            (binp / "podman").write_text("#!/bin/sh\nexit 0\n")
            (binp / "podman").chmod(0o755)
            return run_impl("ls", *flags, env={
                "WK_STORE": store["WK_STORE"],
                "XDG_STATE_HOME": str(store["path"] / "state"),
                "WK_TARGET": "container",
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
        self.assertNotIn("NAME", cp.stdout, "the second half must not print a second header")

    def test_a_bare_ls_still_prints_the_note_alone(self):
        cp = self._ls()
        self.assertIn("no workspaces", cp.stdout)

    def _halves(self, first_half_lines):
        """The arguments `bare_report` (lib/wk/dispatch.py) hands each half
        of a bare `wk ls` when the first half prints `first_half_lines`: a
        stub `ls` is the first half and records its argv, and the
        dispatcher's own seams -- which targets are here, whether the machine
        runs, the forward -- answer as told."""
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
        """the first half is told more follows, and the second whether the
        first was empty -- decided once, by the dispatcher"""
        first, second = self._halves(["NAME"])
        self.assertEqual(first, ["--more-follows"])
        self.assertEqual(second, ["--continued", "--empty-so-far"])
        first, second = self._halves(["NAME", "a-workspace"])
        self.assertEqual(first, ["--more-follows"])
        self.assertEqual(second, ["--continued"])


if __name__ == "__main__":
    unittest.main()
