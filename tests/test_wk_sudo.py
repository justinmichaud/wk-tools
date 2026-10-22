"""`wk key sudo` (lib/wk/sudo.py, cmd/key) against Fake sudo and visudo answers."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.fakes import FakeRegistry, FakeDriver
from tests import fakes
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import sudo  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import HAVE, Result  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.sudo import Sudo  # noqa: E402

cmd = load_cmd("key")


def key_sudo(argv, env=None, reg=None):
    """`wk key sudo <argv>` as the dispatcher hands it on."""
    return cmd.main(as_dispatched("key", ["sudo"] + argv), None, env={} if env is None else env, reg=reg or registry())

LISTING_UNSET = "User justinmichaud may run the following commands on tolken:\n    (ALL : ALL) ALL\n"


def _fake(free=False, listing=LISTING_UNSET, rc_l=0):
    f = fakes.World()
    f.answer(["id", "-un"], 0, "justinmichaud\n")
    f.answer(["hostname", "-s"], 0, "tolken\n")
    f.answer(["sudo", "-n", "true"], 0 if free else 1)
    f.answer(["sudo", "-n", "-l"], rc_l, listing)
    return f


class TestArgv(unittest.TestCase):
    """What each spelling reaches: the verb, --on and --all read by the dispatcher off key's table."""

    def reached(self, argv):
        seen = []
        with mock.patch.object(sudo, "on_machine", lambda reg, a, t, env: seen.append(("machine", a, t)) or 0), \
                mock.patch.object(sudo, "all_machines", lambda reg, a, env: seen.append(("all", a)) or 0), \
                mock.patch.object(sudo, "status_here", lambda s, env: seen.append(("here", "status")) or 0), \
                mock.patch.object(Sudo, "setup", lambda s: seen.append(("here", "setup")) or 0):
            key_sudo(argv)
        return seen

    def test_each_spelling_reaches_its_arm(self):
        for argv, want in (([], ("here", "status")), (["setup"], ("here", "setup")),
                           (["setup", "--on", "moose"], ("machine", "setup", "moose")),
                           (["status", "--all"], ("all", "status"))):
            with self.subTest(argv=argv):
                self.assertEqual([want], self.reached(argv))

    def test_an_unknown_verb_or_an_empty_target_is_refused(self):
        for argv in (["bogus"], ["--on", ""]):
            with self.subTest(argv=argv), self.assertRaises(Refused):
                self.reached(argv)


class TestVerdict(unittest.TestCase):
    """0 exactly when a password is required and the window is 30s or less."""

    def test_each_listing_s_verdict(self):
        timeout = lambda t: "    timestamp_timeout=%s\n\n" % t + LISTING_UNSET  # noqa: E731
        head = "User justinmichaud may run the following commands on tolken:\n"
        # sudoers' last match wins: a later blanket NOPASSWD overrides an earlier ALL; one naming a program is no blanket.
        for name, free, listing, rc_l, dropin, env, want, msg in (
                ("our window", False, timeout("0.5"), 0, False, {}, 0, "30 seconds"),
                ("longer window", False, timeout("15"), 0, False, {}, 1, "wanted 30 seconds"),
                ("zero", False, timeout("0"), 0, False, {}, 0, "stricter"),
                ("never expires", False, timeout("-1"), 0, False, {}, 1, ""),
                ("unset", False, LISTING_UNSET, 0, False, {}, 1, "sudo's default"),
                ("blanket last", True, head + "    (ALL : ALL) ALL\n    (root) NOPASSWD: ALL\n", 0, False, {}, 1,
                 "NOPASSWD: ALL is granted"),
                ("scoped", True, head + "    (root) NOPASSWD: /usr/local/libexec/wk-quiesce-priv\n    (ALL : ALL) ALL\n",
                 0, False, {}, 1, "a timestamp is cached"),
                ("unreadable, drop-in", False, LISTING_UNSET, 1, True, {}, 0, "is installed"),
                ("unreadable, none", False, LISTING_UNSET, 1, False, {}, 1, "sudo's default")):
            with self.subTest(name):
                f = _fake(free=free, listing=listing, rc_l=rc_l)
                s = Sudo(f, env)
                if dropin:
                    f.files[s.dropin()] = "..."
                rc, got = s.verdict()
                self.assertEqual(rc, want, got)
                self.assertIn(msg, got)


