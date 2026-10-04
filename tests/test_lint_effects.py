"""lint.effects_through_machine: a process, a file write, a removal or an exec
in lib/wk or a Python command goes through lib/wk/machine.py or lib/wk/act.py,
the one path `--dry-run` intercepts. A file that acts directly is named below,
in NOT_STATE with why that is no state change.

Run: python3 tests/run.py --lint -k test_lint_effects
"""
TIER = "lint"
import re
import unittest

from tests.support import REPO

SEAM = {"lib/wk/machine.py", "lib/wk/act.py"}
EFFECT = re.compile(
    r"\bsubprocess\.(run|Popen|call|check_call|check_output)\("
    r"|\bos\.(remove|unlink|rename|replace|exec\w*|system|rmdir|makedirs|mkdir|symlink|chmod|kill|truncate)\("
    r"|\bshutil\.(rmtree|copy\w*|move)\("
    r"|\bopen\([^)]*,\s*[\"'][wax]b?\+?[\"']"
    r"|\.write_(text|bytes)\(|\.(unlink|rmdir|symlink_to|touch)\(")

NOT_STATE = {
    "lib/wk/dispatch.py": "the dispatcher execs into the command it resolved, and probes for it",
    "lib/wk/sysimage/task.py": "a stage's process execs into its build tool; the stage is the act",
    "lib/wk/lock.py": "a lock is process coordination, not state (CLAUDE.md rule 4)",
    "lib/wk/job.py": "the job's own log, opened only past its dry-run check",
    "lib/wk/targets.py": "the ProxyCommand exec that is the route",
    "lib/wk/tools.py": "removes its own temporary bundle",
    "lib/wk/boot/mac.py": "removes its own temporary tar",
    "lib/wk/doctor.py": "probes: doctor is read-only",
    "lib/wk/priv.py": "asks `sudo -l` what it lists",
    "lib/wk/status.py": "a checksum read",
    "lib/wk/statusview.py": "the --html page asked for",
    "lib/wk/slot.py": "`python3 -m wk.slot`: a step's tool writing the output its caller named",
    "lib/wk/mac.py": "`python3 -`: the program a Machine runs on the Mac (autorun's `sudo wkmac display-mode --declare`); the effect is that act_run",
    "lib/wk/pgo.py": "`python3 -m wk.pgo`: a step's tool writing the output its caller named",
    "lib/wk/bench/cli.py": "copies into its own temporary directory",
    "cmd/logs": "tail -f is a read",
    "cmd/selftest": "the test runner is the command",
}

def acting_files():
    files = list((REPO / "lib" / "wk").rglob("*.py"))
    files += [p for p in (REPO / "cmd").iterdir()
              if p.is_file() and p.read_text(errors="replace").startswith("#!/usr/bin/env python3")]
    found = set()
    for p in files:
        rel = str(p.relative_to(REPO))
        if rel in SEAM:
            continue
        if any(EFFECT.search(line) and not line.lstrip().startswith("#")
               for line in p.read_text(errors="replace").splitlines()):
            found.add(rel)
    return found


class TestEffectsGoThroughMachine(unittest.TestCase):
    def test_every_direct_effect_is_named(self):
        found = acting_files()
        self.assertEqual(sorted(found - set(NOT_STATE)), [],
                         "acts outside lib/wk/machine.py: route it through the Machine or act, or name why it is no state change")
        self.assertEqual(sorted(set(NOT_STATE) - found), [], "named here but no longer acts directly: remove it")


if __name__ == "__main__":
    unittest.main()
