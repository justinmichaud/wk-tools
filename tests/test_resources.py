"""Build accounting (lib/resources.sh): every build wk starts leaves a
budget record while it runs, the next build is sized against what is left,
and a machine spoken for refuses rather than oversubscribes. Records are
lock-shaped -- a dead holder's record is pruned on the next read.

Also the rule that makes a refused reading reach the person who ran the
command, in two halves that only work together (TestAReadingRefusalReaches
ItsCaller and TestEveryCallSiteTakesAReadingIntoAVariable below).

Run: python3 -m unittest tests.test_resources -v
"""
import os
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, shell_files, stub_path


class TestBuildRecords(WkTest):
    def _bash(self, script):
        env = {"XDG_STATE_HOME": str(self.tmp / "state"), "WK_AVAIL_MB": "100000",
               "WK_CGROUP_CORES": "64", "WK_MB_PER_JOB": "1000", "WK_BUILD_MACHINE": "testbox"}
        return bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/lib/resources.sh"\n' + script, env=env, timeout=60)

    def test_a_live_record_is_subtracted_and_a_dead_one_pruned(self):
        cp = self._bash('''
sleep 300 & live=$!
build_record "live build" 20 40000 "pid:$live"
build_record "dead build" 30 50000 "pid:99999999"
echo "reserved=$(build_reserved_mb) jobs=$(build_reserved_jobs)"
echo "records=$(ls "$(builds_dir)" | wc -l | tr -d ' ')"   # BSD wc pads its count
echo "next=$(build_jobs)"
kill $live
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reserved=40000 jobs=20", cp.stdout)
        self.assertIn("records=1", cp.stdout, "the dead holder's record is pruned on read")
        # (100000 - 40000) / 1000 = 60 by memory, 64 - 20 = 44 by cores
        self.assertIn("next=44", cp.stdout)

    def test_a_machine_spoken_for_refuses_without_force(self):
        cp = self._bash('''
sleep 300 & live=$!
trap 'kill $live' EXIT
build_record "big build" 60 98000 "pid:$live"
jobs=$(build_jobs); echo "jobs=$jobs"
( build_admit "a second build" "$jobs" ) && echo admitted || echo refused
''')
        self.assertIn("jobs=2", cp.stdout)
        self.assertIn("--force proceeds anyway", cp.stdout + cp.stderr)
        self.assertNotIn("admitted", cp.stdout)

    def test_another_machines_builds_do_not_count(self):
        cp = self._bash('''
sleep 300 & live=$!
WK_BUILD_MACHINE=elsewhere build_record "remote build" 60 98000 "pid:$live"
echo "reserved=$(build_reserved_mb)"
kill $live
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("reserved=0", cp.stdout)


class TestStoreFreeGb(WkTest):
    """store_free_gb (lib/resources.sh), run for real on the machine the
    test is on. Every other disk test stubs it, which is how a GNU-only
    `df -B1G --output=avail` survived: BSD df exits 64 on those options, and
    under `set -euo pipefail` that status came out of the assignment in
    disk_admit and ended `wk build` with no message at all -- every macOS
    build, container and guest alike."""

    def _bash(self, script, store=None):
        env = {"XDG_STATE_HOME": str(self.tmp / "state")}
        if store is not None:
            env["WK_STORE"] = str(store)
        return bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
                    f'. "{REPO}/lib/resources.sh"\n' + script, env=env, timeout=60)

    def test_it_answers_a_number_and_does_not_end_its_caller(self):
        cp = self._bash('free=$(store_free_gb); echo "free=[$free]"; echo alive',
                        store=self.tmp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("alive", cp.stdout, "the caller did not survive store_free_gb")
        free = cp.stdout.split("free=[")[1].split("]")[0]
        self.assertRegex(free, r"^[0-9]+$", f"not a plain integer: {free!r}")
        avail_k = int(subprocess.run(["df", "-Pk", str(self.tmp)], capture_output=True,
                                     text=True, check=True).stdout.splitlines()[1].split()[3])
        self.assertEqual(int(free), -(-avail_k // 1048576))

    def test_a_store_path_df_cannot_answer_for_is_not_a_refusal(self):
        """The contract disk_admit states: no answer is not evidence of a
        full disk. It has to hold as a *return*, not only as an empty
        string -- a failing df must not take the build with it."""
        cp = self._bash('free=$(store_free_gb); echo "free=[$free]"\n'
                        'disk_admit "this build" 60 && echo admitted',
                        store=self.tmp / "no" / "such" / "path")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)


class TestDiskAdmit(WkTest):
    """disk_admit (lib/resources.sh): a build refuses before it starts when
    the store's filesystem cannot take it, so nobody has to read `wk disk`
    first. build_admit asks on the way through, which is what puts the check
    on every build path -- `wk build`, `wk test`, and both image builders."""

    def _bash(self, script, free_gb):
        env = {"XDG_STATE_HOME": str(self.tmp / "state"),
               "WK_AVAIL_MB": "100000", "WK_CGROUP_CORES": "64",
               "WK_BUILD_MACHINE": "testbox", "FREE_GB": str(free_gb)}
        # store_free_gb is what df answers; stubbed so the test does not
        # depend on the machine it runs on.
        pre = (f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
               f'. "{REPO}/lib/resources.sh"\n'
               'store_free_gb() { printf "%s" "$FREE_GB"; }\n')
        return bash(pre + script, env=env, timeout=60)

    def test_it_refuses_when_the_disk_cannot_take_the_build(self):
        cp = self._bash('( disk_admit "this image build" 60 ) && echo admitted || echo refused', 40)
        self.assertIn("refused", cp.stdout)
        self.assertIn("40 GB free", cp.stdout + cp.stderr)
        self.assertIn("wk gc", cp.stdout + cp.stderr, "the refusal names the reclaim")
        self.assertNotIn("admitted", cp.stdout)

    def test_it_admits_when_there_is_room(self):
        cp = self._bash('disk_admit "this image build" 60 && echo admitted', 61)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)

    def test_no_answer_from_df_is_not_read_as_a_full_disk(self):
        cp = self._bash('disk_admit "this build" 60 && echo admitted', "")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)

    def test_a_slot_build_is_not_charged_the_whole_image_s_figure(self):
        # image/yocto.sh sizes per stage: the webkit stage is one cmake tree
        # against a toolchain already on disk, not a whole distribution. A
        # disk that can hold several slot builds must not refuse one.
        cp = self._bash('disk_admit "this WebKit cross build" "$WK_BUILD_DISK_GB" && echo admitted', 30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("admitted", cp.stdout)

    def test_every_build_path_asks_because_build_admit_does(self):
        # No running builds at all: the memory half returns early, and the
        # disk half must still have been asked.
        cp = self._bash('( build_admit "this build" 64 60 ) && echo admitted || echo refused', 10)
        self.assertIn("refused", cp.stdout)
        self.assertIn("10 GB free", cp.stdout + cp.stderr)


# lib/resources.sh's readings: what a refusal has to survive.
DEF = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PARAM = re.compile(r"\$\{[^{}]*\}")

# A reading belongs on the right of an assignment and nowhere else. `local v`
# ahead of it and a `|| ...` after it are that same shape; a case arm or a
# second assignment on the line is still one simple command.
ASSIGNED = re.compile(r"(?:^|[;{)]|&&|\|\||\bthen\b|\bdo\b|\belse\b"
                      r"|\bif\b|\belif\b|\bwhile\b|\buntil\b)\s*"
                      r"(?:local\s+|export\s+|declare\s+-\w+\s+)?[A-Za-z_][A-Za-z0-9_]*=$")
# A trailing backslash too: `if a=$(...) \` continues onto the next line, where the
# status is still the condition's.
SEPARATED = re.compile(r"^\s*($|;|\|\||&&|#|\\\s*$)")


def shell_functions(text):
    """{name: body} for the house style -- `name() {` opening a line and a
    closing `}` alone on one, or the whole function on one line."""
    out, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        m = DEF.match(lines[i])
        if m:
            rest = PARAM.sub("", lines[i][m.end():])
            if "}" in rest:
                out[m.group(1)] = lines[i][m.end():]
            else:
                j = i + 1
                while j < len(lines) and lines[j] != "}":
                    j += 1
                out[m.group(1)] = "\n".join(lines[i + 1:j])
                i = j
        i += 1
    return out


# One target driver per file, closed over on its own: every driver defines
# `t_cores`, so a set built from all of them at once keeps whichever file was
# read last and loses the wrappers of the rest.
WRAPPER_FILES = ("lib/target.sh", "targets/vm.sh", "targets/container.sh",
                 "targets/local.sh", "targets/remote.sh")


def _closure(funcs, named):
    while True:
        more = {f for f, b in funcs.items() if set(TOKEN.findall(b)) & named}
        if more <= named:
            return named
        named |= more


def readings():
    """Every function that can refuse: the lib/resources.sh ones that die or
    put up a barrier, whatever reaches one of those, and the target drivers'
    wrappers over them -- `t_cores` is a reading as surely as `host_cores` is,
    and a refusal it takes discards the same way. Derived from the files rather
    than listed here, so a new reading is covered the day it is written."""
    funcs = shell_functions((REPO / "lib" / "resources.sh").read_text())
    named = _closure(funcs, {f for f, b in funcs.items()
                             if re.search(r"\b(die|barrier|_require_reading)\b", b)})
    out = set(named)
    for rel in WRAPPER_FILES:
        out |= _closure(shell_functions((REPO / rel).read_text()), set(named))
    return out


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
    `WK_MB_PER_JOB=2560 build_jobs` keeps build_jobs, and the build_jobs in
    `x=$(printf %s "$(build_jobs)")` belongs to the inner one."""
    while True:
        cut = re.sub(r"\$\(\((?:[^()]|\([^()]*\))*\)\)|\$\([^()$]*\)", " ", text)
        if cut == text:
            return text
        text = cut


def reading_in_a_word_in(text, rel="<text>"):
    """The audit over one piece of shell, so the rule itself is testable."""
    names = readings()
    out = []
    if True:
        for start, end, inner in substitutions(text):
            if not set(TOKEN.findall(strip_nested(inner))) & names:
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
                      "lib/resources.sh into a word, where its refusal is "
                      "discarded. Each wants the reading on a line of its "
                      "own -- `v=$(...)`, then use $v:\n" + "\n".join(wrong))

    def test_the_readings_are_found_from_the_file_that_defines_them(self):
        """The audit above is worth nothing if the set is empty or has lost
        the composite readings, which is what a rename or a moved function
        would do to it."""
        names = readings()
        self.assertLessEqual(
            {"host_cores", "host_mem_mb", "host_load", "describe_cores",
             "avail_mem_mb", "envelope_cores", "envelope_mem_mb",
             "build_jobs", "explain_jobs", "build_admit", "disk_admit"},
            names)
        self.assertNotIn("store_free_gb", names,
                         "store_free_gb answers nothing rather than refusing")

    def test_the_drivers_wrappers_over_a_reading_are_in_the_set(self):
        """The same rule one call away: a driver's own number ends in a
        reading, so interpolating it discards the same refusal. The dynamic
        half of this pair is TestAReadingRefusalReachesItsCaller's
        test_it_walks_out_through_the_target_drivers_wrappers."""
        self.assertLessEqual({"t_cores", "t_mem_mb", "t_load",
                              "_vm_cpus", "_vm_mem_mb", "_base_cpus", "_base_mem_mb"},
                             readings())

    def test_a_condition_that_tests_the_assignment_is_not_flagged(self):
        """`if v=$(reading); then` keeps the refusal: the status is the
        condition. Flagging it would push callers into hiding it."""
        self.assertEqual([], reading_in_a_word_in(
            'if cores=$(t_cores) && mem=$(t_mem_mb) \\\n   && [ -n "$cores" ]; then :; fi\n'))

    def test_a_reading_interpolated_into_a_word_is_flagged(self):
        """The discriminating half: without it the audit above passes on a
        tree where nothing is checked at all."""
        self.assertEqual(1, len(reading_in_a_word_in('echo "jobs=$(t_cores)"\n')))


class TestAReadingRefusalReachesItsCaller(WkTest):
    """The other half. Bash does not inherit errexit into a command
    substitution, so the reading envelope_cores itself takes from host_cores
    fails without ending envelope_cores: it runs on with an empty value and
    answers 1, 0 or a negative envelope with status 0, and no assignment at
    the call site can tell. Every reading a reader takes therefore carries
    `|| return $?`, and the refusal walks out through every wrapper to the
    person who ran the command."""

    # A machine that will not say how many cores or how much memory it has:
    # nproc/awk are what a Linux reading takes, sysctl a macOS one -- deaf on
    # both, or the fake is only deaf on the platform the suite is not running.
    DEAF = {"nproc": "exit 1", "awk": "exit 1", "sysctl": "exit 1"}

    COMPOSITE = ("envelope_cores", "envelope_mem_mb", "avail_mem_mb",
                 "build_jobs", "explain_jobs", "describe_cores")

    def _res(self, script, stubs=None, env=None):
        e = {"XDG_STATE_HOME": str(self.tmp / "state"),
             "WK_STORE": str(self.tmp / "store"),
             "WK_TEST_CGROUP": str(self.tmp / "no-such-cgroup")}
        e.update(env or {})
        body = (f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
                f'. "{REPO}/lib/resources.sh"\n'
                '_cgroup_mem_max() { echo "$WK_TEST_CGROUP"; }\n' + script)
        with stub_path(stubs if stubs is not None else self.DEAF) as binp:
            e["PATH"] = f"{binp}:{os.environ['PATH']}"
            return bash(body, env=e)

    def test_a_reader_that_reads_through_another_one_still_refuses(self):
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$({name}); echo "SURVIVED [$v]"')
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertNotIn("SURVIVED", cp.stdout,
                                 f"{name} answered a machine that said nothing")
                self.assertIn("Every job count and memory envelope is sized from it",
                              cp.stderr)
                self.assertNotIn("syntax error", cp.stderr)

    def test_the_readings_still_answer_a_machine_that_does_reply(self):
        """The same six against the real machine: `|| return $?` must not
        turn a reading that worked into a refusal."""
        for name in self.COMPOSITE:
            with self.subTest(reading=name):
                cp = self._res(f'v=$({name}); echo "ANSWERED [$v]"', stubs={})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertRegex(cp.stdout, r"ANSWERED \[[0-9]")

    def test_it_walks_out_through_the_target_drivers_wrappers(self):
        """The vm driver's overridable numbers (_vm_cpus, _base_mem_mb, and
        t_cores/t_mem_mb over them) end in a reading, so a workspace is
        never sized from a machine that would not answer."""
        driver = (f'. "{REPO}/lib/store.sh"\n. "{REPO}/lib/target.sh"\n'
                  'load_target vm >/dev/null 2>&1\n')
        for name in ("_vm_cpus", "_vm_mem_mb", "_base_cpus", "_base_mem_mb",
                     "t_cores wk-test", "t_mem_mb wk-test"):
            with self.subTest(wrapper=name):
                cp = self._res(driver + f'v=$({name}); echo "SURVIVED [$v]"',
                               stubs={**self.DEAF, "tart": "echo '{}'"},
                               env={"WK_ROOT": str(REPO)})
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertNotIn("SURVIVED", cp.stdout)

    def test_an_override_answers_without_asking_the_machine(self):
        """WK_VM_CPUS and friends are a person's choice, so they stand on a
        machine whose own reading is unavailable."""
        cp = self._res(f'. "{REPO}/lib/store.sh"\n. "{REPO}/lib/target.sh"\n'
                       'load_target vm >/dev/null 2>&1\n'
                       'printf "%s %s\\n" "$(_vm_cpus)" "$(_base_mem_mb)"',
                       env={"WK_ROOT": str(REPO), "WK_VM_CPUS": "7",
                            "WK_VM_BASE_MEM_MB": "4444"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("7 4444", cp.stdout.strip())


if __name__ == "__main__":
    unittest.main()
