"""Fleet walk: `wk status` reaches a remote target over ssh (targets/remote.sh's
`_rsh_q`/`_remote_probe_try`), and this drives that for real against a faked
machine -- WK_TARGET=remote, a made-up WK_REMOTE_HOST, and a stub `ssh` on
PATH that either answers (a normal shell, standing in for a live machine) or
refuses (ssh's own exit 255, standing in for one that never answers) -- so
`cmd/status` genuinely shells out to the stub rather than to a mocked driver
function.

The named form, `wk status <name> --text` (cmd/status's `collect()`), skips
the bench-device fleet walk entirely and answers for exactly the one target
asked about, which is enough to exercise the real ssh shell-out and the
unreachable-by-name reporting faithfully and fast.

A bare `wk status` also walks this host's bench-device fleet
(`report_fleet_devices`, cmd/status): WK_MACHINES_DIR (boot/machines.sh,
read only by `machines_dir`) points that walk at a directory of faked
`boot/machines/*.conf`-shaped confs instead of the real fleet, and the same
stub `ssh` answers every board's probe too -- `m_ssh`/`i_ssh`
(boot/machines.sh) shell out to `ssh` by name, the same as targets/remote.sh
does. Driving the real fleet for real from a test hung past two minutes in
this environment (measured, then killed); this is what replaces that.

Run: python3 -m unittest tests.test_fleet_walk -v
"""
import os
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, stub_path


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
        # The stub runs the ssh'd command locally, so `t_info` (targets/
        # remote.sh) stats a directory on *this* filesystem: WK_REMOTE_ROOT,
        # pointed at a scratch dir instead of the real $HOME/wk a bare
        # WK_REMOTE_HOST would default to, with the ready marker every
        # driver writes as creation's last act (WK_READY_MARKER,
        # lib/target.sh) already in place -- the answer a real finished
        # workspace on a real reachable machine would give.
        with stub_path({"ssh": _ANSWERING_SSH}) as binp, scratch_dir(prefix="wk-test-remote-root-") as root:
            name = f"demo-{rand_suffix()}"
            (root / "ws" / name).mkdir(parents=True)
            (root / "ws" / name / ".wk-ready").touch()
            env = {
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "WK_REMOTE_ROOT": str(root),
                "PATH": f"{binp}:{self._real_path()}",
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


_FAKE_NODE_CONF = '''NODE_SSH={ssh}
NODE_DRIVER=rpi5-usb
NODE_DEVICE=/dev/sda
NODE_ROOT=/dev/nvme0n1p2
NODE_PROFILE=webkit-2.52-yocto-rpi5-64
NODE_MAC={mac}
NODE_BRIDGE=""
NODE_ROLE=workstation
NODE_OS=any
NODE_LOCAL=""
NODE_VOLUME=""
NODE_DTB=bcm2712-rpi-5-b.dtb
NODE_BENCH_SSH=""
NODE_NET=wifi
NODE_NOTE="{note}"
'''


class TestFleetWalkBareFormMultiMachine(WkTest):
    def test_bare_status_renders_every_faked_machines_block(self):
        # WK_MACHINES_DIR (boot/machines.sh) fakes the fleet without
        # touching boot/machines/*.conf; WK_TARGET=remote (with the stub ssh
        # every boot driver's m_ssh/i_ssh also shells out through, since it
        # calls `ssh` by name) keeps the *workspace target* walk to one
        # fast, fake target instead of every real machine in
        # targets/hosts/*.conf -- the thing that hung past 120s before.
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _ANSWERING_SSH}) as binp:
            # Short: machine_list's listing (boot/machines.sh) is a fixed
            # `%-8s` column with no separator for a name that fills or
            # overflows it, so a longer name runs into its own note.
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
            }
            cp = run("status", "--text", env=env, timeout=45)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            for n in names:
                self.assertIn(n, cp.stdout, f"'{n}' missing from a bare 'wk status --text':\n{cp.stdout}")


class TestResolution(unittest.TestCase):
    def test_a_name_on_two_targets_refuses_naming_both(self):
        """a workspace name on two targets refuses and names both"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/target.sh"
target_all() {{ printf 'alpha\\nbeta\\ngamma\\n'; }}
ws_on_target() {{ case "$1" in alpha|beta) return 0 ;; esac; return 1; }}
ws_target demo-ambiguous
'''
        cp = bash(script)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("demo-ambiguous", cp.stderr, cp.stderr)
        self.assertIn("alpha", cp.stderr, cp.stderr)
        self.assertIn("beta", cp.stderr, cp.stderr)
        self.assertNotIn("gamma", cp.stderr, cp.stderr)

    def test_a_name_on_one_target_still_resolves(self):
        """the same collecting loop still resolves an unambiguous name"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/target.sh"
target_all() {{ printf 'alpha\\nbeta\\n'; }}
ws_on_target() {{ [ "$1" = beta ]; }}
ws_target demo-single
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "beta", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
