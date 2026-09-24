"""`wk key sudo`: lib/wk/sudo.py's Sudo (the status verdict read from `sudo -n -l`
under sudoers' last-match rule, and the drop-in write with its before/after
`visudo -c` validation and cleared-timestamp property test), cmd/key's argv for
it and the --target/--all fan-out. Every sudo/visudo call is a Fake answer;
nothing here runs a real one.

Run: python3 tests/run.py -k tests.test_wk_sudo
"""

import importlib.machinery
import importlib.util
import os
import sys
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import sudo  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.sudo import Sudo, timeout_desc, timeout_is_ours, timeout_secs  # noqa: E402

CMD_KEY = REPO / "cmd" / "key"


def _load_cmd():
    loader = importlib.machinery.SourceFileLoader("wk_cmd_key_sudo_test", str(CMD_KEY))
    spec = importlib.util.spec_from_file_location("wk_cmd_key_sudo_test", str(CMD_KEY), loader=loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


cmd = _load_cmd()


def key_sudo(argv, env=None, reg=None):
    """`wk key sudo <argv>` as the dispatcher hands it on."""
    return cmd.main(["sudo"] + argv, env={} if env is None else env, reg=reg or _FakeRegistry())

LISTING_UNSET = "User justinmichaud may run the following commands on tolken:\n    (ALL : ALL) ALL\n"


def _fake(free=False, listing=LISTING_UNSET, rc_l=0):
    f = Fake("here")
    f.answer(["id", "-un"], 0, "justinmichaud\n")
    f.answer(["hostname", "-s"], 0, "tolken\n")
    f.answer(["sudo", "-n", "true"], 0 if free else 1)
    f.answer(["sudo", "-n", "-l"], rc_l, listing)
    return f


class TestArgv(unittest.TestCase):
    """What each spelling reaches: the verb, --target and --all read through wk.decl.Args."""

    def reached(self, argv):
        seen = []
        with mock.patch.object(sudo, "on_target", lambda reg, a, t, env: seen.append(("target", a, t)) or 0), \
                mock.patch.object(sudo, "all_machines", lambda reg, a, env: seen.append(("all", a)) or 0), \
                mock.patch.object(sudo, "status_here", lambda s, env: seen.append(("here", "status")) or 0), \
                mock.patch.object(Sudo, "setup", lambda s: seen.append(("here", "setup")) or 0):
            key_sudo(argv)
        return seen

    def test_bare_defaults_to_status(self):
        self.assertEqual(self.reached([]), [("here", "status")])

    def test_setup_here(self):
        self.assertEqual(self.reached(["setup"]), [("here", "setup")])

    def test_setup_with_target(self):
        self.assertEqual(self.reached(["setup", "--target", "moose"]), [("target", "setup", "moose")])

    def test_target_needs_a_name(self):
        with self.assertRaises(Refused):
            self.reached(["--target", ""])

    def test_an_unknown_verb_is_refused(self):
        with self.assertRaises(Refused):
            self.reached(["bogus"])

    def test_all_flag(self):
        self.assertEqual(self.reached(["status", "--all"]), [("all", "status")])


class TestTimeoutMath(unittest.TestCase):
    def test_desc_and_secs_agree(self):
        self.assertEqual(timeout_desc("0.5"), "30 seconds")
        self.assertEqual(timeout_secs("0.5"), "30")

    def test_is_ours_compares_numerically(self):
        self.assertTrue(timeout_is_ours("0.5", "0.5"))
        self.assertTrue(timeout_is_ours(".5", "0.50"))
        self.assertFalse(timeout_is_ours("15", "0.5"))
        self.assertFalse(timeout_is_ours("", "0.5"))
        self.assertFalse(timeout_is_ours("unset", "0.5"))


class TestVerdict(unittest.TestCase):
    """0 exactly when a password is required and the window is 30s or less."""

    def test_a_password_required_with_our_window_passes(self):
        f = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        rc, msg = Sudo(f, {"WK_SUDO_TIMEOUT_MIN": "0.5"}).verdict()
        self.assertEqual(rc, 0, msg)
        self.assertIn("30 seconds", msg)

    def test_a_password_required_with_a_longer_window_fails(self):
        f = _fake(free=False, listing="    timestamp_timeout=15\n\n" + LISTING_UNSET)
        rc, msg = Sudo(f, {"WK_SUDO_TIMEOUT_MIN": "0.5"}).verdict()
        self.assertEqual(rc, 1)
        self.assertIn("wanted 30 seconds", msg)

    def test_zero_timeout_is_stricter_and_passes(self):
        f = _fake(free=False, listing="    timestamp_timeout=0\n\n" + LISTING_UNSET)
        rc, msg = Sudo(f, {"WK_SUDO_TIMEOUT_MIN": "0.5"}).verdict()
        self.assertEqual(rc, 0)
        self.assertIn("stricter", msg)

    def test_unset_timeout_fails(self):
        f = _fake(free=False, listing=LISTING_UNSET)
        rc, msg = Sudo(f, {}).verdict()
        self.assertEqual(rc, 1)
        self.assertIn("sudo's default", msg)

    def test_nopasswd_last_match_wins_over_an_earlier_plain_all(self):
        """sudoers' last-match rule: an earlier untagged ALL is overridden by
        a later blanket NOPASSWD, so the verdict reads the drop-in has not
        taken (root costs nothing), not merely a cached timestamp."""
        listing = ("User justinmichaud may run the following commands on tolken:\n"
                   "    (ALL : ALL) ALL\n"
                   "    (root) NOPASSWD: ALL\n")
        f = _fake(free=True, listing=listing)
        rc, msg = Sudo(f, {}).verdict()
        self.assertEqual(rc, 1)
        self.assertIn("NOPASSWD: ALL is granted", msg)

    def test_a_scoped_nopasswd_does_not_count_as_blanket(self):
        """A NOPASSWD grant naming one program is a deliberate exception, not
        the blanket the last-match check looks for."""
        listing = ("User justinmichaud may run the following commands on tolken:\n"
                   "    (root) NOPASSWD: /usr/local/libexec/wk-quiesce-priv\n"
                   "    (ALL : ALL) ALL\n")
        f = _fake(free=True, listing=listing)
        rc, msg = Sudo(f, {}).verdict()
        self.assertEqual(rc, 1)
        self.assertIn("a timestamp is cached", msg)

    def test_unreadable_with_the_dropin_installed_passes(self):
        f = _fake(free=False, rc_l=1)
        s = Sudo(f, {})
        f.files[s.dropin()] = "..."
        rc, msg = s.verdict()
        self.assertEqual(rc, 0)
        self.assertIn("is installed", msg)

    def test_unreadable_without_the_dropin_fails(self):
        f = _fake(free=False, rc_l=1)
        rc, msg = Sudo(f, {}).verdict()
        self.assertEqual(rc, 1)
        self.assertIn("sudo's default", msg)


def _setup_fake(free_before=True, install_ok=True, post_check_ok=True, property_holds=True):
    """A machine on which verdict() fails at first (nopasswd granted), and
    every command Sudo.setup() runs in order answers -- the last `sudo -n
    true`, asked after `sudo -k`, is the property test."""
    f = Fake("here")
    f.answer(["id", "-un"], 0, "justinmichaud\n")
    f.answer(["hostname", "-s"], 0, "tolken\n")
    f.answer(["which", "visudo"], 0, "/usr/bin/visudo\n")
    listing = ("User justinmichaud may run the following commands on tolken:\n"
               "    (root) NOPASSWD: ALL\n") if free_before else LISTING_UNSET
    f.answer(["sudo", "-n", "-l"], 0, listing)

    calls = {"n": 0}

    def sudo_true(argv, fake):
        from wk.machine import Result
        calls["n"] += 1
        if calls["n"] == 1:
            return Result(0 if free_before else 1)
        return Result(1 if property_holds else 0)
    f.react(["sudo", "-n", "true"], sudo_true)

    f.answer(["visudo", "-c", "-f"], 0)
    f.answer(["sudo", "install"], 0 if install_ok else 1)
    f.answer(["sudo", "visudo", "-c"], 0 if post_check_ok else 1)
    f.answer(["sudo", "rm", "-f"], 0)
    f.answer(["sudo", "-k"], 0)
    return f


class TestSetup(unittest.TestCase):
    def test_validates_before_and_after_and_proves_the_property(self):
        f = _setup_fake(free_before=True, property_holds=True)
        rc = Sudo(f, {}, linux=False).setup()
        self.assertEqual(rc, 0)
        argvs = [e[1] for e in f.effects if e[0] == "run"]
        self.assertTrue(any(a[:3] == ("visudo", "-c", "-f") for a in argvs),
                         "the generated file is validated before it is installed")
        self.assertTrue(any(a[:2] == ("sudo", "visudo") for a in argvs),
                         "sudoers is validated again after the install")
        self.assertTrue(any(a[:2] == ("sudo", "-k") for a in argvs),
                         "the cached credential is cleared to prove the property")

    def test_a_file_that_fails_to_validate_is_never_installed(self):
        f = _setup_fake(free_before=True)
        f.answer(["visudo", "-c", "-f"], 1, "", "syntax error")
        s = Sudo(f, {}, linux=False)
        with self.assertRaises(Refused):
            s.setup()
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in f.effects))

    def test_a_sudoers_that_stops_parsing_after_install_is_removed(self):
        f = _setup_fake(free_before=True, post_check_ok=False)
        s = Sudo(f, {}, linux=False)
        with self.assertRaises(Refused):
            s.setup()
        self.assertTrue(any(e[0] == "run" and tuple(e[1][:3]) == ("sudo", "rm", "-f") for e in f.effects))

    def test_the_property_test_fails_when_still_passwordless(self):
        f = _setup_fake(free_before=True, property_holds=False)
        rc = Sudo(f, {}, linux=False).setup()
        self.assertEqual(rc, 1)

    def test_already_set_up_is_a_no_op(self):
        f = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        f.answer(["which", "visudo"], 0, "/usr/bin/visudo\n")
        s = Sudo(f, {"WK_SUDO_TIMEOUT_MIN": "0.5"}, linux=False)
        rc = s.setup()
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in f.effects if e[0] != "run"], [], "no write, install or removal at all")
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in f.effects))


