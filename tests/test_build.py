"""`wk build`: help/list surface, the reproducible `running:` command line,
per-target WK_BUILD_ARGS defaults (and --no-defaults), and lib/resources.sh's
readings. The configs themselves are tests/test_buildconf.py's. Each docstring is the phrase of the behaviour it
checks.

Nothing here builds WebKit or touches real hardware: --dry-run, a
FakeWorkspace (an empty directory standing in for a checkout), and direct
calls into lib/resources.sh cover the logic without it.

Run: python3 -m unittest tests.test_build -v
"""
import contextlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import (FLEET_ENV, REAL_MACHINES, REPO, WkTest, bash, fake_workspace,
                           podman_vm_ssh, requires_podman_vm, run, stub_path)

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestHelpAndList(WkTest):
    def test_build_list_shows_all_configs(self):
        """`wk build --list` prints the available configs"""
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)

    def test_build_help_mentions_list_dry_run_no_defaults(self):
        """`wk build -h` names --list, --dry-run and --no-defaults"""
        cp = run("build", "-h")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--list", cp.stdout)
        self.assertIn("--dry-run", cp.stdout)
        self.assertIn("--no-defaults", cp.stdout)
        # The header's whole point for this defect: it says what --list and
        # --dry-run actually do, not just that they exist.
        self.assertIn("running:", cp.stdout)
        self.assertIn("WK_BUILD_ARGS", cp.stdout)


class TestDryRunRunningLine(WkTest):
    """A dry run against a fake workspace resolves everything and prints the
    exact command line, prefixed `running:`, with nothing built."""

    def test_dry_run_prints_running_line(self):
        """`wk build <config> --dry-run` prints a `running:` command line"""
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("dry run -- nothing was built", cp.stdout)
            lines = [l for l in cp.stdout.splitlines() if "running:" in l]
            self.assertEqual(len(lines), 1, cp.stdout)
            # One line, pastable: it names the checkout and the actual
            # Tools/Scripts invocation, not the internal env/build-in-target.sh
            # plumbing above it. Which script, and whether there is a port
            # flag, is the platform's answer -- a fake workspace is the `local`
            # target, so it is this machine's (see TestMacJscUsesXcode).
            self.assertIn("cd ", lines[0])
            if platform.system() == "Darwin":
                self.assertIn("build-jsc", lines[0])
                self.assertNotIn("--jsc-only", lines[0])
            else:
                self.assertIn("build-webkit", lines[0])
                self.assertIn("--jsc-only", lines[0])

    def test_no_defaults_flag_is_consumed_not_passed_through(self):
        """--no-defaults is recognised, not forwarded to build-webkit"""
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run", "--no-defaults")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            running = [l for l in cp.stdout.splitlines() if "running:" in l][0]
            self.assertNotIn("--no-defaults", running)


