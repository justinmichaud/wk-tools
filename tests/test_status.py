"""`wk status` typed into worlds: a container workspace's row, the tasks its store records, its log and the readings its
build left; a fleet of a build box and a peer seen as one view. What no world reaches -- `--wait`, the order a stream
draws in, the colour of a terminal -- is at the bottom, with the reports run for real."""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import test_places_flows as places_flows
from tests.fakes import Fake, FakeClock, FakeDriver, FakeRegistry, FlowTest, Result
from tests.support import BLIND_FLEET, REPO, as_dispatched, clean_env, invocation, load_cmd, requires_container_place, run
from tests.test_wk_workspace import Flow

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch, fleet, places, status, statusview  # noqa: E402
from wk.act import Refused  # noqa: E402

CMD = load_cmd("status")
NONE = "  (no workspaces on it)"


def real(argv):
    r = subprocess.run(argv, capture_output=True, text=True)
    return Result(r.returncode, r.stdout, r.stderr)


def line(out, start):
    """The one line of `out` that starts with `start` once its indent is gone."""
    found = [l.rstrip() for l in out.splitlines() if l.strip().startswith(start)]
    if len(found) != 1:
        raise AssertionError("%d lines start with %r:\n%s" % (len(found), start, out))
    return found[0]


class Host(places_flows.World):
    """The machine holding container workspaces: a workspace's git probe answers `git`, or runs for real in the
    repository `repo`; the gates script runs for real with the checkout at `src`; `mute` workspaces never answer
    whether a pid lives."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        self.git, self.repo, self.mute = "", None, set()
        self.src = str(self.tmp / "src")

    def _in_ws(self, argv, f):
        script = argv[4] if len(argv) > 4 else ""
        if argv[2:4] == ["sh", "-c"] and "wsbase=" in script:
            return real(argv[2:-2] + [self.repo, argv[-1]]) if self.repo else Result(0, self.git)
        if argv[2:4] == ["bash", "-c"] and "SRC=" in script:
            return real(["bash", "-c", script.replace("/src/WebKit", self.src).replace("/opt/wk-tools", str(REPO))])
        if argv[2:4] == ["kill", "-0"] and argv[1] in self.mute:
            return Result(124)
        return super()._in_ws(argv, f)


class HostFlow(Flow):
    world_class = Host

    def setUp(self):
        super().setUp()
        self.w.make("ws")

    def status(self, *argv):
        self.w.effects = []
        return self.wk(self.w, "status", *argv)

    def doc(self, *argv):
        rc, out, _ = self.status("--json", *argv)
        return rc, json.loads(out)

    def tasks(self, *argv):
        _, doc = self.doc(*argv)
        return [t for m in doc["machines"] for t in m["tasks"]]

    def begin(self, name="ws", kind="build", **kw):
        return self.w.begin(kind, name, **kw)


class TestAWorkspaceRow(HostFlow):
    def test_its_row_is_its_state_branch_base_work_and_last_build_from_one_probe_inside_it(self):
        self.w.files[os.path.join(self.w.ws_dir(), "changes", ".git", "HEAD")] = "ref: refs/heads/feature\n"
        self.w.git = "dirty=2\nuntracked=1\nunpushed=3\nbehind=4\nahead=1\nwsbase=main\n"
        self.begin(pid=None).end(1)
        rc, out, _ = self.status()
        self.assertEqual((rc, line(out, "ws ")), (1, "    ws         running  feature ↓4 ↑1  main  3 unpushed, 2 dirty, +1 new  build=failed"))
        self.assertEqual([e[1][:2] if e[1][0] == "podman" else e[1][0] for e in self.w.effects if e[0] == "run"], [("podman", "ps"), ("podman", "inspect"), "env"])

    def test_one_nothing_is_known_of_has_no_branch_an_unknown_base_and_is_clean(self):
        rc, out, _ = self.status("ws")
        self.assertEqual((rc, line(out, "ws ")), (0, "    ws         running  -       ?     clean"))

    def test_a_machine_with_none_says_so_once(self):
        self.w.containers.clear()
        rc, out, _ = self.status()
        self.assertEqual((rc, out.count(NONE), line(out, "here")), (0, 1, "here   this machine"))

    def test_an_image_workspaces_base_is_its_presets_release_in_the_row_and_the_json(self):
        for kind, preset in (("buildroot", "webkit-2.52-buildroot-rpi3-32"), ("yocto", "wpewebkit-2.46-yocto-rpi3-32")):
            name = "%s-%s" % (kind, preset)
            release = re.search(r"^CFG_RELEASE=(\S+)", (REPO / "image" / "presets" / (preset + ".conf")).read_text(), re.M).group(1)
            self.w.make(name)
            rc, out, _ = self.status(name)
            self.assertEqual(line(out, name).split()[3], release)
            _, doc = self.doc(name)
            self.assertEqual(doc["machines"][0]["methods"][0]["workspaces"][0]["base"], release)
        self.w.make("yocto-no-such-preset")
        self.assertEqual(line(self.status("yocto-no-such-preset")[1], "yocto-no-such-preset").split()[3], "?")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class TestTheBaseItTracks(HostFlow):
    """The upstream WebKit line a checkout's HEAD descends from, by the probe run in real disposable repositories."""

    def base(self):
        repo = Path(tempfile.mkdtemp(dir=str(self.tmp))) / "r"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "a@example.invalid")
        git(repo, "config", "user.name", "wk-tools test")
        (repo / "f").write_text("one\n")
        git(repo, "add", "f")
        git(repo, "commit", "-qm", "init")
        self.w.repo = str(repo)
        return repo

    def shown(self):
        return line(self.status("ws")[1], "ws ").split()[3]

    def test_the_line_a_tracked_branch_names(self):
        for branch, remote, ref, want in (("main", "origin", "main", "main"), ("work", "wpe", "webkitglib/2.52", "2.52"),
                                          ("eng/stringimpl-2.38", "fork", "eng/stringimpl-2.38", "?")):
            repo = self.base()
            git(repo, "checkout", "-qB", branch)
            git(repo, "remote", "add", remote, "https://example.invalid/%s.git" % remote)
            git(repo, "update-ref", "refs/remotes/%s/%s" % (remote, ref), "HEAD")
            git(repo, "branch", "--set-upstream-to=%s/%s" % (remote, ref), branch)
            self.assertEqual(self.shown(), want, ref)

    def test_detached_with_nothing_reachable_is_unknown(self):
        git(self.base(), "checkout", "-q", "--detach", "HEAD")
        self.assertEqual(self.shown(), "?")

    def test_detached_but_reachable_from_two_releases_is_the_newer(self):
        repo = self.base()
        first = git(repo, "rev-parse", "HEAD")
        (repo / "f").write_text("two\n")
        git(repo, "commit", "-qam", "second")
        git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.46", first)
        (repo / "f").write_text("three\n")
        git(repo, "commit", "-qam", "third")
        git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.52", "HEAD")
        git(repo, "checkout", "-q", "--detach", first)
        self.assertEqual(self.shown(), "2.52")


