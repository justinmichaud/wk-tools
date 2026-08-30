"""Every closed-set value a command's argument parser accepts must appear in
its own `wk <cmd> -h` text (docs/defects, "Help options should always list
all valid values for params"). CLOSED_SETS pins the values found by reading
each parser once; a new case arm added without a matching help entry makes
this fail, since the value will not appear in a stale header.

Open-ended sets (a workspace name, a machine/host name, a free-text path, a
git ref, a benchmark plan from `Tools/Scripts/run-benchmark --list`, a build
config from `wk build --list`) are deliberately not enumerated here or in the
commands' headers -- the header says in prose where the list comes from
instead.

Run: python3 -m unittest tests.test_help_values -v
"""
import subprocess
import unittest

from tests.support import REPO

# {cmd: {flag_or_positional_name: [valid, values, ...]}}
# Only sets that are genuinely closed -- read directly out of each command's
# own case statement or while-loop -- belong here.
CLOSED_SETS = {
    "push": {"action": ["on", "off", "status"]},
    "remote": {"verb": ["setup", "rm"]},
    "ab": {"--builder": ["buildroot", "yocto"], "--bits": ["32", "64"]},
    "completion": {"shell": ["bash", "zsh"]},
    "bridge": {"subverb": ["ls", "provision", "setup", "tailnet", "status", "rm"]},
}


def help_text(cmd):
    cp = subprocess.run(
        [str(REPO / "wk"), cmd, "-h"],
        cwd=str(REPO), capture_output=True, text=True, timeout=30,
    )
    return cp.stdout + cp.stderr


class TestHelpListsClosedSetValues(unittest.TestCase):
    def test_every_closed_set_value_appears_in_help(self):
        for cmd, params in CLOSED_SETS.items():
            text = help_text(cmd)
            for param, values in params.items():
                for v in values:
                    with self.subTest(cmd=cmd, param=param, value=v):
                        self.assertIn(v, text,
                                      f"wk {cmd} -h does not mention '{v}' "
                                      f"(a valid value for {param})")


if __name__ == "__main__":
    unittest.main()
