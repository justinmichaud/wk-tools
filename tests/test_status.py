"""`wk status` rendering and `wk push status --all` -- regression tests for:

  1. a machine's load and free memory appear per machine, in text and json,
     never a fabricated number for one that did not answer (lib/status-view.py)
  2. the fleet's re-provisioning line renders whatever text the record carries,
     including "missing <FIELD>" for a conf that has nothing to compose a
     recipe from -- never a guess (lib/status-view.py)
  6. one merged document feeds every view: a field present in text is present
     in json (lib/status-view.py)
  5. `wk push status --all` prints one line per configured machine, or none
     when none are configured -- never nothing while machines exist (cmd/push)

Run: python3 -m unittest tests.test_status -v
"""
import json
import subprocess
import unittest

from tests.support import REPO, WkTest, bash

STATUS_VIEW = REPO / "lib" / "status-view.py"


def render(records, mode):
    """python3 lib/status-view.py <mode> <recordsfile>, on a synthetic
    JSON-lines file -- the same input `wk status --records` writes and the
    same renderer `wk status` uses, so this is exactly what a person or an
    agent reading `wk status` sees, with no real machine required."""
    path = None
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, dir="/tmp"
    ) as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        path = fh.name
    try:
        cp = subprocess.run(
            ["python3", str(STATUS_VIEW), mode, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp
    finally:
        import os

        os.unlink(path)


def machine_rec(name, **extra):
    r = {"kind": "machine", "name": name}
    r.update(extra)
    return r


class TestLoadLine(unittest.TestCase):
    """Defect 1: each machine's load and free memory, never invented."""

    def test_text_shows_load_and_free_memory(self):
        recs = [
            machine_rec("buildbox4"),
            {
                "kind": "capacity",
                "machine": "buildbox4",
                "cores": "128",
                "load": "8",
                "free_mb": "170000",
                "mem_mb": "196000",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("buildbox4", out)
        self.assertIn("8", out)
        self.assertIn("128 cores", out)
        self.assertIn("free", out)

    def test_json_carries_the_same_capacity_record_as_text(self):
        """One document, three views (defect 6): the field text renders from
        is the same field json exposes."""
        recs = [
            machine_rec("moose"),
            {
                "kind": "capacity",
                "machine": "moose",
                "cores": "80",
                "load": "3",
                "free_mb": "121000",
            },
            {"kind": "exit", "code": 0},
        ]
        text_out = render(recs, "text").stdout
        json_out = json.loads(render(recs, "json").stdout)
        self.assertIn("80 cores", text_out)
        moose = next(m for m in json_out["machines"] if m["name"] == "moose")
        cap = moose["capacity"][0]
        self.assertEqual(cap["cores"], "80")
        self.assertEqual(cap["load"], "3")
        self.assertEqual(cap["free_mb"], "121000")

    def test_a_machine_that_did_not_answer_says_so_not_a_number(self):
        """A capacity probe that failed carries a note and no cores/load/
        free_mb -- the renderer must say so, never print a blank a reader
        could mistake for zero."""
        recs = [
            machine_rec("devbox-arm64-2"),
            {
                "kind": "capacity",
                "machine": "devbox-arm64-2",
                "note": "could not measure load/memory on devbox-arm64-2",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("could not measure load/memory on devbox-arm64-2", out)
        # No invented cores/load figure sits next to the note.
        self.assertNotIn("of  cores", out)

    def test_no_capacity_record_at_all_is_silence_not_a_zero(self):
        """A machine nobody asked about (no capacity record emitted) gets no
        load line at all -- not a fabricated 0."""
        recs = [machine_rec("quiet-machine"), {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("quiet-machine", out)
        self.assertNotIn("0 of", out)


class TestReprovisionLine(unittest.TestCase):
    """Defect 2: the fleet's re-provisioning recipe, and a missing field
    that says so rather than guessing."""

    def test_text_shows_the_recipe_from_the_record(self):
        recs = [
            {
                "kind": "fleet",
                "machine": "rpi3",
                "role": "bench-device",
                "mode": "base image -- not a bench system",
                "media": "SD card",
                "reprovision": "wk sysimage build webkit-2.52-yocto-rpi3-32\n"
                "    in a workspace; hours\n"
                "wk boot rpi3",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("re-provisioning", out)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi3-32", out)
        self.assertIn("wk boot rpi3", out)

    def test_a_device_missing_mach_profile_renders_the_missing_field_not_a_guess(self):
        """A conf with no NODE_PROFILE has nothing to compose a command
        from (cmd/status's _fleet_probe guards this before ever calling a
        driver's b_reprovision); the record says which field is missing and
        the renderer must show exactly that, never a made-up profile name."""
        recs = [
            {
                "kind": "fleet",
                "machine": "newdevice",
                "role": "bench-device",
                "mode": "unreachable",
                "media": "unknown",
                "reprovision": "missing NODE_PROFILE in boot/machines/newdevice.conf"
                " -- nothing to compose a recipe from",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("missing NODE_PROFILE in boot/machines/newdevice.conf", out)
        # Nothing invented in its place: no 'wk sysimage build' line for a
        # profile that was never named.
        self.assertNotIn("wk sysimage build newdevice", out)

    def test_the_by_role_sample_command_differs_per_role(self):
        recs = [
            {
                "kind": "fleet",
                "machine": "rpi4",
                "role": "bench-device",
                "mode": "host mode",
                "media": "usb stick",
                "reprovision": "wk sysimage build p\nwk boot rpi4",
            },
            {
                "kind": "fleet",
                "machine": "rpi5",
                "role": "workstation",
                "mode": "host mode",
                "media": "nvme",
                "reprovision": "wk sysimage build p2\nwk boot rpi5",
            },
            {"kind": "bridge", "name": "some-bridge", "device": "d", "segment": "s"},
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("by role", out)
        self.assertIn("a rescue system", out)
        self.assertIn("a bench system", out)
        self.assertIn("a workstation", out)
        self.assertIn("a tailnet bridge", out)


class TestPushStatusAll(WkTest):
    """Defect 5: `wk push status --all` prints one line per machine (never
    nothing while machines exist). `--all` means all: this machine holds a
    store too, and answers for it through its own `wk`."""

    def _this_machine(self):
        return bash(". lib/common.sh; wk_machine_name", timeout=30).stdout.strip()

    def _configured_machines(self):
        """The same list `for_each_machine` (lib/target.sh) walks: every
        target_all entry except container/vm/local. cmd/push asks this
        machine separately, so the rows are those plus this one."""
        cp = bash(
            """
            . lib/common.sh
            . lib/store.sh
            . lib/target.sh
            for t in $(target_all 2>/dev/null); do
                case "$t" in container|vm|local) continue ;; esac
                echo "$t"
            done
            """,
            timeout=30,
        )
        return [l for l in cp.stdout.splitlines() if l.strip()]

    def test_one_line_per_machine_including_this_one(self):
        here = self._this_machine()
        expected = set(self._configured_machines()) | {here}
        try:
            cp = self.run_wk("push", "status", "--all", timeout=180)
        except subprocess.TimeoutExpired:
            self.skipTest("no route to the configured machines from here")

        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        seen = {l.split()[0] for l in lines}
        self.assertIn(here, seen, "--all skipped the machine it was typed on")
        self.assertEqual(
            seen,
            expected,
            "wk push status --all must answer for every machine, "
            "not print nothing while machines exist",
        )
        # Exit status is meaningful (cmd/push's own 0/1/4), never silently 0
        # while a machine reported it holds no keys at all.
        self.assertIn(cp.returncode, (0, 1, 4))






class TestTasksOfOneWorkspace(WkTest):
    """`wk status <ws>` reports that workspace, so the task block reports that
    workspace's tasks: `report_tasks` (cmd/status) takes the name `collect`
    resolved and skips every record belonging to another workspace. A bare
    `wk status` passes no name and reports them all."""

    STUBS = """. "{repo}/lib/common.sh"
. "{repo}/lib/task.sh"
rec_start() {{ :; }}
rec_set()   {{ printf '  %s=%s\\n' "$1" "$2"; }}
rec_opt()   {{ [ -z "${{2:-}}" ] || rec_set "$1" "$2"; }}
rec_json()  {{ rec_set "$1" "$2"; }}
rec_emit()  {{ :; }}
note() {{ :; }}
note_warn() {{ :; }}
bump() {{ :; }}
_jesc() {{ printf '%s' "$1"; }}
_progress_line() {{ printf 'compiling\\n'; }}
first_error() {{ :; }}
"""

    def setUp(self):
        super().setUp()
        self.store = str(self.tmp / "store")
        cp = bash('. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n'
                  'task_begin build here ws1 "wk build ws1 --kill" /nolog compile >/dev/null\n'
                  'task_begin test here ws2 "^C where it runs" /nolog jsc >/dev/null\n'
                  % (REPO, REPO), env={"WK_STORE": self.store})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def _names_reported(self, *args):
        """The `name` field of every task record `report_tasks` emits, with
        the record writer stubbed to plain lines: what is under test is which
        records it walks, not the JSON encoder every report_* shares."""
        lift = subprocess.run(["sed", "-n", "/^report_tasks()/,/^}/p",
                               str(REPO / "cmd" / "status")],
                              capture_output=True, text=True, check=True).stdout
        self.assertIn("only=", lift, "report_tasks() moved or takes no name")
        call = " ".join('"%s"' % a for a in args)
        cp = bash(self.STUBS.format(repo=REPO) + lift
                  + "\n_tasks_said=' '\nreport_tasks %s\n" % call,
                  env={"WK_STORE": self.store})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return [l.split("=", 1)[1] for l in cp.stdout.splitlines()
                if l.startswith("  name=")]

    def test_a_named_workspace_reports_only_its_own_tasks(self):
        self.assertEqual(self._names_reported("ws1"), ["ws1"])
        self.assertEqual(self._names_reported("ws2"), ["ws2"])

    def test_no_name_reports_every_task(self):
        self.assertEqual(sorted(self._names_reported()), ["ws1", "ws2"])

    def test_a_task_that_ended_as_asked_is_not_reported_at_all(self):
        """An `ok`, `cancelled`, `stopped` or `refused` record is history: what
        it produced is the report, so only a task still running or one that
        ended badly gets a block."""
        for word, reported in (("0", False), ("cancelled", False),
                               ("stopped", False), ("refused", False),
                               ("3", True), ("stalled", True)):
            with self.subTest(word=word):
                # A fresh record per word: the first verdict on a record
                # stands (lib/task.sh task_end), and task_begin is what
                # clears it.
                cp = bash('. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n'
                          'task_begin build here ws1 "wk build ws1 --kill" /nolog compile >/dev/null\n'
                          'task_end "$(task_find build ws1)" %s\n' % (REPO, REPO, word),
                          env={"WK_STORE": self.store})
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(self._names_reported("ws1"),
                                 ["ws1"] if reported else [])

    def _lines_reported(self, stubs="", *args):
        """Every field `report_tasks` emits, with extra stubs appended."""
        lift = subprocess.run(["sed", "-n", "/^report_tasks()/,/^}/p",
                               str(REPO / "cmd" / "status")],
                              capture_output=True, text=True, check=True).stdout
        call = " ".join('"%s"' % a for a in args)
        cp = bash(self.STUBS.format(repo=REPO) + stubs + lift
                  + "\n_tasks_said=' '\nreport_tasks %s\n" % call,
                  env={"WK_STORE": self.store}, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_a_task_whose_pid_is_in_a_workspace_is_not_asked_through_it(self):
        """This listing is read-only and must answer *about* a wedged
        workspace: a `t_exec` into one has no timeout of its own, so the
        verdict for a `target` record comes from the log's age instead
        (task_verdict's `log` reading)."""
        bash('. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n'
             'd=$(task_begin build target ws3 "wk build ws3 --kill" /nolog compile)\n'
             'task_pid "$d" 4242\n' % (REPO, REPO), env={"WK_STORE": self.store})
        marker = self.tmp / "t_exec-called"
        out = self._lines_reported(
            't_exec() { printf x >> "%s"; sleep 30; }\n'
            'ws_target() { printf container; }\n'
            'load_target() { :; }\n' % marker, "ws3")
        self.assertIn("state=running", out, out)
        self.assertFalse(marker.exists(), "it asked the workspace it was reporting on")

    def test_a_task_is_reported_on_the_machine_its_pid_is_on(self):
        """The record names the machine running the job (`t_task_put` rewrites
        it for a remote target), and that is where a reader has to look for
        the pid and the log -- not the machine whose store holds the record."""
        bash('. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n'
             'd=$(task_begin build target ws4 "wk build ws4 --kill" /nolog compile)\n'
             'task_pid "$d" 4242 farbox\n' % (REPO, REPO), env={"WK_STORE": self.store})
        out = self._lines_reported("", "ws4")
        self.assertIn("machine=farbox", out, out)

    def test_the_single_workspace_path_passes_the_name(self):
        """`report_target <target> [ws]` is the one caller that knows a name
        was asked for, and it hands it on."""
        self.assertIn('report_tasks "${2:-}"',
                      (REPO / "cmd" / "status").read_text())


class TestBenchTaskLine(unittest.TestCase):
    """A benchmark task is one `bench` record per task shown (cmd/status
    report_health): the task's name, its state coloured by the shared
    vocabulary, and the summary recomputed from its runs -- every running
    task, else the newest -- in text and json alike."""

    def _records(self):
        return [
            machine_rec("moose", host_self=True),
            {"kind": "bench", "machine": "moose", "task": "20260830T120000Z-wpe-pr1725",
             "path": "/store/bench/20260830T120000Z-wpe-pr1725", "state": "running",
             "summary": "3/10 runs ended, 3 ok, 0 failed, 1 round usable; now speedometer2.1 rpi3 pr1725",
             "subject": "A/B wpe:1725: afa2ed9e70 vs base 04abe09851 · rpi3 · speedometer2.1 · 5 rounds"},
            {"kind": "bench", "machine": "moose", "task": "20260830T130000Z-rpi4-base-vs-pr1725",
             "path": "/store/bench/20260830T130000Z-rpi4-base-vs-pr1725", "state": "incomplete",
             "summary": "2/6 runs ended, 1 ok, 1 failed, 0 rounds usable",
             "subject": "base vs pr1725 · rpi4 · speedometer2.1 · 3 rounds"},
        ]

    def test_text_names_every_task_with_state_and_summary(self):
        cp = render(self._records(), "text")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("20260830T120000Z-wpe-pr1725", cp.stdout)
        self.assertIn("running", cp.stdout)
        self.assertIn("now speedometer2.1 rpi3 pr1725", cp.stdout)
        self.assertIn("20260830T130000Z-rpi4-base-vs-pr1725", cp.stdout)
        self.assertIn("incomplete", cp.stdout)
        self.assertIn("/store/bench/20260830T120000Z-wpe-pr1725", cp.stdout)

    def test_json_carries_both_records(self):
        cp = render(self._records(), "json")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(cp.stdout)
        machines = doc["machines"] if isinstance(doc, dict) else doc
        found = json.dumps(machines)
        self.assertIn("20260830T120000Z-wpe-pr1725", found)
        self.assertIn("20260830T130000Z-rpi4-base-vs-pr1725", found)


if __name__ == "__main__":
    unittest.main()
