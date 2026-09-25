"""One `--dry-run` plan still owed (docs/PLAN.md):

  - `wk sysimage write --dry-run` prints its plan from a Mac, where GNU-only
    tools (`stat -c`, `numfmt`) are not on $PATH; it self-skips elsewhere by
    name rather than faking a platform it is not on.

The mac class closes here too: `wk bench mac` (bench/mac-lane.sh) is retired,
and the trip it drove by hand is `wk bench run --system mbp`, the pipeline's
own (lib/wk/bench/mac.py's `MacHostSystem`/`HostRun`) -- real everywhere,
with no macOS gate, no state file and no fork of a script that no longer
exists. `tests/test_bench_pipeline.py` proves the pipeline itself, against a
fake Mac; this file proves only that the old verb points at it.

Run: python3 -m unittest tests.test_owed_bench -v
"""
import contextlib
import io
import os
import platform
import sys
import unittest

from tests.support import (REPO, TAILSCALE_KNOWS_NOTHING, WkTest, rand_suffix, requires_machine,
                           run, scratch_dir, stub_path)

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import cli  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import Fake  # noqa: E402


def _is_macos():
    return platform.system() == "Darwin"


class TestBenchMacIsATombstone(unittest.TestCase):
    """`wk bench mac` drove one bench system's whole trip by hand; the tombstone in
    `lib/wk/bench/cli.py` names the pipeline invocation that now runs it, `wk bench run --system
    mbp`, rather than a sequence of commands to re-derive from the retired `bench/mac-lane.sh`."""

    def refused(self):
        err = io.StringIO()
        with self.assertRaises(Refused) as cm, contextlib.redirect_stderr(err):
            cli.Bench(REPO, targets.Registry(REPO, env={}, machine=Fake()), Clock()).mac()
        self.assertEqual(cm.exception.status, 1)
        return err.getvalue()

    def test_it_names_the_pipeline_invocation(self):
        out = self.refused()
        self.assertIn("wk bench run <workspace> <plan> --system mbp", out, out)
        self.assertIn("wk bench ab --devices mbp", out, out)

    def test_the_dispatcher_routes_it_to_the_same_tombstone(self):
        """`wk bench mac`, through the dispatcher: it dies the tombstone's own words, not the
        'no such file' a fork of the deleted script would have printed."""
        cp = run("bench", "mac", "fakews", "--plan", "speedometer3")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'wk bench mac' is gone", cp.stdout)
        self.assertNotIn("No such file or directory", cp.stdout)


class TestBenchReachesTheRealMbp(unittest.TestCase):
    """Read-only, as `requires_machine` is: what `wk bench mac` used to drive over ssh -- stage a
    build, arm and reboot mbp into bench mode, run a plan there, quiesce and screen-watch it, come
    back -- is the owed half of `live bench[mbp]` and `live bench.first_run_after_stage[mbp]`
    (docs/PLAN.md). Which workspace and plan to spend that machine's time staging and running is a
    decision for whoever runs the live tier next; this only confirms the machine the tombstone
    above names is real and answers, before anything stages or reboots it."""

    @requires_machine("tolken")
    def test_wk_boot_reaches_mbp(self):
        cp = run("boot", "mbp", "--status")
        self.assertIn(cp.returncode, (0, 2, 3), cp.stdout)


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
        with stub_path({"ssh": self._SSH, "tailscale": TAILSCALE_KNOWS_NOTHING}) as binp, \
                scratch_dir() as store:
            cp = run(
                "sysimage", "write", "--from", str(img),
                "--disk", f"rpi5:/dev/sd{rand_suffix(2)}", "--dry-run",
                env={"PATH": f"{binp}:{os.environ['PATH']}", "WK_STORE": str(store)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run -- nothing was written.", out, out)
        self.assertNotIn("command not found", out, out)
        self.assertNotIn("illegal option", out, out)  # BSD stat/numfmt rejecting a GNU flag


if __name__ == "__main__":
    unittest.main()
