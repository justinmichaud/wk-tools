"""host/linux/machine.sh under WK_DRY_RUN=1: every mutating step reports what it would do and does none of it, and
without it does each. The machine facts it branches on, sudo, loginctl and the board's tuning tree are stubs on PATH."""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

STAGE = REPO / "host" / "linux" / "machine.sh"

FAKE_GREP = '''#!/bin/sh
case "$*" in
*"Raspberry Pi 5"*) exit "$WK_TEST_RPI5" ;;
*/etc/subuid*|*/etc/subgid*) exit 1 ;;   # this machine has no subordinate ids for the user, whatever the real files say
esac
exec /usr/bin/grep "$@"
'''

# WK_TEST_SUDO_FAIL names the one `sudo <tool>` this machine refuses.
FAKE_SUDO = '''#!/bin/sh
printf "sudo %s\\n" "$*" >> "$WK_TEST_SUDO_LOG"
[ "$1" != "${WK_TEST_SUDO_FAIL:-}" ] || exit 1
case "$1" in loginctl) exec "$@" ;; esac
'''
FAKE_LOGINCTL = '''#!/bin/sh
case "$1" in
show-user) if [ -f "$WK_TEST_LINGER" ]; then echo yes; else echo no; fi ;;
enable-linger) : > "$WK_TEST_LINGER" ;;
esac
'''
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
               if k not in ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_IN_VM")}
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
                  '. "$WK_ROOT/host/linux/machine.sh"\n')
        with stub_path({"sudo": FAKE_SUDO, "loginctl": FAKE_LOGINCTL,
                        "getent": FAKE_GETENT, "grep": FAKE_GREP}) as binp:
            env["PATH"] = f"{binp}:{os.environ['PATH']}"
            cp = subprocess.run(["bash", "-c", script], env=env, cwd=str(REPO),
                                capture_output=True, text=True, timeout=120)
        return cp, {"store": store, "marker": marker, "sudo_log": sudo_log,
                    "rpi5_ran": rpi5_ran}

    def test_a_dry_run_reports_every_step_and_runs_and_writes_nothing(self):
        cp, f = self._run(dry=True)
        out = cp.stdout + cp.stderr
        for phrase in ("would create", "would remove the headless marker",
                       "would add subordinate id ranges", "usermod -aG",
                       "would enable systemd lingering", "would seed",
                       "would run this board's tuning tree"):
            self.assertIn(phrase, out, out)
        self.assertRegex(out, r"would write: \S+/cache/ccache/ccache\.conf\n")
        self.assertFalse(f["sudo_log"].exists(), out)
        self.assertFalse(f["rpi5_ran"].exists(), out)
        self.assertFalse((f["store"] / "pi-hosts").exists(), out)
        self.assertFalse((f["store"] / "cache" / "ccache" / "ccache.conf").exists(), out)
        self.assertTrue(f["marker"].exists(), "the headless marker was removed")
        self.assertFalse(any((f["store"] / "skills").glob("*")), out)
        self.assertFalse((f["store"] / "log" / "rpi5-setup.log").exists(), out)

    def test_without_it_the_same_stage_does_each_of_those(self):
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

    def test_a_refused_sudo_stops_the_stage_and_names_the_remedy(self):
        cp, f = self._run(dry=False, sudo_fails="loginctl")
        out = cp.stdout + cp.stderr
        self.assertNotEqual(0, cp.returncode, out)
        self.assertIn("sudo loginctl enable-linger", out)


if __name__ == "__main__":
    unittest.main()
