"""WK_* override audit -- bench/mac-*.sh, image/{buildroot,pmos,yocto}.sh,
host/{linux,macos}/*.sh, container/{gpu/gpu-probe.sh,proxy/ensure-bridge.sh}
and admin/wk-quiesce-priv.

docs/HANDOFF-test-runner.md's owed item: every WK_* override read with a
default is documented where the user meets it and covered by a test, or
removed. This module covers the overrides in the files above.

Two kinds of test:

  - A handful drive the real script end to end and check the observable
    effect, with no hardware ever touched: a fake ssh destination that fails
    DNS resolution before any packet leaves this machine, or a
    --dry-run/--status/--preflight action that only prints what it would do
    (bench/mac-lane.sh, bench/mac-ab.sh), or a read-only `diskutil info`
    query against this machine's own disk for a volume name that does not
    exist (bench/mac-bench-volume.sh, macOS only).

  - The rest lift the exact `${WK_X:-default}` expression out of the source
    file (the same brace-balanced slice the audit itself used) and evaluate
    it under bash with and without the variable set, or (for a value
    computed inside a shell function) lift the whole function with sed --
    the tests/test_wifi_seed.py idiom for calling one function without
    sourcing a whole script that may need root, ssh or a real disk at the
    top. Lifting the expression rather than hardcoding both sides of it
    means a changed default cannot silently invalidate what is asserted:
    only the override *mechanism* is asserted, never a specific number.

Two overrides turned out not to be real, in admin/wk-quiesce-priv:
WK_SESSION_TTY was read with a default, but every caller runs
`sudo -n wk-quiesce-priv <verb>` with no `VAR=value` prefix, and sudo's own
env_reset would strip one anyway (admin/install.sh already said so in a
comment) -- the override could never be reached, so it is now a plain
assignment, not `${WK_SESSION_TTY:-...}`. WK_SESSION_USER in the same file
is not an override either: it comes from sourcing a root-owned config file,
never from the caller's environment; a test below proves the caller's own
WK_SESSION_USER cannot reach it.

Run: python3 -m unittest tests.test_wk_overrides_misc -v
"""
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash


BENCH = REPO / "bench"
IMAGE = REPO / "image"
HOST_LINUX = REPO / "host" / "linux"
HOST_MACOS = REPO / "host" / "macos"
CONTAINER = REPO / "container"
QUIESCE_PRIV = REPO / "admin" / "wk-quiesce-priv"

# Never resolves (.invalid is reserved, RFC 2606): ssh fails on the resolver
# in milliseconds, before any packet reaches a network, let alone a machine.
FAKE_SSH = "wk-selftest-invalid.invalid"
FAKE_BENCH_SSH = "wk-selftest-bench-invalid.invalid"


def _is_macos():
    import os
    return os.uname().sysname == "Darwin"


def _lift(path, func):
    """A function's body, sed'd out of `path` -- tests/test_wifi_seed.py's
    idiom for calling one function without sourcing a whole script."""
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"no {func}() in {path}"
    return text


def _expr(path, var):
    """The exact `${VAR:-default}` expression for `var` in `path`,
    brace-balanced so a default that is itself `${...}` or `$(...)` comes
    back whole -- the same slice the wk-overrides audit used to build its
    inventory, so this test asks the identical question the audit did."""
    text = Path(path).read_text()
    m = re.search(r"\$\{" + re.escape(var) + r":-", text)
    assert m, f"no \\${{{var}:-...}} in {path}"
    depth = 1
    j = m.end()
    while depth:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
        j += 1
    return text[m.start():j]


def _default_and_override(path, var, override="wk-selftest-override"):
    """Evaluate `_expr(path, var)` under bash with and without `var` set."""
    expr = _expr(path, var)
    default = bash(f'printf %s "{expr}"').stdout
    overridden = bash(f'printf %s "{expr}"', env={var: override}).stdout
    return default, overridden


