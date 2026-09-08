"""The boot helper: the narrowest of the three privileged carve-outs.

A workstation whose bench medium is its own stick (rpi5) is armed by a firmware
mailbox call and the reboot that spends it, both root. wk drives a workstation
as a person over a BatchMode ssh with no terminal, so `sudo -n vcmailbox` there
answers "interactive authentication is required" and the arming died before the
mailbox call ever ran (rpi5, 2026-09-03). A bench-device is driven as root and
never comes through here.

A Mac's bench install is armed by `bless --setBoot`, also root, and the same
BatchMode ssh has the same problem.

What is under test is the *grant*: a fixed verb list, one argument that is
checked rather than escaped, a mailbox tag no caller can name, and a bless
whose only volume is a mounted wk benchmark install that is not the running
one. The verbs are exercised with vcmailbox, setsid, systemctl, bless and
stat stubbed -- this machine is not a Pi, is not a Mac, and must not reboot.

`bless --help` on macOS 26.6.2 lists --user/--stdinpass under Snapshot
options and not under Mount Mode, so the helper blesses with root alone and
quotes what bless answered; whether that suffices is the platform's answer and
one run has it. It reads no credential and puts nothing on stdin, which is
pinned below.

Run: python3 -m unittest tests.test_boot_priv -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, func_body, stub_path

HELPER = REPO / "admin" / "wk-boot-priv"
DRIVER = REPO / "boot" / "mac-volume.sh"

# The helper minus the privilege: its own shell options, and `deny` and `fail`
# exiting as they really do, so a refusal is a status a test can assert on.
_SAY = '''
set -euo pipefail
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


def _q(word):
    return "'" + word.replace("'", "'\\''") + "'"


def _code():
    """The helper minus its prose, so a comment recording a measurement can
    neither satisfy nor break an assertion about what runs."""
    return "\n".join(l for l in HELPER.read_text().splitlines()
                      if not l.lstrip().startswith("#"))


def _dispatcher():
    return re.search(r"(?ms)^case \"\$verb\" in.*?^esac",
                     HELPER.read_text()).group(0)


# `stat -f %d` as the helper asks it: a fake volume whose name says `running`
# shares the root's device number, which is how the helper spots the install it
# is booted from.
_STAT = """#!/bin/sh
case "$3" in /|*running*) echo 1 ;; *) echo 2 ;; esac
"""

_BLESS = """#!/bin/sh
echo "bless-args: $*"
"""

_BLESS_REFUSES = """#!/bin/sh
echo "bless: Error -60005: unable to sign the LocalPolicy" >&2
exit 1
"""


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
        """Every spelling that is not one of the six, including the near
        misses: the case has no arm that runs anything."""
        for verb in ("wat", "", "bless", "boot", "boot-volume ", "BOOT-HOST",
                     "--help", "status;id", "$(id)"):
            with self.subTest(verb=verb):
                cp = bash(_SAY + '''
verb=%s; shift || true
''' % _q(verb) + _dispatcher() + "\n")
                self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("usage: wk-boot-priv", cp.stderr)

    def test_every_verb_in_the_usage_line_is_dispatched(self):
        text = HELPER.read_text()
        case = re.search(r"(?ms)^case \"\$verb\" in.*?^esac", text).group(0)
        usage = re.search(r"usage: wk-boot-priv ([^\"]*)", text).group(1)
        for verb in re.findall(r"[a-z][a-z-]+", usage.split("<")[0]):
            with self.subTest(verb=verb):
                self.assertRegex(case, rf"{re.escape(verb)}\)\s*v_")


MARKER = "profile=perf-macos\nid=webkit-2.52-perf-macos\n"


class TestTheBlessGateIsTheVolumeAndNotAnArgument(WkTest):
    """bench_install's `/Volumes/*` glob is the whole gate, so each test builds
    a fake /Volumes and points the lifted function's one path literal at it."""

    def _volumes(self, *installs):
        root = self.tmp / "Volumes"
        root.mkdir(parents=True, exist_ok=True)
        for name, marker in installs:
            v = root / name
            (v / "System/Library/CoreServices").mkdir(parents=True)
            (v / "System/Library/CoreServices/SystemVersion.plist").write_text("<plist/>\n")
            (v / "etc").mkdir(parents=True)
            if marker is not None:
                (v / "etc/wk-image").write_text(marker)
        return root

    def _bless(self, root, verb="boot-volume", args="", bless=_BLESS):
        lifted = _lift("bench_install", "_bless", "v_boot_volume", "v_boot_host")
        lifted = lifted.replace("/Volumes/*", f"{root}/*")
        script = (_SAY + lifted
                  + "\nv_%s %s\n" % (verb.replace("-", "_"), args))
        with stub_path({"bless": bless, "stat": _STAT}) as binp:
            return bash(script, env={"PATH": f"{binp}:/usr/bin:/bin"})

    def test_the_one_install_is_blessed_with_root_alone(self):
        """`bless --help` documents --user/--stdinpass as Snapshot options, so
        Mount Mode with root alone is what this asks for, and whether that
        suffices is in the run rather than in a comment."""
        root = self._volumes(("WK Bench", MARKER))
        cp = self._bless(root)
        out = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("bless-args: --mount %s/WK Bench --setBoot" % root, out)
        self.assertIn("blessed %s/WK Bench" % root, out)

    def test_it_reads_no_credential_and_sends_nothing_on_stdin(self):
        """The narrowing worth pinning: no file on disk can change what this
        supplies, so there is no login password for a machine to have to keep."""
        code = _code()
        for gone in ("--user", "--stdinpass", "wk-bench/password",
                     "owner-password", "BENCH_ACCOUNT"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, code)
        for line in code.splitlines():
            if "bless --mount" in line:
                self.assertNotIn("<", line, line)   # no file reaches its stdin
        root = self._volumes(("WK Bench", MARKER))
        cp = self._bless(root)
        self.assertIn("bless-args: --mount %s/WK Bench --setBoot\n" % root,
                      cp.stdout, cp.stdout + cp.stderr)

    def test_a_refusal_repeats_what_bless_said(self):
        root = self._volumes(("WK Bench", MARKER))
        cp = self._bless(root, bless=_BLESS_REFUSES)
        out = cp.stdout + cp.stderr
        self.assertEqual(1, cp.returncode, out)
        self.assertIn("Error -60005: unable to sign the LocalPolicy", out)
        self.assertIn("bless exited 1", out)
        self.assertNotIn("blessed", out)

    def test_nothing_is_blessed_where_the_count_is_not_one(self):
        for installs, want in (((), "0 mounted"),
                               ((("WK Bench", MARKER),
                                 ("WK Bench 2", MARKER)), "2 mounted")):
            with self.subTest(want=want):
                cp = self._bless(self._volumes(*installs))
                out = cp.stdout + cp.stderr
                self.assertEqual(3, cp.returncode, out)
                self.assertIn(want, out)
                self.assertNotIn("bless-args", out)

    def test_a_volume_that_is_not_a_wk_benchmark_install_is_not_a_candidate(self):
        """The running install (same device number as /), a macOS install with
        no marker, and one whose marker names another profile."""
        for name, marker in (("running-install", MARKER),
                             ("Some Other HD", None),
                             ("Yocto Scratch", "profile=webkit-yocto\n")):
            with self.subTest(volume=name):
                cp = self._bless(self._volumes((name, marker)))
                out = cp.stdout + cp.stderr
                self.assertEqual(3, cp.returncode, out)
                self.assertIn("0 mounted", out)
                self.assertNotIn("bless-args", out)

    def test_the_bless_verbs_take_no_arguments(self):
        for verb in ("boot-volume", "boot-host"):
            with self.subTest(verb=verb):
                cp = self._bless(self._volumes(), verb=verb, args="/Volumes/x")
                self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("takes no arguments", cp.stderr)

    def test_the_running_install_is_blessed_by_mount_point_and_nothing_else(self):
        cp = self._bless(self._volumes(), verb="boot-host")
        out = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("bless-args: --mount / --setBoot", out)
        self.assertIn("blessed the running install", out)


