"""host/linux/machine.sh under `./setup --dry-run` (WK_DRY_RUN=1): every
mutating step reports what it would do and does none of it.

The stage is the Linux workstation's, and this drives it on whatever runs the
suite: the machine facts it branches on -- /etc/subuid, the render and video
groups, systemd lingering, /proc/device-tree/model -- come from stubs on PATH,
so both arms are reachable everywhere. `sudo`, `loginctl` and the board's
tuning tree record their argv instead of running, which is what the dry run
must leave untouched, and the second half runs the same stage without
WK_DRY_RUN to show each of those records is written when it is not set.

WK_ROOT is a scratch tree whose lib/ and claude/ are this checkout's and whose
host/linux/rpi5/rpi5-setup.sh is the recorder: the stage names that script by
$WK_ROOT, so a dry run that runs it anyway is caught rather than performed.

Run: python3 -m unittest tests.test_linux_machine_dry_run -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

STAGE = REPO / "host" / "linux" / "machine.sh"

# The one probe this stage makes of the board it is on; every other grep is
# the real one, including the two this stage makes of /etc/subuid.
FAKE_GREP = '''#!/bin/sh
case "$*" in
*"Raspberry Pi 5"*) exit "$WK_TEST_RPI5" ;;
esac
exec /usr/bin/grep "$@"
'''

# Every privileged command is `sudo <what>`, so the log records the whole line
# and a step that reached the tool without sudo leaves no record at all.
# WK_TEST_SUDO_FAIL names the one `sudo <tool>` this machine refuses.
FAKE_SUDO = '''#!/bin/sh
printf "sudo %s\\n" "$*" >> "$WK_TEST_SUDO_LOG"
[ "$1" != "${WK_TEST_SUDO_FAIL:-}" ] || exit 1
case "$1" in loginctl) exec "$@" ;; esac
'''
# Lingering as systemd answers it: off until enable-linger has run.
FAKE_LOGINCTL = '''#!/bin/sh
case "$1" in
show-user) if [ -f "$WK_TEST_LINGER" ]; then echo yes; else echo no; fi ;;
enable-linger) : > "$WK_TEST_LINGER" ;;
esac
'''
# render and video exist on this machine, as far as the stage can tell; `id
# -nG` then says the user is in neither, so the membership step has work.
FAKE_GETENT = 'exit 0\n'
RPI5_RECORDER = 'printf "ran\\n" > "$WK_TEST_RPI5_RAN"\n'


class TestTheLinuxMachineStageHonoursDryRun(WkTest):
    def _run(self, dry, sudo_fails=""):
        root = self.tmp / ("root-dry" if dry else "root-wet")
        (root / "host" / "linux" / "rpi5").mkdir(parents=True)
        for name in ("lib", "claude"):
            (root / name).symlink_to(REPO / name)
        (root / "host" / "linux" / "machine.sh").symlink_to(STAGE)
        rpi5 = root / "host" / "linux" / "rpi5" / "rpi5-setup.sh"
        rpi5.write_text("#!/bin/sh\n" + RPI5_RECORDER)
        rpi5.chmod(0o755)

        store = self.tmp / (("store-dry" if dry else "store-wet")
                            + ("-sudofail" if sudo_fails else ""))
        (store / "log").mkdir(parents=True)
        marker = store / ".headless"
        marker.write_text("")
        secrets = self.tmp / ("secrets-dry" if dry else "secrets-wet")
        secrets.mkdir()
        tag = ("dry" if dry else "wet") + ("-sudofail" if sudo_fails else "")
        sudo_log = self.tmp / f"sudo-{tag}.log"
        rpi5_ran = self.tmp / f"rpi5-{tag}"

        env = {k: v for k, v in os.environ.items()
               if k not in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_IN_VM")}
        env.update({
            "WK_ROOT": str(root),
            "WK_STORE": str(store),
            "WK_HOST_SECRETS": str(secrets),
            "HOME": str(self.tmp / "home"),
            "WK_TEST_SUDO_LOG": str(sudo_log),
            "WK_TEST_LINGER": str(self.tmp / f"linger-{tag}"),
            "WK_TEST_SUDO_FAIL": sudo_fails,
            "WK_TEST_RPI5": "0",           # `grep -aqs '^Raspberry Pi 5'` succeeds
            "WK_TEST_RPI5_RAN": str(rpi5_ran),
            "WK_DEBUG": "1",
        })
        env["WK_DRY_RUN"] = "1" if dry else ""
        (self.tmp / "home").mkdir(exist_ok=True)

        script = ('set -euo pipefail\n'
                  '. "$WK_ROOT/lib/common.sh"\n'
                  '. "$WK_ROOT/lib/resources.sh"\n'
                  '. "$WK_ROOT/host/linux/machine.sh"\n')
        with stub_path({"sudo": FAKE_SUDO, "loginctl": FAKE_LOGINCTL,
                        "getent": FAKE_GETENT, "grep": FAKE_GREP}) as binp:
            env["PATH"] = f"{binp}:{os.environ['PATH']}"
            cp = subprocess.run(["bash", "-c", script], env=env, cwd=str(REPO),
                                capture_output=True, text=True, timeout=120)
        return cp, {"store": store, "marker": marker, "sudo_log": sudo_log,
                    "rpi5_ran": rpi5_ran}

    def _wet(self, **kw):
        return self._run(dry=False, **kw)

    def setUp(self):
        super().setUp()
        self.cp, self.f = self._run(dry=True)
        self.out = self.cp.stdout + self.cp.stderr

    def test_the_stage_reaches_every_step(self):
        """Each mutating step says what it would do, so a step that wrote
        nothing because it never ran is not mistaken for one held back."""
        for phrase in ("would create", "would remove the headless marker",
                       "would add subordinate id ranges", "usermod -aG",
                       "would enable systemd lingering", "would seed",
                       "would run this board's tuning tree"):
            self.assertIn(phrase, self.out, self.out)

    def test_it_runs_no_privileged_command(self):
        """usermod and enable-linger are what the stage asks root for."""
        self.assertFalse(self.f["sudo_log"].exists(), self.out)

    def test_it_writes_nothing(self):
        self.assertFalse((self.f["store"] / "pi-hosts").exists(), self.out)
        self.assertFalse((self.f["store"] / "cache" / "ccache" / "ccache.conf").exists(),
                         self.out)
        self.assertTrue(self.f["marker"].exists(), "the headless marker was removed")
        self.assertFalse(any((self.f["store"] / "skills").glob("*")), self.out)
        self.assertFalse((self.f["store"] / "log" / "rpi5-setup.log").exists(), self.out)

    def test_it_reports_the_ccache_settings_it_would_write(self):
        """store_init reaches a store with no cache/ccache directory on a dry
        run, since ensure_dir made none: the settings are reported, not
        written into a directory that is not there."""
        self.assertIn("would write", self.out)
        self.assertIn("ccache.conf", self.out)
        self.assertIn("max_size", self.out)

    def test_it_does_not_run_the_boards_tuning_tree(self):
        self.assertFalse(self.f["rpi5_ran"].exists(), self.out)

    def test_without_it_the_same_stage_does_each_of_those(self):
        """The control: every file and command the dry run withheld."""
        cp, f = self._run(dry=False)
        out = cp.stdout + cp.stderr
        self.assertTrue((f["store"] / "pi-hosts").exists(), out)
        self.assertFalse(f["marker"].exists(), out)
        self.assertTrue(any((f["store"] / "skills").glob("*")), out)
        self.assertTrue(f["rpi5_ran"].exists(), out)
        self.assertTrue((f["store"] / "cache" / "ccache" / "ccache.conf").exists(), out)
        sudo = f["sudo_log"].read_text()
        self.assertIn("sudo usermod --add-subuids", sudo, sudo)
        self.assertIn("sudo loginctl enable-linger", sudo, sudo)
        self.assertIn("enabled lingering", out)

    def test_lingering_is_asked_of_root_and_of_nothing_else(self):
        """One path, so one privileged command: a plain `loginctl
        enable-linger` writes /var/lib/systemd/linger and is refused without
        polkit's say-so, and a second path nothing runs is a second path
        nothing tests."""
        stage = STAGE.read_text()
        self.assertEqual(1, stage.count("enable-linger \"$_user\""), stage)
        self.assertIn('sudo loginctl enable-linger "$_user"', stage)

    def test_a_refused_sudo_stops_the_stage_and_names_the_remedy(self):
        """Nothing silently degrades: lingering off is the egress proxy gone
        at logout, so the stage says so rather than carrying on."""
        cp, f = self._wet(sudo_fails="loginctl")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("could not enable lingering", out)
        self.assertIn("sudo loginctl enable-linger", out)


if __name__ == "__main__":
    unittest.main()