class TestSetupConvergesAndDryRun(unittest.TestCase):
    """setup() is idempotent: verdict() fails the same way on a fresh or a
    partially-applied machine, so re-running the whole flow after a kill at
    any point converges on the same result -- there is no partial resume to get wrong."""

    class World:
        def __init__(self):
            self.fake = _setup_fake(free_before=False, property_holds=True)
            self.rc = None

    @staticmethod
    def _run_once(w):
        w.rc = Sudo(w.fake, {}, linux=False).setup()

    @staticmethod
    def _state(w):
        return w.rc

    def test_killed_after_any_effect_and_rerun_converges(self):
        converges(self, self.World, self._run_once, self._state)

    def test_a_dry_run_touches_no_file_and_makes_no_real_effect(self):
        wet = self.World()
        self._run_once(wet)
        self.assertEqual(wet.rc, 0)

        dry = self.World()
        before = dict(dry.fake.files)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            self._run_once(dry)
        self.assertEqual(dry.fake.files, before)
        self.assertEqual([e for e in dry.fake.effects if e[0] == "run" and e[1][:2] == ("sudo", "install")], [])


class _FakeTarget:
    def __init__(self, side="answering", why="", wk_result=(0, "ok\n")):
        self.name = "box"
        self.env = {}
        self._side = side
        self._why = why
        self._wk_result = wk_result

    def probe(self):
        return self._side, self._why

    def wk(self, *args, env=None, quiet=False):
        self.asked = args
        return self._wk_result


