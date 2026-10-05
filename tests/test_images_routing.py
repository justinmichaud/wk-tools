"""`holds` and `path` are routed to the machine holding the image workspace: the dispatcher asks cmd/sysimage which
workspace they name (`--wsname`), and the verdict is printed rather than returned, since a readonly command forwarded
to a stopped podman machine exits 0. Their answers in process are tests/test_sysimage_ls.py's."""
import subprocess

from tests.support import REPO, WkTest, run_here

PROFILE = "webkit-2.52-yocto-rpi5-64"
WS = "yocto-" + PROFILE


def hook(*args):
    cp = subprocess.run([str(REPO / "cmd" / "sysimage"), *args], capture_output=True, text=True, timeout=120,
                        env={"WK_ROOT": str(REPO), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/tmp"})
    return cp.stdout.strip(), cp


class TestHoldsAndPathAreRouted(WkTest):
    def test_both_name_their_image_workspace_for_the_dispatcher(self):
        for verb in ("holds", "path"):
            got, cp = hook("--wsname", verb, PROFILE)
            self.assertEqual((cp.returncode, got), (0, WS), cp.stderr)
        self.assertEqual(hook("--wsname", "holds", PROFILE, "--workspace", WS + "-base")[0], WS + "-base")

    def test_an_image_nothing_has_built_is_no_on_stdout_and_exit_0(self):
        cp = run_here("sysimage", "holds", PROFILE, "--workspace", WS + "-selftest", timeout=240)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "no"), cp.stdout)

    def test_a_workspace_with_no_image_has_no_path(self):
        cp = run_here("sysimage", "path", PROFILE, timeout=240, env={"WK_STORE": str(self.tmp / "store")})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout.strip(), "")
