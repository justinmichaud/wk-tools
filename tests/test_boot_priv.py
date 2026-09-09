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
import os
import pwd
import re
import shutil
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, func_body, stub_path

HELPER = REPO / "admin" / "wk-boot-priv"
DRIVER = REPO / "boot" / "mac-volume.sh"
INSTALL = REPO / "admin" / "install.sh"

# The helper minus the privilege: its own shell options, and `deny` and `fail`
# exiting as they really do, so a refusal is a status a test can assert on.
_SAY = '''
set -euo pipefail
say()  { printf 'wk-boot-priv: %s\\n' "$*"; }
deny() { printf 'wk-boot-priv: REFUSED: %s\\n' "$*" >&2; exit 3; }
fail() { printf 'wk-boot-priv: %s\\n' "$*" >&2; exit 1; }
'''

_VCMAILBOX = '#!/bin/sh\necho "vcmailbox $*"\n'


def _lift_from(path, *funcs):
    out = []
    for func in funcs:
        text = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
                              capture_output=True, text=True).stdout
        assert text.strip(), f"could not lift {func} from {path}"
        out.append(text)
    return "\n".join(out)


def _lift(*funcs):
    return _lift_from(HELPER, *funcs)


def _lift_install(*funcs):
    return _lift_from(INSTALL, *funcs)


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

    def test_the_detach_is_one_every_machine_it_runs_on_ships(self):
        """`setsid` is util-linux and macOS does not ship it, so with it this
        verb printed "rebooting in 3s" and rebooted nothing on every Mac -- and
        exited 0 doing it, which is why `wk bench mac-ab` verifies the restart
        against kern.boottime rather than trusting the helper. `nohup` is POSIX
        and is on both. Measured on tolken, macOS 26.6.2: `command -v setsid`
        answers nothing, `command -v nohup` answers /usr/bin/nohup."""
        body = HELPER.read_text()
        code = [l for l in body.splitlines() if not l.lstrip().startswith("#")]
        self.assertEqual([], [l for l in code if "setsid" in l], code)
        for fn in ("v_reboot", "v_reboot_tryboot"):
            with self.subTest(verb=fn):
                text = re.search(r"(?ms)^%s\(\) \{.*?^\}" % fn, body).group(0)
                self.assertIn("nohup ", text)
                # Detached from the ssh that asked, whose teardown SIGHUPs the group.
                self.assertIn("&", text)
                self.assertIn("</dev/null", text)

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


