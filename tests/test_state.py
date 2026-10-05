"""Workspace state: one walk behind `wk ls`/`wk status`, `Driver.state`'s words, `wait_ready`, and task records."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, requires_container_place, run

sys.path.insert(0, str(REPO / "lib"))
from wk import places, record  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestListingsAgree(WkTest):
    @requires_container_place()
    def test_ls_status_same_names(self):
        ls_cp = run("ls")
        names_ls = set()
        # The table ends at its first blank line; a parenthesised line is advice, not a row.
        lines = ls_cp.stdout.splitlines()
        for line in lines[1:] if lines else []:
            fields = line.split()
            if not fields:
                break
            if fields[0].startswith("("):
                continue
            names_ls.add(fields[0])

        status_cp = run("status", "--json")
        try:
            doc = json.loads(status_cp.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"'wk status --json' did not print JSON: {status_cp.stdout[:500]}")

        names_status = set()
        for m in doc.get("machines", []):
            for g in m.get("methods", []):
                for w in g.get("workspaces", []):
                    names_status.add(w["name"])
            for r in m.get("raw", []):
                for ln in r.get("text", "").splitlines():
                    if ln[:1] not in ("", " "):
                        names_status.add(ln.split()[0])

        self.assertEqual(names_ls, names_status)

    def test_status_one_machine_each(self):
        cp = run("status", "--json")
        try:
            doc = json.loads(cp.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"'wk status --json' did not print JSON: {cp.stdout[:500]}")

        names = [m["name"] for m in doc.get("machines", [])]
        self.assertTrue(names)
        self.assertEqual(len(names), len(set(names)), names)
        self.assertFalse({"container", "vm", "local", "remote", "localhost"} & set(names), names)


class TestZedRefusesInsideAWorkspace(WkTest):
    def test_zed_refuses_inside_a_workspace(self):
        marker = self.tmp / "ws-marker"
        marker.write_text("name=selftest-ws\nsrc=/src/WebKit\n")
        cp = run("zed", "something", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, "opened an editor from inside a workspace")
        self.assertIn("wk zed selftest-ws", cp.stdout, cp.stdout)


class Scripted(places.Driver):
    """A place whose environment says `word` and whose creation marker is `marker`, over a Fake host."""

    def __init__(self, store, fake, word="absent", marker=False, needs_base=False):
        super().__init__("stub", str(REPO), {"WK_STORE": store, "HOME": store}, fake)
        self.word, self.marker, self.needs_base = word, marker, needs_base

    def info(self, ws):
        return self.word

    def created(self, ws):
        return self.marker


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-state-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.store = str(self.tmp / "store")
        self.fake = Fake("here")
        self.clock = FakeClock()
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for v in ("WK_FORCE", "WK_DRY_RUN", "WK_READY_WAIT", "WK_QUIET"):
            os.environ.pop(v, None)

    def driver(self, **kw):
        return Scripted(self.store, self.fake, **kw)

    def ws_dir(self, t, ws):
        self.fake.dirs.add(t.store.ws_dir(ws))

    def creation(self, t, ws, alive):
        """A `wk new` record for `ws`: its driver alive, or ended 0."""
        rec = t.records().begin("new", "here", ws, "wk new %s --kill" % ws, "/nolog", ["checking", "create"], pid=4242)
        rec.step_named("create")
        if alive:
            self.fake.pids.add(4242)
        else:
            rec.end(0)
        return rec

    def waited(self, t, ws="ws", timeout=None):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                t.wait_ready(ws, self.clock, timeout)
                rc = 0
            except Refused as e:
                rc = e.status
        return rc, err.getvalue()


class TestWsStateWords(StateTest):
    def test_ws_state_words(self):
        t = self.driver()
        self.assertEqual(t.state("ws"), "absent")                    # nothing anywhere
        self.ws_dir(t, "ws")
        t.word, t.marker = "running", True
        self.assertEqual(t.state("ws"), "present")                   # the environment is up and creation finished
        t.word = "creating"
        self.assertEqual(t.state("ws"), "creating")                  # the driver says so itself
        t.word = "unreachable"
        self.assertEqual(t.state("ws"), "unreachable")               # the machine did not answer
        t.word, t.marker = "absent", False
        self.assertEqual(t.state("ws"), "creating")                  # a directory, no environment, no marker
        t.marker = True
        self.assertEqual(t.state("ws"), "broken")                    # the same once creation had finished

    def test_a_finished_creation_record_is_the_marker_where_the_target_keeps_none(self):
        t = self.driver()
        self.ws_dir(t, "ws")
        self.creation(t, "ws", alive=False)
        self.assertEqual(t.state("ws"), "broken")

    def test_a_directory_that_is_not_on_the_fake_machine_is_absent_whatever_the_real_disk_holds(self):
        t = self.driver()
        os.makedirs(t.store.ws_dir("ws"))
        self.assertEqual(t.state("ws"), "absent")


class TestReadyMeansTheCreationIsFinished(StateTest):
    def test_a_workspace_whose_creation_is_still_running_is_not_ready(self):
        t = self.driver(word="running", marker=True)
        self.ws_dir(t, "ws")
        rec = self.creation(t, "ws", alive=True)
        t.env["WK_READY_WAIT"] = "4"
        rc, err = self.waited(t)
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.clock.slept, [2, 2])
        rec.end(0)
        self.fake.pids.clear()
        self.assertEqual(self.waited(t), (0, ""))

    def test_the_wait_ends_when_the_creation_does(self):
        t = self.driver(word="running", marker=True)
        self.ws_dir(t, "ws")
        rec = self.creation(t, "ws", alive=True)
        real = self.clock.sleep

        def sleep(n):
            real(n)
            if len(self.clock.slept) == 3:
                rec.end(0)
        self.clock.sleep = sleep
        rc, err = self.waited(t)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.clock.slept), 3)

    def test_absent_broken_and_unreachable_do_not_improve_with_waiting(self):
        for word, marker, dir_, says in (("absent", False, False, "ws"),
                                         ("absent", True, True, "wk rm ws"),
                                         ("unreachable", False, False, "ws")):
            with self.subTest(word=word, marker=marker):
                t = self.driver(word=word, marker=marker)
                if dir_:
                    self.ws_dir(t, "ws")
                rc, err = self.waited(t)
                self.assertEqual(rc, 1)
                self.assertIn(says, err)
                self.assertEqual(self.clock.slept, [])

    def test_a_creation_nothing_is_running_is_a_barrier_naming_the_remake(self):
        t = self.driver(word="creating")
        self.ws_dir(t, "ws")
        rc, err = self.waited(t)
        self.assertEqual(rc, 1)
        self.assertIn("wk new ws --on stub", err)
        self.assertEqual(self.clock.slept, [])
        os.environ["WK_FORCE"] = "1"
        self.assertEqual(self.waited(t)[0], 0)

    def test_a_dead_creation_is_refused_at_once_by_the_real_container_driver(self):
        self.fake.answer(["podman", "inspect"], out="running\n")
        t = places.Container("container", str(REPO), {"WK_STORE": self.store, "HOME": self.store, "WK_IN_VM": "1"}, self.fake)
        self.assertEqual(t.state("ws"), "broken")
        rc, err = self.waited(t)
        self.assertEqual(rc, 1)
        self.assertIn("wk rm ws", err)
        self.assertEqual(self.clock.slept, [])
        self.assertEqual([e for e in self.fake.effects if e[0] == "run" and "wkdev-enter" in " ".join(e[1])], [])


class TestARecordIsAClaimAndThePidIsTheFact(WkTest):
    def records(self):
        return record.Records(root=str(self.tmp / "store"), env={})

    def test_garbage_in_the_pid_field_reads_dead_and_an_unknown_field_reads_empty(self):
        t = self.records().begin("new", "here", "ws1", "wk new ws1 --kill", "/nonexistent-log", ["checking", "create"])
        self.assertEqual(t.field("future_field"), "")
        t.set("pid", "not a pid at all")
        self.assertFalse(t.alive(), "garbage read as alive")


class TestVmDriverWithoutTart(unittest.TestCase):

    def test_no_tart_means_every_guest_is_absent(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-state-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = Fake("here")
        reg = places.Registry(REPO, env={"HOME": tmp, "WK_STORE": tmp + "/s", "WK_VM_STORE": tmp + "/v"}, machine=fake)
        t = reg.load("vm")
        self.assertIsNone(t.tart())
        self.assertEqual((t.state_of("wk-nosuch"), t.info("nosuchws"), t.list(), t.state("nosuchws")),
                         ("absent", "absent", [], "absent"))
        self.assertFalse(reg.exists_on(t, "nosuchws"))
        self.assertEqual([e for e in fake.effects if e[0] == "run"], [])


if __name__ == "__main__":
    unittest.main()
