"""`wk key sudo` typed into a world whose sudo and visudo answer as the real ones do, and whose build machines answer
through their own wk."""
import os
import sys
import unittest

from tests import fakes
from tests.fakes import FakeDriver, FakeRegistry
from tests.test_wk_workspace import said

DROPIN = "/etc/sudoers.d/zz-justinmichaud-passwd"
HEAD = "User justinmichaud may run the following commands on here:\n"
UNSET = HEAD + "    (ALL : ALL) ALL\n"
BLANKET = HEAD + "    (root) NOPASSWD: ALL\n"
BODY = ("# Installed by 'wk key sudo setup'. Do not edit; re-run the command instead.\n"
        "Defaults:justinmichaud timestamp_timeout=0.5\njustinmichaud ALL=(ALL:ALL) PASSWD: ALL\n")
GROUP = "wheel" if sys.platform == "darwin" else "root"
QUIESCE_PRIV = "/usr/local/libexec/wk-quiesce-priv"
TMP = "/tmp/wk-sudoers.%d" % os.getpid()
INSTALL = ("sudo", "install", "-m", "0440", "-o", "root", "-g", GROUP, TMP, DROPIN)


def window(minutes):
    return "    timestamp_timeout=%s\n\n" % minutes + UNSET


class Box(FakeDriver):
    """A build machine whose own wk is a `PEER <name> <args>` line its machine runs, answering `out`."""

    def wk(self, *args, env=None, quiet=False):
        r = self.machine.run(["sh", "-c", self.wk_cmd(args, env)])
        return r.rc, r.out


class World(fakes.World):
    """This host: `sudo -n -l` answers `listing` (`readable` False: it wants a password), `sudo -n true` runs while
    `free` (a cached timestamp or a NOPASSWD rule), and after `sudo -k` only while `still_free` (a rule later in the
    include order than the drop-in); `visudo -c` passes `parses` on the generated file, `parses_after` on the whole."""

    def __init__(self, tmp=None, listing=UNSET, free=False, readable=True, boxes=None):
        super().__init__(tmp)
        self.listing, self.free, self.readable = listing, free, readable
        self.still_free, self.parses, self.parses_after = False, True, True
        R = fakes.Result
        self.answer(["id", "-un"], out="justinmichaud\n")
        self.answer(["sh", "-c", 'command -v "$1" >/dev/null', "sh", "visudo"])
        self.react(["sudo", "-n", "-l"], lambda a, f: R(0, f.listing) if f.readable else R(1))
        self.react(["sudo", "-n", "true"], lambda a, f: R(0 if f.free else 1))
        self.react(["sudo", "-k"], lambda a, f: R(0 if setattr(f, "free", f.still_free) is None else 1))
        self.react(["visudo", "-c", "-f"], lambda a, f: R(0) if f.parses else R(1, "", "syntax error"))
        self.react(["sudo", "install"], lambda a, f: R(0 if f._set_file(a[-1], f.files[a[-2]]) is None else 1))
        self.react(["sudo", "visudo", "-c"], lambda a, f: R(0 if f.parses_after else 1))
        self.react(["sudo", "rm", "-f"], lambda a, f: R(0 if f._drop(a[-1]) is None else 1))
        self.react(["sh", "-c"], lambda a, f: R(0, f.boxes[a[2].split()[1]].out))   # PEER <name> ...
        self.boxes = {n: Box(n, machine=self, out=b) if isinstance(b, str) else b for n, b in (boxes or {}).items()}
        self.reg = FakeRegistry(self.env, self, lambda n, e: self.boxes[n], names=list(self.boxes))


class Flow(fakes.FlowTest):
    def typed(self, w, *argv):
        w.effects = []
        return self.wk(w, "key", "sudo", *argv)


