"""`wk pi`: the bare/`-h`/unknown-subcommand usage page (cmd/pi's
pi_usage_page, read out of this file's own header comment), `wk pi bench
--cores` (the same cpu-list flag `wk bench` has, through the same parser --
lib/wkdata.py cores-valid/cores-wrap -- applied as a taskset -c prefix on the
browser command the board runs), and the slot-shaped refusals.

Unit tests only -- no board, no ssh, no hardware. Bare `wk pi` and `wk pi
bogus` are run for real: nothing before the subcommand dispatch in cmd/pi
touches a board, so both are as safe as any other refusal. pi_launch_cmd is
lifted verbatim out of cmd/pi with `sed -n '/^fn()/,/^}/p'`, the idiom
tests/test_quick.py uses to lift cmd/status's `bump`.

Run: python3 -m unittest tests.test_pi -v
"""
import unittest

from tests.support import REPO, WkTest, bash

CMD_PI = REPO / "cmd" / "pi"


def lift(fn_name):
    """The `sed -n '/^fn()/,/^}/p'` idiom, as a reusable fragment: prints the
    named function's body out of cmd/pi, ready to `eval`."""
    return f'''
body="$(sed -n '/^{fn_name}()/,/^}}/p' "{CMD_PI}")"
[ -n "$body" ] || {{ echo "lift {fn_name} failed"; exit 1; }}
eval "$body"
'''


class TestPiUsagePage(WkTest):
    """Bare `wk pi` and an unknown subcommand both print the whole
    board-lifecycle sequence -- the header comment `wk pi -h` reads through
    the dispatcher's explain_cmd -- rather than a bare usage line. Bare exits
    0 (asking for help is not an error); the unknown-subcommand case is the
    same text as a refusal, on stderr, exit 1."""

    STEPS = (
        "wk sysimage build",
        "wk sysimage write --from",
        "wk pi boot-order",
        "wk boot",
        "wk pi setup",
        "wk sysimage webkit",
        "wk pi deploy",
        "wk pi bench",
    )

    def test_bare_exits_0_and_names_every_step(self):
        cp = self.run_wk("pi")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        for step in self.STEPS:
            self.assertIn(step, cp.stdout, f"{step!r} missing from bare 'wk pi'")

    def test_unknown_subcommand_exits_1_with_the_same_text(self):
        bare = self.run_wk("pi")
        bogus = self.run_wk("pi", "bogus")
        self.assertEqual(bogus.returncode, 1, bogus.stdout)
        # The unknown verb is named ahead of the same page a bare `wk pi` prints.
        self.assertTrue(bogus.stdout.startswith("usage:"), bogus.stdout[:120])
        self.assertIn("unknown verb: bogus", bogus.stdout)
        self.assertTrue(bogus.stdout.endswith(bare.stdout), "the usage page differs")

    def test_no_board_is_contacted_before_a_subcommand_is_chosen(self):
        """Safe to run for real: ssh only starts after cmd/pi's dispatch
        case, so a bogus host name never gets reached for either verb."""
        cp = self.run_wk("pi", "bogus", "not-a-real-host", timeout=15)
        self.assertEqual(cp.returncode, 1)


