"""`wk pi bench --ab-systems` (cmd/pi): an interleaved A/B across *system
images*, a boot per leg. What these tests pin down is the verification the
whole mode exists for: a leg runs on the system it names -- proven from the
marker the running system serves, never the arming record -- or it does not
run. The boot cycle and the wrong-leg refusal run against the lifted
functions with every remote half stubbed (a stub `wk` records the boot
verbs; a fake clock drives the deadlines); the end-to-end proof is a real
`wk pi bench <machine> <plan> --ab-systems <a>,<b>` against a board.

Run: python3 -m unittest tests.test_pi_ab_systems -v
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CMD_PI = REPO / "cmd" / "pi"


def lift(fn):
    text = subprocess.run(
        ["sed", "-n", f"/^{fn}()/,/^}}/p", str(CMD_PI)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"could not lift {fn} from cmd/pi"
    return text


def bash(script, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


PRELUDE = f'''
set -uo pipefail
. "{REPO}/lib/common.sh"
machine=rpi3
MACH_ROOT=/dev/mmcblk0p2
MACH_DEVICE=/dev/mmcblk0
_b_probe_sh='probe'
b_system_kind() {{
    case "$1" in
        /dev/mmcblk0p2*) printf 'base' ;;
        /dev/mmcblk0*)   printf 'bench' ;;
        *)               printf 'unknown' ;;
    esac
}}
'''


PI_SYSTEM_TRIES = int(
    re.search(r"^PI_SYSTEM_TRIES=(\d+)", (REPO / "cmd" / "pi").read_text(), re.M).group(1))


class TestSystemBoot(unittest.TestCase):
    """pi_system_boot against a scripted board: the probe answers are a
    queue in a file (the function reads them from subshells, so a variable
    would not carry), the clock is a file the stubbed sleep advances, and
    the stub wk logs every boot verb."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-absys-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        (self.tmp / "wk").write_text("#!/bin/sh\necho \"wk $*\" >> \"$WK_LOG\"\n")
        (self.tmp / "wk").chmod(0o755)

    def _boot(self, probes, want="sys-a", m_ssh_rc=0):
        """probes: the id/rootdev pairs successive probes answer, last one
        repeating."""
        q = self.tmp / "probes"
        q.write_text("\n".join(probes) + "\n")
        clock = self.tmp / "clock"
        clock.write_text("1000")
        script = PRELUDE + f'''
WK_ROOT="{self.tmp}"
PI_SYSTEM_TRIES={PI_SYSTEM_TRIES}
WK_LOG="{self.tmp}/wk.log"; export WK_LOG
CLK="{clock}"
date() {{ cat "$CLK"; }}
sleep() {{ echo $(( $(cat "$CLK") + 60 )) > "$CLK"; }}
Q="{q}"
i_ssh() {{
    case "$1" in
        probe)
            line=$(head -1 "$Q")
            rest=$(tail -n +2 "$Q"); [ -n "$rest" ] && printf '%s\\n' "$rest" > "$Q"
            printf '%s\\n' "$line" | tr ';' '\\n' ;;
        *) echo "i_ssh: $*" >> "{self.tmp}/wk.log" ;;
    esac
}}
m_ssh() {{ return {m_ssh_rc}; }}
''' + lift("pi_system_boot") + f'''
pi_system_boot {want}
'''
        cp = bash(script)
        log = (self.tmp / "wk.log").read_text() if (self.tmp / "wk.log").exists() else ""
        return cp, log

    def test_already_on_the_wanted_system_is_a_claim_and_nothing_else(self):
        cp, log = self._boot(["id=sys-a;rootdev=/dev/mmcblk0p6"])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk-keep-running", log, "the leg did not claim the board")
        self.assertNotIn("wk boot", log, "a boot happened for a system already up")

    def test_the_other_leg_is_armed_where_it_stands(self):
        """A leg switch does not route through the rescue. The arming is a file
        on the medium, so it is taken where the board stands and the next
        reboot reads it -- one boot instead of two, and no wait on a rescue
        that an arming still in force can stop from ever appearing (rpi4,
        2026-09-01: every leg switch lost its leg to that wait)."""
        cp, log = self._boot([
            "id=sys-b;rootdev=/dev/mmcblk0p8",   # what answers first: the other leg
            "id=sys-a;rootdev=/dev/mmcblk0p6",   # what answers after the arming
        ])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("--back", log, "the leg switch went via the rescue")
        self.assertIn("wk boot rpi3 --system sys-a", log)
        self.assertIn("wk-keep-running", log)

    def test_a_board_that_comes_up_wrong_is_armed_again(self):
        """Convergence, which is what stops a round being dropped for a reason
        that is not about the code under test: the board comes up as the other
        leg twice and is re-armed each time, then lands and is claimed."""
        # Two probes per pass: the one that decides, and the one that sees the
        # board answer again after the reboot.
        cp, log = self._boot([
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-a;rootdev=/dev/mmcblk0p6",
        ])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(log.count("wk boot rpi3 --system sys-a"), 2,
                         "the second attempt did not happen:\n" + log)
        self.assertIn("wk-keep-running", log)

    def test_a_board_that_will_not_arm_from_bench_goes_back_first(self):
        """Some boards can only be armed from their rescue: the arming is an
        edit their privileged card helper makes, and only a rescue carries it
        (pi-sd). `wk boot --system` refuses, so the leg goes back and the next
        pass arms from the rescue -- neither command having to know which board
        it is."""
        q = self.tmp / "probes"
        clock = self.tmp / "clock"
        # A stub wk that refuses --system while the board is in a bench system,
        # the way cmd/boot does, and accepts it once --back has been asked for.
        (self.tmp / "wk").write_text(
            "#!/bin/sh\n"
            'echo "wk $*" >> "$WK_LOG"\n'
            'case "$*" in\n'
            '  *--system*) [ -f "$WK_LOG.back" ] || exit 1 ;;\n'
            '  *--back*) : > "$WK_LOG.back" ;;\n'
            "esac\nexit 0\n")
        (self.tmp / "wk").chmod(0o755)
        cp, log = self._boot([
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-b;rootdev=/dev/mmcblk0p8",
            "id=sys-a;rootdev=/dev/mmcblk0p6",
        ])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("wk boot rpi3 --back", log, "it never went back:\n" + log)
        self.assertLess(log.index("--back"), log.rindex("--system"),
                        "it did not arm after going back:\n" + log)
        self.assertIn("wk-keep-running", log)

    def test_a_board_between_systems_is_waited_for_not_counted(self):
        """A probe that answers nothing means the board is rebooting, which is
        not a failed attempt. Counting it spends the whole budget in seconds on
        a boot that was in progress -- which is what dropped rounds 3 to 5 of
        rpi3's first real A/B (2026-09-01)."""
        cp, log = self._boot([
            ";",                                  # nothing answers: mid-reboot
            ";",
            "id=sys-a;rootdev=/dev/mmcblk0p6",    # ...and then it is there
        ], m_ssh_rc=1)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("not answering yet", cp.stdout + cp.stderr)
        self.assertNotIn("--system", log,
                         "it armed a board that was simply still booting:\n" + log)
        self.assertIn("wk-keep-running", log)

    def test_a_board_that_never_lands_loses_the_leg_after_the_last_try(self):
        """...and it is bounded: a board that will not take the arming loses
        the leg rather than the run."""
        cp, log = self._boot(["id=sys-b;rootdev=/dev/mmcblk0p8"])
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("the leg is lost", cp.stdout + cp.stderr)
        self.assertEqual(log.count("wk boot rpi3 --system sys-a"), PI_SYSTEM_TRIES,
                         "it did not try PI_SYSTEM_TRIES times:\n" + log)

    def test_the_last_arming_is_read_before_the_leg_is_given_up(self):
        """The passes are one more than the armings: a board that comes up
        correctly on the final arming has won its leg, and reporting it lost
        discards a measurement that happened.

        This is the rpi3's every leg switch (2026-09-03): the first arming is
        accepted where the board stands but does not hold, the second is
        refused, the trip through the rescue makes the third work -- and with
        no pass left to look, the leg was lost while the board sat in exactly
        the system asked for."""
        (self.tmp / "wk").write_text(
            "#!/bin/sh\n"
            'echo "wk $*" >> "$WK_LOG"\n'
            'case "$*" in\n'
            '  *--system*)\n'
            '     n=$(( $(cat "$WK_LOG.n" 2>/dev/null || echo 0) + 1 ))\n'
            '     echo $n > "$WK_LOG.n"\n'
            '     # 1st accepted (does not hold), 2nd refused, later fine.\n'
            '     [ "$n" = 2 ] && exit 1\n'
            '     ;;\n'
            '  *--back*) : > "$WK_LOG.back" ;;\n'
            "esac\nexit 0\n")
        (self.tmp / "wk").chmod(0o755)
        cp, log = self._boot([
            "id=sys-b;rootdev=/dev/mmcblk0p8",     # decides: arm #1
            "id=sys-b;rootdev=/dev/mmcblk0p8",     # did not hold: arm #2, refused -> --back
            "id=rescue-img;rootdev=/dev/mmcblk0p2",  # on the rescue: arm #3, works
            "id=sys-a;rootdev=/dev/mmcblk0p6",     # the pass that reads arm #3
        ])
        self.assertEqual(cp.returncode, 0,
                         "the leg was given up without reading the last arming:\n"
                         + cp.stdout + cp.stderr)
        self.assertNotIn("the leg is lost", cp.stdout + cp.stderr)
        self.assertLessEqual(log.count("wk boot rpi3 --system sys-a"), PI_SYSTEM_TRIES,
                             "the armings are still bounded at PI_SYSTEM_TRIES:\n" + log)
        self.assertIn("wk-keep-running", log)

    def test_the_rescue_is_not_sent_back_before_arming(self):
        """host mode (the rescue answering as base) is the state to arm
        *from*: no --back, straight to the arming."""
        cp, log = self._boot([
            "id=rescue-img;rootdev=/dev/mmcblk0p2",
            "id=sys-a;rootdev=/dev/mmcblk0p6",
        ])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("--back", log)
        self.assertIn("wk boot rpi3 --system sys-a", log)

    def test_a_leg_that_never_comes_up_is_lost_softly_and_never_claimed(self):
        cp, log = self._boot(["id=sys-b;rootdev=/dev/mmcblk0p8"], want="sys-a")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("the leg is lost", cp.stderr)
        # The claim would land on whatever else is answering -- it must not.
        self.assertNotIn("wk-keep-running", log.split("wk boot rpi3 --system", 1)[-1])


