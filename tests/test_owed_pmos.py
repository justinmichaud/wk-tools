"""The pmos builder against a Fake world: the driving half (lib/wk/sysimage/pmos.py) and the pure parts of the
build host's half (lib/wk/sysimage/pmos_build.py).

Rows closed here (docs/PLAN.md 5.20):
  - `wk sysimage build <pmos profile>` reports the remote build's own exit code, or that it lost track of
    it, rather than a bare non-zero from the ssh round trip (Pmos._report_follow).
  - a second `wk sysimage build` refuses while one runs, matched by `pgrep -f` against the ssh command line
    that carries the build module's own name (Pmos._refuse_if_running).
  - PMO_BUILD_HOST/WK_PMOS_HOST and WK_IMAGE_KEY, the build host and the key the image accepts.
  - the image comes off the build host as bytes through `Machine.copy_out`, checked against the far hash.
  - `wk gc`'s rows are built from the probe's numbers, and taking one asks nothing of its own.

Run: python3 -m unittest tests.test_owed_pmos -v
"""
import contextlib
import io
import lzma
import os
import sys
import unittest
from unittest import mock

from tests.support import BLIND_FLEET, REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import pmos, pmos_build  # noqa: E402

PMOS_PY = REPO / "lib" / "wk" / "sysimage" / "pmos.py"

PROFILE = {
    "IMG_PROFILE": "test-profile", "IMG_BUILDER": "pmos", "IMG_HOSTNAME": "test-host", "IMG_ARCH": "aarch64",
    "PMO_DEVICE": "purism-librem5", "PMO_BRIDGE": "", "PMO_UI": "phosh", "PMO_CHANNEL": "v25.12",
    "PMO_PMB_VERSION": "3.9.0", "PMO_USER": "user", "PMO_PASSWORD": "147147", "PMO_PACKAGES": "",
    "PMO_EXTRA_SPACE": "512", "PMO_BUILD_HOST": "buildhost1", "PMO_WIFI_BANDS": "", "PMO_KERNEL_APORT": "",
    "PMO_KCONFIG": "",
}


class Reg:
    """Just enough of targets.Registry for Pmos: a machine, an env, and a fleet BLIND_FLEET never declares
    'buildhost1' in, so host resolution falls through to the raw name (the not-a-fleet-name case)."""

    def __init__(self, machine, env):
        self.machine, self.env = machine, dict(env)
        self.fleet = fleet.Fleet(str(BLIND_FLEET), self.env)


def make(env=None, profile=None):
    machine = Fake("buildhost1")
    return pmos.Pmos(Reg(machine, env or {}), dict(profile or PROFILE), "test-profile", FakeClock()), machine


def sh_react(handlers):
    """A Fake `sh -c <text>` responder: the first handler whose key is a substring of the text answers it."""
    def fn(argv, fake):
        text = argv[2]
        for key, result in handlers:
            if key in text:
                return result
        return Result(1, "", "no answer registered for: %s" % text)
    return fn


class TestPmosFollowReportsAFailure(unittest.TestCase):
    """Pmos._report_follow: the remote build's own exit code, read back once `job.wait_remote` has already
    decided it saw one -- this is pmos's own reporting, not wait_remote's polling, which is tested on its own
    in tests/test_wk_job.py."""

    def test_a_failed_remote_build_dies_naming_the_host_code_and_log(self):
        p, machine = make()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(Refused):
                p._report_follow(machine, "/home/x/wk-pmos/out/test-profile-20260101T000000Z", "1", True)
        out = err.getvalue()
        self.assertIn("the build failed on buildhost1", out)
        self.assertIn("exit 1", out)
        self.assertIn("build.log", out)

    def test_a_successful_remote_build_reports_nothing_and_continues(self):
        p, machine = make()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            p._report_follow(machine, "/home/x/wk-pmos/out/id", "0", True)
        self.assertNotIn("failed", err.getvalue())

    def test_a_lost_connection_names_resume_rather_than_a_bare_failure(self):
        p, machine = make()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(Refused):
                p._report_follow(machine, "/home/x/wk-pmos/out/id", "", False)
        out = err.getvalue()
        self.assertIn("lost track of the build", out)
        self.assertIn("--resume", out)