class TestItsTasks(HostFlow):
    def test_a_name_reports_its_own_tasks_and_one_that_ended_as_asked_is_not_reported(self):
        self.w.make("other")
        self.begin()
        self.begin("other", "test")
        self.assertEqual(sorted((t["task_kind"], t["name"]) for t in self.tasks()), [("build", "ws"), ("test", "other")])
        self.assertEqual([t["name"] for t in self.tasks("ws")], ["ws"])
        for word, reported in (("0", False), ("cancelled", False), ("refused", False), ("3", True), ("stalled", True)):
            self.w.make("w3")
            self.begin("w3", pid=None).end(word)
            self.assertEqual(len(self.tasks("w3")), reported, word)

    def test_a_pid_inside_a_workspace_is_asked_of_it_and_reported_on_the_machine_it_named(self):
        self.begin(where="place", pid=None).pid(4242, "farbox")
        for answer, state, rc in (("lives", "running", 2), ("gone", "died", 4), ("mute", "unanswered", 4)):
            self.w.pids.discard(4242)
            self.w.pids.update({4242} if answer == "lives" else ())
            self.w.mute = {"ws"} if answer == "mute" else set()
            got, out, _ = self.status("ws")
            self.assertEqual((got, line(out, "build ws").split("since")[0]), (rc, "  build ws  %s  farbox  " % state))
            self.assertIn(("kill", "-0", "4242"), [e[1][-3:] for e in self.w.effects if e[0] == "run"])

    def test_a_task_shows_its_subject_steps_holds_kill_and_log(self):
        self.w.pids.add(4242)
        t = self.begin(kind="yocto", plan=["layers", "fetch", "image"], holds=["ws:ws", "build:here"])
        t.step(2)
        t.set("subject", "slot base")
        rc, out, _ = self.status("ws")
        block = out[out.index("  yocto ws"):].splitlines()
        self.assertEqual((rc, block[1:8]), (2, ["      slot base", "      [x] layers", "      [>] fetch", "      [ ] image",
                                                 "      holds: ws:ws build:here", "      kill: wk yocto ws --kill", "      log:  /nolog"]))

    def test_each_verdict_is_its_exit_status(self):
        log = os.path.join(self.w.ws_dir(), "build.log")
        self.w.files[log] = "[9/4200] cc\nx.cpp:1:1: error: no\n"
        for end, age, state, rc in ((None, 0, "running", 2), (None, 600, "silent", 2), (None, 4000, "silent", 4), ("1", 0, "failed", 1),
                                    ("stalled", 0, "stalled", 3), ("oom", 0, "oom", 3), ("gave-up", 0, "gave-up", 1), ("died", 0, "died", 4)):
            self.w.mtimes[log] = self.w.clock.now() - age
            t = self.begin(pid=4242 if end != "died" else 4343, log=log, fields={"abort_after": "1800"})
            self.w.pids.add(4242)
            if end not in (None, "died"):
                t.end(end)
            got, out, _ = self.status("ws")
            self.assertEqual((got, line(out, "build ws").split()[2]), (rc, state), (end, age))


