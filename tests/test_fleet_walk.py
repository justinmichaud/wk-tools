"""Fleet walk: `wk status` reaches a remote target through a stub `ssh` that answers (running the far command here)
or refuses (exit 255), and a bare `wk status` walks a faked WK_MACHINES_DIR fleet the same way."""
import os
import sys
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402


_ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''

_REFUSING_SSH = '''#!/bin/sh
exit 255
'''


class TestFleetWalkNamedFormRendersAFakedMachine(WkTest):
    def test_reachable_machine_renders_its_block(self):
        with stub_path({"ssh": _ANSWERING_SSH}) as binp, scratch_dir(prefix="wk-test-remote-root-") as root:
            name = f"demo-{rand_suffix()}"
            (root / "ws" / name).mkdir(parents=True)
            (root / "ws" / name / ".wk-ready").touch()
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "WK_REMOTE_ROOT": str(root),
                "PATH": f"{binp}:{self._real_path()}",
                "WK_PROBE_SECONDS": "1",
            }
            cp = run("status", name, "--text", env=env, timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("remote", cp.stdout, cp.stdout)
            self.assertIn(name, cp.stdout, cp.stdout)
            self.assertNotIn("unreachable", cp.stdout, cp.stdout)

    def test_non_answering_machine_is_marked_unreachable_by_name(self):
        with stub_path({"ssh": _REFUSING_SSH}) as binp:
            name = f"demo-{rand_suffix()}"
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-down-machine",
                "PATH": f"{binp}:{self._real_path()}",
                "WK_SSH_TIMEOUT": "2",
                "WK_PROBE_SECONDS": "1",
            }
            cp = run("status", name, "--text", env=env, timeout=30)
            self.assertEqual(cp.returncode, 4, cp.stdout)
            self.assertIn("unreachable", cp.stdout, cp.stdout)
            self.assertIn(
                "remote", cp.stdout,
                f"the unreachable machine should be named, not just reported blind: {cp.stdout}",
            )

    @staticmethod
    def _real_path():
        return os.environ.get("PATH", "/usr/bin:/bin")


_FAKE_NODE_CONF = '''ssh={ssh}
kind=board
driver=rpi5-usb
device=/dev/sda
root=/dev/nvme0n1p2
profile=webkit-2.52-yocto-rpi5-64
mac={mac}
bridge=""
role=workstation
os=any
volume=""
dtb=bcm2712-rpi-5-b.dtb
bench_ssh=""
net=wifi
note="{note}"
'''


class TestFleetWalkBareFormMultiMachine(WkTest):
    def test_bare_status_renders_every_faked_machines_block(self):
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _ANSWERING_SSH}) as binp:
            suffix = rand_suffix(3)
            names = [f"f{i}{suffix}" for i in range(2)]
            for i, n in enumerate(names):
                (machdir / f"{n}.conf").write_text(
                    _FAKE_NODE_CONF.format(ssh=n, mac=f"02:00:00:00:00:0{i}", note=f"fake bench board {n}")
                )
            env = {
                "WK_MACHINES_DIR": str(machdir),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "WK_PROBE_SECONDS": "1",
            }
            cp = run("status", "--text", env=env, timeout=45)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            for n in names:
                self.assertIn(n, cp.stdout, f"'{n}' missing from a bare 'wk status --text':\n{cp.stdout}")


class _Registry(targets.Registry):
    """A registry of named targets, `holding` the ones a workspace is on."""

    def __init__(self, names, holding):
        super().__init__(REPO, env={}, machine=Fake())
        self.names, self.holding = names, holding

    def all(self):
        return self.names

    def machines(self):
        return []

    def on_target(self, name, ws):
        return name in self.holding


class TestResolution(unittest.TestCase):
    def test_a_name_on_two_targets_refuses_naming_both(self):
        with self.assertRaises(LookupError) as e:
            _Registry(["alpha", "beta", "gamma"], {"alpha", "beta"}).ws_target("demo-ambiguous")
        self.assertIn("demo-ambiguous", str(e.exception))
        self.assertIn("alpha beta", str(e.exception))
        self.assertNotIn("gamma", str(e.exception))

    def test_a_name_on_one_target_still_resolves(self):
        self.assertEqual(_Registry(["alpha", "beta"], {"beta"}).ws_target("demo-single"), "beta")


if __name__ == "__main__":
    unittest.main()
