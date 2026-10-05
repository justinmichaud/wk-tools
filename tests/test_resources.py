"""Build accounting (lib/wk/resources.py), and the rule that a refused reading reaches the caller."""
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, shell_files, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import places, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402
from wk.sysimage import guestbase, yocto  # noqa: E402

from wk.act import RETRY_EXIT  # noqa: E402

MB_PER_JOB = yocto.WEBKIT_MB_PER_JOB


class TestTheRetryStatus(unittest.TestCase):
    def test_the_two_languages_agree_on_the_retry_status(self):
        cp = bash(f'. "{REPO}/lib/common.sh"\necho "$WK_RETRY_EXIT"')
        self.assertEqual(cp.stdout.strip(), str(RETRY_EXIT), cp.stderr)


class TestTheDefaultsAreThePythons(WkTest):

    def test_the_bash_variables_are_the_python_constants(self):
        cp = bash(f'. "{REPO}/lib/common.sh"\nWK_RESERVE_MB=7\neval "$(wk_py wk.resources --os linux defaults)"\n'
                  'echo "$WK_RESERVE_MB $WK_RESERVE_CORES $WK_MB_PER_JOB $WK_BUILD_DISK_GB"')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        from wk.presets import DISK_GB
        self.assertEqual(cp.stdout.split(), ["7", str(resources.RESERVE_CORES), str(resources.MB_PER_JOB), str(DISK_GB)])


