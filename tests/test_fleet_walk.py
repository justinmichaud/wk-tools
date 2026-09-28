"""Fleet walk: `wk status` reaches a remote target over ssh (Remote.probed,
lib/wk/targets.py), and this drives that for real against a faked machine --
WK_TARGET=remote, a made-up WK_REMOTE_HOST, and a stub `ssh` on PATH that
either answers (a normal shell, standing in for a live machine) or refuses
(ssh's own exit 255, standing in for one that never answers) -- so
`cmd/status` genuinely shells out to the stub rather than to a mocked driver.

The named form, `wk status <name> --text`, skips the bench-device fleet walk
entirely and answers for exactly the one target asked about, which is enough
to exercise the real ssh shell-out and the unreachable-by-name reporting
faithfully and fast.

A bare `wk status` also walks this host's bench-device fleet
(`Status.fleet_devices`, lib/wk/status.py): WK_MACHINES_DIR (read by
lib/wk/fleet.py alone) points that walk at a directory of faked
`machines/*.conf`-shaped confs instead of the real fleet, and the same stub
`ssh` answers every board's probe too. Driving the real fleet for real from
a test hung past two minutes in this environment (measured, then killed);
this is what replaces that.

Run: python3 -m unittest tests.test_fleet_walk -v
"""
import os
import sys
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402


_ANSWERING_SSH = '''#!/bin/sh
# Stand in for a live machine: run the remote command locally, the way a
# real ssh to a reachable host would.
for last; do :; done
exec bash -c "$last"
'''

_REFUSING_SSH = '''#!/bin/sh
# ssh's own exit code for "could not connect".
exit 255
'''


class TestFleetWalkNamedFormRendersAFakedMachine(WkTest):
    def test_reachable_machine_renders_its_block(self):
        # The stub runs the ssh'd command locally, so `Remote.info` stats a
        # directory on *this* filesystem: WK_REMOTE_ROOT, pointed at a scratch
        # dir instead of the real $HOME/wk a bare WK_REMOTE_HOST would default
        # to, with the ready marker every driver writes as creation's last act
        # already in place -- the answer a real finished workspace on a real
        # reachable machine would give.
        with stub_path({"ssh": _ANSWERING_SSH}) as binp, scratch_dir(prefix="wk-test-remote-root-") as root:
            name = f"demo-{rand_suffix()}"
            (root / "ws" / name).mkdir(parents=True)
            (root / "ws" / name / ".wk-ready").touch()
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "WK_REMOTE_ROOT": str(root),
                "PATH": f"{binp}:{self._real_path()}",
                # The probe's cap: every stub here answers at once.
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
            # cmd/status's own documented contract (cmd/status header): exit
            # 4 is "a machine that will not answer".
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
        # WK_MACHINES_DIR (lib/wk/fleet.py) fakes the fleet without
        # touching machines/*.conf; WK_TARGET=remote (with the stub ssh
        # every boot driver's m_ssh also shells out through, since it
        # calls `ssh` by name) keeps the *workspace target* walk to one
        # fast, fake target instead of every real machine in
        # machines/*.conf -- the thing that hung past 120s before.
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _ANSWERING_SSH}) as binp:
            # Short, so a name fits the listing's machine column.
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
        """a workspace name on two targets refuses and names both"""
        with self.assertRaises(LookupError) as e:
            _Registry(["alpha", "beta", "gamma"], {"alpha", "beta"}).ws_target("demo-ambiguous")
        self.assertIn("demo-ambiguous", str(e.exception))
        self.assertIn("alpha beta", str(e.exception))
        self.assertNotIn("gamma", str(e.exception))

    def test_a_name_on_one_target_still_resolves(self):
        """the same collecting walk still resolves an unambiguous name"""
        self.assertEqual(_Registry(["alpha", "beta"], {"beta"}).ws_target("demo-single"), "beta")


if __name__ == "__main__":
    unittest.main()
