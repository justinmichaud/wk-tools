"""When the record and the machine disagree, the command reports what the machine holds and acts on that."""
import os
import sys

from tests.support import REPO
from tests.test_rm_final_state import RealContainerWorld, RmFinalStateTest, VmWorld
from tests.test_wk_workspace import World

sys.path.insert(0, str(REPO / "lib"))
from wk import sshalias, status, workspace  # noqa: E402
from wk.machine import Result  # noqa: E402

USER = "Host mine\n    HostName m.example\n# kept as written\n"
LATER = "Host added-later\n    User me\n"


class TestMachineWins(RmFinalStateTest):
    def status_of(self, w):
        walk = status.Walk(REPO, env=w.env, reg=w.reg, fleet=False, devices=False, clock=w.clock)
        rec, _ = walk.workspace(w.driver, "here", "native", "ws", w.records)
        return rec["ws"], [n["text"] for n in rec.get("notes", [])]

    def runs_of(self, w, head):
        return [e[1][:2] for e in w.effects if e[0] == "run" and e[1][:len(head)] == head]

    def gone_by_hand(self, cls, by_hand):
        """Status names the half the machine still holds; rm takes it without re-running what already happened."""
        w = self.world(cls)
        by_hand(w)
        word, notes = self.status_of(w)
        self.assertEqual(word, "broken")
        here = w.isdir(w.ws_dir())
        self.assertEqual(any(w.ws_dir() in n for n in notes), here, notes)
        w.effects = []
        rc, err = self.rm(w)
        self.assertEqual((rc, w.left()), (0, {}), err)
        return w

    def case_podman_rm(self):
        w = self.gone_by_hand(RealContainerWorld, lambda w: w.containers.clear())
        self.assertEqual(self.runs_of(w, ("podman", "rm")), [])

    def case_tart_delete(self):
        w = self.gone_by_hand(VmWorld, lambda w: w.vms.clear())
        self.assertEqual(self.runs_of(w, (w.tart, "delete")) + self.runs_of(w, (w.tart, "stop")), [])

    def case_deleted_ws_dir(self):
        w = self.gone_by_hand(RealContainerWorld, lambda w: w._rm_rf(["rm", w.ws_dir()], w))
        self.assertEqual(self.runs_of(w, ("podman", "unshare")), [])

    def case_edited_alias(self):
        """What the user wrote around the block is what rm leaves, byte for byte; a block the user took out is not written for."""
        w = World(self.tmp)
        conf = sshalias.alias_path(w.env)
        w.files[conf] = USER
        w.dirs.add(os.path.dirname(conf))
        sshalias.alias_set(w, w.env, "ws", "1.2.3.4", "u")
        w.files[conf] = w.files[conf].replace("HostName 1.2.3.4", "HostName hand.example") + LATER
        sshalias.alias_set(w, w.env, "ws", "5.6.7.8", "u")
        self.assertIn("HostName 5.6.7.8", w.files[conf])
        self.assertNotIn("hand.example", w.files[conf])
        sshalias.alias_remove(w, w.env, "ws")
        self.assertEqual(w.files[conf], USER + LATER)
        w.effects = []
        sshalias.alias_remove(w, w.env, "ws")
        self.assertEqual(w.effects, [])

    def case_fetch_into_published_base(self):
        """A snapshot whose tree moved is not the one the record names: new builds on neither, and leaves it as it is."""
        w = World(self.tmp)
        w.react(["git", "-C"], lambda a, f: Result(0, "b" * 40 + "\n") if a[3:] == ["rev-parse", "HEAD"] else World._git(w, a, f))
        base = w.driver.store.snapshots_dir()
        for given in ("", "main-1"):
            w.effects = []
            self.refused(lambda: workspace.new_detached_run(w.driver, w.records, w.lock, w.clock, "ws", given, "native"))
            self.assertEqual([a for a in self.runs(w) if a[0] == "wkdev-create"], [])
            self.assertEqual([e for e in w.effects if e[0] in ("write", "remove", "mkdir") and e[1].startswith(base)], [])
            (t,) = w.records.list()
            self.assertEqual((t.verdict(), t.stage()), ("failed", ["base"]))

    def test_machine_wins(self):
        for case in ("podman_rm", "tart_delete", "deleted_ws_dir", "edited_alias", "fetch_into_published_base"):
            with self.subTest(case=case):
                getattr(self, "case_" + case)()