class TestStatus(Flow):
    """`wk key sudo` reports one row: 0 exactly when a password is required and the window is 30 seconds or less."""

    def test_each_listing_is_judged_by_its_window_and_what_runs_without_a_password(self):
        # sudoers' last match wins: a later blanket NOPASSWD overrides an earlier ALL; one naming a program is no blanket.
        keeps, wanted = "a password is required, but sudo keeps a timestamp", "-- wanted 30 seconds"
        for world, rc, row in (
                (dict(listing=window("0.5")), 0, "a password is required, and the timestamp lasts 30 seconds"),
                (dict(listing=window("15")), 1, "%s for 15 minute(s) (900 seconds) %s" % (keeps, wanted)),
                (dict(listing=window("0")), 0, "a password is required, and no timestamp is kept at all (timeout 0) -- "
                                               "stricter than the 30 seconds this installs, so nothing to do"),
                (dict(listing=window("-1")), 1, "%s for -1 minute(s) (-60 seconds) %s" % (keeps, wanted)),
                (dict(), 1, keeps + " (timeout unset -- sudo's default)"),
                (dict(listing=UNSET + BLANKET[len(HEAD):], free=True), 1, "NOPASSWD: ALL is granted here -- root costs nothing at all"),
                (dict(listing=HEAD + "    (root) NOPASSWD: %s\n    (ALL : ALL) ALL\n" % QUIESCE_PRIV, free=True), 1,
                 "sudo runs without a password right now -- a timestamp is cached")):
            with self.subTest(row):
                w = World(self.tmp, **world)
                code, out, err = self.typed(w)
                self.assertEqual((code, out, rc == 1), (rc, "%-22s %s\n" % ("here", row), "  fix: wk key sudo setup" in err))
                self.assertEqual([], w.mutations())

    def test_a_listing_that_wants_a_password_is_judged_by_the_drop_in_alone(self):
        for held, rc, tail in ((True, 0, "but %s is installed" % DROPIN),
                               (False, 1, "and %s is not installed, so it is sudo's default (a few minutes)" % DROPIN)):
            with self.subTest(held=held):
                w = World(self.tmp, readable=False)
                if held:
                    w.files[DROPIN] = BODY
                code, out, _ = self.typed(w, "status")
                self.assertEqual((code, out.splitlines()[-1]), (rc, "  the timeout cannot be read without authenticating -- " + tail))

    def test_a_verb_it_does_not_have_and_an_empty_machine_are_refused(self):
        w = World(self.tmp)
        code, _, err = self.typed(w, "bogus")
        self.assertEqual((code, err.splitlines()[0]), (2, "warning: unknown verb: bogus (one of status, setup)"))
        code, _, err = self.typed(w, "status", "--on", "")
        self.assertEqual((code, said(err)), (1, "error: --on needs a name"))
        self.assertEqual([], w.effects)

    def test_inside_a_workspace_it_is_refused_before_anything_is_asked(self):
        w = World(self.tmp)
        w.env["WK_MARKER"] = str(self.tmp / "marker")
        (self.tmp / "marker").write_text("name=myws\n")
        code, _, err = self.typed(w)
        self.assertEqual((code, said(err).split(".")[0]), (1, "error: 'wk key' acts on a host, and this is workspace 'myws'"))
        self.assertEqual([], w.effects)