class TestPmosRefusesASecondConcurrentBuild(unittest.TestCase):
    """Pmos._refuse_if_running: `pgrep -f 'wk[.]sysimage[.]pmos_build'` on the build host, asked over the same ssh
    (`pmos.ask`) that carries every other question this driver asks it."""

    def test_a_build_already_running_refuses_and_names_the_host(self):
        p, machine = make()
        machine.react(("sh", "-c"), sh_react([("pgrep -f", Result(0, "yes\n"))]))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(Refused):
                p._refuse_if_running(machine, "/home/x/wk-pmos")
        self.assertIn("a pmos build is already running on buildhost1", err.getvalue())

    def test_no_build_running_lets_a_new_one_proceed(self):
        p, machine = make()
        machine.react(("sh", "-c"), sh_react([("pgrep -f", Result(1, ""))]))
        p._refuse_if_running(machine, "/home/x/wk-pmos")   # does not raise

    def test_the_pattern_is_bracketed_so_the_asking_ssh_does_not_match_itself(self):
        """`pgrep -f` matches every process's full command line, including the ssh carrying this very check,
        which contains the pattern's own spelling -- so the pattern must match the far command line and not itself."""
        import re
        far = " ".join(pmos_build.argv_for("/r", "remote-build"))
        self.assertRegex(far, pmos.RUNNING_PATTERN)
        self.assertIsNone(re.search(pmos.RUNNING_PATTERN, "pgrep -f %s" % pmos.RUNNING_PATTERN))
        self.assertIn('"pgrep -f %s', PMOS_PY.read_text())


