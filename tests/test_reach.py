"""How a machine is reached (lib/wk/reach.py): the tailnet read once per invocation, `ssh -G`'s answer, the
neighbour table as a sweep's answer, and the bash shims (lib/reach.sh) handing the shell's one read to the Python.

Run: python3 tests/run.py --unit -k test_reach
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, reach  # noqa: E402
from wk.machine import Fake  # noqa: E402

STATUS = json.dumps({"Self": {"DNSName": "Here.tail.ts.net.", "TailscaleIPs": ["100.64.0.1"]},
                     "Peer": {"k1": {"DNSName": "downboard.tail.ts.net.", "TailscaleIPs": ["100.64.0.2"], "Online": False},
                              "k2": {"DNSName": "rpi4-rescue.tail.ts.net.", "TailscaleIPs": ["100.64.0.3"], "Online": True},
                              "k3": {"HostName": "localhost", "TailscaleIPs": ["100.64.0.4"], "Online": True}}})


def world(confs=None):
    d = Path(tempfile.mkdtemp(prefix="wk-test-reach-"))
    for name, text in (confs or {}).items():
        (d / (name + ".conf")).write_text(text)
    env = {"WK_MACHINES_DIR": str(d), "XDG_CONFIG_HOME": str(d / "none")}
    f = Fake("here")
    f.answer(["tailscale", "status", "--json"], out=STATUS)
    return f, reach.Reach(f, env, fleet.Fleet(REPO, env))


class TestTheTailnet(unittest.TestCase):
    def test_peers_are_named_by_dns_name_lowercased_with_this_machine_first(self):
        self.assertEqual(reach.parse_peers(STATUS)[:2], [("here", "100.64.0.1", "up"), ("downboard", "100.64.0.2", "down")])
        self.assertNotIn("localhost", [p[0] for p in reach.parse_peers(STATUS)])

    def test_it_is_read_once_however_often_it_is_asked(self):
        """`machine.probed_once_per_invocation`, for the coordinator: one `tailscale status` per Reach."""
        f, r = world()
        r.tailnet("downboard"), r.offline("downboard"), r.tailnet("nothing"), r.peers()
        self.assertEqual(len([e for e in f.effects if e[1][0] == "tailscale"]), 1)

    def test_the_app_store_cli_is_asked_when_there_is_no_tailscale_on_path(self):
        f = Fake("here")   # no `tailscale` answers: 127, as for a command not on PATH
        f.files[reach.APP_CLI] = ""
        f.answer([reach.APP_CLI], out=STATUS)
        self.assertEqual(reach.Reach(f, {}).tailnet("downboard"), "100.64.0.2 (down)")

    def test_a_down_node_is_refused_by_name_and_an_unlisted_one_is_not(self):
        _f, r = world()
        self.assertEqual(r.offline("downboard"), "the tailnet says downboard is offline -- power it on, or 'wk machine probe downboard'")
        self.assertEqual(r.offline("rpi4-rescue"), "")
        self.assertEqual(r.offline("unlisted"), "")

    def test_a_bench_machine_is_reached_under_its_role_names(self):
        _f, r = world({"rpi4": "KIND=board\nNODE_SSH=rpi4-rescue\nNODE_BENCH_SSH=rpi4-bench\n"})
        self.assertEqual(r.fleet_line("rpi4"), "rpi4-rescue 100.64.0.3 (up); rpi4-bench not a node")
        self.assertEqual(r.without_tailnet("rpi4"), "", "a role name on the tailnet is how it is reached")


class TestWithoutTheTailnet(unittest.TestCase):
    def test_ssh_g_is_the_route_when_it_names_another_host(self):
        f, r = world({"box": "KIND=build\n"})
        f.answer(["ssh", "-G", "box"], out="user me\nhostname 10.0.0.5\nport 2222\nproxyjump gw\n")
        self.assertEqual(r.without_tailnet("box"), "me@10.0.0.5:2222  (through gw)")

    def test_a_board_is_swept_for_by_its_hardware_address(self):
        f, r = world({"b": "KIND=board\nNODE_MAC=AA:BB:CC:DD:EE:FF\n"})
        f.answer(["ssh", "-G", "b"], out="hostname b\n")
        f.answer(["ip", "-4", "-o", "addr", "show"], out="1: lo inet 127.0.0.1/8\n2: en0 inet 10.1.2.3/24 brd x\n"
                                                         "3: tailscale0 inet 100.64.0.1/32\n")
        f.answer(["sh", "-c", reach.SWEEP], out="10.1.2.9 dev en0 lladdr aa:bb:cc:dd:ee:ff STALE\n")
        self.assertEqual(r.without_tailnet("b"), "10.1.2.9  (found by sweeping 10.1.2.0/24 -- not stored)")
        f.answer(["sh", "-c", reach.SWEEP], out="")
        self.assertEqual(r.without_tailnet("b"), "not on the tailnet -- wk machine probe b sweeps for it")


class TestTheSweep(unittest.TestCase):
    def test_the_neighbour_table_is_the_answer_on_the_segment_alone(self):
        text = ("10.0.0.2 dev e lladdr AA:00:00:00:00:02 REACHABLE\n10.0.0.3 dev e FAILED\n"
                "10.0.0.4 dev e lladdr aa:00:00:00:00:04 INCOMPLETE\n10.0.1.5 dev e lladdr aa:00:00:00:00:05 STALE\n"
                "10.0.0.6 dev e INCOMPLETE\nfe80::1 dev e lladdr aa:00:00:00:00:07 STALE\n")
        self.assertEqual(reach.parse_neigh(text, "10.0.0.0/24"), [("10.0.0.2", "aa:00:00:00:00:02", "REACHABLE")])

    def test_a_vantage_that_cannot_sweep_is_blind_not_empty(self):
        f, r = world()
        f.answer(["ssh"], rc=255, err="ssh: connect to host phone: No route to host\n")
        self.assertIsNone(r.sweep("10.99.0.0/24", "phone"))
        f.answer(["sh", "-c", reach.SWEEP], rc=66)
        self.assertIsNone(r.sweep("10.99.0.0/24"))

    def test_a_bridge_contributes_the_segment_it_routes(self):
        f, r = world({"br": "KIND=bridge\nBR_SSH=br-phone\nBR_SEGMENT=10.99.1.0/24\nBR_LEASES=\"aa:bb:cc:00:00:01,10.99.1.10,rpi4\"\n"})
        f.answer(["ip", "-4", "-o", "addr", "show"], out="2: en0 inet 192.168.1.4/24 brd x\n")
        s = reach.Survey(r)
        self.assertEqual(s.vantages(), [("local", "192.168.1.0/24", "local"), ("br-phone", "10.99.1.0/24", "br")])
        self.assertEqual(s.leases(), {"aa:bb:cc:00:00:01": ("10.99.1.10", "rpi4", "br")})


class TestTheShims(unittest.TestCase):
    def test_the_shells_one_read_is_what_the_python_judges(self):
        """reach_offline answers from the peers this shell read, by exit status and REACH_WHY."""
        cp = bash(f'''
. "{REPO}/lib/common.sh"; . "{REPO}/lib/reach.sh"
wk_tailscale_peers() {{ printf 'downboard\\t100.64.0.2\\tdown\\nup\\t100.64.0.3\\tup\\n'; }}
reach_offline downboard && echo "down: $REACH_WHY"
reach_offline up || echo "up is not refused"
echo "tailnet: $(reach_tailnet up)"
''', env={"WK_ROOT": str(REPO)})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("down: the tailnet says downboard is offline", cp.stdout)
        self.assertIn("up is not refused", cp.stdout)
        self.assertIn("tailnet: 100.64.0.3 (up)", cp.stdout)

    def test_the_shim_file_is_one_line_per_function(self):
        text = (REPO / "lib" / "reach.sh").read_text()
        defs = [l for l in text.splitlines() if "() {" in l]
        self.assertTrue(defs)
        for line in defs:
            self.assertTrue(line.rstrip().endswith("}") or "}   #" in line, line)


if __name__ == "__main__":
    unittest.main()