FAILED = "Building place foo\nfoo.cpp:10:5: error: use of undeclared identifier 'x'\nninja: build stopped: subcommand failed.\n"
CLEAN = "-- no error: handling here, everything is fine\nx.cpp:3:1: warning: cast: error: prone\nninja: no work to do.\n"


class TestItsLog(HostFlow):
    """`wk status <ws> --log` on the machine holding the store: build.log, else the image stage log under home/."""

    def log(self, files, *argv):
        d = self.w.ws_dir()
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            Path(d, rel).write_text(text)
        return self.status("ws", "--log", *argv)

    def test_which_log_is_shown_and_its_first_error(self):
        for files, showing, last, error in (({"home/yocto-image.log": "the yocto stage output\n"}, "==> showing %s/home/yocto-image.log", "the yocto stage output", "(none)"),
                                            ({"home/buildroot-image.log": "the buildroot stage output\n"}, "==> showing %s/home/buildroot-image.log", "the buildroot stage output", "(none)"),
                                            ({"build.log": FAILED}, None, "ninja: build stopped: subcommand failed.", "2:foo.cpp:10:5: error: use of undeclared identifier 'x'")):
            shutil.rmtree(self.w.ws_dir(), True)
            rc, _, err = self.log(files)
            lines = err.splitlines()
            tail = lines[lines.index("last output:") + 1:]
            self.assertEqual((rc, lines[0] if showing else None, lines[lines.index("errors:") + 1].strip(), tail[:tail.index("")][-1].strip()),
                             (0, showing and showing % self.w.ws_dir(), error, last))

    def test_a_build_log_is_shown_over_a_stale_image_stage_log(self):
        rc, _, err = self.log({"home/yocto-image.log": "stale image-stage output\n", "build.log": "ninja: building places\n"})
        self.assertEqual((rc, err.splitlines()[:5]), (0, ["errors:", "  (none)", "", "last output:", "  ninja: building places"]))

    def test_the_word_error_mid_sentence_or_in_a_warning_is_not_an_error(self):
        rc, _, err = self.log({"build.log": CLEAN})
        self.assertEqual((rc, err.splitlines()[:2]), (0, ["errors:", "  (none)"]))

    def test_no_log_is_refused_naming_what_makes_one(self):
        rc, _, err = self.status("ws", "--log")
        self.assertEqual((rc, err.splitlines()[0]), (1, "error: no log for 'ws': nothing has made, built or staged it here ('wk new', 'wk build', 'wk sysimage build')"))


GOOD_BROWSER = {"accelerator": "IOAccelerator", "dpr": 2, "focused": True, "raf_hz": 59.7, "renderer": "Apple M1 Pro", "screen": [1512, 982],
                "webgl": "WebGL 2.0", "webkit_gpu_clients": {"913": "com.apple.WebKit.GPU"}}
LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
GOOD_PROFILE = {"profile_dir": "/Users/wk/pgo", "arch": "arm64", "missing": [],
                "combined": {lib: {"total_functions": 40000, "maximum_function_count": 900000} for lib in LIBRARIES},
                "compressed": {lib: 1234567 for lib in LIBRARIES},
                "benchmarks": {b: {lib: {"total_functions": 24000, "maximum_function_count": 4200} for lib in LIBRARIES}
                               for b in ("speedometer3", "jetstream3", "motionmark")}}


class TestTheReadingsTravelWithTheBuild(HostFlow):
    """`wk status <ws> --log --gates`: the readings a build left beside its products, read by the script run in the workspace."""

    def gates(self, browser=None, profile=None, pins=None):
        products = Path(self.w.src, "WebKitBuild", "Release-mac-release-pgo")
        products.mkdir(parents=True)
        for name, text in (("wk-browser-check.json", browser and json.dumps(browser)), ("wk-profile-check.json", profile and json.dumps(profile)),
                           ("wk-payload-pins", pins)):
            if text:
                (products / name).write_text(text)
        rc, out, _ = self.status("ws", "--log", "--gates")
        return rc, out.replace(self.w.src, "/src/WebKit")

    def test_a_build_with_no_readings_says_which_builds_have_them(self):
        rc, out = self.gates()
        self.assertEqual((rc, out.splitlines()[0]), (0, "no readings under /src/WebKit/WebKitBuild -- only a profile-guided build"))

    def test_the_readings_are_shown_from_beside_the_products(self):
        rc, out = self.gates(GOOD_BROWSER, GOOD_PROFILE, "speedometer3 9f3c1a\nmotionmark 44ab02\n")
        self.assertEqual(rc, 0, out)
        for want in ("raf_hz=59.7", "JavaScriptCore: functions=40000", "  speedometer3 9f3c1a"):
            self.assertIn(want, out)

    def test_a_reading_that_did_not_justify_a_measurement_says_so_again(self):
        rc, out = self.gates(dict(GOOD_BROWSER, raf_hz=8.0, webgl=None, webkit_gpu_clients={}))
        self.assertEqual(rc, 0, out)
        for want in ("this window is throttled", "no WebGL context"):
            self.assertIn(want, out)


def ws_rec(machine, name, state="running", ws="present"):
    return {"kind": "workspace", "machine": machine, "method": "container", "name": name, "state": state, "ws": ws}


