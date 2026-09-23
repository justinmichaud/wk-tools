"""cmd/test: a layout-test path the workspace does not have is refused
before the runner starts.

The defect this closes: run-webkit-tests answers a path it cannot find with
"Found 0 tests; running 0, skipping 0. All tests skipped." and exit 0, and
`wk test` read that exit code as success -- it printed "TESTS OK" for a run
that had executed nothing. Measured 2026-09-04 on a guest and on buildbox4
with the same mistyped path.

Drives `cmd.test.missing_layout_paths` directly, its `runner` a plain
callable standing in for `Target.exec` -- the existence test itself has to
run on the target's filesystem, so the shell fragment stays shell, but
nothing beyond a line-split of its answer happens outside Python.

Run: python3 -m unittest tests.test_layout_paths -v
"""
import importlib.machinery
import importlib.util
import unittest

from tests.support import REPO
from wk.machine import Result


def _load_cmd_test():
    """`cmd/test` has no `.py` suffix (it is `wk test -h`'s own help block, dispatcher
    convention), so the loader is named explicitly rather than inferred from the path."""
    loader = importlib.machinery.SourceFileLoader("cmd_test", str(REPO / "cmd" / "test"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CMD_TEST = _load_cmd_test()


class TestLayoutPathCheck(unittest.TestCase):
    def _run(self, paths, present=()):
        """A `runner` answering as `sh -c` would against a checkout holding `present`."""
        present = set(present)

        def runner(argv):
            self.assertEqual(argv[0], "sh")
            script = argv[-1]
            self.assertIn("done", script)
            missing = []
            for p in paths:
                stem = p.split("?", 1)[0]
                if stem not in present:
                    missing.append(p)
            return Result(0, "".join(m + "\n" for m in missing))

        return CMD_TEST.missing_layout_paths(runner, "/src/WebKit", paths)

    def test_a_path_that_is_not_there_is_refused_by_name(self):
        missing = self._run(["fast/dom/Element/id-attribute.html"])
        self.assertEqual(missing, ["fast/dom/Element/id-attribute.html"])

    def test_a_path_that_is_there_runs(self):
        missing = self._run(["fast/dom/Comment"], present=["fast/dom/Comment"])
        self.assertEqual(missing, [])

    def test_every_missing_one_is_named_not_just_the_first(self):
        """A person who mistyped two paths should not have to run twice."""
        missing = self._run(["fast/gone-a.html", "fast/dom/Comment", "fast/gone-b.html"],
                            present=["fast/dom/Comment"])
        self.assertEqual(missing, ["fast/gone-a.html", "fast/gone-b.html"])

    def test_a_query_string_is_not_part_of_the_path(self):
        """WPT tests are named with one, and the file on disk has no `?`."""
        missing = self._run(["fast/dom/Comment/basic.html?variant=1"], present=["fast/dom/Comment/basic.html"])
        self.assertEqual(missing, [])

    def test_the_whole_suite_with_no_paths_is_not_a_missing_path(self):
        """No paths at all needs no round trip -- `missing_layout_paths` never calls `runner`."""
        def runner(argv):
            raise AssertionError("no paths named -- nothing should have been asked")
        self.assertEqual(CMD_TEST.missing_layout_paths(runner, "/src/WebKit", []), [])

    def test_a_runner_that_cannot_answer_is_not_read_as_every_path_missing(self):
        """`target.exec` failing outright (the workspace unreachable) is not evidence
        a path is missing -- that would refuse a run for the wrong reason."""
        self.assertEqual(CMD_TEST.missing_layout_paths(lambda argv: Result(1, "", "unreachable"),
                                                        "/src/WebKit", ["fast/gone.html"]), [])


if __name__ == "__main__":
    unittest.main()
