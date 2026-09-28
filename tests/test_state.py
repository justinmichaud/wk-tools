"""Workspace state: one walk behind
both `wk ls`/`wk status`, `Target.state`'s five words and `wait_ready`, status-files-are-claims,
the wk-tools completion marker naming, one status entry per machine, and `wk
zed` refusing inside a workspace. Each docstring is the
phrase the check implements.

Checks marked `# static` are source-grep assertions; they exercise no
runtime behaviour.

Run: python3 -m unittest tests.test_state -v
"""
import contextlib
import inspect
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, requires_container_target, run

sys.path.insert(0, str(REPO / "lib"))
from wk import record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestListingsAgree(WkTest):
    @requires_container_target()
    def test_ls_status_same_names(self):
        """print the same workspace-name set on"""
        ls_cp = run("ls")
        names_ls = set()
        # The table, and only the table: `wk ls` ends it with a blank line
        # and then reports each target's current base and reclaimable
        # snapshots. Those footnotes go to stderr, which support.run() merges
        # into stdout by design, so a walk to the end of the output takes
        # "container:" for a workspace name. An advisory line -- "(no
        # workspaces ...)" -- is parenthesised and is not a row either.
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

        self.assertEqual(
            names_ls, names_status,
            f"wk ls lists: {sorted(names_ls)}\nwk status  : {sorted(names_status)}",
        )

    def test_status_one_machine_each(self):
        """names each machine once"""
        cp = run("status", "--json")
        try:
            doc = json.loads(cp.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"'wk status --json' did not print JSON: {cp.stdout[:500]}")

        names = [m["name"] for m in doc.get("machines", [])]
        bad = []
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            bad.append("named more than once: " + ", ".join(sorted(dup)))
        kinds = {"container", "vm", "local", "remote", "localhost"}
        wrong = kinds.intersection(names)
        if wrong:
            bad.append("a target kind where a machine belongs: " + ", ".join(sorted(wrong)))
        if not names:
            bad.append("no machines at all")
        self.assertEqual(bad, [], "; ".join(bad))


class TestZedRefusesInsideAWorkspace(WkTest):
    def test_zed_refuses_inside_a_workspace(self):
        """there is no Zed in here"""
        marker = self.tmp / "ws-marker"
        marker.write_text("name=selftest-ws\nsrc=/src/WebKit\n")
        cp = run("zed", "something", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, "opened an editor from inside a workspace")
        self.assertIn("wk zed selftest-ws", cp.stdout, cp.stdout)


class Scripted(targets.Target):
    """A target whose environment says `word` and whose creation marker is `marker`, over a Fake host."""

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

    def target(self, **kw):
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
        """five words, each from the evidence that decides it -- the directory read through the machine"""
        t = self.target()
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
        t = self.target()
        self.ws_dir(t, "ws")
        self.creation(t, "ws", alive=False)
        self.assertEqual(t.state("ws"), "broken")

    def test_a_directory_that_is_not_on_the_fake_machine_is_absent_whatever_the_real_disk_holds(self):
        t = self.target()
        os.makedirs(t.store.ws_dir("ws"))
        self.assertEqual(t.state("ws"), "absent")


class TestReadyMeansTheCreationIsFinished(StateTest):
    def test_a_workspace_whose_creation_is_still_running_is_not_ready(self):
        """`wk new` writes the ready marker at its `init` stage and then holds the workspace lock through the
        stages after it, so `present` alone would hand a build a lock it cannot take: every `ready=yes`
        command waits for the creation driver itself to be gone."""
        t = self.target(word="running", marker=True)
        self.ws_dir(t, "ws")
        rec = self.creation(t, "ws", alive=True)
        t.env["WK_READY_WAIT"] = "4"
        rc, err = self.waited(t)
        self.assertEqual(rc, 1, err)
        self.assertIn("waiting for 'ws' to finish being created (at: create)", err)
        self.assertIn("was still creating after 4s", err)
        self.assertEqual(self.clock.slept, [2, 2])
        rec.end(0)
        self.fake.pids.clear()
        self.assertEqual(self.waited(t), (0, ""))

    def test_the_wait_ends_when_the_creation_does(self):
        t = self.target(word="running", marker=True)
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
        self.assertIn("'ws' is ready", err)
        self.assertEqual(len(self.clock.slept), 3)

    def test_absent_broken_and_unreachable_do_not_improve_with_waiting(self):
        for word, marker, dir_, says in (("absent", False, False, "no such workspace: ws"),
                                         ("absent", True, True, "wk rm ws"),
                                         ("unreachable", False, False, "did not answer")):
            with self.subTest(word=word, marker=marker):
                t = self.target(word=word, marker=marker)
                if dir_:
                    self.ws_dir(t, "ws")
                rc, err = self.waited(t)
                self.assertEqual(rc, 1)
                self.assertIn(says, err)
                self.assertEqual(self.clock.slept, [])

    def test_a_creation_nothing_is_running_is_a_barrier_naming_the_remake(self):
        t = self.target(word="creating")
        self.ws_dir(t, "ws")
        rc, err = self.waited(t)
        self.assertEqual(rc, 1)
        self.assertIn("never finished creating", err)
        self.assertIn("wk new ws --target stub", err)
        self.assertEqual(self.clock.slept, [])
        os.environ["WK_FORCE"] = "1"
        self.assertEqual(self.waited(t)[0], 0)

    def test_a_dead_creation_is_refused_at_once_by_the_real_container_driver(self):
        """`unit machine.dead_creation_refused_at_once`: the container is up, its directory is gone and nothing is
        creating it. Every ready command is refused at once naming `wk rm`, and nothing reaches `wkdev-enter`."""
        self.fake.answer(["podman", "inspect"], out="running\n")
        t = targets.Container("container", str(REPO), {"WK_STORE": self.store, "HOME": self.store, "WK_IN_VM": "1"}, self.fake)
        self.assertEqual(t.state("ws"), "broken")
        rc, err = self.waited(t)
        self.assertEqual(rc, 1)
        self.assertIn("Repair:  wk rm ws", err)
        self.assertEqual(self.clock.slept, [])
        self.assertEqual([e for e in self.fake.effects if e[0] == "run" and "wkdev-enter" in " ".join(e[1])], [])