class Fleet(places_flows.Fleet):
    """A Linux workstation with no container of its own, a build box `box` and a peer `pal`; `says(name, rc, *records)`
    is what that machine's own `wk status --records` prints and exits."""
    WK = {"box": "/home/u/wk/tools/wk", "pal": "/opt/wk-tools/wk"}

    def __init__(self, tmp, confs=None):
        super().__init__(tmp, confs)
        for name in self.WK:
            self.says(name, 0)

    def says(self, name, rc, *records, end="\n"):
        self.far[self.WK[name] + " status --records"] = Result(rc, "".join((r if isinstance(r, str) else json.dumps(r)) + end for r in records))

    def status_asked(self, name):
        return [a for a in self.asked(name) if "wk status" in a]


class FleetTest(FlowTest):
    OS = "Linux"

    def setUp(self):
        super().setUp()
        p = mock.patch("os.uname", return_value=os.uname_result((self.OS, "here", "25.0", "", "arm64")))
        p.start()
        self.addCleanup(p.stop)
        self.w = Fleet(self.tmp)

    def status(self, *argv):
        self.w.effects = []
        return self.wk(self.w, "status", *argv)

    def block(self, out, machine):
        """`machine`'s block of the text, its heading first."""
        lines = out.splitlines()
        start = lines.index(machine)
        end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith(" ")), len(lines))
        return [l for l in lines[start:end] if l]


class TestTheFleetIsOneView(FleetTest):
    def test_each_machine_is_asked_for_its_records_under_its_name_and_its_rows_are_its_block(self):
        self.w.says("box", 2, {"kind": "machine", "name": "box"}, ws_rec("box", "bw"), end="\r\n")
        rc, out, _ = self.status()
        self.assertEqual((rc, self.block(out, "box")), (2, ["box", "  container", "    WORKSPACE  STATE    BRANCH  BASE  WORK   BUILD",
                                                             "    bw         running  -       ?     clean"]))
        self.assertEqual((self.w.status_asked("box"), self.w.status_asked("pal")),
                         (["cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk status --records"],
                          ["cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=pal /opt/wk-tools/wk status --records"]))
        self.assertEqual((self.block(out, "pal"), self.block(out, "here   this machine")), (["pal", NONE], ["here   this machine", NONE]))

    def test_the_exit_is_the_worst_state_found_anywhere_in_every_mode(self):
        self.w.says("box", 1, ws_rec("box", "x", state="failed"))
        self.w.says("pal", 3, ws_rec("pal", "y"))
        for argv in ([], ["--json"], ["--records"]):
            self.assertEqual(self.status(*argv)[0], 3, argv)
        self.w.says("pal", 0, ws_rec("pal", "z"), ws_rec("pal", "z", state="absent", ws="absent"))
        for argv in ([], ["--json"], ["--records"]):
            self.assertEqual(self.status(*argv)[0], 4, argv)

    def test_an_exit_outside_0_to_4_from_a_far_wk_reads_4(self):
        self.w.says("box", 7)
        self.assertEqual(self.status()[0], 4)

    def test_a_disagreement_names_both_views_and_a_pipe_gets_no_colour(self):
        self.w.says("box", 2, ws_rec("box", "ws"), ws_rec("box", "ws", state="absent", ws="absent"))
        rc, out, _ = self.status()
        self.assertEqual((rc, line(out, "ws ")), (4, "    ws         disagree (running vs absent)  -       ?     clean"))
        self.assertNotIn("\033[", out)

    def test_json_is_the_one_merged_document(self):
        self.w.says("box", 2, ws_rec("box", "bw"), {"kind": "task", "machine": "box", "name": "bw", "state": "running"})
        rc, out, _ = self.status("--json")
        box = {"name": "box", "self": False, "raw": [], "methods": [{"name": "container", "workspaces": [ws_rec("box", "bw")]}],
               "tasks": [{"kind": "task", "machine": "box", "name": "bw", "state": "running"}]}
        doc = json.loads(out)
        self.assertEqual((rc, doc["exit"], [m for m in doc["machines"] if m["name"] == "box"]), (2, 2, [box]))

    def test_a_machine_record_after_its_rows_still_heads_its_block_and_an_unreadable_line_is_skipped_and_said(self):
        self.w.says("box", 0, ws_rec("box", "bw"), {"kind": "machine", "name": "box"}, "{not json")
        rc, out, err = self.status()
        self.assertEqual((rc, self.block(out, "box")[0], line(out, "bw")), (0, "box", "    bw         running  -       ?     clean"))
        self.assertEqual(err, "wk status: unreadable record: {not json\n")

    def test_an_unreachable_machine_is_a_line_saying_why_after_one_ssh_and_is_4(self):
        self.w.down["box"] = Result(255, "", "ssh: connect to host box.example port 22: Connection refused\n")
        rc, out, _ = self.status()
        self.assertEqual((rc, self.block(out, "box"), len(self.w.asked("box"))),
                         (4, ["box", "  box: unreachable over ssh: connect to host box.example port 22: Connection refused"], 1))

    def test_a_box_with_no_wk_of_its_own_is_read_from_here(self):
        self.w.far.update({"test -f $HOME/.wk-remote": Result(1), "ls -1A /home/u/wk/ws": Result(0, "a\n"), "ws/a ]": Result(0, "present\n")})
        rc, out, _ = self.status()
        self.assertEqual((rc, self.block(out, "box")[1:], self.w.status_asked("box")), (0, ["  native", "    WORKSPACE  STATE    BRANCH  BASE  WORK   BUILD",
                                                                                            "    a          present  -       ?     clean"], []))

    def test_a_name_on_one_machine_is_asked_of_it_alone_and_one_on_two_is_refused_naming_both(self):
        self.w.far["ws/demo ]"] = Result(0, "present\n")
        self.w.says("box", 0, ws_rec("box", "demo"))
        rc, out, _ = self.status("demo")
        self.assertEqual((rc, line(out, "demo"), self.w.status_asked("box"), self.w.status_asked("pal")),
                         (0, "    demo       running  -       ?     clean", ["cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk status --records demo"], []))
        self.w.far["/opt/wk-tools/wk ls --json"] = self.w.lists(("demo", "pal", "running"))
        rc, _, err = self.status("demo")
        self.assertEqual((rc, err.splitlines()[0]), (1, "error: workspace 'demo' exists on places: box pal -- this cannot be"))