class TestLegVerification(unittest.TestCase):
    def _leg(self, answered, expected):
        return bash(PRELUDE + f'''
image_addr() {{ printf 'rpi3-bench'; }}
i_ssh() {{
    case "$1" in
        probe) printf 'id={answered}\\nrootdev=/dev/mmcblk0p6\\nbuilder=buildroot\\nrole=bench\\n' ;;
        *slot.json*) printf '{{"slot": "base", "browser": "cog"}}' ;;
        *uname*) printf '5.15.84-v7l+' ;;
        *) printf '' ;;
    esac
}}
pi_display() {{ printf 'drm:card0-HDMI-A-1'; }}
pi_tmp() {{ PI_TMP=$(mktemp -d); }}
pi_slot_dir() {{ printf '/var/wk/slots/%s' "$1"; }}
pi_pin_governor() {{ printf 'performance'; }}
slot=base; ab=""; cores=""
''' + lift("pi_leg_prepare") + f'''
pi_leg_prepare "{expected}"
echo "prepared sysid=$sysid"
''')

    def test_the_wrong_leg_is_refused_before_anything_runs(self):
        cp = self._leg(answered="sys-b", expected="sys-a")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not the 'sys-a' this leg is for", cp.stderr)
        self.assertNotIn("prepared", cp.stdout)

    def test_the_right_leg_prepares(self):
        cp = self._leg(answered="sys-a", expected="sys-a")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("prepared sysid=sys-a", cp.stdout)


class TestParse(unittest.TestCase):
    def test_ab_systems_takes_two_different_ids(self):
        text = CMD_PI.read_text()
        self.assertIn("--ab-systems takes two system ids", text)
        self.assertIn("--ab-systems needs two different systems", text)

    def test_ab_and_ab_systems_are_mutually_exclusive(self):
        text = CMD_PI.read_text()
        self.assertIn("they are different comparisons", text)

    def test_every_leg_records_its_round_and_arm(self):
        """the report pairs rounds by ab.round/ab.arm; the system A/B sets
        them exactly like the slot A/B does."""
        body = re.search(r"^pi_bench_ab_systems\(\) \{.*?^\}", CMD_PI.read_text(), re.M | re.S).group(0)
        for piece in ("PI_AB_ROUND=", "PI_AB_ARM=a", "PI_AB_ARM=b", "PI_AB_A=", "PI_AB_B="):
            self.assertIn(piece, body)


if __name__ == "__main__":
    unittest.main()