class TestExprOverrideMechanism(WkTest):
    """One assertion per override: setting the variable changes the read
    site's value, and not setting it falls back to whatever default is
    coded there right now (whatever that is)."""

    def _assert_overridable(self, path, var, override="wk-selftest-override"):
        default, overridden = _default_and_override(path, var, override)
        self.assertEqual(overridden, override, f"{var} in {path} does not override")
        self.assertNotEqual(default, override, f"{var} in {path} defaults to the override value by coincidence")

    def test_wk_buildroot_base(self):
        self._assert_overridable(IMAGE / "buildroot.sh", "WK_BUILDROOT_BASE")

    def test_wk_yocto_base(self):
        self._assert_overridable(IMAGE / "yocto.sh", "WK_YOCTO_BASE")

    def test_wk_yocto_poll_seconds(self):
        self._assert_overridable(IMAGE / "yocto.sh", "WK_YOCTO_POLL_SECONDS", "7")

    def test_wk_image_key(self):
        self._assert_overridable(IMAGE / "pmos.sh", "WK_IMAGE_KEY", "/tmp/wk-selftest-key.pub")

    def test_wk_disk_gb(self):
        self._assert_overridable(HOST_MACOS / "machine.sh", "WK_DISK_GB", "77")

    def test_wk_softnet_bin(self):
        self._assert_overridable(HOST_MACOS / "softnet.sh", "WK_SOFTNET_BIN", "/tmp/wk-selftest-softnet")

    def test_wk_softnet_version(self):
        self._assert_overridable(HOST_MACOS / "softnet.sh", "WK_SOFTNET_VERSION", "9.9.9")

    def test_wk_gpu_probe_bin(self):
        self._assert_overridable(CONTAINER / "gpu" / "gpu-probe.sh", "WK_GPU_PROBE_BIN", "/tmp/wk-selftest-probe")

    def test_wk_proxy_port(self):
        self._assert_overridable(CONTAINER / "proxy" / "ensure-bridge.sh", "WK_PROXY_PORT", "19999")

    def test_wk_bench_user(self):
        self._assert_overridable(BENCH / "mac-bench-firstboot.sh", "WK_BENCH_USER", "selftest")

    def test_wk_bench_password(self):
        self._assert_overridable(BENCH / "mac-bench-firstboot.sh", "WK_BENCH_PASSWORD", "s3lftest")

    def test_wk_bench_admin(self):
        self._assert_overridable(BENCH / "mac-bench-volume.sh", "WK_BENCH_ADMIN", "selftest-admin")

    def test_wk_bench_no_pkg(self):
        self._assert_overridable(BENCH / "mac-bench-volume.sh", "WK_BENCH_NO_PKG", "1")

    def test_wk_ab_root(self):
        self._assert_overridable(BENCH / "mac-bench-autorun.sh", "WK_AB_ROOT", "/tmp/wk-selftest-ab")

    def test_wk_sdk(self):
        self._assert_overridable(HOST_LINUX / "sdk.sh", "WK_SDK", "/tmp/wk-selftest-sdk")


class TestPmosFunctions(WkTest):
    """image/pmos.sh's pmos_host/pmos_root, lifted rather than sourcing the
    whole file (which needs boot/machines.sh's machine_load and does not
    stand alone)."""

    def test_pmos_host_env_override_wins_over_the_profile(self):
        func = _lift(IMAGE / "pmos.sh", "pmos_host")
        script = func + "\npmos_host"
        cp = self.bash(script, env={"PMO_BUILD_HOST": "profile-host"})
        self.assertEqual(cp.stdout.strip(), "profile-host")
        cp2 = self.bash(script, env={"PMO_BUILD_HOST": "profile-host", "WK_PMOS_HOST": "override-host"})
        self.assertEqual(cp2.stdout.strip(), "override-host")

    def test_pmos_host_refuses_with_no_profile_and_no_override(self):
        func = _lift(IMAGE / "pmos.sh", "pmos_host")
        script = f'die() {{ echo "die: $*" >&2; exit 1; }}\n{func}\npmos_host'
        cp = self.bash(script)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no PMO_BUILD_HOST", cp.stdout + cp.stderr)

    def test_pmos_root_env_override(self):
        func = _lift(IMAGE / "pmos.sh", "pmos_root")
        script = func + "\npmos_root"
        cp = self.bash(script)
        self.assertIn("wk-pmos", cp.stdout)
        cp2 = self.bash(script, env={"WK_PMOS_ROOT": "/srv/selftest-pmos"})
        self.assertEqual(cp2.stdout.strip(), "/srv/selftest-pmos")