class TestStatusReportsWhatThisMachineCanDo(unittest.TestCase):
    """`status` is what every caller asks before it arms anything, so a static
    yes is a claim three of its four lines cannot back: a Pi has no `bless`, a
    Mac has neither vcmailbox nor /run/systemd."""

    def _status(self, have=(), systemd=False):
        stubs = {name: "#!/bin/sh\nexit 0\n" for name in have}
        with stub_path(stubs) as binp:
            script = _SAY + _lift("v_status")
            if not systemd:
                # `[ -d /run/systemd ]` is the reading; a real one on this host
                # would answer for the test rather than the case under test.
                script = script.replace("[ -d /run/systemd ]", "false")
            return bash(script + "\nv_status\n",
                        env={"PATH": "%s:/usr/bin:/bin" % binp})

    def _lines(self, cp):
        return dict(l.split(": ", 1)[1].split("=", 1)
                    for l in cp.stdout.splitlines() if "=" in l)

    def test_a_pi_says_it_cannot_bless(self):
        cp = self._status(have=("vcmailbox",), systemd=True)
        got = self._lines(cp)
        self.assertEqual("yes", got["order"])
        self.assertEqual("yes", got["tryboot"])
        self.assertTrue(got["bless"].startswith("no"), got)

    def test_a_mac_says_it_has_neither_an_order_nor_tryboot(self):
        cp = self._status(have=("bless",))
        got = self._lines(cp)
        self.assertTrue(got["order"].startswith("no"), got)
        self.assertTrue(got["tryboot"].startswith("no"), got)
        self.assertEqual("yes", got["bless"])

    def test_every_no_says_why(self):
        cp = self._status()
        for key, value in self._lines(cp).items():
            if value == "no" or value.startswith("no ") or value.startswith("no("):
                with self.subTest(key=key):
                    self.assertIn("(", value, value)

    def test_the_reboot_every_machine_has_is_the_one_unconditional_yes(self):
        """It is the verb the bench lane needs, and the helper's own reboot is
        the same on both platforms."""
        for have in ((), ("bless",), ("vcmailbox",)):
            with self.subTest(have=have):
                self.assertEqual("yes", self._lines(self._status(have=have))["reboot"])

    def test_it_names_the_detach_its_reboot_verbs_use(self):
        """Answering is not being able. `status` said ok for as long as the
        reboot verbs detached with `setsid`, which macOS does not ship, so the
        verb exited 0 having rebooted nothing -- and every caller that read
        "the helper answers" as "this machine can be restarted" was wrong. The
        verb names its mechanism, so a helper too old to name one is detectable
        from the driving end without asking its version."""
        got = self._lines(self._status())
        self.assertEqual("nohup", got["detach"])
        body = HELPER.read_text()
        for fn in ("v_reboot", "v_reboot_tryboot"):
            with self.subTest(verb=fn):
                text = re.search(r"(?ms)^%s\(\) \{.*?^\}" % fn, body).group(0)
                self.assertIn(got["detach"] + " ", text)

    def test_the_mac_driver_requires_that_line_and_not_merely_an_answer(self):
        line = [l for l in (REPO / "boot" / "mac-volume.sh").read_text().splitlines()
                if l.startswith("mv_reboot_ready()")]
        self.assertEqual(1, len(line), line)
        self.assertIn("detach=", line[0])
        self.assertNotIn("status >/dev/null 2>&1; }", line[0])

    def test_it_still_opens_with_ok(self):
        """`mv_reboot_ready` reads the exit status, and `wk doctor` the first
        line: a status that stopped saying ok would read as a broken helper."""
        cp = self._status()
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("wk-boot-priv: ok", cp.stdout.splitlines()[0])


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
mac_firmware_default() {
    head -1 "$FW"
    tail -n +2 "$FW" > "$FW.rest" && mv "$FW.rest" "$FW"
}
m_ssh() {
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
        log = self.tmp / "m_ssh.log"
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

    def test_the_helper_is_asked_for_through_m_ssh_and_never_a_local_sudo(self):
        """`sudo -n` rides inside the command m_ssh runs, so every verb answers
        on that Mac and from any machine that can reach it."""
        text = DRIVER.read_text()
        self.assertNotIn('sudo -n "$BOOT_HELPER"', text)
        for line in text.splitlines():
            if "sudo -n" in line:
                self.assertIn("m_ssh", line, line)
        self.assertIn("m_ssh", func_body(text, "b_arm"))
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
        """One installer for all three, so the boot helper's name and the name of its
        rule are the shared table's answers rather than literals in the installer."""
        text = INSTALL.read_text()
        self.assertIn("$(wk_priv_helpers)", text)
        self.assertIn("wk_priv_sudoers", text)
        self.assertIn("visudo -cqf", text)
        for literal in ("zzz-wk-boot", "zzz-wk-card", "zzz-wk-quiesce"):
            with self.subTest(literal=literal):
                self.assertNotIn(literal, text,
                                 "a rule's name is a literal in the installer again")

    def test_claude_md_names_all_three_carve_outs(self):
        """The rule is only worth anything if it lists what actually exists."""
        text = (REPO / "CLAUDE.md").read_text()
        for h in ("wk-quiesce-priv", "wk-card-priv", "wk-boot-priv"):
            with self.subTest(helper=h):
                self.assertIn(h, text)

class TheHelperInstallsOnBothPlatforms(WkTest):
    """`install -g root` is an error on macOS, which has no `root` group -- root's is
    `wheel`. One install line asks the platform for the name, so no helper can be the one
    that gets it wrong."""

    INSTALL = INSTALL

    def test_no_block_asks_for_a_group_by_name(self):
        text = self.INSTALL.read_text()
        self.assertNotIn('-g root', text)
        self.assertNotIn('-g wheel', text)
        self.assertEqual(1, text.count('-g "$_rootgrp"'), text.count('-g "$_rootgrp"'))

    def test_the_group_is_asked_of_the_platform(self):
        for os_name, want in (("macos", "wheel"), ("linux", "root")):
            with self.subTest(os=os_name):
                cp = bash('is_macos() { %s; }\n%s\necho "$_rootgrp"'
                          % ("return 0" if os_name == "macos" else "return 1",
                             'if is_macos; then _rootgrp=wheel; else _rootgrp=root; fi'))
                self.assertEqual(want, cp.stdout.strip(), cp.stdout + cp.stderr)

class SetupRefusesRoot(WkTest):
    """Every grant is written for `id -un`. Run under sudo they all name root,
    which grants nothing to the person who logs in and leaves root-owned
    dotfiles, credentials and state in that user's home. Measured 2026-09-08:
    /etc/sudoers.d/zzz-wk-boot on tolken is 58 bytes, which is
    `<4-char user> ALL=(root) NOPASSWD: /usr/local/libexec/wk-boot-priv`."""

    SETUP = REPO / "setup"

    def test_it_refuses_and_says_why(self):
        cp = bash('id() { [ "$1" = -u ] && echo 0 || echo root; }\n'
                  'die() { echo "DIE $*"; exit 1; }\n'
                  'export SUDO_USER=justinmichaud\n'
                  '%s' % self._guard())
        out = cp.stdout + cp.stderr
        self.assertIn("DIE", out, out)
        self.assertIn("as yourself", out, out)
        self.assertIn("justinmichaud", out, "it does not name who to run as")

    def test_it_says_nothing_when_run_as_a_person(self):
        cp = bash('id() { [ "$1" = -u ] && echo 1000 || echo someone; }\n'
                  'die() { echo "DIE $*"; exit 1; }\n'
                  '%s\necho PASSED' % self._guard())
        out = cp.stdout + cp.stderr
        self.assertIn("PASSED", out, out)
        self.assertNotIn("DIE", out, out)

    def test_every_grant_is_built_from_the_running_user(self):
        """So the guard is the only thing standing between a sudo'd setup and a
        sudoers file that grants nobody."""
        text = INSTALL.read_text()
        self.assertEqual(1, text.count('$(id -un) ALL=(root) NOPASSWD:'),
                         text.count('$(id -un) ALL=(root) NOPASSWD:'))

    def _guard(self):
        """The die message contains a blank line of its own, so the lift ends at
        the closing quote rather than at the first paragraph break."""
        text = self.SETUP.read_text()
        start = text.index('[ "$(id -u)" -eq 0 ]')
        end = text.index('SUDO_USER.}"', start) + len('SUDO_USER.}"')
        return text[start:end]


# admin/install.sh, minus the privilege and minus the real machine's paths. Every path it
# writes comes from wk_priv_path/wk_priv_sudoers/$_libexec/$_rules_dir, so redefining those
# four moves the whole installer into a scratch directory; `sudo` becomes a function that
# runs its argv under this user (nothing here is root) and refuses any path outside it.
_FAKE = '''set -euo pipefail
. "$WK_ROOT/lib/common.sh"
FAKE=%(fake)s
NOSUDO=%(nosudo)d
VISUDO=%(visudo)d
_libexec="$FAKE/libexec"
_check_source="$WK_ROOT/boot/check-boot-files.py"
_check_target="$_libexec/wk-check-boot-files.py"
_rules_dir="$FAKE/rules"
_rootgrp="$(id -gn)"
is_linux() { return %(notlinux)d; }
is_macos() { return %(notmacos)d; }
wk_priv_path() { printf '%%s/%%s' "$_libexec" "$1"; }
wk_priv_sudoers() { local n="${1#wk-}"; printf '%%s/sudoers.d/zzz-wk-%%s' "$FAKE" "${n%%%%-priv}"; }

sudo() {
    local a args=()
    if [ "${1:-}" = -n ]; then
        shift
        if [ "${1:-}" = true ]; then return "$NOSUDO"; fi
        # `sudo -n -l`: the rules sudo would apply to this user, which is the only thing
        # lib/common.sh's wk_priv_answers reads. A run under a cached credential succeeds
        # below whatever this lists, so the two cannot stand in for one another.
        if [ "${1:-}" = -l ]; then
            grep -h "^$(id -un) " "$FAKE"/sudoers.d/* 2>/dev/null || true
            return 0
        fi
    fi
    if [ "${1:-}" = visudo ]; then return "$VISUDO"; fi
    for a in "$@"; do
        case "$a" in
            root) args+=("$(id -un)") ;;
            "$FAKE"/*|"$WK_ROOT"/*) args+=("$a") ;;
            /*) printf 'OUTSIDE %%s\\n' "$a" >&2; return 0 ;;
            *) args+=("$a") ;;
        esac
    done
    "${args[@]}"
}
'''

_PRIV_FUNCS = ("_priv_owner", "_priv_mode", "_priv_companions", "_priv_state",
               "_priv_explain", "_priv_repair", "_priv_converge", "_priv_retired",
               "_priv_sweep_retired")


class TestOneConvergentInstallForAllThreeHelpers(WkTest):
    """The installer's declared final state for a helper is two halves at once: the binary
    this tree ships, installed root-owned and writable by nobody else, and a grant that
    answers `sudo -n`. Measured on tolken 2026-09-08: a byte-identical root-owned
    wk-boot-priv and /etc/sudoers.d/zzz-wk-boot reading `root ALL=(root) NOPASSWD:
    /usr/local/libexec/wk-boot-priv` -- a right binary, a rule naming nobody who logs in,
    and every check that looks at the binary alone reporting done.

    Nothing here is root: `sudo` is a function (see _FAKE) that runs its argv as this user
    inside a scratch directory and refuses any absolute path outside it.
    """

    def setUp(self):
        super().setUp()
        self.fake = self.tmp / "fake"
        (self.fake / "libexec").mkdir(parents=True)
        (self.fake / "sudoers.d").mkdir(parents=True)
        self.me = pwd.getpwuid(os.getuid()).pw_name

    # --- the machine's state before the run -------------------------------------------

    def target(self, name="wk-boot-priv"):
        return self.fake / "libexec" / name

    def sudoers(self, name="wk-boot-priv"):
        short = name[len("wk-"):]
        if short.endswith("-priv"):
            short = short[:-len("-priv")]
        return self.fake / "sudoers.d" / ("zzz-wk-" + short)

    def rule(self, name="wk-boot-priv", user=None):
        return "%s ALL=(root) NOPASSWD: %s\n" % (user or self.me, self.target(name))

    def plant_binary(self, name="wk-boot-priv", stale=False, mode=0o755, companion=True):
        tgt = self.target(name)
        shutil.copyfile(REPO / "admin" / name, tgt)
        if stale:
            with tgt.open("a") as f:
                f.write("# an older revision\n")
        tgt.chmod(mode)
        if companion and name == "wk-card-priv":
            comp = self.fake / "libexec" / "wk-check-boot-files.py"
            shutil.copyfile(REPO / "boot" / "check-boot-files.py", comp)
            comp.chmod(0o644)
        return tgt

    def plant_rule(self, name="wk-boot-priv", user=None, text=None):
        p = self.sudoers(name)
        if p.exists():
            p.unlink()        # 0440, as sudo wants it
        p.write_text(text if text is not None else self.rule(name, user))
        p.chmod(0o440)
        return p

    # --- the run ----------------------------------------------------------------------

    def drive(self, script, nosudo=0, visudo=0, granted=None, macos=False,
              owner="root"):
        """The lifted installer, run against that state. nosudo=1 is a machine with no
        passwordless sudo, which with no terminal (this harness has none) is the branch
        that reports rather than installs. The grant half is lib/common.sh's real
        wk_priv_answers reading the stub `sudo -l` listing, unless `granted` forces the
        answer. Nothing here can be owned by root, so the one function that reads an owner
        off the filesystem answers `owner`; the real one is asked of a real file below."""
        pre = _FAKE % {"fake": _q(str(self.fake)), "nosudo": nosudo, "visudo": visudo,
                       "notlinux": 1 if macos else 0, "notmacos": 0 if macos else 1}
        if granted is not None:
            pre += "wk_priv_answers() { return %d; }\n" % (0 if granted else 1)
        cp = bash(pre + _lift_install(*_PRIV_FUNCS)
                  + "\n_priv_owner() { printf '%%s' %s; }\n" % _q(owner) + script
                  + '\necho "CHANGES=$WK_CHANGES"\n', env={"WK_DEBUG": "1"})
        return cp

    def converge(self, name="wk-boot-priv", platform="any", **kw):
        cp = self.drive('_priv_converge %s %s "what it is for" </dev/null'
                        % (_q(name), platform), **kw)
        m = re.search(r"CHANGES=(\d+)", cp.stdout)
        cp.changes = int(m.group(1)) if m else -1
        cp.said = cp.stdout + cp.stderr
        return cp

    def state(self, name="wk-boot-priv", **kw):
        cp = self.drive('_priv_state %s </dev/null' % _q(name), **kw)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp.stdout.splitlines()[0].strip()

    # --- the state tolken is in -------------------------------------------------------

    def test_a_rule_that_names_another_user_is_detected(self):
        """The half nothing looked at. The binary is this tree's, root-owned, 0755."""
        self.plant_binary()
        self.plant_rule(user="root")
        self.assertEqual("ok silent", self.state())

    def test_a_rule_that_names_another_user_is_rewritten(self):
        self.plant_binary()
        self.plant_rule(user="root")
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(self.rule(), self.sudoers().read_text())
        self.assertIn("installed %s" % self.sudoers(), cp.said)
        self.assertGreaterEqual(cp.changes, 1, cp.said)

    def test_the_rewritten_rule_is_the_grant_and_nothing_wider(self):
        self.plant_binary()
        self.plant_rule(user="root")
        self.converge()
        text = self.sudoers().read_text()
        self.assertEqual("%s ALL=(root) NOPASSWD: %s" % (self.me, self.target()),
                         text.strip())
        self.assertNotIn("NOPASSWD: ALL", text)
        self.assertEqual("440", oct(self.sudoers().stat().st_mode)[-3:])

    def test_the_installed_binary_is_a_copy_and_never_a_symlink_into_this_repo(self):
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertFalse(self.target().is_symlink())
        self.assertEqual((REPO / "admin" / "wk-boot-priv").read_bytes(),
                         self.target().read_bytes())

    # --- the other half, and the state that is already right --------------------------

    def test_a_stale_binary_with_a_working_grant_is_detected_and_replaced(self):
        self.plant_binary(stale=True)
        self.plant_rule()
        self.assertEqual("stale ok", self.state())
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual((REPO / "admin" / "wk-boot-priv").read_bytes(),
                         self.target().read_bytes())
        self.assertIn("installed %s" % self.target(), cp.said)

    def test_a_binary_owned_by_anyone_but_root_is_detected(self):
        self.plant_binary()
        self.plant_rule()
        cp = self.drive('_priv_state wk-boot-priv', owner="someone")
        self.assertEqual("foreign ok", cp.stdout.splitlines()[0].strip())
        self.assertIn("owned by someone, not root",
                      self.converge(owner="someone", nosudo=1).said)

    def test_a_correct_state_is_left_alone_and_reports_no_change(self):
        self.plant_binary()
        self.plant_rule()
        before = self.sudoers().stat().st_mtime_ns, self.target().stat().st_mtime_ns
        self.assertEqual("ok ok", self.state())
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(0, cp.changes, cp.said)
        self.assertIn("ok: wk-boot-priv and %s" % self.sudoers(), cp.said)
        self.assertNotIn("installing", cp.said)
        self.assertEqual(before,
                         (self.sudoers().stat().st_mtime_ns, self.target().stat().st_mtime_ns))

    def test_the_repair_is_idempotent(self):
        """Twice in a row: one change, then none. What ./setup's own contract asks of
        every stage."""
        self.plant_binary()
        self.plant_rule(user="root")
        first = self.converge()
        self.assertGreaterEqual(first.changes, 1, first.said)
        second = self.converge()
        self.assertEqual(0, second.changes, second.said)

    # --- every state a kill can leave behind ------------------------------------------

    def test_a_kill_between_the_binary_and_its_rule_converges(self):
        """The order the repair installs in, so this is the state a kill in the middle
        leaves: the helper on disk with no grant at all."""
        self.plant_binary()
        self.assertEqual("ok silent", self.state())
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(self.rule(), self.sudoers().read_text())
        self.assertEqual("ok ok", self.state())

    def test_a_kill_after_validation_and_before_the_install_converges(self):
        """`visudo -cqf` passed and the install never ran, so the candidate rule is on
        disk at its fixed path and the grant is still whatever it was."""
        cand = self.fake / "rules" / "wk-boot-priv.rule"
        cand.parent.mkdir(parents=True)
        cand.write_text("half-written garbage\n")
        self.plant_binary()
        self.plant_rule(user="root")
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(self.rule(), self.sudoers().read_text())
        self.assertFalse(cand.exists(), "the candidate rule is left behind")

    def test_the_candidate_rule_is_a_fixed_path_and_not_an_unpredictable_one(self):
        """A mktemp name a kill leaves behind is a file nothing will ever find again."""
        code = "\n".join(l for l in INSTALL.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("mktemp", code)
        self.plant_binary()
        cp = self.converge()
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual([], sorted((self.fake / "rules").glob("*")))

    def test_a_kill_before_the_companion_leaves_a_state_that_converges(self):
        """The card helper's boot-file checker is installed beside it, so a helper that
        is byte-identical with no checker beside it is a half-made install."""
        self.plant_binary("wk-card-priv", companion=False)
        self.plant_rule("wk-card-priv")
        self.assertEqual("stale ok", self.state("wk-card-priv"))
        cp = self.converge("wk-card-priv", "linux")
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual((REPO / "boot" / "check-boot-files.py").read_bytes(),
                         (self.fake / "libexec" / "wk-check-boot-files.py").read_bytes())
        self.assertEqual("ok ok", self.state("wk-card-priv"))

    def test_a_pre_zzz_grant_reads_as_the_helper_being_in_force(self):
        """Which is why it cannot be left for the repair to remove: it grants nothing
        (zz-<user>-passwd out-ranks it) and `sudo -l` lists it all the same."""
        self.plant_binary()
        (self.fake / "sudoers.d" / "wk-boot").write_text(self.rule())
        self.assertEqual("ok ok", self.state())

    def test_the_pre_zzz_grant_is_swept_before_any_helper_is_judged(self):
        self.plant_binary()
        old = self.fake / "sudoers.d" / "wk-boot"
        old.write_text(self.rule())
        cp = self.drive('_priv_sweep_retired </dev/null\n'
                        '_priv_converge wk-boot-priv any "what it is for" </dev/null')
        said = cp.stdout + cp.stderr
        self.assertEqual(0, cp.returncode, said)
        self.assertFalse(old.exists(), said)
        self.assertIn("removed %s" % old, said)
        self.assertEqual(self.rule(), self.sudoers().read_text())

    def test_every_retired_name_is_derived_and_the_sweep_comes_first(self):
        cp = self.drive('_priv_retired </dev/null')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        listed = cp.stdout.split()
        for short in ("wk-quiesce", "wk-card", "wk-boot"):
            with self.subTest(retired=short):
                self.assertIn(str(self.fake / "sudoers.d" / short), listed)
        self.assertIn(str(self.fake / "libexec" / "wk-tftpd"), listed)
        code = INSTALL.read_text()
        self.assertLess(code.index("\n_priv_sweep_retired\n"),
                        code.index("while read -r _pname"),
                        "a helper is judged before the dead grants are swept")

    # --- the refusals -----------------------------------------------------------------

    def test_visudo_refusing_installs_no_rule(self):
        """An invalid sudoers file locks the account out of sudo entirely."""
        self.plant_binary()
        cp = self.converge(visudo=1)
        self.assertEqual(1, cp.returncode, cp.said)
        self.assertIn("failed validation", cp.said)
        self.assertFalse(self.sudoers().exists(), cp.said)
        self.assertFalse((self.fake / "rules" / "wk-boot-priv.rule").exists())

    def test_visudo_refusing_leaves_an_existing_rule_alone(self):
        self.plant_binary()
        self.plant_rule(user="root")
        cp = self.converge(visudo=1)
        self.assertEqual(1, cp.returncode, cp.said)
        self.assertEqual(self.rule(user="root"), self.sudoers().read_text())

    def test_a_group_or_world_writable_helper_refuses_and_names_the_remedy(self):
        for mode in (0o775, 0o757, 0o777):
            with self.subTest(mode=oct(mode)):
                self.plant_binary(mode=mode)
                self.plant_rule()
                cp = self.converge()
                self.assertEqual(1, cp.returncode, cp.said)
                self.assertIn("root escalation", cp.said)
                self.assertIn("remove %s" % self.sudoers(), cp.said)

    def test_a_mode_that_cannot_be_read_refuses_to_vouch_for_the_grant(self):
        self.plant_binary()
        self.plant_rule()
        cp = self.drive('_priv_mode() { printf ""; }\n'
                        '_priv_converge wk-boot-priv any "what it is for"')
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("could not read the mode", cp.stdout + cp.stderr)

    def test_a_helper_missing_from_this_tree_is_named_and_skipped(self):
        cp = self.converge("wk-nothing-priv")
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(0, cp.changes, cp.said)
        self.assertIn("missing at", cp.said)

    def test_the_card_helper_is_skipped_on_macos(self):
        """macOS has no card lane, and a helper held to is_linux was how the boot helper
        went unchecked there."""
        cp = self.converge("wk-card-priv", "linux", macos=True)
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(0, cp.changes, cp.said)
        self.assertFalse(self.target("wk-card-priv").exists())
        self.assertFalse(self.sudoers("wk-card-priv").exists())
        self.assertIn("linux only", cp.said)

    def test_the_other_two_are_installed_on_macos(self):
        cp = self.drive('while read -r n w rest; do _priv_converge "$n" "$w" "$rest"; done'
                        ' <<ROWS\n$(wk_priv_helpers)\nROWS', macos=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name in ("wk-quiesce-priv", "wk-boot-priv"):
            with self.subTest(helper=name):
                self.assertEqual(self.rule(name), self.sudoers(name).read_text())
        self.assertFalse(self.target("wk-card-priv").exists())

    def test_a_grant_sudo_does_not_report_is_never_claimed_done(self):
        """The rule on disk is already byte-for-byte the one this would write and sudo
        still reports no grant -- an include after it, or a `sudo -l` this machine will
        not answer without a password. Nothing here can repair that, so the run says
        which half is wrong and claims no change it did not make, now or next time."""
        self.plant_binary()
        self.plant_rule()
        first = self.converge(granted=False)
        self.assertEqual(0, first.returncode, first.said)
        self.assertIn("does not answer", first.said)
        self.assertIn("last match", first.said)
        self.assertIn("sudo -l", first.said)
        self.assertEqual(0, first.changes, first.said)
        second = self.converge(granted=False)
        self.assertEqual(0, second.changes, second.said)

    def test_without_sudo_or_a_terminal_it_reports_which_half_is_wrong(self):
        self.plant_binary()
        self.plant_rule(user="root")
        cp = self.converge(nosudo=1)
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertEqual(0, cp.changes, cp.said)
        self.assertIn("lists no NOPASSWD rule", cp.said)
        self.assertIn("./setup --stage quiesce", cp.said)
        self.assertEqual(self.rule(user="root"), self.sudoers().read_text())

    def test_without_sudo_or_a_terminal_it_names_a_missing_binary(self):
        cp = self.converge(nosudo=1)
        self.assertEqual(0, cp.returncode, cp.said)
        self.assertIn("%s is not installed" % self.target(), cp.said)

    def test_a_cached_sudo_credential_cannot_make_a_missing_grant_read_as_done(self):
        """What masked this on tolken: ./setup authenticates once and holds the sudo
        timestamp open for its whole run, so the old `sudo -n <helper> status` succeeded
        for every helper while it was open -- including the one whose rule granted `root`
        -- and the repair was suppressed by the check meant to trigger it. Here the stub
        `sudo` runs anything (a credential is cached) while its `-l` listing names no rule
        for this path, which is that machine exactly."""
        self.plant_binary()
        self.plant_rule(user="root")
        proof = self.drive('sudo %s status && echo "A RUN SUCCEEDS"\n'
                           '_priv_state wk-boot-priv </dev/null' % _q(str(self.target())))
        self.assertIn("A RUN SUCCEEDS", proof.stdout, proof.stdout + proof.stderr)
        self.assertIn("ok silent", proof.stdout)
        cp = self.converge()
        self.assertEqual(self.rule(), self.sudoers().read_text())
        self.assertGreaterEqual(cp.changes, 1, cp.said)

    def test_no_privileged_run_decides_whether_a_grant_is_in_force(self):
        """The rule above as a property of the file: every `sudo -n` in the installer is
        `true` (is there a credential at all) or `grep` (reading a root-owned config), and
        the grant half of the state is the shared predicate that reads the rule."""
        code = "\n".join(l for l in INSTALL.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertIn('wk_priv_answers "$tgt"', code)
        for m in re.finditer(r"sudo -n (\S+)", code):
            with self.subTest(call=m.group(0)):
                self.assertIn(m.group(1), ("true", "grep"), m.group(0))

    # --- the two facts read off the filesystem ----------------------------------------

    def test_the_owner_and_mode_are_read_by_the_form_this_platform_answers(self):
        """`stat -c` on Linux, `stat -f` on macOS: the GNU form is asked first because
        Linux's `stat -f` succeeds as "filesystem status" and never as an owner."""
        f = self.tmp / "a-file"
        f.write_text("x\n")
        f.chmod(0o640)
        cp = bash('. "$WK_ROOT/lib/common.sh"\n' + _lift_install("_priv_owner", "_priv_mode")
                  + '\n_priv_owner %s\n_priv_mode %s\n' % (_q(str(f)), _q(str(f))))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual([self.me, "640"], cp.stdout.split())

    def test_an_absent_file_answers_neither(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"\nset -euo pipefail\n'
                  + _lift_install("_priv_owner", "_priv_mode")
                  + '\necho "owner=[$(_priv_owner /nope)] mode=[$(_priv_mode /nope)]"\n')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("owner=[] mode=[]", cp.stdout)


if __name__ == "__main__":
    unittest.main()
