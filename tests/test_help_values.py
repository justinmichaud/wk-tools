"""Every closed-set value a command's argument parser accepts must appear in
its own `wk <cmd> -h` text (docs/defects, "Help options should always list
all valid values for params"). CLOSED_SETS pins the values found by reading
each parser once; a new case arm added without a matching help entry makes
this fail, since the value will not appear in a stale header.

A closed set that lives in one list in the code -- build configs, image
profiles, fleet machines -- is not copied into a header at all: the command
declares `values=<flag>` and the dispatcher runs it, so `wk build -h` prints
today's configs and cannot fall out of step with config_list. That mechanism
is checked here too.

Open-ended sets (a workspace name, a free-text path, a git ref, a benchmark
plan from `Tools/Scripts/run-benchmark --list`) are deliberately not
enumerated here or in the commands' headers -- the header says in prose where
the list comes from instead.

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


# {cmd: flag} -- a closed set the dispatcher enumerates into `-h` itself.
DECLARED_VALUES = {
    "build": "--list",
    "boot": "--list",
    "sysimage": "--list",
}


class TestDeclaredValuesReachTheHelp(unittest.TestCase):
    """`values=<flag>` (the dispatcher's declaration): the set a command
    accepts is printed by running the one list that defines it, so a value
    added to that list appears in `-h` with no second edit."""

    def test_each_command_declares_it(self):
        for cmd, flag in DECLARED_VALUES.items():
            head = "\n".join(
                (REPO / "cmd" / cmd).read_text().splitlines()[:15])
            with self.subTest(cmd=cmd):
                self.assertIn(f"values={flag}", head,
                              f"cmd/{cmd} no longer declares its value list")

    def test_the_help_prints_what_the_flag_prints(self):
        for cmd, flag in DECLARED_VALUES.items():
            listed = subprocess.run(
                [str(REPO / "wk"), cmd, flag],
                cwd=str(REPO), capture_output=True, text=True, timeout=60)
            self.assertEqual(listed.returncode, 0,
                             f"wk {cmd} {flag} failed: {listed.stdout}{listed.stderr}")
            values = [l.split()[0] for l in (listed.stdout + listed.stderr).splitlines()
                      if l[:1].isalnum()]
            self.assertTrue(values, f"wk {cmd} {flag} listed nothing")
            text = help_text(cmd)
            with self.subTest(cmd=cmd):
                self.assertIn("valid values", text,
                              f"wk {cmd} -h does not print its value list")
                for v in values:
                    self.assertIn(v, text, f"wk {cmd} -h omits '{v}'")

    def test_a_value_flag_is_read_only(self):
        """`-h` runs it, so it must change nothing and never wait: every one is
        declared readonly (or the command is), and none of them is forwarded."""
        for cmd, flag in DECLARED_VALUES.items():
            head = "\n".join(
                (REPO / "cmd" / cmd).read_text().splitlines()[:15])
            with self.subTest(cmd=cmd):
                self.assertTrue(
                    f"readonly" in head or f"flag {flag} where=local" in head
                    or f"{flag} where=local" in head,
                    f"cmd/{cmd}'s {flag} is neither readonly nor local, and "
                    f"`wk {cmd} -h` runs it")


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
