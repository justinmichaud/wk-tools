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

from tests.support import REPO, bash, scratch_dir, temp_store

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
    "profile": "--list",
    "pi": "--list",
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


class TestBenchListPlans(unittest.TestCase):
    """`wk bench --list` (docs/defects): the plan set is not a list in this
    repo -- it lives in the WebKit tree -- so it is not a DECLARED_VALUES
    entry above: running it for real asks podman about this machine's store
    (`where=store`), which the tests must never do, unlike build/boot/
    sysimage's --list, answered entirely out of this repo with `where=host`
    or `where=local`. Its declaration is checked directly, and its read of
    the store against a fake mirror this test builds -- the one read `wk
    bench --list` forwards to the podman VM for on a macOS host, and this
    suite never exercises through the podman machine itself."""

    def test_declares_values_readonly_and_store(self):
        head = "\n".join((REPO / "cmd" / "bench").read_text().splitlines()[:15])
        self.assertIn("values=--list", head,
                       "cmd/bench no longer declares its plan list")
        self.assertIn("flag --list where=store", head,
                       "cmd/bench --list no longer reads the store")
        self.assertIn("readonly --list", head,
                       "cmd/bench --list is not read-only, so it could start the podman VM")

    def test_help_ends_with_the_plans_or_the_one_honest_line(self):
        text = help_text("bench")
        self.assertIn("valid values (wk bench --list):", text,
                       "wk bench -h does not print its plan list")
        tail = text.split("valid values (wk bench --list):", 1)[1].strip()
        self.assertTrue(tail, "wk bench -h prints nothing after its value-list header")
        # Either real plan names (this host has a readable mirror) or the
        # one line naming where the list actually lives (it does not, here).
        self.assertTrue("no mirror at" in tail or tail.split(),
                         f"wk bench -h's plan list is empty: {tail!r}")

    def test_lists_plan_names_from_a_fake_mirror(self):
        with scratch_dir("wk-test-bench-src-") as src, temp_store() as store:
            plans = src / "Tools/Scripts/webkitpy/benchmark_runner/data/plans"
            plans.mkdir(parents=True)
            (plans / "speedometer3.1.plan").write_text("{}")
            (plans / "jetstream2.2.plan").write_text("{}")
            subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
            subprocess.run(["git", "-C", str(src), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(src), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(src), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "plans"], check=True)

            mirror = store["path"] / "git" / "WebKit.git"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--bare", str(src), str(mirror)], check=True)

            cp = bash(
                ". lib/common.sh; . lib/store.sh; . lib/bench.sh; bench_plan_list",
                env={"WK_STORE": store["WK_STORE"]},
            )
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(sorted(cp.stdout.split()),
                              ["jetstream2.2", "speedometer3.1"])


if __name__ == "__main__":
    unittest.main()