class TestSeenFromOneWorkstation(FleetTest):
    """Two workstations reaching one box see one state: each asks it under its own name and reads the same block."""
    HOST = "left"

    def test_the_box_is_one_state_whoever_asks(self):
        self.w.says("box", 2, ws_rec("box", "ws"))
        rc, out, _ = self.status("--json")
        self.assertEqual((rc, [m for m in json.loads(out)["machines"] if m["name"] == "box"], self.w.status_asked("box")),
                         (2, [{"name": "box", "self": False, "raw": [], "tasks": [], "methods": [{"name": "container", "workspaces": [ws_rec("box", "ws")]}]}],
                          ["cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk status --records"]))


class TestSeenFromAnother(TestSeenFromOneWorkstation):
    HOST = "right"


class TestOnAMac(FleetTest):
    """The containers are in the podman machine `wk`, asked through `podman machine ssh` as this machine's own rows."""
    OS = "Darwin"

    def machine(self, state):
        self.w.answer(["podman", "machine", "inspect", "wk"], out=json.dumps([{"Name": "wk", "State": state, "Resources": {"CPUs": 8, "Memory": 16384}}]))

    def test_a_running_podman_machine_is_asked_for_its_records_as_this_machine(self):
        self.machine("running")
        self.w.answer(["podman", "machine", "ssh", "wk", "--"], out=json.dumps(ws_rec("here", "c")) + "\n")
        rc, out, _ = self.status()
        self.assertEqual((rc, self.block(out, "here   this machine")[1:]), (0, ["  container", "    WORKSPACE  STATE    BRANCH  BASE  WORK   BUILD",
                                                                                "    c          running  -       ?     clean"]))
        self.assertIn(("podman", "machine", "ssh", "wk", "--", "WK_IN_VM=1 WK_HOST_SELF=1 WK_NO_DELEGATE=1 WK_ROW_LABEL=here /opt/wk-tools/wk status --records"),
                      [e[1] for e in self.w.effects if e[0] == "run"])

    def test_a_stopped_or_missing_podman_machine_is_a_line_naming_the_remedy_and_no_failure(self):
        for state, said in (("stopped", "the podman machine 'wk' is stopped -- 'wk start' brings it up"), (None, "no podman machine 'wk' yet -- ./setup makes it")):
            if state:
                self.machine(state)
            else:
                self.w.answer(["podman", "machine", "inspect", "wk"], rc=125)
            rc, out, _ = self.status()
            self.assertEqual((rc, self.block(out, "here   this machine")), (0, ["here   this machine", "  container: " + said]))


class TestTheFanOut(FleetTest):
    """The same fan-out lists every machine's bench tasks and stops every machine's tasks."""

    def test_bench_ls_lists_each_answering_machines_rows_and_warns_of_each_other(self):
        self.w = Fleet(self.tmp, dict(Fleet.CONFS, far="kind=build\nhost=far.example\n"))
        self.w.far.update({"wk/tools/wk bench ls --continued": Result(0, "a\r\n\nb\n"), "/opt/wk-tools/wk bench ls --continued": Result(2)})
        self.w.down["far"] = Result(255, "", "ssh: connect to host far.example port 22: Connection refused\n")
        rc, out, err = self.wk(self.w, "bench", "ls")
        self.assertEqual((rc, out, err.splitlines()[:2]), (0, "a\nb\n", [
            "warning: far: unreachable over ssh: connect to host far.example port 22: Connection refused, so the tasks in its store are not listed",
            "warning: 'pal' did not answer the listing, so the tasks in its store are not here."]))
        self.assertEqual([a for a in self.w.asked("box") if "bench" in a], ["cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk bench ls --continued"])

    def test_stop_tasks_all_names_a_box_with_no_wk_of_its_own(self):
        self.w.far.update({"test -f $HOME/.wk-remote": Result(1), "/opt/wk-tools/wk stop --tasks": Result(0, "no task\n")})
        rc, out, err = self.wk(self.w, "stop", "--tasks", "--all", "--yes")
        self.assertEqual((rc, out, err.splitlines()[-1]), (3, "pal                   no task\n", "box                    no wk-tools there yet -- 'wk machine setup box'"))


