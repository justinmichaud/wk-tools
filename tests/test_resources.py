"""Build accounting (lib/wk/resources.py): the next build is sized against
what other builds have not spoken for, and a machine spoken for refuses rather
than oversubscribes. The records themselves are tests/test_wk_resources.py's.

Also the rule that makes a refused reading reach the person who ran the
command, in two halves that only work together (TestAReadingRefusalReaches
ItsCaller and TestEveryCallSiteTakesAReadingIntoAVariable below).

Run: python3 tests/run.py -k tests.test_resources
"""
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
from wk import resources, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402
from wk.sysimage import guestbase, yocto  # noqa: E402

from wk.act import RETRY_EXIT  # noqa: E402

MB_PER_JOB = yocto.WEBKIT_MB_PER_JOB


class TestTheAdmissions(unittest.TestCase):
    def budget(self):
        return resources.Budget(Fake(), {"WK_BUILD_MACHINE": "testbox"})

    def refusal(self, fn):
        with self.assertRaises(Refused) as cm, contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
        return cm.exception.status, err.getvalue()

    def test_a_second_build_is_refused_however_much_room_is_left(self):
        """One machine builds one thing at a time. A build that would still
        get its jobs is refused all the same: two sharing a machine take
        longer together than in turn, and each moves the other's numbers."""
        status, err = self.refusal(lambda: self.budget().admit("a second build", 60, [("one small build", 1, 1000)]))
        self.assertIn("one thing at a time", err)
        self.assertIn("one small build", err, "the refusal does not name what is already building")
        self.assertIn("--force proceeds anyway", err)
        # The refusal a scheduled step comes back to: another build ending is
        # what changes the answer, so lib/wk/sched.py puts the step back in the queue.
        self.assertEqual(RETRY_EXIT, status)

    def test_a_disk_refusal_is_no_rather_than_not_now(self):
        """A step ending does not give the filesystem its blocks back, so this
        one is a plain refusal and the scheduler does not come back to it."""
        status, err = self.refusal(lambda: self.budget().disk_admit("a build", 60, 1, "/s"))
        self.assertEqual(1, status, "a disk refusal asked the scheduler to retry it")
        self.assertIn("wk gc", err, "the refusal names the reclaim")

    def test_the_two_languages_agree_on_the_retry_status(self):
        """One protocol number, written in bash and in python: a test rather
        than a copy, since neither file can read the other's constant."""
        self.assertEqual(
            int(re.search(r"^WK_RETRY_EXIT=(\d+)",
                          (REPO / "lib" / "common.sh").read_text(), re.M).group(1)),
            RETRY_EXIT)


class TestTheDefaultsAreThePythons(WkTest):
    """The variables a bash caller reads (`$WK_RESERVE_MB`, `$WK_BUILD_DISK_GB`)
    are lib/wk/resources.py's figures, and one a caller set stands."""

    def test_the_bash_variables_are_the_python_constants(self):
        cp = bash(f'. "{REPO}/lib/common.sh"\nWK_RESERVE_MB=7\neval "$(wk_py wk.resources --os linux defaults)"\n'
                  'echo "$WK_RESERVE_MB $WK_RESERVE_CORES $WK_MB_PER_JOB $WK_BUILD_DISK_GB"')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        from wk.buildconf import DISK_GB
        self.assertEqual(cp.stdout.split(), ["7", str(resources.RESERVE_CORES), str(resources.MB_PER_JOB), str(DISK_GB)])