class TestTargetBuildArgsDefaults(unittest.TestCase):
    """Item 2: a target's conf can set WK_BUILD_ARGS, the same mechanism
    WK_TARGET_CMAKE already uses (machines/<name>.conf, load_target).
    Exercised at the two points that actually implement it, rather than
    through a full `wk new --target <remote>` (which needs a real machine):
    load_target's conf read here, buildconf.build_env's use of it in
    tests/test_buildconf.py.
    """

    def test_load_target_reads_WK_BUILD_ARGS_from_conf(self):
        """load_target sources a target's WK_BUILD_ARGS the same way as WK_TARGET_CMAKE"""
        name = "wk-test-build-args-probe"
        # A registry of this one machine (WK_MACHINES_DIR, lib/target.sh):
        # the conf load_target reads is the behaviour under test, and the real
        # machines/ is left alone.
        registry = Path(tempfile.mkdtemp(prefix="wk-test-registry-"))
        self.addCleanup(shutil.rmtree, registry, True)
        (registry / f"{name}.conf").write_text(
            'KIND=build\nWK_TARGET_KIND=remote\n'
            'WK_REMOTE_HOST=nonexistent.invalid\n'
            'WK_BUILD_ARGS="--no-fatal-warnings --extra-flag"\n'
        )
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
load_target "{name}"
echo "WK_BUILD_ARGS=[$WK_BUILD_ARGS]"
''', env={"WK_MACHINES_DIR": str(registry)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WK_BUILD_ARGS=[--no-fatal-warnings --extra-flag]", cp.stdout)


class TestStaleLoadAverage(unittest.TestCase):
    """Item 3(b): a killed build's dead compilers keep the 1-minute load
    average elevated for up to a minute. build_jobs treats a high load
    average as stale (and halves it) only when the memory envelope already
    looks idle -- the signature of a kill, not of a second build genuinely
    running (which would also be holding memory)."""

    def _jobs(self, script_env):
        env = "\n".join(f'{k}={v}' for k, v in script_env.items())
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
{env}
build_jobs polite
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return int(cp.stdout.strip())

    def test_stale_load_with_idle_memory_is_discounted(self):
        """high load + memory-idle machine -- not clamped to the load's face value"""
        stale = self._jobs({"WK_CGROUP_CORES": 10, "WK_AVAIL_MB": 100000, "WK_LOAD": 9})
        # Without the fix this would be 1 (10 cores - 9 load, still under the
        # half-a-box cap); the memory envelope says the machine is idle, so
        # the stale load is halved instead of trusted outright.
        self.assertGreater(stale, 1, "a stale-looking load average was trusted outright")

    def test_genuine_load_with_matching_memory_use_still_throttles(self):
        """high load + memory actually in use -- the load is trusted, not discounted"""
        busy = self._jobs({"WK_CGROUP_CORES": 20, "WK_AVAIL_MB": 23040, "WK_LOAD": 18})
        self.assertEqual(busy, 2, "a genuinely busy machine's load was discounted")


if __name__ == "__main__":
    unittest.main()


# The one round trip _remote_probe_cmd (targets/remote.sh) makes, answered as
# the Linux build machine every conf in machines/ describes.
FAKE_SSH_PROBE = r'''#!/bin/sh
cat <<'EOF'
/home/builder
Linux
80
0.00 0.00 0.00 1/1 1
===MEM===
MemTotal:       131072000 kB
MemAvailable:    65536000 kB
===IONICE===
yes
EOF
'''


class TestARealHostConfReachesTheBuildFlags(WkTest):
    """The two machine-decided defaults, read the way a build reads them: a
    conf in machines/, loaded by load_target, then config_load. The
    per-config tests above set the variables directly; this is the path that
    proves a conf's field actually arrives -- buildbox4 is the machine whose
    fields differ from every other's.

    A remote target's platform is a reading of the machine and not a field of
    its conf (t_os, one ssh round trip), so `ssh` here is a stub that answers
    it: the conf, load_target and config_load are the real ones, and an audit
    of what this repo ships does not wait on a shared box being up."""

    def _cmake(self, target, config="jsc-release"):
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
load_target {target} >/dev/null 2>&1
. "{REPO}/build/configs.sh"
config_load {config} "$(t_os)"
echo "KIND=$CFG_KIND"
echo "CMAKE=$CFG_CMAKE"
'''
        with stub_path({"ssh": FAKE_SSH_PROBE}) as binp:
            cp = bash(script, env={
                "WK_MACHINES_DIR": str(REAL_MACHINES),
                "XDG_STATE_HOME": str(self.tmp / "state"),
                "PATH": f"{binp}:{os.environ['PATH']}",
            })
        assert cp.returncode == 0, cp.stdout + cp.stderr
        out = dict(l.split("=", 1) for l in cp.stdout.strip().splitlines()
                   if l.startswith(("KIND=", "CMAKE=")))
        return out["KIND"], out["CMAKE"]

    def test_buildbox4s_conf_turns_libcxx_off_and_libbacktrace_with_it(self):
        """Both come from that machine being a `remote` target with no libc++
        package (measured there, and its conf says so)."""
        kind, cmake = self._cmake("buildbox4")
        self.assertEqual("remote", kind)
        self.assertNotIn("-stdlib=libc++", cmake)
        self.assertIn("-DUSE_LIBBACKTRACE=OFF", cmake)

    def test_a_machine_whose_conf_says_1_gets_libcxx(self):
        """The other side of the same field, from a conf that ships here."""
        _kind, cmake = self._cmake("devbox-arm64-2")
        self.assertIn("-stdlib=libc++", cmake)

    def test_every_host_conf_carries_a_value_the_loader_accepts(self):
        """A conf with a typo in the field would only be found by building on
        that machine."""
        for name in fleet.Fleet(REPO, FLEET_ENV).names(fleet.TARGET_KINDS):
            with self.subTest(machine=name):
                self._cmake(name)


# `sysctl -n <name>`: every reading lib/wk/resources.py takes on a Mac.
FAKE_SYSCTL = r"""#!/bin/sh
case "$2" in
hw.ncpu)                  echo 12 ;;
hw.memsize)               echo 17179869184 ;;
vm.loadavg)               echo '{ 3.41 2.20 1.90 }' ;;
*) exit 1 ;;
esac
"""
SYSCTL = {"hw.ncpu": "12\n", "hw.memsize": "17179869184\n", "vm.loadavg": "{ 3.41 2.20 1.90 }\n"}