class TestTheHelp(FlowTest):
    def test_it_leads_with_this_machine_its_role_and_its_mode(self):
        rc, out, _ = self.wk(places_flows.Fleet(self.tmp), "help")
        self.assertEqual((rc, out.splitlines()[0]), (0, "here -- workstation, host mode"))

    def test_every_report_says_it_changes_nothing(self):
        for cmd in ("status", "ls", "doctor"):
            rc, out, _ = self.wk(places_flows.Fleet(self.tmp), cmd, "-h")
            self.assertEqual((rc, line(out, "changes things:")), (0, "  changes things: no -- starts nothing, writes nothing, repairs nothing"), cmd)


# What no world reaches. `--wait` reads the real fleet: each look builds a places.Registry of its own (cmd/status), so
# a flow's world answers none of it.

class TestWait(unittest.TestCase):
    """`--wait` polls while the answer is busy (2), and another answer ends it only once the next poll repeats it;
    `--timeout` is elapsed time, and ends the wait unanswered."""

    def wait(self, answers, timeout):
        clock, said, it = FakeClock(), [], iter(answers)

        def poll():
            clock.t += 0.5   # a walk costs time of its own
            return next(it)
        return status.wait(poll, timeout, clock, "ws", said.append, said.append), said, clock

    def test_only_a_repeated_answer_other_than_busy_ends_it(self):
        for answers, want in (([0], 0), ([4], 4), ([2, 2, 0, 0], 0), ([2, 4, 2, 0, 0], 0), ([2, 4, 4], 4)):
            rc, said, clock = self.wait(answers, 0)
            self.assertEqual(rc, want, answers)
            self.assertEqual(clock.slept, [status.WAIT_SECONDS] * (len(answers) - 1))

    def test_the_timeout_is_elapsed_time_and_says_the_work_goes_on(self):
        rc, said, clock = self.wait([2] * 100, 10)
        self.assertEqual(rc, 2)
        self.assertLess(clock.now() - FakeClock().now(), 20)
        self.assertIn("still busy after 10s", said[-1])

    def test_the_exit_is_the_waits_verdict_not_the_walk_after_it(self):
        class FlakyWalk:
            worst = 4

            def __init__(self, *a, **kw):
                pass

            def worst_only(self):
                return 4

            def records(self, markers=True):
                return iter(())
        with mock.patch.object(CMD, "Walk", FlakyWalk), mock.patch.object(CMD, "wait", return_value=0), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, CMD.main(as_dispatched("status", ["--wait", "--records"]), invocation()))
            self.assertEqual(4, CMD.main(as_dispatched("status", ["--records"]), invocation()))

    def test_the_exit_is_the_worst_state_found_anywhere(self):
        def answering(name, rc, *rows):
            d = FakeDriver(name, rc=rc, out="".join(json.dumps(r) + "\n" for r in [{"kind": "machine", "name": name}, *rows]))
            d.host = name + ".tailnet"
            return d
        for drivers, want in (([answering("alpha", 1, ws_rec("alpha", "x", state="failed")), answering("beta", 3, ws_rec("beta", "y"))], 3),
                              ([answering("gamma", 0, ws_rec("gamma", "z"), ws_rec("gamma", "z", state="absent", ws="absent"))], 4)):
            by = {d.name: d for d in drivers}
            with mock.patch.dict(os.environ, clean_env({"WK_IN_VM": "1"}), clear=True), mock.patch.object(dispatch, "_registry", None), \
                    mock.patch.object(places, "Registry", lambda *a, **kw: FakeRegistry(dict(os.environ), Fake(), lambda n, e: by[n], names=list(by))), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Refused) as e:
                dispatch.main(["status", "--wait"])
            self.assertEqual(e.exception.status, want)


# The order a stream draws in is set by which place's thread answers first, which no command line fixes; a flush for
# a job outside the plan is one Walk never makes.

def render(records):
    """Records drawn as text: tests/test_task_record.py and tests/test_build_subject.py read their rows with it."""
    buf = io.StringIO()
    statusview.render_text_stream(iter(records), buf, False)
    return buf.getvalue()


def _plan(*jobs):
    return dict(kind="plan", jobs=[{"job": j, "machine": m} for j, m in jobs])


def _machine_lines(name):
    return [dict(kind="machine", name=name, self=(name == "alpha")), ws_rec(name, "ws-" + name), dict(kind="flush", job=name)]


class _Tap:
    """The renderer's output, each line stamped with how many records it had consumed when the line appeared."""

    def __init__(self, records):
        self.records, self.consumed, self.lines = records, 0, []

    def feed(self):
        for r in self.records:
            self.consumed += 1
            yield r

    def write(self, text):
        for l in text.rstrip("\n").split("\n"):
            self.lines.append((self.consumed, l))

    def flush(self):
        pass

    def when(self, predicate):
        return next(consumed for consumed, l in self.lines if predicate(l))