class _FakeRegistry:
    def __init__(self, targets_by_name=None, machine=None):
        self._targets = targets_by_name or {}
        self.machine = machine or Fake("here")

    def in_workspace(self):
        return False

    def load(self, name):
        if name not in self._targets:
            raise LookupError("unknown target '%s'" % name)
        return self._targets[name]

    def machines(self):
        return list(self._targets)


class TestTargetAndAll(unittest.TestCase):
    def test_target_status_asks_the_machine_and_reports_its_line(self):
        box = _FakeTarget(wk_result=(0, "tolken box status line\n"))
        rc = key_sudo(["status", "--target", "box"], reg=_FakeRegistry({"box": box}))
        self.assertEqual(rc, 0)
        self.assertEqual(box.asked, ("key", "sudo", "status", "--quiet"))

    def test_target_setup_goes_over_a_tty(self):
        reg = _FakeRegistry({"box": _FakeTarget(wk_result=(0, "setup ok\n"))})
        rc = key_sudo(["setup", "--target", "box"], reg=reg)
        self.assertEqual(rc, 0)

    def test_an_unreachable_target_is_named_not_run(self):
        reg = _FakeRegistry({"box": _FakeTarget(side="unreachable", why="timed out after 10s")})
        rc = key_sudo(["status", "--target", "box"], reg=reg)
        self.assertEqual(rc, 1)

    def test_an_unknown_target_is_refused(self):
        reg = _FakeRegistry({})
        with self.assertRaises(Refused):
            key_sudo(["status", "--target", "nowhere"], reg=reg)

    def test_all_reports_the_local_machine_and_fans_out(self):
        local = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        reg = _FakeRegistry({"box": _FakeTarget(wk_result=(0, "box status line\n"))}, machine=local)
        rc = key_sudo(["status", "--all"], env={"WK_SUDO_TIMEOUT_MIN": "0.5"}, reg=reg)
        self.assertEqual(rc, 0)

    def test_all_setup_never_sets_up_the_local_machine(self):
        """`--all` always reports the local machine's own status, whatever the action -- only
        'wk key sudo setup' bare or --target actually sets a machine up."""
        local = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        reg = _FakeRegistry({"box": _FakeTarget(wk_result=(0, "setup ok\n"))}, machine=local)
        key_sudo(["setup", "--all"], env={"WK_SUDO_TIMEOUT_MIN": "0.5"}, reg=reg)
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in local.effects))


class TestRefusesInAWorkspace(unittest.TestCase):
    def test_refuses_in_a_workspace(self):
        reg = _FakeRegistry({})
        reg.in_workspace = lambda: True
        with self.assertRaises(Refused):
            key_sudo(["status"], env={"WK_NAME": "myws"}, reg=reg)



class TestTheOldSpellings(unittest.TestCase):
    def test_wk_sudo_and_wk_backup_are_tombstones_naming_wk_key(self):
        for old, new in (("sudo", "'wk sudo' is now 'wk key sudo'"), ("backup", "'wk backup' is now 'wk key backup'")):
            with self.subTest(old=old):
                cp = run(old)
                self.assertEqual(cp.returncode, 1, cp.stdout)
                self.assertIn(new, cp.stdout)


if __name__ == "__main__":
    unittest.main()