class TestMacLaneOverrides(WkTest):
    """bench/mac-lane.sh, driven directly (not through `wk`) against a fake
    ssh destination and, for WK_MAC_BENCH_SSH, a throwaway machine conf
    (WK_MACHINES_DIR) so the die path is reachable without touching mbp's
    real conf. Every case here either dies before any ssh, or its one ssh
    attempt is against a `.invalid` name that fails to resolve -- no real
    machine is ever reached."""

    SCRIPT = BENCH / "mac-lane.sh"

    def _run(self, *args, env=None):
        e = dict(env or {})
        return subprocess.run(
            [str(self.SCRIPT), *args],
            capture_output=True, text=True, timeout=20,
            env={**{"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.tmp)}, **e},
        )

    def test_wk_mac_machine_names_itself_in_the_refusal(self):
        cp = self._run("--status", env={"WK_MAC_MACHINE": "wk-selftest-nonexistent"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no such machine: wk-selftest-nonexistent", cp.stdout + cp.stderr)

    def test_wk_mac_ssh_is_the_preflight_target(self):
        cp = self._run("--preflight", env={"WK_MAC_SSH": FAKE_SSH})
        self.assertIn(f"{FAKE_SSH} does not answer ssh", cp.stdout + cp.stderr)

    def test_wk_mac_bench_ssh_avoids_the_no_mach_bench_ssh_refusal(self):
        conf_dir = self.tmp / "machines"
        conf_dir.mkdir()
        (conf_dir / "faketest.conf").write_text(
            f'NODE_SSH={FAKE_SSH}\nNODE_DRIVER=mac-volume\nNODE_NOTE="wk-selftest fake machine"\n'
        )
        env = {"WK_MACHINES_DIR": str(conf_dir), "WK_MAC_MACHINE": "faketest"}

        cp = self._run("--status", env=env)
        self.assertNotEqual(cp.returncode, 0, "no WK_MAC_BENCH_SSH and no NODE_BENCH_SSH still ran")
        self.assertIn("sets no NODE_BENCH_SSH", cp.stdout + cp.stderr)

        cp2 = self._run("--status", env={**env, "WK_MAC_BENCH_SSH": FAKE_BENCH_SSH})
        self.assertEqual(cp2.returncode, 0, cp2.stdout + cp2.stderr)
        self.assertNotIn("sets no NODE_BENCH_SSH", cp2.stdout + cp2.stderr)

    def _dry_run_transcript(self, extra_env):
        env = {
            "WK_MAC_SSH": FAKE_SSH,
            "WK_MAC_BENCH_SSH": FAKE_BENCH_SSH,
            "WK_MAC_TOOLS": "/opt/selftest-tools",
            "WK_MAC_BENCH_TOOLS": "/opt/selftest-bench-tools",
            "XDG_STATE_HOME": str(self.tmp / "state"),
            **extra_env,
        }
        cp = self._run("wk-selftest-ws", "--dry-run", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_wk_mac_tools_and_bench_tools_in_the_planned_commands(self):
        out = self._dry_run_transcript({})
        self.assertIn("would run [host] cd '/opt/selftest-tools'", out)
        self.assertIn("would run [bench] cd '/opt/selftest-bench-tools'", out)

    def test_wk_mac_plan_config_timeout_in_the_planned_commands(self):
        out = self._dry_run_transcript({
            "WK_MAC_PLAN": "jetstream2.2",
            "WK_MAC_CONFIG": "wpe-release",
            "WK_MAC_TIMEOUT": "42",
        })
        self.assertIn("'--plan' 'jetstream2.2' '--timeout' '42'", out)
        self.assertIn("jetstream2.2 on mbp, from wk-selftest-ws (wpe-release)", out)

    def test_wk_mac_boot_wait_and_back_wait_defaults(self):
        out = self._dry_run_transcript({})
        self.assertIn("would wait up to 3600s for bench mode", out)
        self.assertIn("would wait up to 600s for host mode", out)

    def test_wk_mac_boot_wait_override(self):
        out = self._dry_run_transcript({"WK_MAC_BOOT_WAIT": "111"})
        self.assertIn("would wait up to 111s for bench mode", out)

    def test_wk_mac_back_wait_override(self):
        out = self._dry_run_transcript({"WK_MAC_BACK_WAIT": "222"})
        self.assertIn("would wait up to 222s for host mode", out)

    def test_wk_mac_detach_adds_the_detach_flag(self):
        without = self._dry_run_transcript({})
        self.assertNotIn("'--detach'", without)
        withit = self._dry_run_transcript({"WK_MAC_DETACH": "1"})
        self.assertIn("./wk 'build' 'wk-selftest-ws' 'mac-release' '--detach'", withit)


class TestMacAbOverrides(WkTest):
    """bench/mac-ab.sh reads the same WK_MAC_* protocol as bench/mac-lane.sh
    (documented once, in mac-lane.sh's header); this spot-checks that this
    file's own read sites see the override too, without repeating the fuller
    coverage above."""

    SCRIPT = BENCH / "mac-ab.sh"

    def test_wk_mac_ssh_is_the_preflight_target(self):
        cp = subprocess.run(
            [str(self.SCRIPT), "--preflight"],
            capture_output=True, text=True, timeout=20,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.tmp),
                 "WK_MAC_SSH": FAKE_SSH},
        )
        out = cp.stdout + cp.stderr
        self.assertIn(FAKE_SSH, out)
        self.assertIn("does not answer ssh", out)


@unittest.skipUnless(_is_macos(), "diskutil, and this machine's own disk")
class TestMacBenchVolumeOverrides(WkTest):
    """bench/mac-bench-volume.sh's default (no-flag) report queries this
    machine's own disk read-only (`diskutil info <name>` on a volume name
    that does not exist -- never creates, installs or deletes anything)."""

    def test_wk_bench_volume_and_need_gb_in_the_report(self):
        cp = subprocess.run(
            [str(BENCH / "mac-bench-volume.sh")],
            capture_output=True, text=True, timeout=20,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(Path.home()),
                 "WK_BENCH_VOLUME": "wk-selftest-no-such-volume",
                 "WK_BENCH_NEED_GB": "1"},
        )
        out = cp.stdout + cp.stderr
        self.assertIn("volume name:    wk-selftest-no-such-volume", out)
        self.assertIn("need 1 GB", out)


class TestQuiescePrivSessionVars(WkTest):
    """admin/wk-quiesce-priv's WK_SESSION_TTY and WK_SESSION_USER, see the
    module docstring: neither is a real caller-facing override."""

    def test_wk_session_tty_is_a_plain_assignment_not_an_override(self):
        text = QUIESCE_PRIV.read_text()
        self.assertIn("WK_SESSION_TTY=/dev/tty2", text)
        self.assertNotIn("${WK_SESSION_TTY:-", text,
                          "WK_SESSION_TTY is read as an override again, but no caller can ever set "
                          "it (sudo -n with no VAR=value prefix, and env_reset besides) -- see "
                          "admin/install.sh's comment on this")

    def test_wk_session_user_comes_from_the_conf_file_not_the_caller(self):
        func = _lift(QUIESCE_PRIV, "session_user")
        conf = self.tmp / "wk-session.env"
        conf.write_text("WK_SESSION_USER=root\n")
        cp = self.bash(func + "\nsession_user", env={
            "WK_SESSION_CONF": str(conf),
            "WK_SESSION_USER": "wk-selftest-hostile-value",
        })
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "root",
                          "the caller's own WK_SESSION_USER leaked through instead of the conf file's")

    def test_wk_session_user_fails_closed_with_no_conf_file(self):
        func = _lift(QUIESCE_PRIV, "session_user")
        cp = self.bash(func + "\nsession_user", env={
            "WK_SESSION_CONF": str(self.tmp / "no-such-file"),
            "WK_SESSION_USER": "wk-selftest-hostile-value",
        })
        self.assertNotEqual(cp.returncode, 0, "no conf file, yet session_user still returned a user")


class TestRemovedOverridesStayRemoved(unittest.TestCase):
    """Source-level regression guards: an override removed because nothing
    used it should not silently come back."""

    def test_wk_vmtools_only_removed(self):
        self.assertNotIn("WK_VMTOOLS_ONLY", (HOST_MACOS / "vmtools.sh").read_text())


if __name__ == "__main__":
    unittest.main()