def _render(records):
    tap, err = _Tap(records), io.StringIO()
    with contextlib.redirect_stderr(err):
        statusview.render_text_stream(tap.feed(), tap, False)
    return tap, err.getvalue()


class TestTextStreamsAsRecordsArrive(unittest.TestCase):
    def test_first_machine_prints_before_second_machines_records_are_consumed(self):
        tap, err = _render([_plan(("alpha", "alpha"), ("beta", "beta"))] + _machine_lines("alpha") + _machine_lines("beta") + [dict(kind="exit", code=0)])
        self.assertLessEqual(tap.when(lambda l: l.startswith("alpha")), 4, "alpha's block waited for beta's records -- not streaming")
        self.assertGreater(tap.when(lambda l: l.startswith("beta")), 4)
        self.assertEqual(err, "")

    def test_a_planned_machine_shows_as_probing_before_it_answers(self):
        tap, _ = _render([_plan(("slowbox", "slowbox"))] + _machine_lines("slowbox") + [dict(kind="exit", code=0)])
        self.assertEqual(tap.when(lambda l: "probing slowbox" in l), 1)
        self.assertGreater(tap.when(lambda l: l.strip() == "slowbox"), 1)

    def test_a_block_waits_for_every_job_planned_for_its_machine(self):
        tap, err = _render([_plan(("container", "host"), ("vm", "host")), dict(kind="machine", name="host", self=True), dict(kind="flush", job="container"),
                            dict(kind="machine", name="host", self=True), dict(ws_rec("host", "ws-v", state="stopped"), method="macOS guest"),
                            dict(kind="flush", job="vm"), dict(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertEqual((err, len([l for l in out.splitlines() if l.startswith("host ")]), "no workspaces" in out), ("", 1, False))
        self.assertLess(out.index("probing vm"), out.index("host "))
        self.assertIn("ws-v", out)

    def test_a_flush_for_a_job_outside_the_plan_is_reported_and_draws_nothing(self):
        tap, err = _render([_plan(("container", "host")), dict(kind="machine", name="host", self=True), dict(kind="flush", job="vm"),
                            ws_rec("host", "ws-c"), dict(kind="flush", job="container"), dict(kind="exit", code=0)])
        out = "\n".join(l for _, l in tap.lines)
        self.assertIn("'vm' ended without being in the plan", err)
        self.assertEqual(("ws-c" in out, "no workspaces" in out), (True, False))


class TestColourOnATerminal(unittest.TestCase):
    """A flow's stdout is always a pipe."""

    def test_only_a_terminal_without_no_color_gets_colour(self):
        for tty, env, want in ((True, {}, True), (True, {"NO_COLOR": "1"}, False), (False, {}, False), (False, {"NO_COLOR": "1"}, False)):
            self.assertEqual(statusview.colour_wanted(types.SimpleNamespace(isatty=lambda: tty), env), want, (tty, env))

    def test_the_renderer_honours_it(self):
        for colour in (True, False):
            out = io.StringIO()
            statusview.render_text_stream(iter([{"kind": "machine", "name": "b"}, ws_rec("b", "demo"), {"kind": "exit", "code": 0}]), out, colour)
            self.assertEqual("\033[" in out.getvalue(), colour)


class TestAnUnknownMachine(unittest.TestCase):
    """Every name a command fans out to comes from a conf the registry loads."""

    def test_is_an_answer_naming_it(self):
        a, = fleet.each(FakeRegistry({}, Fake(), lambda n, e: None, names=[]), ["status"], names=["ghost"])
        self.assertEqual((a.side, a.driver, "ghost" in a.why), ("unknown", None, True))


# The reports run for real.

class TestEveryWorkspaceIsInTheListing(unittest.TestCase):
    @requires_container_place()
    def test_bare_status_lists_every_workspace_ls_lists(self):
        ls = run("ls", "--json", timeout=120)
        self.assertEqual(ls.returncode, 0, ls.stdout)
        names = [w["name"] for w in json.loads(ls.stdout)["workspaces"]]
        st = run("status", env={"NO_COLOR": "1"}, timeout=180)
        self.assertIn(st.returncode, (0, 2, 4), st.stdout)
        for n in names:
            self.assertRegex(st.stdout, r"(?m)^\s+%s\s" % n, f"'{n}' missing from a bare 'wk status':\n{st.stdout}")


REPORTS = (("status",), ("ls",), ("status", "nosuch", "--log"), ("doctor",))
READS = {"status", "ls", "doctor"}   # what a report may ask a far wk
PLACES = {"store": {"WK_IN_VM": "1"}, "workstation": {}}
TOOLS = ("ssh", "scp", "rsync", "podman", "tart", "tailscale", "sudo", "systemctl", "launchctl", "gh", "nmap")
# A tool followed by one of these words acts; the power verbs and the copies act whatever follows.
ACTS = {"podman": {"start", "run", "create", "rm", "rmi", "stop", "kill", "restart", "pull", "build", "init", "exec", "set"},
        "tart": {"run", "clone", "set", "stop", "delete", "pull", "push", "create", "import", "prune"},
        "tailscale": {"up", "down", "set", "login", "logout", "switch", "cert"},
        "systemctl": {"start", "stop", "restart", "enable", "disable", "kill", "reboot", "poweroff", "daemon-reload"},
        "launchctl": {"load", "unload", "bootstrap", "bootout", "kickstart", "start", "stop", "enable", "disable"}}
ALWAYS = {"reboot", "shutdown", "poweroff", "scp", "rsync"}
HELPERS = {"wk-boot-priv", "wk-card-priv", "wk-quiesce-priv"}   # the privileged helpers: `status` is their one read


def acts_in(tokens, readonly):
    """The first call in `tokens` that acts, or "": a tool followed within two words by an acting verb, a wk that is
    not asked a readonly verb, or a privileged helper asked anything but its status."""
    for i, tok in enumerate(tokens):
        tool, rest = os.path.basename(tok), tokens[i + 1:]
        verbs = [a for a in rest if not a.startswith("-")][:2]
        if tool in ALWAYS or (ACTS.get(tool, set()) & set(verbs)):
            return " ".join(tokens[i:i + 4])
        if tok.endswith("/wk") and (verbs[:1] or [""])[0] not in readonly:
            return " ".join(tokens[i:i + 3])
        if tool in HELPERS and verbs[:1] != ["status"]:
            return " ".join(tokens[i:i + 3])
    return ""


def acts(argv, readonly):
    """What in one recorded call acts, or "": an ssh's far command line is read word by word, whatever quoting it is in."""
    if argv[0] == "ssh":
        return "" if "-G" in argv else acts_in(re.findall(r"[A-Za-z0-9_./:@=+-]+", " ".join(argv[1:])), readonly)
    return acts_in(argv, readonly)


def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


class TestTheClassifier(unittest.TestCase):
    def test_it_names_a_start_a_boot_and_a_far_mutation_and_passes_a_read(self):
        ro = {"status", "ls"}
        for argv in (["podman", "machine", "start", "wk"], ["tart", "run", "wk-a"], ["sudo", "reboot"], ["sudo", "-n", "/usr/local/sbin/wk-boot-priv", "reboot"],
                     ["ssh", "-o", "BatchMode=yes", "box", "cd /x && ./wk build a"], ["ssh", "box", "PATH=/x; tart clone a b"],
                     ["ssh", "box", "sh -c 'systemctl restart wk-proxy'"], ["tailscale", "up"]):
            self.assertTrue(acts(argv, ro), argv)
        for argv in (["podman", "machine", "inspect", "wk"], ["tart", "list", "--format", "json"], ["ssh", "-G", "box"],
                     ["ssh", "box", "cd /x && ./wk status --continued"], ["sudo", "-n", "-l"], ["tailscale", "status", "--json"]):
            self.assertFalse(acts(argv, ro), argv)


class TestReportsAreReadOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-readonly-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, "calls")
        shims = os.path.join(self.tmp, "bin")
        os.makedirs(shims)
        for tool in TOOLS:
            p = os.path.join(shims, tool)
            with open(p, "w") as f:
                f.write('#!/bin/sh\n{ printf "%%s\\0" %s "$@"; printf "\\n"; } >> %s\nexit 97\n' % (tool, shlex.quote(self.log)))
            os.chmod(p, 0o755)
        machines = os.path.join(self.tmp, "fleet")
        os.makedirs(machines)
        for conf in os.listdir(BLIND_FLEET):
            os.symlink(os.path.join(BLIND_FLEET, conf), os.path.join(machines, conf))
        with open(os.path.join(machines, "farbox.conf"), "w") as f:
            f.write("kind=build\ndriver=remote\nhost=farbox\n")
        self.watched = {k: os.path.join(self.tmp, k) for k in ("home", "state", "store", "secrets")}
        for d in self.watched.values():
            os.makedirs(d)
        self.env = {"PATH": shims + os.pathsep + os.environ["PATH"], "WK_MACHINES_DIR": machines, "HOME": self.watched["home"],
                    "XDG_STATE_HOME": self.watched["state"], "WK_STORE": self.watched["store"], "WK_HOST_SECRETS": self.watched["secrets"]}

    def calls(self):
        try:
            with open(self.log) as f:
                return [l.rstrip("\0").split("\0") for l in f.read().split("\0\n") if l]
        except OSError:
            return []

    def test_a_report_starts_boots_and_repairs_nothing_here_or_on_the_far_machine(self):
        for place, extra in PLACES.items():
            for argv in REPORTS:
                with self.subTest(place=place, cmd=argv[0]):
                    open(self.log, "w").close()
                    before = {k: tree(d) for k, d in self.watched.items()}
                    run(*argv, env=dict(self.env, **extra), timeout=60)
                    after = {k: tree(d) for k, d in self.watched.items()}
                    self.assertEqual([a for a in map(lambda c: acts(c, READS), self.calls()) if a], [])
                    self.assertEqual(before, after, "wk %s wrote under the scratch %s" % (argv[0], place))


if __name__ == "__main__":
    unittest.main()
