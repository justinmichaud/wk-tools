"""WK_* override audit -- bench/mac-*.sh, image/buildroot.sh,
host/{linux,macos}/*.sh, container/{gpu/gpu-probe.sh,proxy/ensure-bridge.sh}
and admin/wk-quiesce-priv. WK_IMAGE_KEY, PMO_BUILD_HOST and WK_PMOS_HOST
(lib/wk/sysimage/pmos.py) are covered by tests/test_owed_pmos.py.

docs/PLAN.md's owed item: every WK_* override read with a
default is documented where the user meets it and covered by a test, or
removed. This module covers the overrides in the files above.

Each test lifts the exact `${WK_X:-default}` expression out of the source
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
HOST_LINUX = REPO / "host" / "linux"
HOST_MACOS = REPO / "host" / "macos"
CONTAINER = REPO / "container"
QUIESCE_PRIV = REPO / "admin" / "wk-quiesce-priv"


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

    def test_wk_sdk(self):
        self._assert_overridable(HOST_LINUX / "sdk.sh", "WK_SDK", "/tmp/wk-selftest-sdk")


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