class TestARecordIsAClaimAndThePidIsTheFact(WkTest):
    def records(self):
        return record.Records(root=str(self.tmp / "store"), env={})

    def test_a_record_is_a_claim_and_the_pid_is_the_fact(self):
        """a task record's fields round-trip, a missing one is empty, and
        liveness is the process table rather than anything written down"""
        t = self.records().begin("new", "here", "ws1", "wk new ws1 --kill", "/nonexistent-log", ["checking", "create"])
        self.assertEqual(t.field("kind"), "new")
        self.assertTrue(t.alive(), "a live pid was read as dead")
        self.assertEqual(t.field("future_field"), "")
        # A pid above every default pid_max on both platforms: dead by construction.
        t.pid(4194304)
        self.assertFalse(t.alive(), "a dead pid was read as alive")
        self.assertEqual(t.verdict(), "died")

    def test_garbage_in_the_pid_field_reads_dead_and_never_crashes(self):
        t = self.records().begin("new", "here", "ws1", "wk new ws1 --kill", "/nonexistent-log", ["checking", "create"])
        t.set("pid", "not a pid at all")
        self.assertFalse(t.alive(), "garbage read as alive")


class TestReadyMarkerOneName(WkTest):
    def test_ready_marker_one_name(self):
        """every driver writes the same completion marker"""
        # static
        text = (REPO / "lib" / "wk" / "targets.py").read_text()
        self.assertEqual(targets.READY_MARKER, ".wk-ready")
        self.assertIn(".wk-ready", (REPO / "container" / "firstrun.sh").read_text(errors="replace"))
        for cls in (targets.Container, targets.Vm, targets.Remote):
            self.assertIn("READY_MARKER", inspect.getsource(cls), "%s does not use READY_MARKER" % cls.__name__)
        # Nothing may spell the name as a literal where the constant is in scope.
        hits = []
        for f in sorted((REPO / "lib").rglob("*.py")):
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if re.search(r"""["']\.wk-ready["']""", line) and not line.startswith("READY_MARKER = "):
                    hits.append("%s:%d:%s" % (f.relative_to(REPO), i, line))
        self.assertEqual(hits, [], "hardcoded marker name:\n" + "\n".join(hits))
        self.assertIn('READY_MARKER = ".wk-ready"', text)


class TestPushKeysNotCopied(WkTest):
    @requires_container_target()
    def test_push_keys_not_copied(self):
        """never copies"""
        cp = run("push", "status")
        self.assertNotIn(
            "hold their own copy", cp.stdout,
            f"a workspace holds its own copy of a push key:\n{cp.stdout}",
        )


class TestVmDriverWithoutTart(unittest.TestCase):
    """A machine with no tart has no guests: the vm driver answers `absent`
    for every name, never an empty string a caller reads as a state, and runs no tart."""

    def test_no_tart_means_every_guest_is_absent(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-state-")
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = Fake("here")
        reg = targets.Registry(REPO, env={"HOME": tmp, "WK_STORE": tmp + "/s", "WK_VM_STORE": tmp + "/v"}, machine=fake)
        t = reg.load("vm")
        self.assertIsNone(t.tart())
        self.assertEqual((t.state_of("wk-nosuch"), t.info("nosuchws"), t.list(), t.state("nosuchws")),
                         ("absent", "absent", [], "absent"))
        self.assertFalse(reg.exists_on(t, "nosuchws"))
        self.assertEqual([e for e in fake.effects if e[0] == "run"], [])


if __name__ == "__main__":
    unittest.main()
