"""lint.effects_through_machine: a process, a file write, a removal or an exec"""
TIER = "lint"
import re
import unittest

from tests.support import REPO

SEAM = {"lib/wk/machine.py"}
EFFECT = re.compile(
    r"\bsubprocess\.(run|Popen|call|check_call|check_output)\("
    r"|\bos\.(remove|unlink|rename|replace|exec\w*|system|rmdir|makedirs|mkdir|symlink|chmod|kill|truncate)\("
    r"|\bshutil\.(rmtree|copy\w*|move)\("
    r"|\bopen\([^)]*,\s*[\"'][wax]b?\+?[\"']"
    r"|\.write_(text|bytes)\(|\.(unlink|rmdir|symlink_to|touch)\(")

NOT_STATE = {
    "lib/wk/dispatch.py": "the dispatcher execs into the command it resolved, and probes for it",
    "lib/wk/job.py": "the job's own log, opened only past its dry-run check",
    "lib/wk/places.py": "the ProxyCommand exec that is the route",
    "lib/wk/tools.py": "removes its own temporary bundle",
    "lib/wk/boot/mac.py": "removes its own temporary tar",
    "lib/wk/statusview.py": "the --html page asked for",
    "lib/wk/slot.py": "`python3 -m wk.slot`: a step's tool writing the output its caller named",
    "lib/wk/mac.py": "`python3 -`: the program a Machine runs on the Mac (autorun's `sudo wkmac display-mode --declare`); the effect is that act_run",
    "lib/wk/pgo.py": "`python3 -m wk.pgo`: a step's tool writing the output its caller named",
    "lib/wk/bench/cli.py": "copies into its own temporary directory",
}

PROCESS = re.compile(r"\bsubprocess\.(run|Popen|call|check_call|check_output)\b|\bos\.(exec\w*|system|spawn\w*|posix_spawn\w*|popen)\(")

FORCED = {
    "lib/wk/dispatch.py": "the dispatcher starts the command it resolved under --dry-run too, since that command is what honours it",
    "lib/wk/places.py": "the ProxyCommand exec is the transport a dry run's reads also travel",
    "lib/wk/mac.py": "a program sent whole to a Mac's `python3 -`, where no wk package is importable",
}


def python_files(*tops):
    files = [p for t in tops for p in (REPO / t).rglob("*.py")]
    return files + [p for p in (REPO / "cmd").iterdir()
                    if p.is_file() and p.read_text(errors="replace").startswith("#!/usr/bin/env python3")]


def files_matching(pattern, files):
    found = set()
    for p in files:
        rel = str(p.relative_to(REPO))
        if rel in SEAM:
            continue
        if any(pattern.search(line) and not line.lstrip().startswith("#")
               for line in p.read_text(errors="replace").splitlines()):
            found.add(rel)
    return found


class TestEffectsGoThroughMachine(unittest.TestCase):
    def test_every_direct_effect_is_named(self):
        found = files_matching(EFFECT, python_files("lib/wk"))
        self.assertEqual(sorted(found - set(NOT_STATE)), [],
                         "acts outside lib/wk/machine.py: route it through the Machine or act, or name why it is no state change")
        self.assertEqual(sorted(set(NOT_STATE) - found), [], "named here but no longer acts directly: remove it")

    def test_every_process_starts_through_the_machine(self):
        found = files_matching(PROCESS, python_files("lib"))
        self.assertEqual(sorted(found - set(FORCED)), [],
                         "starts a process outside lib/wk/machine.py: use its run, act_run, run_tty, spawn, start or exec")
        self.assertEqual(sorted(set(FORCED) - found), [], "named here but no longer starts a process directly: remove it")


if __name__ == "__main__":
    unittest.main()