class TestAReadingTheMachineWillNotGive(WkTest):
    """lib/wk/resources.py reads this machine's cores, memory and load average,
    and every job count, envelope and admission is sized from one of them. A
    reading that does not come back refuses and names what could not be read:
    fed into arithmetic instead, an empty one is an error several frames away
    from the sysctl or /proc file that was missing.

    Which spelling reads the machine is $(wk_os)'s answer in the bash shim, so
    the macOS arm is driven through it here by defining wk_os -- not is_macos,
    which tests/test_machine_mounts.py defines to drive a macOS *stage* on Linux.
    The refusals are read against a fake machine on either platform."""

    def res(self, os_name, sysctl=None, files=None, env=None, nproc=None):
        m = Fake()
        for k, v in (sysctl or {}).items():
            m.answer(["sysctl", "-n", k], out=v)
        if nproc is not None:
            m.answer(["nproc"], out=nproc)
        m.files.update(files or {})
        return resources.Resources(m, env or {"HOME": "/h"}, os_name)

    def refusal(self, fn):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            fn()
        self.assertNotIn("Traceback", err.getvalue())
        return err.getvalue()

    def test_each_reading_comes_from_the_kernel_the_shim_names(self):
        """The macOS arms, driven through wk_os: 16 GiB is 16384 MB, and a
        load average of 3.41 is three cores already spoken for."""
        with stub_path({"sysctl": FAKE_SYSCTL}) as binp:
            cp = bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/lib/resources.sh"\n'
                      'wk_os() { echo macos; }\nprintf "%s %s %s\\n" "$(host_cores)" "$(host_mem_mb)" "$(host_load)"',
                      env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("12 16384 3", cp.stdout.strip())

    @unittest.skipUnless(platform.system() == "Linux", "reads this machine's real nproc and /proc")
    def test_the_linux_arms_answer_from_proc_and_nproc(self):
        cp = bash(f'. "{REPO}/lib/common.sh"\n. "{REPO}/lib/resources.sh"\n'
                  'printf "%s %s %s\\n" "$(host_cores)" "$(host_mem_mb)" "$(host_load)"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        cores, mem, load = cp.stdout.split()
        self.assertEqual(cores, subprocess.run(["nproc"], capture_output=True,
                                               text=True, check=True).stdout.strip())
        self.assertGreater(int(mem), 0)
        self.assertRegex(load, r"^[0-9]+$")

    def test_a_core_count_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("the core count (nproc)", self.refusal(self.res("linux").host_cores))
        self.assertIn("the core count (sysctl hw.ncpu)", self.refusal(self.res("macos").host_cores))

    def test_a_memory_reading_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("total memory (/proc/meminfo MemTotal)", self.refusal(self.res("linux").host_mem_mb))
        self.assertIn("total memory (sysctl hw.memsize)", self.refusal(self.res("macos").host_mem_mb))

    def test_a_polite_build_reads_this_machines_load_when_no_caller_measured_one(self):
        """WK_LOAD is a remote target's, measured by whoever could reach it;
        without one the machine is asked rather than assumed idle."""
        env = {"WK_CGROUP_CORES": "12", "WK_AVAIL_MB": "100000", "WK_MB_PER_JOB": "1000"}
        r = self.res("macos", SYSCTL, env=env)
        # 12 cores, load 3.41 -> 3 spoken for, and never more than half a box.
        self.assertEqual(6, resources.build_jobs(r, resources.Budget(r.machine, env), [], polite=True))

    def test_free_memory_on_a_mac_is_the_total_less_the_reserve(self):
        self.assertEqual(12288, self.res("macos", SYSCTL, env={"WK_RESERVE_MB": "4096"}).avail_mem_mb())

    def test_free_memory_that_did_not_come_back_refuses_and_names_it(self):
        self.assertIn("free memory (/proc/meminfo MemAvailable)", self.refusal(self.res("linux").avail_mem_mb))

    def test_a_cgroup_limit_clamps_free_memory_and_max_is_no_limit(self):
        """Inside a container MemAvailable reports the whole machine's free
        memory, which sizes a job count the cgroup kills."""
        meminfo = {"/proc/meminfo": "MemAvailable:   20480000 kB\n"}
        r = self.res("linux", files=dict(meminfo, **{resources.CGROUP_MEM_MAX: "1073741824\n"}))
        self.assertEqual(1024, r.avail_mem_mb())
        # `max` is no limit at all, and not a number to divide down: what the
        # caller measured of the target's own cgroup stands.
        r = self.res("linux", files=dict(meminfo, **{resources.CGROUP_MEM_MAX: "max\n"}), env={"WK_CGROUP_MB": "2048"})
        self.assertEqual(2048, r.avail_mem_mb())

    def test_a_cgroup_limit_it_could_not_read_refuses_rather_than_ignoring_it(self):
        r = self.res("linux", files={"/proc/meminfo": "MemAvailable:   20480000 kB\n", resources.CGROUP_MEM_MAX: ""})
        self.assertIn("the cgroup memory limit", self.refusal(r.avail_mem_mb))


class TestSdkImageCarriesLibbacktrace(unittest.TestCase):
    """USE_LIBBACKTRACE=ON is the container/vm/local default (_cfg_use_
    libbacktrace, build/configs.sh) on the assumption that the external
    wkdev SDK image (targets/container.sh, WK_SDK_REPO) carries the library --
    not recorded anywhere in this repo, so checked directly against whatever
    image is already pulled onto the podman VM this repo drives. Read-only:
    it inspects an already-pulled image, it never creates a workspace."""

    @requires_podman_vm()
    def test_sdk_image_has_libbacktrace(self):
        img_cp = podman_vm_ssh(
            "podman images --format '{{.Repository}}:{{.Tag}}' "
            "| grep '^ghcr.io/igalia/wkdev-sdk:' | head -1"
        )
        img = img_cp.stdout.strip()
        if not img:
            self.skipTest("no ghcr.io/igalia/wkdev-sdk image pulled on the podman VM")
        cp = podman_vm_ssh(
            f"podman run --rm {img} sh -c "
            "'pkg-config --exists libbacktrace || test -f /usr/include/backtrace.h'"
        )
        self.assertEqual(cp.returncode, 0,
            f"the wkdev SDK image ({img}) carries no libbacktrace -- "
            f"USE_LIBBACKTRACE=ON (build/configs.sh, container/vm/local kinds) "
            f"would fail to configure: {cp.stdout}{cp.stderr}")


class TestJobCountNeverReachesACompilerLine(unittest.TestCase):
    """The other half of the same invariant, checked statically rather than
    by running a build: build/build-in-target.sh is the one place that turns
    CFG_ENV's WK_JOBS into the argument a build driver actually sees, and
    every one of those argument constructions is a parallelism flag consumed
    by xcodebuild or make/ninja (or the memory watchdog's job count) --
    never a compiler flag or a cmake -D, which is what would put it inside
    ccache's hash."""

    BUILD_IN_TARGET = REPO / "build" / "build-in-target.sh"

    def test_the_compiler_flag_variables_never_mention_the_job_count(self):
        """cmakeargs (--cmakeargs=..., the -D flags) and CFLAGS/CXXFLAGS/
        LDFLAGS are each assigned exactly once in this file, from
        WK_BUILD_CMAKE/WK_ARCH_CFLAGS/WK_ARCH_LDFLAGS -- never from the job
        count. A second assignment, or an append, that mentions the job
        count anywhere in this file is exactly the leak this test exists to
        catch."""
        text = self.BUILD_IN_TARGET.read_text()
        for name in ("cmakeargs", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
            assignments = re.findall(rf'^\s*(?:export\s+)?{name}\+?=.*$', text, re.M)
            self.assertEqual(len(assignments), 1,
                f"{name} is assigned {len(assignments)} times in "
                f"build-in-target.sh, expected exactly 1: {assignments}")
            self.assertNotIn("jobs", assignments[0],
                f"the job count reaches {name}: {assignments[0]!r}")

    def test_every_other_use_of_the_job_count_is_a_named_parallelism_flag(self):
        """Every code line in build-in-target.sh that names the job count,
        enumerated: `guard_jobs` clamping it to the cgroup limit, xcodebuild's
        `-jobs N`, build-webkit's `--makeargs=-jN`, and guard_exec's own
        argument (the memory watchdog's budget, build/guard.sh). If a new
        line uses the job count another way, this fails until the new use
        gets the same scrutiny this test encodes."""
        text = self.BUILD_IN_TARGET.read_text()
        lines = [l.strip() for l in text.splitlines()
                 if re.search(r'\bjobs\b', l) and not l.strip().startswith("#")]
        allowed = {
            'jobs=$(guard_jobs "${WK_JOBS:-4}")',
            'XC=(-jobs "$jobs")',
            'args+=("--makeargs=-j$jobs")',
            'guard_exec "$jobs" -- $wrapper "$script" "${args[@]}" ${@+"$@"}',
        }
        self.assertEqual(set(lines), allowed,
            f"build-in-target.sh's uses of the job count changed: {lines}")