class TestPmosHostResolution(unittest.TestCase):
    """PMO_BUILD_HOST and its WK_PMOS_HOST override, read through pmos.host_for."""

    def test_the_profile_names_the_host(self):
        self.assertEqual("buildhost1", pmos.host_for(PROFILE, {}))

    def test_wk_pmos_host_overrides_the_profile(self):
        self.assertEqual("override-host", pmos.host_for(PROFILE, {"WK_PMOS_HOST": "override-host"}))

    def test_no_build_host_and_no_override_refuses(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(Refused):
                pmos.host_for(dict(PROFILE, PMO_BUILD_HOST=""), {})
        self.assertIn("no PMO_BUILD_HOST", err.getvalue())


class TestPmosImageKey(WkTest):
    """WK_IMAGE_KEY: the ssh key the image accepts on first boot (Pmos.key_path)."""

    def test_default_is_the_driving_machine_s_own_key(self):
        p, _ = make(env={"HOME": str(self.tmp)})
        self.assertEqual(str(self.tmp / ".ssh" / "id_ed25519.pub"), p.key_path())

    def test_wk_image_key_overrides_the_default(self):
        p, _ = make(env={"HOME": str(self.tmp), "WK_IMAGE_KEY": "/tmp/wk-selftest-key.pub"})
        self.assertEqual("/tmp/wk-selftest-key.pub", p.key_path())


class TestPmosBuildHostsAndCacheProbe(unittest.TestCase):
    """What `wk disk` and `wk gc` read: every unique build host a pmos profile names, and what each holds."""

    def test_build_hosts_is_the_pmos_profiles_own_build_host(self):
        self.assertIn("rpi5", pmos.build_hosts({}))

    def test_a_host_that_does_not_answer_is_not_a_measurement(self):
        machine = Fake("buildhost1")
        machine.answer(("sh", "-c"), rc=255, err="no route to host")
        self.assertIsNone(pmos.cache_probe(machine, {}))

    def test_work_and_out_are_measured_apart(self):
        machine = Fake("buildhost1")
        machine.answer(("sh", "-c"), out="4096\t/home/x/wk-pmos/work\n2048\t/home/x/wk-pmos/out\n")
        self.assertEqual({"work": 4096, "out": 2048}, pmos.cache_probe(machine, {}))

    def test_rows_carry_the_probe_numbers_whatever_they_are_called(self):
        machine = Fake("buildhost1")
        machine.answer(("sh", "-c"), out="4096\t/p/work\n2048\t/p/out\n")
        rows = pmos.rubble(["buildhost1"], lambda h: machine, {"WK_PMOS_ROOT": "/p"})
        self.assertEqual({"pmos-builds": 2048, "pmos-work": 4096}, {r.kind: r.kb for r in rows})
        self.assertEqual(["--purge-pmos"], [r.flag for r in rows if r.kind == "pmos-work"])

    def test_an_unreachable_host_is_one_row_saying_so(self):
        machine = Fake("buildhost1")
        machine.answer(("sh", "-c"), rc=255)
        rows = pmos.rubble(["buildhost1"], lambda h: machine, {})
        self.assertEqual([("pmos", None)], [(r.kind, r.kb) for r in rows])
        self.assertTrue(rows[0].why)

    def test_purging_the_chroots_asks_nothing_of_its_own(self):
        """`wk gc --purge-pmos` asked once already; a second question here would be answered by nobody."""
        machine = Fake("buildhost1")
        machine.answer(("sh", "-c"))
        with mock.patch.object(act, "confirm", side_effect=AssertionError("asked twice")), \
                mock.patch.dict(os.environ, {"WK_CONFIRMED": "1"}), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(pmos.purge_work(machine, {"WK_PMOS_ROOT": "/p"}, "buildhost1", 4096))
        self.assertTrue(any("rm -rf /p/work" in e[1][-1] for e in machine.effects if e[0] == "run"))


class TestTheImageComesOffAsBytes(WkTest):
    """pmos.fetch_out: `Machine.copy_out` moves the .xz, `xz -d` runs here, and the far hash is checked in blocks."""

    RAW = bytes(range(256)) * 64

    def world(self, far_hash):
        far, here = Fake("buildhost1"), Fake("here")
        out = "/p/out/pm-1"
        far._set_file(out + "/disk.wic.xz", lzma.compress(self.RAW, format=lzma.FORMAT_XZ))
        far._set_file(out + "/result", "device=x\nraw_sha256=%s\n" % far_hash)
        far.react(("sh", "-c"), lambda a, f: Result(0, f.read(a[2].split()[-1].strip("'"))) if a[2].startswith("cat ") else Result(1))

        def unxz(argv, fake):
            with open(argv[-1], "rb") as src, open(argv[-1][:-3], "wb") as dst:
                dst.write(lzma.decompress(src.read()))
            os.remove(argv[-1])
            return Result(0)
        here.react(("xz", "-d", "-f"), unxz)
        return far, here

    def fetch(self, far_hash):
        import hashlib
        far, here = self.world(far_hash or hashlib.sha256(self.RAW).hexdigest())
        dest = str(self.tmp / "img")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                got = pmos.fetch_out(far, {"WK_PMOS_ROOT": "/p"}, "pm-1", dest, here=here)
            except Refused:
                got = None
        return got, dest, far, err.getvalue()

    def test_binary_bytes_survive_the_trip(self):
        got, dest, far, err = self.fetch("")
        self.assertEqual(dest, got, err)
        with open(dest, "rb") as f:
            self.assertEqual(self.RAW, f.read())
        self.assertIn(("copy_out", "/p/out/pm-1/disk.wic.xz", dest + ".xz"), far.effects)

    def test_a_hash_that_differs_refuses(self):
        got, _, _, _ = self.fetch("0" * 64)
        self.assertIsNone(got)


class TestTheBuildHostHalf(unittest.TestCase):
    """lib/wk/sysimage/pmos_build.py's pure parts; pmbootstrap, loop devices and sudo run only on a real build host."""

    def test_the_far_command_runs_the_copied_tree(self):
        self.assertEqual(["env", "PYTHONPATH=/r/lib", "python3", "-m", "wk.sysimage.pmos_build", "wifi-ssid"],
                         pmos_build.argv_for("/r", "wifi-ssid"))

    def test_a_kconfig_delta_replaces_set_and_unset_lines_and_appends_the_rest(self):
        lines = ["CONFIG_A=y", "# CONFIG_B is not set", "CONFIG_C=m"]
        self.assertEqual(["CONFIG_A=n", "CONFIG_B=y", "CONFIG_C=m", "CONFIG_D=y"],
                         pmos_build.kconfig_delta(lines, ["CONFIG_A=n", "CONFIG_B=y", "CONFIG_D=y"]))


if __name__ == "__main__":
    unittest.main()