class TestSetup(Flow):
    def test_it_validates_installs_validates_again_forgets_the_timestamp_and_proves_a_password_is_asked(self):
        w = World(self.tmp, listing=BLANKET, free=True)
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err)), (0, "==> sudo on here now asks for a password, and forgets after 30 seconds"))
        self.assertEqual(w.mutations(), [("write", TMP), ("act", INSTALL), ("remove", TMP), ("act", ("sudo", "visudo", "-c")),
                                         ("act", ("sudo", "-k"))])
        self.assertLess(w.effects.index(("run", ("visudo", "-c", "-f", TMP))), w.effects.index(("act", INSTALL)))
        self.assertEqual((w.effects[-1], w.files, w.free), (("run", ("sudo", "-n", "true")), {DROPIN: BODY}, False))

    def test_a_file_that_does_not_parse_is_never_installed(self):
        w = World(self.tmp, listing=BLANKET, free=True)
        w.parses = False
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err)), (1, "error: the sudoers file wk generated does not parse -- refusing to install it"))
        self.assertEqual((w.mutations(), w.files), ([("write", TMP), ("remove", TMP)], {}))

    def test_a_sudoers_that_stops_parsing_after_the_install_has_the_drop_in_removed(self):
        w = World(self.tmp, listing=BLANKET, free=True)
        w.parses_after = False
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err)), (1, "error: sudoers was left as it was; nothing changed"))
        self.assertEqual((w.mutations()[-2:], w.files), ([("act", ("sudo", "visudo", "-c")), ("act", ("sudo", "rm", "-f", DROPIN))], {}))

    def test_a_rule_later_than_the_drop_in_that_still_grants_nopasswd_fails_it(self):
        w = World(self.tmp, listing=BLANKET, free=True)
        w.still_free = True
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err).split(":")[0]), (1, "warning"))
        self.assertIn("sudo still runs without a password after installing " + DROPIN, err)

    def test_a_drop_in_that_out_ranks_a_privileged_helper_is_named(self):
        w = World(self.tmp)
        w.files[QUIESCE_PRIV] = "#!/bin/sh\n"
        w.answer(["sudo", "-n", QUIESCE_PRIV, "status"], rc=1)
        code, _, err = self.typed(w, "setup")
        self.assertEqual(code, 1)
        self.assertIn("the drop-in out-ranks a privileged helper: " + QUIESCE_PRIV, err)

    def test_already_set_up_changes_nothing(self):
        w = World(self.tmp, listing=window("0.5"))
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err)), (0, "==> already set up on here: a password is required, and the timestamp lasts 30 seconds"))
        self.assertEqual([], w.mutations())

    def test_without_visudo_nothing_is_written(self):
        w = World(self.tmp)
        w.answer(["sh", "-c", 'command -v "$1" >/dev/null', "sh", "visudo"], rc=1)
        code, _, err = self.typed(w, "setup")
        self.assertEqual((code, said(err).split(" --")[0]), (1, "error: visudo is required (part of the sudo package) but not on PATH"))
        self.assertEqual([], w.mutations())


class TestTheFleet(Flow):
    def test_on_a_machine_its_own_wk_is_asked_and_its_line_is_the_row(self):
        w = World(self.tmp, boxes={"box": "a password is required, and the timestamp lasts 30 seconds\n"})
        code, out, _ = self.typed(w, "status", "--on", "box")
        self.assertEqual((code, out), (0, "%-22s %s\n" % ("box", "a password is required, and the timestamp lasts 30 seconds")))
        self.assertEqual([("run", ("sh", "-c", "PEER box key sudo status --quiet"))], w.effects)

    def test_setup_on_a_machine_runs_its_own_wk_on_a_terminal_for_its_password(self):
        w = World(self.tmp, boxes={"box": ""})
        self.assertEqual(self.typed(w, "setup", "--on", "box")[0], 0)
        self.assertEqual([("run_tty", ("sh", "-c", "PEER box key sudo setup"), None)], w.effects)

    def test_an_unreachable_machine_is_named_and_not_asked(self):
        for verb in ("status", "setup"):
            with self.subTest(verb):
                w = World(self.tmp, boxes={"box": FakeDriver("box", side="unreachable", why="timed out after 10s")})
                code, out, _ = self.typed(w, verb, "--on", "box")
                self.assertEqual((code, out), (1, "%-22s %s\n" % ("box", "unreachable over ssh: timed out after 10s")))
                self.assertEqual([], w.effects)

    def test_an_unknown_machine_is_refused(self):
        code, _, err = self.typed(World(self.tmp), "status", "--on", "nowhere")
        self.assertEqual((code, said(err).split(".")[0]), (1, "error: unknown place 'nowhere'"))

    def test_all_reports_this_machine_then_every_other(self):
        w = World(self.tmp, listing=window("0.5"), boxes={"box": "it holds there\n"})
        code, out, _ = self.typed(w, "status", "--all")
        self.assertEqual((code, out.splitlines()), (0, ["%-22s %s" % ("here", "a password is required, and the timestamp lasts 30 seconds"),
                                                         "%-22s %s" % ("box", "it holds there")]))

    def test_all_setup_sets_up_every_other_machine_and_only_reports_this_one(self):
        w = World(self.tmp, boxes={"box": ""})
        code, out, _ = self.typed(w, "setup", "--all")
        self.assertEqual((code, out.split()[0]), (1, "here"))
        self.assertEqual([], w.mutations())
        self.assertEqual(("run_tty", ("sh", "-c", "PEER box key sudo setup"), None), w.effects[-1])
        self.assertEqual({}, w.files)


if __name__ == "__main__":
    unittest.main()