class TestTheLoadCoresAndJobCount(unittest.TestCase):

    def res(self, os_name, env=None, answers=(), files=None):
        m = Fake()
        for argv, out in answers:
            m.answer(argv, out=out)
        m.files.update(files or {})
        return resources.Resources(m, env or {}, os_name)

    def refusal(self, fn):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
        return err.getvalue()

    def test_a_load_average_is_whole_cores_from_either_kernel(self):
        mac = self.res("macos", answers=[(["sysctl", "-n", "vm.loadavg"], "{ 3.41 2.20 1.90 }\n")])
        linux = self.res("linux", files={"/proc/loadavg": "5.93 4.00 3.00 2/900 12345\n"})
        self.assertEqual((mac.host_load(), linux.host_load()), (3, 5))

    def test_a_load_average_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("vm.loadavg", self.refusal(self.res("macos").host_load))
        self.assertIn("/proc/loadavg", self.refusal(self.res("linux").host_load))

    def test_both_core_kinds_are_named_for_a_person_or_neither_is(self):
        both = [(["sysctl", "-n", "hw.perflevel0.logicalcpu"], "8\n"), (["sysctl", "-n", "hw.perflevel1.logicalcpu"], "4\n")]
        self.assertEqual(self.res("macos", answers=both).describe_cores(), "8 P + 4 E")
        self.assertEqual(self.res("macos", answers=[(["sysctl", "-n", "hw.ncpu"], "12\n")]).describe_cores(), "12 cores")
        self.refusal(self.res("macos", answers=both[:1]).describe_cores)

    def test_the_job_count_is_the_memory_and_cores_not_spoken_for(self):
        env = {"WK_CGROUP_CORES": "64", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        res = self.res("linux", env)
        budget = resources.Budget(res.machine, env)
        self.assertEqual(resources.build_jobs(res, budget, [("live build", 20, 40000)]), 44)

    def test_a_polite_count_reads_this_machines_load_when_no_caller_measured_one(self):
        env = {"WK_CGROUP_CORES": "12", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        res = self.res("macos", env, answers=[(["sysctl", "-n", "vm.loadavg"], "{ 3.41 2.20 1.90 }\n")])
        # 12 cores, load 3 spoken for, and never more than half a box.
        self.assertEqual(self.polite(res, env), 6)
        # A measured load stands, and with memory busy it is not read as a killed build's stale average.
        env = dict(env, WK_LOAD="9", WK_AVAIL_MB="8000")
        self.assertEqual(self.polite(self.res("macos", env), env), 3)

    def polite(self, res, env):
        return resources.Budget(res.machine, env).jobs(res.cores(), res.avail_mem_mb(), res.mb_per_job(), load=res.load())


class TestStoreFreeGb(WkTest):
    """The store's free space, read with this machine's real df."""

    def test_it_answers_the_one_spelling_both_dfs_have(self):
        free = resources.Budget(Local(), {}).free_gb(str(self.tmp))
        avail_k = int(subprocess.run(["df", "-Pk", str(self.tmp)], capture_output=True,
                                     text=True, check=True).stdout.splitlines()[1].split()[3])
        self.assertEqual(free, -(-avail_k // 1048576))

    def test_a_store_path_df_cannot_answer_for_is_not_a_refusal(self):
        budget = resources.Budget(Local(), {})
        free = budget.free_gb(str(self.tmp / "no" / "such"))
        self.assertIsNone(free)
        budget.disk_admit("this build", 60, free, "nowhere")


class TestImageStageBudget(unittest.TestCase):
    """yocto.stage_budget: a bitbake stage books the machine, a cross WebKit build its own jobs, the mix one job."""

    def test_each_stage_books_what_it_uses(self):
        for stage, want in (("layers", (79, 113000)), ("fetch", (79, 113000)), ("image", (79, 113000)),
                            ("toolchain", (79, 113000)), ("webkit", (8, 8 * MB_PER_JOB)), ("pgo-mix", (1, MB_PER_JOB))):
            self.assertEqual(yocto.stage_budget(stage, 79, 113000, 8), want, stage)

    def test_a_slot_build_leaves_jobs_and_an_image_build_leaves_none(self):
        def left(booked_mb):
            return resources.Budget(Fake(), {}).jobs(80, 113000, MB_PER_JOB, running=[("booked", 8, booked_mb)])
        self.assertGreaterEqual(left(8 * MB_PER_JOB), 4)
        self.assertLess(left(113000), 4)


# `wk_py wk.resources <verb>`'s readings: what a refusal has to survive.
VERB = re.compile(r"\bwk\.resources\b(.*)")
WORD = re.compile(r"[a-z][a-z-]*")

# A reading belongs on the right of an assignment and nowhere else. `local v`
# ahead of it and a `|| ...` after it are that same shape; a case arm or a
# second assignment on the line is still one simple command.
ASSIGNED = re.compile(r"(?:^|[;{)]|&&|\|\||\bthen\b|\bdo\b|\belse\b"
                      r"|\bif\b|\belif\b|\bwhile\b|\buntil\b)\s*"
                      r"(?:local\s+|export\s+|declare\s+-\w+\s+)?[A-Za-z_][A-Za-z0-9_]*=$")
# A trailing backslash too: `if a=$(...) \` continues onto the next line, where the
# status is still the condition's.
SEPARATED = re.compile(r"^\s*($|;|\|\||&&|#|\\\s*$)")


CANNOT_REFUSE = {"headless-marker", "defaults"}


def verbs():
    """Every verb lib/wk/resources.py answers."""
    return set(resources.READINGS)


def readings():
    """Every verb that can refuse."""
    return verbs() - CANNOT_REFUSE


class TestTheExemptionsDoNotRefuse(unittest.TestCase):
    def test_a_deaf_machine_answers_every_exempt_verb(self):
        self.assertLessEqual(CANNOT_REFUSE, verbs())
        for verb in CANNOT_REFUSE:
            with self.subTest(verb=verb), mock.patch("wk.machine.here", return_value=Fake()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(resources.main(["--os", "linux", verb], env={}), 0)


def substitutions(text):
    """(offset, end, inner) for every `$( ... )`, nesting included."""
    out, i = [], 0
    while True:
        i = text.find("$(", i)
        if i < 0:
            return out
        if text[i:i + 3] == "$((":
            i += 3
            continue
        depth, j = 0, i + 1
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append((i, j + 1, text[i + 2:j]))
        i += 2


def strip_nested(text):
    """The command a substitution runs, with its own substitutions removed:
    the one in `x=$(printf %s "$(wk_py wk.resources envelope-cores)")` belongs to the inner one."""
    while True:
        cut = re.sub(r"\$\(\((?:[^()]|\([^()]*\))*\)\)|\$\([^()$]*\)", " ", text)
        if cut == text:
            return text
        text = cut


def reading_in_a_word_in(text, rel="<text>"):
    """The audit over one piece of shell, so the rule itself is testable."""
    names = readings()
    out = []
    for start, end, inner in substitutions(text):
        if not set(WORD.findall(" ".join(VERB.findall(strip_nested(inner))))) & names:
            continue
        bol = text.rfind("\n", 0, start) + 1
        eol = text.find("\n", end)
        if ASSIGNED.search(text[bol:start]) \
           and SEPARATED.match(text[end:eol if eol >= 0 else len(text)]):
            continue
        out.append(f"  {rel}:{text.count(chr(10), 0, start) + 1}: "
                   f"{text[bol:end].strip()[:90]}")
    return out


def reading_in_a_word():
    """Every call site in the tree that takes a reading into a word."""
    out = []
    for path in shell_files():
        rel = str(path.relative_to(REPO))
        if rel.startswith("tests/"):
            continue
        out += reading_in_a_word_in(path.read_text(), rel)
    return out


class TestEveryCallSiteTakesAReadingIntoAVariable(unittest.TestCase):
    """A refusal inside `$(...)` kills only that subshell, so a reading belongs alone on the right of an assignment."""

    def test_no_reading_is_taken_into_a_word(self):
        wrong = reading_in_a_word()
        if wrong:
            self.fail(f"{len(wrong)} call site(s) take a reading from "
                      "lib/wk/resources.py into a word, where its refusal is "
                      "discarded. Each wants the reading on a line of its "
                      "own -- `v=$(...)`, then use $v:\n" + "\n".join(wrong))

    def test_the_readings_are_found_from_the_file_that_defines_them(self):
        names = readings()
        self.assertLessEqual({"host-mem-mb", "describe-cores", "envelope-cores", "envelope-mem-mb"}, names)
        self.assertNotIn("headless-marker", names, "a path refuses nothing")

    def test_a_condition_that_tests_the_assignment_is_not_flagged(self):
        self.assertEqual([], reading_in_a_word_in(
            'if cores=$(wk_py wk.resources envelope-cores) && mem=$(wk_py wk.resources envelope-mem-mb) \\\n   && [ -n "$cores" ]; then :; fi\n'))

    def test_a_reading_interpolated_into_a_word_is_flagged(self):
        self.assertEqual(1, len(reading_in_a_word_in('echo "jobs=$(wk_py wk.resources --os "$(wk_os)" envelope-cores)"\n')))


class TestAReadingRefusalReachesItsCaller(WkTest):
    DEAF = {"nproc": "exit 1", "sysctl": "exit 1"}

    COMPOSITE = ("envelope-cores", "describe-cores")

    def _res(self, script, stubs=None, env=None):
        e = {"XDG_STATE_HOME": str(self.tmp / "state"),
             "WK_STORE": str(self.tmp / "store"),
             }
        e.update(env or {})
        body = f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n' + script
        with stub_path(stubs if stubs is not None else self.DEAF) as binp:
            e["PATH"] = f"{binp}:{os.environ['PATH']}"
            return bash(body, env=e)

    def test_a_reader_that_reads_through_another_one_still_refuses(self):
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$(wk_py wk.resources --os "$(wk_os)" {name}); echo "SURVIVED [$v]"')
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertNotIn("SURVIVED", cp.stdout)
                self.assertNotIn("syntax error", cp.stderr)

    def test_the_readings_still_answer_a_machine_that_does_reply(self):
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$(wk_py wk.resources --os "$(wk_os)" {name}); echo "ANSWERED [$v]"', stubs={})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertRegex(cp.stdout, r"ANSWERED \[[0-9]")

    def _guest(self, env=None):
        """A vm place with tart present but never configured, so its sizing falls to WK_VM_* or the host reading."""
        p = mock.patch.object(places.Vm, "tart", lambda s: "/t/tart")
        p.start()
        self.addCleanup(p.stop)
        fake = Fake("here")
        vm = places.Vm("vm", str(REPO), dict(env or {}), fake)
        return vm, guestbase.Base(vm), fake

    def test_it_walks_out_through_the_target_drivers_wrappers(self):
        vm, base, fake = self._guest()
        with self.assertRaises(Refused):
            vm.cores("g")
        with self.assertRaises(Refused):
            vm.mem_mb("g")
        with self.assertRaises(Refused):
            base.sizing()

    def test_an_override_answers_without_asking_the_machine(self):
        vm, _, fake = self._guest({"WK_VM_CPUS": "7"})
        self.assertEqual(vm.cores("g"), 7)
        self.assertNotIn(("run", ("sysctl", "-n", "hw.ncpu")), fake.effects)

        _, base, fake2 = self._guest({"WK_VM_BASE_MEM_MB": "4444"})
        fake2.answer(["sysctl", "-n", "hw.ncpu"], out="20\n")
        self.assertEqual(base.sizing()[1], "4444")
        self.assertNotIn(("run", ("sysctl", "-n", "hw.memsize")), fake2.effects)


if __name__ == "__main__":
    unittest.main()
