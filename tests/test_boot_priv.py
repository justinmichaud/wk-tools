"""The boot helper: the narrowest of the three privileged carve-outs.

A workstation whose bench medium is its own stick (rpi5) is armed by a firmware
mailbox call and the reboot that spends it, both root. wk drives a workstation
as a person over a BatchMode ssh with no terminal, so `sudo -n vcmailbox` there
answers "interactive authentication is required" and the arming died before the
mailbox call ever ran (rpi5, 2026-09-03). A bench-device is driven as root and
never comes through here.

What is under test is the *grant*: a fixed verb list, one argument that is
checked rather than escaped, and a mailbox tag no caller can name. The verbs
are exercised with vcmailbox, setsid and systemctl stubbed -- this machine is
not a Pi and must not reboot.

Run: python3 -m unittest tests.test_boot_priv -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, stub_path

HELPER = REPO / "admin" / "wk-boot-priv"

# The helper minus the privilege: `deny` and `fail` exit as they really do, so
# a refusal is a status a test can assert on.
_SAY = '''
say()  { printf 'wk-boot-priv: %s\\n' "$*"; }
deny() { printf 'wk-boot-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }
fail() { printf 'wk-boot-priv: %s\\n' "$*" >&2; exit 1; }
'''

_VCMAILBOX = '#!/bin/sh\necho "vcmailbox $*"\n'


def _lift(*funcs):
    out = []
    for func in funcs:
        text = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(HELPER)],
                              capture_output=True, text=True).stdout
        assert text.strip(), f"could not lift {func} from {HELPER}"
        out.append(text)
    return "\n".join(out)


class TestTheGrantIsNarrow(WkTest):
    def _order(self, arg, path=None):
        script = (_SAY + "MAILBOX_TAG=0x0003808b\n"
                  + _lift("check_order", "v_order") + f'\nv_order {arg}\n')
        env = {"PATH": f"{path}:/usr/bin:/bin"} if path else None
        return bash(script, env=env)

    def test_a_real_order_reaches_the_fixed_tag(self):
        with stub_path({"vcmailbox": _VCMAILBOX}) as binp:
            cp = self._order("0xf64", path=binp)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("vcmailbox 0x0003808b 4 4 0xf64", cp.stdout)

    def test_the_mailbox_tag_never_comes_from_the_caller(self):
        """Otherwise this is 'call any mailbox', not 'set the boot order'."""
        body = HELPER.read_text()
        m = re.search(r"(?ms)^v_order\(\) \{.*?^\}", body)
        self.assertIsNotNone(m)
        self.assertIn('"$MAILBOX_TAG"', m.group(0))
        self.assertRegex(body, r"(?m)^MAILBOX_TAG=0x0003808b$")

    def test_anything_that_is_not_a_boot_order_is_refused(self):
        for arg in ("''", "f64", "0x", "0xzz", "0x1234567890",
                    "'0xf64; reboot'", "'0xf64 4'", "'; id'", "'$(id)'"):
            with self.subTest(arg=arg):
                cp = self._order(arg)
                self.assertEqual(3, cp.returncode, f"{arg}: {cp.stdout}{cp.stderr}")
                self.assertIn("REFUSED", cp.stderr)

    def test_the_reboot_verbs_take_no_arguments(self):
        for fn, verb in (("v_reboot", "reboot"),
                         ("v_reboot_tryboot", "reboot-tryboot")):
            with self.subTest(verb=verb):
                cp = bash(_SAY + _lift(fn) + f"\n{fn} something\n")
                self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("takes no arguments", cp.stderr)

    def test_it_writes_no_file_and_names_no_path(self):
        """The whole grant is two firmware operations. A path here would make
        it a file-writing helper, which is a different and much larger thing --
        /run/systemd/reboot-param is systemd's own fixed interface and the only
        one, written with a literal the caller cannot influence."""
        body = HELPER.read_text()
        paths = [p for p in re.findall(r'>\s*"?(/[A-Za-z0-9_./-]+)', body)
                 if p != "/dev/null"]   # output suppression, not a written path
        self.assertEqual(["/run/systemd/reboot-param"], sorted(set(paths)), body)

    def test_nothing_a_caller_sends_reaches_the_one_file_it_writes(self):
        """reboot-tryboot writes a literal; if an argument could reach that
        redirect the helper would be a file-writer with a fixed name."""
        body = HELPER.read_text()
        fn = re.search(r"(?ms)^v_reboot_tryboot\(\) \{.*?^\}", body).group(0)
        self.assertIn('printf "0 tryboot"', fn)
        for ref in ("$1", "$2", "$@", "$*"):
            with self.subTest(ref=ref):
                self.assertNotIn(ref + " >", fn)
                self.assertNotIn("printf \"%s\" " + ref, fn)


class TestTheDispatcherHasNoDefaultThatRuns(unittest.TestCase):
    def test_an_unknown_verb_is_a_usage_error(self):
        cp = bash(_SAY + '''
verb="wat"; shift || true
''' + re.search(r"(?ms)^case \"\$verb\" in.*?^esac", HELPER.read_text()).group(0) + "\n")
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("usage: wk-boot-priv", cp.stderr)

    def test_every_verb_in_the_usage_line_is_dispatched(self):
        text = HELPER.read_text()
        case = re.search(r"(?ms)^case \"\$verb\" in.*?^esac", text).group(0)
        usage = re.search(r"usage: wk-boot-priv ([^\"]*)", text).group(1)
        for verb in re.findall(r"[a-z][a-z-]+", usage.split("<")[0]):
            with self.subTest(verb=verb):
                self.assertRegex(case, rf"{re.escape(verb)}\)\s*v_")


class TestTheDrivingEndAsksForTheOperationNotThePrivilege(unittest.TestCase):
    """One spelling for both roles: a bench-device is already root, a
    workstation goes through the helper, and no driver decides which."""

    def test_the_rpi5_driver_never_sudoes_the_firmware_itself(self):
        body = (REPO / "boot" / "rpi5-usb.sh").read_text()
        self.assertNotIn("r_sudo \"vcmailbox", body)
        self.assertIn("boot_priv order", body)
        self.assertIn("boot_priv reboot", body)

    def test_it_checks_the_helper_before_the_firmware_call(self):
        """Otherwise a missing helper is reported as a firmware that would not
        answer, which sends the reader to the wrong place."""
        body = (REPO / "boot" / "rpi5-usb.sh").read_text()
        arm = body[body.index("b_arm()"):]
        arm = arm[:arm.index("\n}\n")]
        self.assertLess(arm.index("boot_priv_require"), arm.index("boot_priv order"))

    def test_the_refusal_names_the_remedy(self):
        body = (REPO / "boot" / "machines.sh").read_text()
        fn = body[body.index("boot_priv_require()"):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("./setup --stage quiesce", fn)

    def test_setup_installs_it_with_its_own_sudoers_rule(self):
        text = (REPO / "admin" / "install.sh").read_text()
        self.assertIn("wk-boot-priv", text)
        self.assertIn("zzz-wk-boot", text)
        self.assertIn("visudo -cqf", text)

    def test_claude_md_names_all_three_carve_outs(self):
        """The rule is only worth anything if it lists what actually exists."""
        text = (REPO / "CLAUDE.md").read_text()
        for h in ("wk-quiesce-priv", "wk-card-priv", "wk-boot-priv"):
            with self.subTest(helper=h):
                self.assertIn(h, text)


if __name__ == "__main__":
    unittest.main()