_PRE = """. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"
NODE_NAME=mbp
NODE_SSH=fakemac
NODE_VOLUME="WK Bench"
. "$WK_ROOT/boot/mac-volume.sh"
is_macos() { return 1; }
mac_firmware_default() {
    head -1 "$FW"
    tail -n +2 "$FW" > "$FW.rest" && mv "$FW.rest" "$FW"
}
mv_sh() {
    printf '%s\\n' "$1" >> "$LOG"
    case "$1" in
        *"test -d"*|*"test -x"*) return 0 ;;
        *boot-host*)
            printf '%s\\n' "$HOST_SAID"
            return "$HOST_RC" ;;
        *boot-volume*)
            printf '%s\\n' "$VOL_SAID"
            return "$VOL_RC" ;;
    esac
}
"""


class TestTheDrivingEndProvesTheReturnBeforeItArms(WkTest):
    """--setBoot is sticky and Apple Silicon has no one-shot form, so `b_arm`
    blesses the running install first and refuses on what that answered."""

    # What `wk boot mbp --status` reads back, one line per call, in the order
    # a working arming produces them.
    HOST = "GRP (the host install -- a plain reboot stays in host mode)"
    BENCH = "GRP ('WK Bench' -- a plain reboot is expected to enter bench mode)"

    def _arm(self, host_rc=0, host_said="wk-boot-priv: bless said: nothing",
             vol_rc=0, vol_said="wk-boot-priv: blessed /Volumes/WK Bench",
             firmware=None):
        log = self.tmp / "mv_sh.log"
        log.write_text("")
        fw = self.tmp / "firmware"
        fw.write_text("\n".join(firmware if firmware is not None
                                else [self.HOST, self.BENCH]) + "\n")
        script = ('LOG=%s\nFW=%s\n'
                  'HOST_RC=%d\nHOST_SAID=%s\nVOL_RC=%d\nVOL_SAID=%s\n'
                  % (_q(str(log)), _q(str(fw)),
                     host_rc, _q(host_said), vol_rc, _q(vol_said))
                  + _PRE + "b_arm\n")
        cp = bash(script)
        return cp, log.read_text()

    def test_it_arms_and_asserts_the_firmware_afterwards(self):
        cp, asked = self._arm()
        out = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, out)
        self.assertIn("the firmware will boot 'WK Bench' next", out)
        self.assertLess(asked.index("boot-host"), asked.index("boot-volume"),
                        asked)

    def test_a_mac_that_cannot_boot_itself_again_is_never_sent_away(self):
        cp, asked = self._arm(
            host_rc=1,
            host_said="wk-boot-priv: bless said: Error -60005: cannot sign")
        out = cp.stdout + cp.stderr
        self.assertEqual(1, cp.returncode, out)
        self.assertIn("Error -60005: cannot sign", out)
        self.assertNotIn("boot-volume", asked)

    def test_a_firmware_that_will_not_take_the_volume_is_reported_verbatim(self):
        cp, _ = self._arm(vol_rc=1,
                          vol_said="wk-boot-priv: bless exited 1, so the "
                                   "firmware was not told")
        out = cp.stdout + cp.stderr
        self.assertEqual(1, cp.returncode, out)
        self.assertIn("nothing was changed", out)
        self.assertIn("bless exited 1", out)

    def test_a_return_the_firmware_does_not_confirm_arms_nothing(self):
        """bless exiting 0 is bless's word; what the firmware names is the
        evidence, and an unproven return is a one-way trip."""
        cp, asked = self._arm(firmware=[self.BENCH, self.BENCH])
        out = cp.stdout + cp.stderr
        self.assertEqual(1, cp.returncode, out)
        self.assertIn("a return this cannot see", out)
        self.assertNotIn("boot-volume", asked)

    def test_an_arming_the_firmware_does_not_confirm_is_reported(self):
        cp, _ = self._arm(firmware=[self.HOST, self.HOST])
        out = cp.stdout + cp.stderr
        self.assertEqual(1, cp.returncode, out)
        self.assertIn("the firmware still names", out)

    def test_the_refusal_asserts_nothing_about_a_credential(self):
        """What bless needs is the platform's answer, and the run has it."""
        body = func_body(DRIVER.read_text(), "b_arm")
        self.assertNotIn("owner-password", body)
        self.assertNotIn("volume owner", body)

    def test_the_helper_is_asked_for_through_mv_sh_and_never_a_local_sudo(self):
        """`sudo -n` rides inside the command mv_sh runs, so every verb answers
        on that Mac and from any machine that can reach it."""
        text = DRIVER.read_text()
        self.assertNotIn('sudo -n "$BOOT_HELPER"', text)
        for line in text.splitlines():
            if "sudo -n" in line:
                self.assertIn("mv_sh", line, line)
        self.assertIn("mv_sh", func_body(text, "b_arm"))
        _, asked = self._arm()
        self.assertRegex(asked, r"(?m)^sudo -n \S*wk-boot-priv'? boot-host 2>&1$")


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

class TheHelperInstallsOnBothPlatforms(WkTest):
    """`install -g root` is an error on macOS, which has no `root` group -- root's
    is `wheel`. It failed there on every run, which is why tolken carried the
    quiesce helper (whose block asked for wheel first) and not the boot one."""

    INSTALL = REPO / "admin" / "install.sh"

    def test_no_block_asks_for_a_group_by_name(self):
        text = self.INSTALL.read_text()
        self.assertNotIn('-g root', text)
        self.assertNotIn('-g wheel', text)
        self.assertEqual(3, text.count('-g "$_rootgrp"'), text.count('-g "$_rootgrp"'))

    def test_the_group_is_asked_of_the_platform(self):
        for os_name, want in (("macos", "wheel"), ("linux", "root")):
            with self.subTest(os=os_name):
                cp = bash('is_macos() { %s; }\n%s\necho "$_rootgrp"'
                          % ("return 0" if os_name == "macos" else "return 1",
                             'if is_macos; then _rootgrp=wheel; else _rootgrp=root; fi'))
                self.assertEqual(want, cp.stdout.strip(), cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
