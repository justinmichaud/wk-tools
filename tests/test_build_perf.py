"""Build performance fixes: JSC-only configs ask ccache for explicitly
(build/configs.sh), a workspace's WebKitBuild is a bind-mounted plain
directory rather than overlay upperdir churn (targets/container.sh's
t_create), and cmd/sudo requires visudo outright rather than falling back to
a second lookup path.

Run: python3 -m unittest tests.test_build_perf -v
"""
import os
import re
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, fake_workspace


# --------------------------------------------------------------------------- #
# Item 1: the JSC-only configs (build/configs.sh) state WK_USE_CCACHE=YES in
# the environment config_build_env assembles, rather than depending on
# build-webkit's own default -- the same channel build-webkit's own
# --use-ccache writes (WK_USE_CCACHE, read directly by
# Source/cmake/WebKitCCache.cmake), so it does not disturb WK_BUILD_ARGS,
# the literal build-webkit command line other tests (tests/test_build.py)
# already pin exactly.
#
# A --dry-run's `running:` line is checked too, for the negative: it must
# never carry --no-use-ccache, which is the one spelling that would turn
# caching off regardless of WK_USE_CCACHE.
# --------------------------------------------------------------------------- #

class TestJscConfigsUseCcache(WkTest):
    def _wk_use_ccache(self, config, os="linux"):
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
config_load {config} {os}
config_build_env /src/WebKit 4 10 native
for e in "${{CFG_ENV[@]}}"; do case "$e" in WK_USE_CCACHE=*) echo "$e" ;; esac; done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def _running_line(self, config):
        with fake_workspace() as ws:
            cp = ws.run("build", config, "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            lines = [l for l in cp.stdout.splitlines() if "running:" in l]
            self.assertEqual(len(lines), 1, cp.stdout)
            return lines[0]

    def test_jsc_debug_asks_for_ccache(self):
        """jsc-debug's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-debug"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-debug"))

    def test_jsc_release_asks_for_ccache(self):
        """jsc-release's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-release"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-release"))

    def test_jsc_release_asan_asks_for_ccache(self):
        """jsc-release-asan's environment carries WK_USE_CCACHE=YES, and the dry-run line never disables it"""
        self.assertEqual(self._wk_use_ccache("jsc-release-asan"), "WK_USE_CCACHE=YES")
        self.assertNotIn("--no-use-ccache", self._running_line("jsc-release-asan"))

    def test_other_ports_are_untouched(self):
        """gtk/wpe configs get no WK_USE_CCACHE -- this fixes the JSC path, not every port"""
        self.assertEqual(self._wk_use_ccache("gtk-release"), "")


# --------------------------------------------------------------------------- #
# Item 2: t_create (targets/container.sh) bind-mounts $ws/build over the
# checkout's WebKitBuild, so a build's objects land on a plain directory
# instead of paying overlayfs copy-up on every write. Driven directly rather
# than through `wk new`: the real driver shells out to wkdev-create (from the
# webkit-container-sdk, not this repo) to actually talk to podman, so that
# script is stubbed to record the argv t_create built it, the same way other
# tests here stub a binary on PATH (tests/test_wifi_seed.py's fake
# `tailscale`). A fake `podman` stands in too, so this needs neither a real
# podman install nor its VM running.
# --------------------------------------------------------------------------- #

def _t_create_argv(name, t):
    """Run the real t_create, under scratch directory `t`, against a fake SDK
    and a fake podman, and return the argv it invoked wkdev-create with --
    one element per line, in order, exactly as t_create built it."""
    bindir = t / "bin"
    sdk_hook = t / "sdk" / "scripts" / "host-only" / "wkdev-create"
    sdk_hook.parent.mkdir(parents=True)
    argv_out = t / "argv.out"

    # Records its own argv, one per line, and does nothing else -- nothing
    # here needs a container to actually come up.
    sdk_hook.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$@" > "$WK_TEST_ARGV_OUT"\n'
        "exit 0\n"
    )
    sdk_hook.chmod(0o755)

    # Answers "no such container" to the one podman call t_create makes
    # itself (container exists), so it proceeds without a real daemon.
    bindir.mkdir()
    podman_stub = bindir / "podman"
    podman_stub.write_text("#!/bin/sh\nexit 1\n")
    podman_stub.chmod(0o755)

    (t / "store" / "base" / "20260101" / "WebKit").mkdir(parents=True)
    (t / "run").mkdir()

    script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/targets/container.sh"
wk_base_dir() {{ echo "{t}/store/base"; }}
t_create {name} 20260101 native
'''
    env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "WK_STORE": str(t / "store"),
        "WK_SDK": str(t / "sdk"),
        "WK_TEST_ARGV_OUT": str(argv_out),
        "XDG_RUNTIME_DIR": str(t / "run"),
    }
    cp = bash(script, env=env)
    assert cp.returncode == 0, f"t_create failed: {cp.stdout}\n{cp.stderr}"
    return argv_out.read_text().splitlines()


class TestContainerWebKitBuildMount(WkTest):
    def test_t_create_mounts_webkitbuild_from_ws_build(self):
        """t_create's additional-flags mount WebKitBuild from a plain $ws/build, not the overlay"""
        argv = _t_create_argv("wk-test-argv", self.tmp)
        flags = "\n".join(argv)
        ws_build = self.tmp / "store" / "ws" / "wk-test-argv" / "build"
        self.assertIn(
            f"--volume {ws_build}:/src/WebKit/WebKitBuild",
            flags,
            f"no WebKitBuild bind mount in the additional-flags:\n{flags}",
        )
        # The directory it mounts has to actually exist, or podman would be
        # asked to bind-mount nothing.
        self.assertTrue(ws_build.is_dir(), f"{ws_build} was never created")


# --------------------------------------------------------------------------- #
# Item 4: cmd/sudo requires visudo outright -- no second, hardcoded lookup
# path tried only when `have visudo` fails.
# --------------------------------------------------------------------------- #

class TestSudoRequiresVisudo(WkTest):
    def test_no_have_visudo_fallback(self):
        """static: cmd/sudo no longer branches on `have visudo` with a second lookup"""
        text = (REPO / "cmd" / "sudo").read_text()
        self.assertNotIn("have visudo", text)

    def test_visudo_resolve_refuses_outright_when_absent(self):
        """visudo_resolve dies naming visudo/sudo when it is not on PATH"""
        text = (REPO / "cmd" / "sudo").read_text()
        m = re.search(r"(?ms)^visudo_resolve\(\).*?^\}", text)
        self.assertIsNotNone(m, "visudo_resolve not found in cmd/sudo")
        body = m.group(0)
        self.assertNotIn("/usr/sbin/visudo", body, "a second, hardcoded lookup path is still there")
        self.assertIn("visudo", body)


if __name__ == "__main__":
    unittest.main()