class TestTheLoadCoresAndJobCount(unittest.TestCase):
    """host_load, describe_cores and the composite job count, against a fake machine."""

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
        self.assertIn("the load average (sysctl vm.loadavg)", self.refusal(self.res("macos").host_load))
        self.assertIn("the load average (/proc/loadavg)", self.refusal(self.res("linux").host_load))

    def test_both_core_kinds_are_named_for_a_person_or_neither_is(self):
        """Apple silicon has two kinds of core and an Intel Mac one; half of the pair is a report with a hole in it."""
        both = [(["sysctl", "-n", "hw.perflevel0.logicalcpu"], "8\n"), (["sysctl", "-n", "hw.perflevel1.logicalcpu"], "4\n")]
        self.assertEqual(self.res("macos", answers=both).describe_cores(), "8 P + 4 E")
        self.assertEqual(self.res("macos", answers=[(["sysctl", "-n", "hw.ncpu"], "12\n")]).describe_cores(), "12 cores")
        self.assertIn("the efficiency core count", self.refusal(self.res("macos", answers=both[:1]).describe_cores))

    def test_the_job_count_is_the_memory_and_cores_not_spoken_for(self):
        env = {"WK_CGROUP_CORES": "64", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        res = self.res("linux", env)
        budget = resources.Budget(res.machine, env)
        self.assertEqual(resources.build_jobs(res, budget, [("live build", 20, 40000)]), 44)

    def test_a_polite_count_reads_this_machines_load_when_no_caller_measured_one(self):
        """WK_LOAD is a remote target's, measured by whoever could reach it;
        without one the machine is asked rather than assumed idle."""
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
    """The store's free space, read for real on the machine the test is on.
    Every other disk test stubs df, which is how a GNU-only `df -B1G
    --output=avail` survived: BSD df exits 64 on those options, and that ended
    `wk build` with no message at all -- every macOS build, container and
    guest alike."""

    def test_it_answers_the_one_spelling_both_dfs_have(self):
        free = resources.Budget(Local(), {}).free_gb(str(self.tmp))
        avail_k = int(subprocess.run(["df", "-Pk", str(self.tmp)], capture_output=True,
                                     text=True, check=True).stdout.splitlines()[1].split()[3])
        self.assertEqual(free, -(-avail_k // 1048576))

    def test_a_store_path_df_cannot_answer_for_is_not_a_refusal(self):
        """The contract disk_admit states: no answer is not evidence of a
        full disk, so a failing df must not take the build with it."""
        budget = resources.Budget(Local(), {})
        free = budget.free_gb(str(self.tmp / "no" / "such"))
        self.assertIsNone(free)
        budget.disk_admit("this build", 60, free, "nowhere")


class TestImageStageBudget(WkTest):
    """yocto.stage_budget (lib/wk/sysimage/yocto.py): what a stage puts on the books is
    what it uses -- a bitbake stage is the machine, a cross WebKit build is its
    own job count, the mix is one job. It sizes the job count and the memory
    watchdog's budget; whether a build may start at all is one per machine
    (Budget.admit), whatever it books."""

    def _budget(self, stage, machine_jobs=79, machine_mb=113000, webkit_jobs=8):
        return yocto.stage_budget(stage, machine_jobs, machine_mb, webkit_jobs)

    def test_a_bitbake_stage_books_the_machine(self):
        for stage in ("layers", "fetch", "image", "toolchain"):
            self.assertEqual(self._budget(stage), (79, 113000), stage)

    def test_a_slot_build_books_only_its_own_jobs(self):
        self.assertEqual(self._budget("webkit"), (8, 8 * MB_PER_JOB))

    def test_the_mix_books_one_job(self):
        self.assertEqual(self._budget("pgo-mix"), (1, MB_PER_JOB))

    def test_a_slot_build_leaves_jobs_and_an_image_build_leaves_none(self):
        # What the split is worth in the units Budget.jobs works in: a build
        # forced beside a booked slot build still gets jobs, and one forced
        # beside a booked bitbake stage gets almost none.
        def left(booked_mb):
            return resources.Budget(Fake(), {}).jobs(80, 113000, MB_PER_JOB, running=[("booked", 8, booked_mb)])

        self.assertGreaterEqual(left(8 * MB_PER_JOB), 4,
                                "a slot build books more of the machine than it uses")
        self.assertLess(left(113000), 4,
                        "a bitbake stage does not book the machine it uses")


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


# The verbs whose answer cannot refuse; every other one is a reading, so a new one is covered the day it
# is written, and TestTheExemptionsDoNotRefuse holds this list to the Python's behaviour.
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
    """`die` inside a command substitution kills that subshell and nothing
    else, so a reading taken into a word prints its refusal and hands the
    caller an empty string -- which then sizes a build, or reaches `$(( ))`
    as a syntax error frames away from the sysctl or /proc file that was
    missing. Taken into a variable of its own it is a simple command, and
    the failed assignment ends the caller.

    Measured over the tree, because a caller re-deciding this is a bug even
    while it happens to decide it correctly."""

    def test_no_reading_is_taken_into_a_word(self):
        wrong = reading_in_a_word()
        if wrong:
            self.fail(f"{len(wrong)} call site(s) take a reading from "
                      "lib/wk/resources.py into a word, where its refusal is "
                      "discarded. Each wants the reading on a line of its "
                      "own -- `v=$(...)`, then use $v:\n" + "\n".join(wrong))

    def test_the_readings_are_found_from_the_file_that_defines_them(self):
        """The audit above is worth nothing if the set is empty or has lost
        the composite readings, which is what a rename or a moved function
        would do to it."""
        names = readings()
        self.assertLessEqual({"host-mem-mb", "describe-cores", "envelope-cores", "envelope-mem-mb"}, names)
        self.assertNotIn("headless-marker", names, "a path refuses nothing")

    def test_a_condition_that_tests_the_assignment_is_not_flagged(self):
        """`if v=$(reading); then` keeps the refusal: the status is the
        condition. Flagging it would push callers into hiding it."""
        self.assertEqual([], reading_in_a_word_in(
            'if cores=$(wk_py wk.resources envelope-cores) && mem=$(wk_py wk.resources envelope-mem-mb) \\\n   && [ -n "$cores" ]; then :; fi\n'))

    def test_a_reading_interpolated_into_a_word_is_flagged(self):
        """The discriminating half: without it the audit above passes on a
        tree where nothing is checked at all."""
        self.assertEqual(1, len(reading_in_a_word_in('echo "jobs=$(wk_py wk.resources --os "$(wk_os)" envelope-cores)"\n')))


class TestAReadingRefusalReachesItsCaller(WkTest):
    """The other half: a composite reading refuses inside the Python, each
    call is one command whose status is that refusal, and it walks out through
    every wrapper to the person who ran the command."""

    # A machine that will not say how many cores it has: nproc is what a Linux
    # reading takes, sysctl a macOS one -- deaf on both.
    DEAF = {"nproc": "exit 1", "sysctl": "exit 1"}

    COMPOSITE = ("envelope-cores", "describe-cores")   # the memory readings are TestEnvelope's, over a fake /proc

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
                self.assertNotIn("SURVIVED", cp.stdout,
                                 f"{name} answered a machine that said nothing")
                self.assertIn("Every job count and memory envelope is sized from it",
                              cp.stderr)
                self.assertNotIn("syntax error", cp.stderr)

    def test_the_readings_still_answer_a_machine_that_does_reply(self):
        """The same readings against the real machine: `|| return $?` must not
        turn a reading that worked into a refusal."""
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$(wk_py wk.resources --os "$(wk_os)" {name}); echo "ANSWERED [$v]"', stubs={})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertRegex(cp.stdout, r"ANSWERED \[[0-9]")

    def _guest(self, env=None):
        """A vm target with tart present but never configured (`Vm.configured` answers None), so
        `Vm.cores`/`mem_mb` and `guestbase.Base.sizing` fall to WK_VM_* or the host reading -- the
        Python successors of _vm_cpus/_vm_mem_mb/_base_cpus/_base_mem_mb."""
        p = mock.patch.object(targets.Vm, "tart", lambda s: "/t/tart")
        p.start()
        self.addCleanup(p.stop)
        fake = Fake("here")
        vm = targets.Vm("vm", str(REPO), dict(env or {}), fake)
        return vm, guestbase.Base(vm), fake

    def test_it_walks_out_through_the_target_drivers_wrappers(self):
        """The vm driver's overridable numbers end in a reading, so a
        workspace is never sized from a machine that would not answer."""
        vm, base, fake = self._guest()
        with self.assertRaises(Refused):
            vm.cores("g")
        with self.assertRaises(Refused):
            vm.mem_mb("g")
        with self.assertRaises(Refused):
            base.sizing()

    def test_an_override_answers_without_asking_the_machine(self):
        """WK_VM_CPUS and WK_VM_BASE_MEM_MB are a person's choice, so they
        stand on a machine whose own reading is unavailable."""
        vm, _, fake = self._guest({"WK_VM_CPUS": "7"})
        self.assertEqual(vm.cores("g"), 7)
        self.assertNotIn(("run", ("sysctl", "-n", "hw.ncpu")), fake.effects)

        _, base, fake2 = self._guest({"WK_VM_BASE_MEM_MB": "4444"})
        fake2.answer(["sysctl", "-n", "hw.ncpu"], out="20\n")   # the un-overridden half of the pair still reads
        self.assertEqual(base.sizing()[1], "4444")
        self.assertNotIn(("run", ("sysctl", "-n", "hw.memsize")), fake2.effects)


if __name__ == "__main__":
    unittest.main()