class TestPiBenchCores(WkTest):
    """`wk pi bench --cores`: refused for a bad cpu list before any board is
    touched (same parser and same message shape as `wk bench --cores`), and
    a valid one lands as a literal `taskset -c <set>` prefix on the browser
    command line the wk-board driver runs on the board (pi_launch_cmd)."""

    def test_invalid_cores_is_refused_before_the_board_is_touched(self):
        cp = self.run_wk(
            "pi", "bench", "not-a-real-machine", "jetstream3", "--cores", "abc",
            timeout=15,
        )
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("not a valid Linux cpu list", cp.stdout)

    def _launch(self, cores_wrap):
        slot = self.tmp / "slot.json"
        slot.write_text('{"browser": "cog", "lib_dir": "usr/lib", '
                        '"exec_dir": "usr/libexec/wpe-webkit-1.1", '
                        '"bundle_dir": "usr/lib/wpe-webkit-1.1/injected-bundle"}')
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
wkslot() {{ python3 "{REPO}/lib/wkslot.py" "$@"; }}
{lift("pi_slot_dir")}
{lift("pi_launch_cmd")}
PI_SLOTS=/var/wk/slots
pi_launch_cmd pr "{slot}" "{cores_wrap}"
'''
        cp = bash(script, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_valid_cores_becomes_a_taskset_prefix_on_the_board(self):
        out = self._launch("taskset -c 0-3 ")
        self.assertIn("taskset -c 0-3 env", out)
        self.assertIn("LD_LIBRARY_PATH=/var/wk/slots/pr/root/usr/lib", out)
        self.assertIn("WEBKIT_EXEC_PATH=/var/wk/slots/pr/root/usr/libexec/wpe-webkit-1.1", out)
        self.assertTrue(out.rstrip().endswith("/usr/bin/cog"), out)

    def test_no_cores_means_no_taskset_prefix(self):
        self.assertNotIn("taskset", self._launch(""))

    def test_a_minibrowser_launch_is_posix_sh(self):
        """The driver runs the launch text through the board's /bin/sh (busybox
        ash), so it may not use bash-only syntax; dash's parser is the judge."""
        slot = self.tmp / "slot.json"
        slot.write_text('{"browser": "minibrowser", "lib_dir": "lib", "exec_dir": "bin", "bundle_dir": "lib"}')
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
wkslot() {{ python3 "{REPO}/lib/wkslot.py" "$@"; }}
{lift("pi_slot_dir")}
{lift("pi_launch_cmd")}
PI_SLOTS=/var/wk/slots
text=$(pi_launch_cmd b "{slot}" "")
sh=$(command -v dash || command -v sh)
printf '%s\\n' "$text" | "$sh" -n
'''
        cp = bash(script, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestPiSlotRefusals(WkTest):
    """The old on-board flags are tombstones naming the replacement, and an
    A/B of one slot against itself is refused as a repeatability check in
    disguise -- both before any board is reached."""

    def test_skeleton_is_a_tombstone(self):
        cp = self.run_wk("pi", "deploy", "some-image", "not-a-real-machine", "--skeleton", timeout=15)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--slot", cp.stdout)

    def test_ab_of_one_slot_twice_is_refused(self):
        cp = self.run_wk("pi", "bench", "not-a-real-machine", "speedometer3", "--ab", "base,base", timeout=15)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("two different slots", cp.stdout)

    def test_ab_needs_a_pair(self):
        cp = self.run_wk("pi", "bench", "not-a-real-machine", "speedometer3", "--ab", "base", timeout=15)
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("two slot names", cp.stdout)


if __name__ == "__main__":
    unittest.main()


class TestPiBenchOnceNamesItsOwnSlot(unittest.TestCase):
    """pi_bench_once's slot.json path is derived from *its* slot argument, not
    the caller's: `local a="$1" b="$a"` expands $a before the first assignment
    lands, which is how an --ab run once looked for slot-a.json."""

    def test_json_path_follows_the_argument(self):
        cp = bash(f'''
{lift("pi_bench_once")}
PI_TMP=/t plan=p machine=m slot=a
# stop at the first thing after the locals that touches the world
ensure_dir() {{ echo "json=$json"; exit 0; }}
pi_bench_once pr1725
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("json=/t/slot-pr1725.json", cp.stdout)


class TestPiHelper(WkTest):
    """`wk pi helper` installs this checkout's card helper on a board's rescue,
    so a fix to the helper reaches the fleet without an image rebuild. Run with
    `rsh` replaced by a local shell -- no board -- and the two files land in a
    temp directory standing in for /usr/local/libexec."""

    def _run(self, extra=""):
        there = self.tmp / "libexec"
        prelude = (
            "set -euo pipefail\n"
            f"WK_ROOT={REPO}\n"
            f"CARD_PRIV={there}/wk-card-priv\n"
            f"CARD_CHECKER={there}/wk-check-boot-files.py\n"
            # A bench-device, which is what this verb installs onto: it writes
            # root-owned paths over a channel that is root only there. An
            # unset role means workstation (boot/machines.sh), and the verb
            # refuses that by naming the step at the machine's own keyboard.
            "HOST=rpi3-rescue; MACH_NAME=rpi3; MACH_DEVICE=/dev/mmcblk0; PI_FLEET=1\n"
            "MACH_ROLE=bench-device\n"
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }\n"
            "info() { printf '%s\\n' \"$*\"; }\n"
            "log()  { printf '%s\\n' \"$*\"; }\n"
            # `card_priv` is boot/disk.sh's one way to run the helper over
            # there; only root can answer `status`, and a board is driven as
            # root, so this stands in for that.
            "card_priv() { [ \"$1\" = status ]; }\n"
            # The board, as a local shell: what the board would do as root,
            # done here without it.
            "rsh() {\n"
            "    local c=\"$*\"\n"
            "    c=\"${c//sudo -n /}\"\n"
            "    c=\"${c//install -d -o root -g root -m 0755/mkdir -p}\"\n"
            "    c=\"${c//chown 0:0/true}\"\n"
            "    bash -c \"$c\"\n"
            "}\n"
            + extra + "\n")
        return bash(prelude + lift("cmd_helper") + "\ncmd_helper\n"), there

    def test_both_files_land_and_match_this_checkout(self):
        cp, there = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((there / "wk-card-priv").read_bytes(),
                         (REPO / "admin" / "wk-card-priv").read_bytes())
        self.assertEqual((there / "wk-check-boot-files.py").read_bytes(),
                         (REPO / "boot" / "check-boot-files.py").read_bytes())
        self.assertFalse(list(there.glob("*.part")), "a .part was left behind")
        self.assertIn("card helper", cp.stdout)

    def test_a_non_fleet_host_is_refused(self):
        """the helper goes on a board this repo drives, named as the fleet
        names it -- not at an arbitrary ssh destination."""
        cp, _ = self._run(extra="PI_FLEET=''")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("not a fleet machine", cp.stderr)

    def test_a_workstation_is_refused_and_names_the_step_at_its_keyboard(self):
        """A workstation that holds a card reader needs the helper too, but
        writing /usr/local/libexec there needs a root it is not driven as, and
        wk takes no passwordless sudo on one. So the verb refuses and names
        the one command that can be answered at that machine."""
        cp, _ = self._run(extra="MACH_ROLE=workstation")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("is a workstation", cp.stderr)
        self.assertIn("./setup --stage quiesce", cp.stderr,
                      "the refusal does not name its remedy")

    def test_a_helper_that_does_not_answer_is_a_failure(self):
        """installed is not working: the copy has to answer `status`, or a
        rescue that cannot write a card reports success anyway."""
        cp, _ = self._run(extra='card_priv() { return 1; }')
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)
        self.assertIn("does not answer", cp.stderr)

    def test_the_usage_page_names_it(self):
        cp = bash(f'"{REPO}/wk" pi')
        self.assertIn("wk pi helper <machine>", cp.stdout)