def _setup_fake(free_before=True, install_ok=True, post_check_ok=True, property_holds=True):
    """Every command Sudo.setup() runs answers; the last `sudo -n true`, asked after `sudo -k`, is the property test."""
    f = fakes.World()
    f.answer(["id", "-un"], 0, "justinmichaud\n")
    f.answer(["hostname", "-s"], 0, "tolken\n")
    f.answer(HAVE + ("visudo",))
    listing = ("User justinmichaud may run the following commands on tolken:\n"
               "    (root) NOPASSWD: ALL\n") if free_before else LISTING_UNSET
    f.answer(["sudo", "-n", "-l"], 0, listing)

    calls = {"n": 0}

    def sudo_true(argv, fake):
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
        f.answer(HAVE + ("visudo",))
        s = Sudo(f, {}, linux=False)
        rc = s.setup()
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in f.effects if e[0] != "run"], [], "no write, install or removal at all")
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in f.effects))


class TestTheWorkspaceDropIn(unittest.TestCase):
    """The drop-in container/firstrun.sh installs through `python3 -m wk.sudo workspace`: one rendered text, every variable
    ensure-bridge.sh exports kept, installed by the same validated path as `wk key sudo setup`'s."""
    FIRSTRUN = (REPO / "container" / "firstrun.sh").read_text()

    def test_the_rendered_text_keeps_every_bridge_variable(self):
        text = sudo.workspace_dropin()
        self.assertEqual(text, "# Installed by container/firstrun.sh (lib/wk/sudo.py). Do not edit; 'wk rm' and 'wk new' remake the workspace.\n"
                               "Defaults env_keep += \"http_proxy https_proxy HTTP_PROXY HTTPS_PROXY no_proxy NO_PROXY REQUESTS_CA_BUNDLE "
                               "CURL_CA_BUNDLE GIT_SSL_CAINFO SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS\"\n")
        bridge = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        for var in sudo.WS_KEEP:
            self.assertIn(var, bridge, "%s is kept but ensure-bridge.sh never exports it" % var)

    @unittest.skipUnless(shutil.which("visudo"), "no visudo on this machine")
    def test_visudo_takes_the_rendered_text(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as f:
            f.write(sudo.workspace_dropin())
        self.addCleanup(os.unlink, f.name)
        cp = subprocess.run(["visudo", "-c", "-f", f.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_the_install_validates_then_installs_root_owned_then_validates_sudoers(self):
        f = fakes.World()
        f.answer(HAVE + ("visudo",))
        for prefix in (["visudo", "-c", "-f"], ["sudo", "install"], ["sudo", "visudo", "-c"]):
            f.answer(prefix, 0)
        self.assertEqual(0, sudo.workspace_setup(f))
        runs = [e[1] for e in f.effects if e[0] == "run"]
        check, install, recheck = [a for a in runs if a[:1] in (("visudo",), ("sudo",))]
        tmp = check[3]
        self.assertEqual(check, ("visudo", "-c", "-f", tmp))
        self.assertEqual(install, ("sudo", "install", "-m", "0440", "-o", "root", "-g", "root", tmp, "/etc/sudoers.d/wk-egress"))
        self.assertEqual(recheck, ("sudo", "visudo", "-c"))
        self.assertEqual([("write", tmp), ("remove", tmp)], [e for e in f.effects if e[0] in ("write", "remove")])
        self.assertNotIn(tmp, f.files)

    def test_a_text_visudo_refuses_is_never_installed(self):
        f = fakes.World()
        f.answer(HAVE + ("visudo",))
        f.answer(["visudo", "-c", "-f"], 1, "", "syntax error")
        with self.assertRaises(Refused):
            sudo.workspace_setup(f)
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in f.effects))

    def test_firstrun_installs_it_before_its_first_sudo_and_names_no_bridge_under_sudo(self):
        text = self.FIRSTRUN
        self.assertLess(text.index("python3 -m wk.sudo workspace"), re.search(r"(?m)^\s*(if |&& )?sudo apt-get ", text).start())
        self.assertNotIn("sudo \"$WK_TOOLS/container/proxy/ensure-bridge.sh\"", text)
        self.assertNotIn("sudo $WK_TOOLS/container/proxy/ensure-bridge.sh", text)

    def test_the_module_entry_runs_the_install_and_a_refusal_is_its_status(self):
        with mock.patch.object(sudo, "workspace_setup", return_value=0) as setup:
            self.assertEqual(0, sudo.cli(["workspace"]))
        self.assertTrue(setup.called)
        self.assertEqual(2, sudo.cli(["bogus"]))
        with mock.patch.object(sudo, "workspace_setup", side_effect=Refused(3)):
            self.assertEqual(3, sudo.cli(["workspace"]))


def registry(drivers=None, machine=None):
    ts = drivers or {}
    return FakeRegistry({}, machine or Fake("here"), lambda n, e: ts[n], names=list(ts), in_workspace=lambda: False)


class TestPlaceAndAll(unittest.TestCase):
    def test_target_status_asks_the_machine_and_reports_its_line(self):
        box = FakeDriver("box", out="tolken box status line\n")
        rc = key_sudo(["status", "--on", "box"], reg=registry({"box": box}))
        self.assertEqual(rc, 0)
        self.assertEqual([args for args, _, _ in box.asked], [("key", "sudo", "status", "--quiet")])

    def test_target_setup_goes_over_a_tty(self):
        box = Fake("box")
        box.answer(["sh", "-c"])
        rc = key_sudo(["setup", "--on", "box"], reg=registry({"box": FakeDriver("box", machine=box)}))
        self.assertEqual(rc, 0)
        self.assertEqual([e[1] for e in box.effects if e[0] == "run_tty"], [("sh", "-c", "PEER box key sudo setup")])

    def test_an_unreachable_target_is_named_not_run(self):
        reg = registry({"box": FakeDriver("box", side="unreachable", why="timed out after 10s", out="ok\n")})
        rc = key_sudo(["status", "--on", "box"], reg=reg)
        self.assertEqual(rc, 1)

    def test_an_unknown_target_is_refused(self):
        reg = registry({})
        with self.assertRaises(Refused):
            key_sudo(["status", "--on", "nowhere"], reg=reg)

    def test_all_reports_the_local_machine_and_fans_out(self):
        local = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        reg = registry({"box": FakeDriver("box", out="box status line\n")}, machine=local)
        rc = key_sudo(["status", "--all"], env={}, reg=reg)
        self.assertEqual(rc, 0)

    def test_all_setup_never_sets_up_the_local_machine(self):
        local = _fake(free=False, listing="    timestamp_timeout=0.5\n\n" + LISTING_UNSET)
        box = Fake("box")
        box.answer(["sh", "-c"])
        reg = registry({"box": FakeDriver("box", machine=box)}, machine=local)
        key_sudo(["setup", "--all"], env={}, reg=reg)
        self.assertFalse(any(e[0] == "run" and e[1][:2] == ("sudo", "install") for e in local.effects))


class TestRefusesInAWorkspace(unittest.TestCase):
    def test_refuses_in_a_workspace(self):
        reg = registry({})
        reg.in_workspace = lambda: True
        with self.assertRaises(Refused):
            key_sudo(["status"], env={"WK_NAME": "myws"}, reg=reg)


if __name__ == "__main__":
    unittest.main()
