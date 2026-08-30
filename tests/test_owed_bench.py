"""Two `--dry-run` plans owed by docs/HANDOFF-test-runner.md:

  - `wk bench mac <ws> --dry-run` on a fresh lane (no state file yet) prints
    the whole plan rather than dying on `set -o pipefail` reading a state
    file that does not exist yet, or on a failed preflight (bench/mac-lane.sh)
  - `wk sysimage write --dry-run` prints its plan from a Mac, where GNU-only
    tools (`stat -c`, `numfmt`) are not on $PATH

Both run on macOS only (needs is_macos / a fake machine's mac-guest driver);
both self-skip elsewhere by name rather than faking a platform they are not on.

Run: python3 -m unittest tests.test_owed_bench -v
"""
import os
import platform
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path


def _is_macos():
    return platform.system() == "Darwin"


@unittest.skipUnless(_is_macos(), "wk bench mac and a plain sysimage write are macOS-only paths")
class TestBenchMacDryRunOnAFreshLane(WkTest):
    """benchvm (boot/machines/benchvm.conf, driver mac-guest) has no
    MACH_SSH of its own -- its bench mode is reached through a host, so
    --host stands in for one here. `ssh` is stubbed to refuse everything
    (an always-unreachable host), which fails preflight; --dry-run must
    show the plan anyway rather than dying, and must not touch the lane's
    state file while doing it (a dry run's own rule: state_set no-ops under
    DRY, so a real run afterwards does not think a phase a dry run never
    did already happened)."""

    _SSH_REFUSES = "#!/bin/sh\nexit 255\n"

    def test_a_fresh_lane_prints_the_whole_plan_and_touches_no_state(self):
        with stub_path({"ssh": self._SSH_REFUSES}) as binp, scratch_dir() as state:
            cp = run(
                "bench", "mac", "fakews", "--machine", "benchvm",
                "--host", "faketesthost.invalid", "--dry-run",
                env={"PATH": f"{binp}:{os.environ['PATH']}",
                     "XDG_STATE_HOME": str(state)},
            )
            state_has_anything = any(state.rglob("*"))
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("showing the plan anyway because this is --dry-run", out, out)
        want = [
            "build: mac-release in the macOS guest",
            "would run [host]",
            "arm: recording the intent",
            "would wait up to",
            "stage: the products into the running guest",
            "run:",
            "collect: listing the result inside the guest",
            "back: stopping the guest",
            "lane complete:",
        ]
        at = -1
        for step in want:
            here = out.find(step)
            self.assertNotEqual(here, -1, f"the dry run never says {step!r}:\n{out}")
            self.assertGreater(here, at, f"{step!r} is reported out of order:\n{out}")
            at = here
        self.assertFalse(state_has_anything,
                          f"a dry run wrote lane state under {state}: {list(state.rglob('*'))}")


@unittest.skipUnless(_is_macos(), "the GNU-tool risk this guards against is specific to a macOS driver")
class TestSysimageWriteDryRunOnAMac(WkTest):
    """A `wk sysimage write --dry-run` from this host is the workstation-
    driven path the write's own preflight and reporting run through; nothing
    in it may depend on a GNU-only `stat -c`/`numfmt` that this machine's
    BSD stat and coreutils do not provide."""

    _SSH = '''#!/bin/sh
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *) exit 0 ;;
esac
'''

    def test_dry_run_prints_its_plan_and_says_nothing_was_written(self):
        img = self.tmp / "fake.img"
        img.write_text("not a real image, just bytes\n")
        with stub_path({"ssh": self._SSH}) as binp, scratch_dir() as store, scratch_dir() as reg:
            cp = run(
                "sysimage", "write", "--from", str(img),
                "--disk", f"rpi5:/dev/sd{rand_suffix(2)}", "--dry-run",
                env={"PATH": f"{binp}:{os.environ['PATH']}",
                     "WK_STORE": str(store), "WK_TARGET_REGISTRY": str(reg)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run -- nothing was written.", out, out)
        self.assertNotIn("command not found", out, out)
        self.assertNotIn("illegal option", out, out)  # BSD stat/numfmt rejecting a GNU flag


if __name__ == "__main__":
    unittest.main()
