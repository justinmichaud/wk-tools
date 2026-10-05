"""`wk status`: the collector (lib/wk/status.py) and the renderer (lib/wk/statusview.py) driven in process, plus
`wk key push status --all`."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, bash, clean_env, load_cmd, run
from tests.test_wk_places import LINUX_PROBE, SshFake

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, places, record, status, statusview  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.resources import Resources  # noqa: E402
from wk.record import Records  # noqa: E402
from wk.store import Store  # noqa: E402


def render(records, mode="text"):
    """The renderer on synthetic records, in process: what a person or an agent reading `wk status` sees, with no
    machine required."""
    records = list(records)
    doc = statusview.merge(records)
    if mode == "json":
        out = json.dumps(doc, indent=2) + "\n"
    elif mode == "html":
        out = statusview.write_page(doc, os.path.join(tempfile.mkdtemp(prefix="wk-status-page-"), "status.html")) + "\n"
    else:
        buf = io.StringIO()
        statusview.render_text_stream(iter(records), buf, False)
        out = buf.getvalue()
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


def machine_rec(name, **extra):
    r = {"kind": "machine", "name": name}
    r.update(extra)
    return r


class TestLoadLine(unittest.TestCase):
    """Each machine's load and free memory, never invented."""

    def test_text_shows_load_and_free_memory(self):
        recs = [machine_rec("buildbox4"),
                {"kind": "capacity", "machine": "buildbox4", "cores": "128", "load": "8", "free_mb": "170000", "mem_mb": "196000"},
                {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("buildbox4", out)
        self.assertIn("8", out)
        self.assertIn("128 cores", out)
        self.assertIn("free", out)

    def test_a_machine_that_did_not_answer_says_so_not_a_number(self):
        recs = [machine_rec("devbox-arm64-2"),
                {"kind": "capacity", "machine": "devbox-arm64-2", "note": "could not measure load/memory on devbox-arm64-2"},
                {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("could not measure load/memory on devbox-arm64-2", out)
        self.assertNotIn("of  cores", out)

    def test_this_hosts_capacity_is_measured_by_resources(self):
        fake = Fake()
        fake.answer(["nproc"], out="16\n")
        fake.files["/proc/meminfo"] = "MemTotal: 33554432 kB\nMemAvailable: 20971520 kB\n"
        rec = status.capacity_here("here", "", Resources(fake, {}, "linux"))
        self.assertEqual((rec["cores"], rec["mem_mb"], rec["free_mb"]), ("16", "32768", "20480"))

    def test_an_unreadable_host_is_a_note_not_a_number(self):
        with mock.patch("sys.stderr", io.StringIO()):
            rec = status.capacity_here("here", "", Resources(Fake(), {}, "linux"))
        self.assertIn("could not measure", rec["note"])

    def test_no_capacity_record_at_all_is_silence_not_a_zero(self):
        out = render([machine_rec("quiet-machine"), {"kind": "exit", "code": 0}], "text").stdout
        self.assertIn("quiet-machine", out)
        self.assertNotIn("0 of", out)

    def test_the_record_a_probe_that_failed_leaves(self):
        rec = status.capacity_record("box", None, "", "", "", "")
        self.assertEqual(rec["note"], "could not measure load/memory on box")
        self.assertNotIn("cores", rec)
        rec = status.capacity_record("box", "the podman VM", "8", "16384", "9000", "1.5")
        self.assertEqual((rec["cores"], rec["mem_mb"], rec["free_mb"], rec["load"], rec["where"]), ("8", "16384", "9000", "1.5", "the podman VM"))


class TestReprovisionLine(unittest.TestCase):
    def test_text_shows_the_recipe_from_the_record(self):
        recs = [{"kind": "fleet", "machine": "rpi3", "role": "bench-device", "mode": "base image -- not a bench system",
                 "media": "SD card", "reprovision": "wk sysimage build webkit-2.52-yocto-rpi3-32\n    in a workspace; hours\nwk boot rpi3"},
                {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("re-provisioning", out)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi3-32", out)
        self.assertIn("wk boot rpi3", out)

    def test_a_device_missing_mach_profile_renders_the_missing_field_not_a_guess(self):
        recs = [{"kind": "fleet", "machine": "newdevice", "role": "bench-device", "mode": "unreachable", "media": "unknown",
                 "reprovision": "missing profile in machines/newdevice.conf -- nothing to compose a recipe from"},
                {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("missing profile in machines/newdevice.conf", out)
        self.assertNotIn("wk sysimage build newdevice", out)

    def test_the_by_role_sample_command_differs_per_role(self):
        recs = [{"kind": "fleet", "machine": "rpi4", "role": "bench-device", "mode": "host mode", "media": "usb stick",
                 "reprovision": "wk sysimage build p\nwk boot rpi4"},
                {"kind": "fleet", "machine": "rpi5", "role": "workstation", "mode": "host mode", "media": "nvme",
                 "reprovision": "wk sysimage build p2\nwk boot rpi5"},
                {"kind": "bridge", "name": "some-bridge", "device": "d", "segment": "s"},
                {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        for text in ("by role", "a rescue system", "a bench system", "a workstation", "a tailnet bridge"):
            self.assertIn(text, out)


class TestFleetDeviceRecord(unittest.TestCase):
    """The probe's fields become one record; a probe that did not answer in its ceiling says so by name."""

    CONF = {"role": "bench-device", "driver": "rpi5-usb", "note": "a board"}

    def test_a_probe_past_its_ceiling_is_named_with_the_ceiling(self):
        rec = status.fleet_record("rpi4", self.CONF, None, 0, reach=lambda m: ("rpi4 not a node", ""))
        self.assertEqual((rec["machine"], rec["role"], rec["mode"]), ("rpi4", "bench-device", "no answer within 0s"))
        self.assertEqual(rec["tailnet"], "rpi4 not a node")
        self.assertEqual(rec["conf"], "machines/rpi4.conf")

    def test_the_mode_words(self):
        for probeable, mode, bridge, want in (("no", "", "", "unknown from here"), ("yes", "host", "", "host mode"),
                                               ("yes", "base abc", "", "base image -- not a bench system"),
                                               ("yes", "bench abc", "", "bench mode"), ("yes", "", "", "unreachable"),
                                               ("yes", "unreachable", "phone", "unreachable via phone")):
            self.assertEqual(status.fleet_mode(probeable, mode, bridge), want)

    def test_a_probe_that_said_nothing_is_no_record(self):
        self.assertIsNone(status.fleet_record("x", self.CONF, {}, 4))

    def test_the_fields_land_where_the_renderer_reads_them(self):
        fields = dict(role="workstation", probeable="yes", mode="host", bridge="", armed="img-1", media="usb",
                      reprovision="wk boot x", tailnet="x 1.2.3.4 (up)", direct="")
        rec = status.fleet_record("x", self.CONF, fields, 4)
        self.assertEqual((rec["mode"], rec["armed"], rec["media"], rec["reprovision"]), ("host mode", "img-1", "usb", "wk boot x"))
        self.assertNotIn("direct", rec)
        self.assertNotIn("armed_by", rec)
        self.assertIn("** armed for img-1 -- wk boot x --status **", render([rec]).stdout)

    ARMED_FIELDS = dict(role="workstation", probeable="yes", mode="host", bridge="", armed="img-1", media="usb",
                        reprovision="", tailnet="", direct="", armed_by="tolken", armed_at="2026-01-01T00:00:00Z")

    def test_an_arm_is_desync_once_consumed_stale_or_unreadable(self):
        clock = FakeClock()
        fresh = dict(self.ARMED_FIELDS, armed_at=clock.iso(), armed_boot="a", boot_id="a")
        rec = status.fleet_record("rpi5", self.CONF, fresh, 4, clock=clock)
        self.assertEqual((rec["armed_by"], rec["armed_at"]), ("tolken", clock.iso()))
        self.assertNotIn("armed_desync", rec)
        for fields in (dict(fresh, boot_id="after"), dict(fresh, armed_at="garbage")):
            self.assertTrue(status.fleet_record("rpi5", self.CONF, fields, 4, clock=clock)["armed_desync"], fields)
        clock.t += status.ARM_STALE_SECONDS + 1
        self.assertTrue(status.fleet_record("rpi5", self.CONF, fresh, 4, clock=clock)["armed_desync"])

    def test_fleet_probe_carries_the_arming_fields(self):
        said = dict(self.ARMED_FIELDS, armed_boot="boot-a", boot_id="boot-b")
        with mock.patch.object(status.Local, "run", return_value=Result(0, out=json.dumps(said))) as run:
            fields = status.fleet_probe(REPO, "rpi5", 1)
        self.assertEqual(fields, said)
        self.assertIn("wk.boot.cli", run.call_args[0][0])

    def test_the_renderer_shows_the_transition_and_desyncs_a_stale_arm(self):
        rec = status.fleet_record("rpi5", self.CONF, dict(self.ARMED_FIELDS, armed_boot="before", boot_id="after"), 4)
        out = render([rec]).stdout
        self.assertIn("armed for img-1 by tolken since 2026-01-01T00:00:00Z", out)
        self.assertIn("desync", out)


class TestBridgeRecord(unittest.TestCase):
    """`fields` is bridge/bin/wk-bridge-healthcheck's raw facts (lib/wk/bridge judges them); `facts` missing from
    an answering, role-carrying phone means the healthcheck itself never ran (no non-interactive root), told
    apart here from one that ran and found something wrong."""

    CONF = dict({k: v("phone") if callable(v) else v for k, v in fleet.BRIDGE_DEFAULTS.items()},
                device="pinephone", segment="10.99.1.0/24", note="a phone")
    HEALTHY = {
        "facts": "yes", "wifi_iface": "wlan0", "wifi_addr": "192.168.1.5/24", "wifi_power_save": "off",
        "seg_iface_exists": "yes", "seg_carrier": "1", "seg_addr": "10.99.1.1/24", "seg_usb_speed": "480",
        "leases_file_nonempty": "yes", "leases_ping": "",
        # The one blob wk-bridge-healthcheck hands judge(): raw `tailscale status --json`.
        "ts_status_json": json.dumps({"BackendState": "Running",
                                       "Self": {"Online": True, "Tags": ["tag:bridge"], "PrimaryRoutes": ["10.99.1.0/24"]}}),
        "svc_wk-bridge-dhcp": "running", "svc_wk-bridge-nftables": "running", "svc_wk-bridge-netwatch": "running",
        "svc_wk-bridge-usb-host": "running", "svc_sshd": "running", "svc_chrony": "running", "svc_nm": "running",
        "svc_tailscale": "running", "nft_table": "yes", "ip_forward": "1",
        "resolv_exists": "yes", "resolv_nameservers": "1", "resolv_first": "nameserver 1.1.1.1", "dns_resolves": "yes",
        "clock_year": "2026", "clock_iso": "2026-09-24 00:00 UTC", "sshd_password_auth": "no",
        "watchdog_device": "yes", "watchdog_fed": "yes", "swap_nonzram": "0", "crashed": "", "uptime": "up 1 day",
        "battery": "",
    }

    def _rec(self, fields, want="123"):
        return status.bridge_record("phone", self.CONF, want, fields, lambda n: ("", ""))

    def test_no_answer_is_unreachable(self):
        self.assertEqual(self._rec({})["state"], "unreachable")

    def test_answering_with_no_role_names_the_setup(self):
        rec = self._rec({"reachable": "yes", "role": "no"})
        self.assertEqual(rec["state"], "no bridge role")
        self.assertIn("wk machine setup phone", rec["notes"][0]["text"])

    def test_a_healthy_role_at_this_repositorys_sum_is_up(self):
        rec = self._rec(dict(self.HEALTHY, reachable="yes", role="yes", sum="123"))
        self.assertEqual((rec["state"], rec["role_insync"]), ("up", True))
        self.assertNotIn("health", rec)

    def test_a_health_check_that_needs_root_is_told_apart_from_an_unhealthy_one(self):
        rec = self._rec({"reachable": "yes", "role": "yes", "sum": "999"})
        self.assertEqual((rec["state"], rec["role_insync"]), ("role installed", False))
        rec = self._rec(dict(self.HEALTHY, reachable="yes", role="yes", sum="123",
                              ts_status_json=json.dumps({"BackendState": "Running",
                                                          "Self": {"Online": True, "Tags": ["tag:bridge"], "PrimaryRoutes": []}})))
        self.assertEqual(rec["state"], "unhealthy")
        self.assertIn("no approved subnet route", rec["health"])

    def test_the_role_sum_is_over_the_repositorys_bridge_files(self):
        want = status.bridge_role_sum(REPO)
        self.assertTrue(want.isdigit(), want)
        again = bash('cd bridge && cat $(ls bin/* | sort) $(ls init.d/* | sort) | cksum | awk \'{print $1}\'').stdout.strip()
        self.assertEqual(want, again)


class TestSelfRoleAndMode(unittest.TestCase):
    """This machine's own role and mode, read locally with no probe of its own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-self-"))

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_role_from_its_own_conf_else_workstation_and_mode_from_the_marker(self):
        self.assertEqual(status.self_role(str(self.tmp), "here"), "workstation")
        (self.tmp / "machines").mkdir(parents=True)
        (self.tmp / "machines" / "here.conf").write_text("kind=board\nrole=bench-device\n")
        self.assertEqual(status.self_role(str(self.tmp), "here"), "bench-device")
        self.assertEqual(status.self_mode_word({"WK_IMAGE_MARKER": str(self.tmp / "no-marker")}), "host")
        marker = self.tmp / "wk-image"
        marker.write_text("id=bench-2026-01\nprofile=p\n")
        self.assertEqual(status.self_mode_word({"WK_IMAGE_MARKER": str(marker)}), "bench bench-2026-01")


class TestWalkLeadsWithSelf(unittest.TestCase):
    """A bare `wk status` yields its own role and mode first, computed locally and not by re-probing itself as a
    fleet device."""

    def _walk(self, root, env):
        w = status.Walk(root, env=env, fleet=False, devices=False)
        w.places = lambda: []   # no registry needed: only the leading record and the exit are asked
        return w

    def test_the_self_record_is_the_first_thing_yielded(self):
        env = {"WK_IMAGE_MARKER": "/nonexistent/wk-image-marker-for-tests", "WK_ROW_LABEL": "here"}
        w = self._walk(REPO, env)
        recs = list(w.records(markers=False))
        self.assertEqual((recs[0]["kind"], recs[0]["machine"], recs[0]["role"], recs[0]["mode"]),
                         ("fleet", "here", "workstation", "host mode"))
        self.assertEqual(recs[-1]["kind"], "exit")

    def test_a_named_workspace_walk_does_not_lead_with_it(self):
        env = {"WK_ROW_LABEL": "here"}
        w = status.Walk(REPO, name="ws1", env=env, fleet=False, devices=False)
        w.reg = types.SimpleNamespace(ws_place=lambda n: "local")
        w._job = lambda tname, name: (lambda: ([], 0))
        recs = list(w.records(markers=False))
        self.assertFalse(any(r.get("kind") == "fleet" for r in recs))

    def test_the_self_machine_is_never_reprobed_as_a_fleet_device(self):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-fleetself-"))
        try:
            (tmp / "boot" / "machines").mkdir(parents=True)
            (tmp / "boot" / "machines" / "here.conf").write_text("driver=x\nnote=this machine\n")
            (tmp / "boot" / "machines.sh").write_text("")
            env = {"WK_ROW_LABEL": "here"}
            w = status.Walk(str(tmp), env=env, fleet=True, devices=True)
            w.reg = types.SimpleNamespace(in_workspace=lambda: False)
            with mock.patch.object(status, "fleet_probe") as fp:
                w.fleet_devices()
            fp.assert_not_called()
        finally:
            subprocess.run(["rm", "-rf", str(tmp)])


class TestBump(unittest.TestCase):
    """The walk's exit code only rises, and anything outside 0-4 reads as 4, never as all clear."""

    def test_the_worst_wins_and_garbage_reads_4(self):
        w = 0
        for c in ("2", 1, "0"):
            w = status.bump(w, c)
        self.assertEqual(w, 2)
        for c in ("", "abc", "7", "-1", "255"):
            self.assertEqual(status.bump(0, c), 4, c)


class TestDefaultView(unittest.TestCase):
    """A bare `wk status` opens the page at a terminal that can show one, and prints the table everywhere else."""

    def test_not_a_terminal_is_text(self):
        self.assertEqual(statusview.default_mode({}, False), "text")

    def test_a_terminal_with_a_browser_is_web(self):
        self.assertEqual(statusview.default_mode({"HOME": "/nonexistent"}, True), "web")

    def test_wk_status_view_decides_whatever_the_terminal(self):
        self.assertEqual(statusview.default_mode({"WK_STATUS_VIEW": "json", "NO_COLOR": "1"}, True), "json")
        self.assertEqual(statusview.default_mode({"WK_STATUS_VIEW": "json"}, False), "json")

    def test_ci_no_color_ssh_and_a_workspace_stay_out_of_the_browser(self):
        for env in ({"CI": "1"}, {"NO_COLOR": "1"}, {"SSH_CONNECTION": "x"}, {"SSH_TTY": "/dev/pts/1"}):
            env["HOME"] = "/nonexistent"
            self.assertEqual(statusview.default_mode(env, True), "text", env)
        self.assertEqual(statusview.default_mode({"SSH_CONNECTION": "x", "DISPLAY": ":0", "HOME": "/nonexistent"}, True), "web")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".wk-workspace").write_text("name=ws\n")
            self.assertEqual(statusview.default_mode({"HOME": tmp}, True), "text")

    def test_colour_needs_a_terminal_and_no_color_unset(self):
        tty = types.SimpleNamespace(isatty=lambda: True)
        pipe = types.SimpleNamespace(isatty=lambda: False)
        self.assertTrue(statusview.colour_wanted(tty, {}))
        self.assertFalse(statusview.colour_wanted(tty, {"NO_COLOR": "1"}))
        self.assertFalse(statusview.colour_wanted(pipe, {}))
        self.assertFalse(statusview.colour_wanted(pipe, {"NO_COLOR": "1"}))


class TestWaitAndTimeout(unittest.TestCase):
    """`--wait` polls while the answer is busy and stops on any other; `--timeout` is elapsed time, and ends the wait without a verdict."""

    def _wait(self, answers, timeout, interval=5):
        clock = FakeClock()
        said = []
        it = iter(answers)
        polls = []

        def poll():
            polls.append(clock.now())
            clock.t += 0.5   # a poll of a busy workspace costs time of its own
            return next(it)
        rc = status.wait_until_idle(poll, timeout, interval, clock, "ws", said.append, said.append)
        return rc, polls, said, clock

    def test_two_is_the_only_state_worth_waiting_through(self):
        for answers, want in (([0], 0), ([1], 1), ([3], 3), ([4], 4), ([2, 2, 0, 0], 0), ([2, 3, 3], 3)):
            rc, polls, said, _ = self._wait(answers, 0)
            self.assertEqual(rc, want, answers)
            self.assertEqual(len(polls), len(answers))
            self.assertEqual(len([s for s in said if "says busy" in s]), 1 if len(answers) > 1 else 0)

    def test_the_timeout_is_elapsed_time_not_the_sum_of_sleeps(self):
        rc, polls, said, clock = self._wait([2] * 100, timeout=10, interval=5)
        self.assertEqual(rc, 2)
        self.assertEqual(len(polls), 3)   # 0, 5.5 and 11: the poll's own cost counts
        self.assertGreaterEqual(clock.now(), 10)
        self.assertIn("still busy after 11s -- the work continues; this only stopped waiting", said[-1])

    def test_the_interval_is_the_sleep_between_polls(self):
        _, _, _, clock = self._wait([2, 2, 0, 0], 0, interval=7)
        self.assertEqual(clock.slept, [7, 7, 7])

    def test_one_poll_that_could_not_ask_does_not_end_the_wait(self):
        rc, polls, said, _ = self._wait([2, 2, 4, 2, 2, 0, 0], timeout=3300)
        self.assertEqual((0, 7), (rc, len(polls)))
        self.assertFalse([s for s in said if "still busy" in s])

    def test_a_verdict_that_persists_ends_the_wait(self):
        rc, polls, _, _ = self._wait([2, 4, 4], timeout=3300)
        self.assertEqual((4, 3), (rc, len(polls)))


def load_status_cmd():
    return load_cmd("status")


class TestTheWaitDecidesTheExit(unittest.TestCase):
    """The walk after a wait shows the state; one whose probe failed must not turn a confirmed idle into another code."""

    def test_the_exit_is_the_waits_verdict_not_the_walk_after_it(self):
        cmd = load_status_cmd()
        walks = []

        class FlakyWalk:
            def __init__(self, *a, **kw):
                walks.append(self)
                self.worst = 4

            def worst_only(self):
                return self.worst

            def records(self, markers=True):
                return iter(())

        with mock.patch.object(cmd, "Walk", FlakyWalk), mock.patch.object(cmd, "wait_until_idle", return_value=0), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd.main(["--wait", "--records"]))
            self.assertEqual(4, cmd.main(["--records"]), "without --wait the walk decides")
        self.assertEqual(2, len(walks))


class TestToolsFact(unittest.TestCase):
    """A machine on another wk-tools sha, or a dirty checkout, is named with both shas and its remedy."""

    def test_a_copy_at_this_commit_is_in_sync_whichever_abbreviation_is_longer(self):
        rec = status.tools_fact({"sha": "abcdef1234567890", "dirty": "no"}, "abcdef1", "box", "box")
        self.assertEqual((rec["insync"], rec["dirty"], rec["sha"], rec["expect"]), (True, False, "abcdef1234567890", "abcdef1"))
        self.assertNotIn("fix", rec)
        self.assertTrue(status.tools_fact({"sha": "abc", "dirty": "no"}, "abcdef1", "box", "box")["insync"])

    def test_another_commit_names_the_push_and_a_dirty_copy_reads_dirty(self):
        rec = status.tools_fact({"sha": "0000000", "dirty": "yes"}, "abcdef1", "box", "box")
        self.assertEqual((rec["insync"], rec["dirty"], rec["fix"]), (False, True, "wk sync --tools box"))

    def test_the_remedy_follows_who_pulls_from_whom(self):
        ver = {"sha": "0000000", "dirty": "no"}
        self.assertEqual(status.tools_fact(ver, "abc", "here", "container", in_vm=True)["fix"],
                         "./setup   (recreates the machine with this checkout mounted at /opt/wk-tools)")
        self.assertEqual(status.tools_fact(ver, "abc", "peer", "peer", peer=True)["fix"],
                         "wk sync --tools   (pulls on peer, and says so if it still differs)")
        self.assertEqual(status.tools_fact(ver, "abc", "peer", "peer", peer=True, dirty_here=True)["fix"],
                         "commit and push here first -- a peer pulls, and this checkout is dirty")

    def test_the_text_says_differs_and_the_page_gets_the_same_document(self):
        rec = status.tools_fact({"sha": "0000000", "dirty": "no"}, "abcdef1", "box", "box")
        out = render([machine_rec("box"), rec]).stdout
        self.assertIn("DIFFERS from the workstation (abcdef1)", out)
        self.assertIn("wk sync --tools box", out)

    def test_a_delegated_answer_stays_its_own_and_is_not_merged_into_this_machines_rows(self):
        far = [{"kind": "machine", "name": "far"},
               {"kind": "workspace", "machine": "far", "method": "native", "name": "ws", "state": "absent", "ws": "absent"}]
        asked = []

        class FarDriver:
            def wk(self, *args, env=None, quiet=False):
                asked.append((args, env))
                return 0, "".join(json.dumps(r) + "\n" for r in far)

        walk = object.__new__(status.Walk)
        recs, rc = walk.delegate(FarDriver(), "far", ["status", "--no-fleet", "--records", "ws"])
        self.assertEqual((rc, asked[0][1]["WK_ROW_LABEL"], asked[0][1]["WK_NO_DELEGATE"]), (0, "far", "1"))
        here = {"kind": "workspace", "machine": "here", "method": "native", "name": "ws", "state": "present", "ws": "present"}
        skew = status.tools_fact({"sha": "0000000", "dirty": "no"}, "abcdef1", "far", "far")
        doc = statusview.merge([machine_rec("here"), here] + [dict(r) for r in recs] + [skew])
        rows = {m["name"]: [w["state"] for meth in m["methods"] for w in meth["workspaces"]] for m in doc["machines"]}
        self.assertEqual([f["sha"] for m in doc["machines"] if m["name"] == "far" for f in m["facts"]], ["0000000"])
        self.assertEqual(rows, {"here": ["present"], "far": ["absent"]})
        self.assertEqual(doc["exit"], 0, "a same-named workspace on another machine is not a disagreement")
        self.assertEqual(skew["machine"], "far")

class TestPushStatusAll(WkTest):
    """`wk key push status --all` prints one line per machine, this one included."""

    def test_one_line_per_machine_including_this_one(self):
        env = clean_env()
        here = record.machine_name(env)
        expected = set(places.Registry(REPO, env=env).machines()) | {here}
        try:
            cp = self.run_wk("key", "push", "status", "--all", timeout=180)
        except subprocess.TimeoutExpired:
            self.skipTest("no route to the configured machines from here")
        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        seen = {l.split()[0] for l in lines}
        self.assertIn(here, seen, "--all skipped the machine it was typed on")
        self.assertEqual(seen, expected, "wk key push status --all must answer for every machine")
        self.assertIn(cp.returncode, (0, 1, 4))


class TaskTest(WkTest):
    """Records written by Records.begin into a scratch store, read by the collector."""

    def setUp(self):
        super().setUp()
        self.store = str(self.tmp / "store")
        self.answers = {}
        self.clock = FakeClock()

    def begin(self, where, name, kill, log, *plan, kind="build", abort=None):
        env = {"WK_STORE": self.store, **({"WK_ABORT_SECONDS": str(abort)} if abort else {})}
        return Records(self.store, clock=self.clock, env=env).begin(kind, where, name, kill, log, list(plan))

    def records(self):
        return Records(self.store, clock=self.clock, ask_place=lambda n, pid, cap: self.answers.get(n),
                       env={"WK_STORE": self.store})

    def reported(self, only=None):
        recs, worst = status.task_records(self.records(), only, self.clock)
        return recs, worst


class TestTasksOfOneWorkspace(TaskTest):
    """`wk status <ws>` reports that workspace's tasks; a bare `wk status` reports them all."""

    def setUp(self):
        super().setUp()
        self.begin("here", "ws1", "wk build ws1 --kill", "/nolog", "compile")
        self.begin("here", "ws2", "^C where it runs", "/nolog", "jsc", kind="test")

    def names(self, only=None):
        return sorted(r["name"] for r in self.reported(only)[0])

    def test_a_named_workspace_reports_only_its_own_tasks(self):
        self.assertEqual(self.names("ws1"), ["ws1"])
        self.assertEqual(self.names("ws2"), ["ws2"])

    def test_no_name_reports_every_task(self):
        self.assertEqual(self.names(), ["ws1", "ws2"])

    def test_a_task_that_ended_as_asked_is_not_reported_at_all(self):
        for word, reported in (("0", False), ("cancelled", False), ("stopped", False), ("refused", False),
                               ("3", True), ("stalled", True)):
            with self.subTest(word=word):
                self.begin("here", "ws1", "wk build ws1 --kill", "/nolog", "compile")
                self.records().find("build", "ws1").end(word)
                self.assertEqual(self.names("ws1"), ["ws1"] if reported else [])

    def test_a_task_whose_pid_is_in_a_workspace_is_asked_of_it_and_no_answer_reads_unanswered(self):
        self.begin("place", "ws3", "wk build ws3 --kill", "/nolog", "compile").pid(4242)
        for answer, state, code in ((True, "running", 2), (False, "died", 4), (None, "unanswered", 4)):
            with self.subTest(answer=answer):
                self.answers["ws3"] = answer
                recs, worst = self.reported("ws3")
                self.assertEqual((recs[0]["state"], worst), (state, code))

    def test_a_task_is_reported_on_the_machine_its_pid_is_on(self):
        self.begin("place", "ws4", "wk build ws4 --kill", "/nolog", "compile").pid(4242, "farbox")
        self.answers["ws4"] = True
        self.assertEqual(self.reported("ws4")[0][0]["machine"], "farbox")

    def test_the_record_carries_its_subject_plan_steps_and_kill(self):
        t = self.begin("here", "ws5", "wk sysimage build p --stop", "/nolog", "layers", "fetch", "image", kind="yocto")
        t.pid(os.getpid())
        t.step(2)
        t.set("subject", "slot base at 6f7bb97")
        rec = self.reported("ws5")[0][0]
        self.assertEqual((rec["task_kind"], rec["subject"], rec["kill"]), ("yocto", "slot base at 6f7bb97", "wk sysimage build p --stop"))
        self.assertEqual((rec["plan"], rec["steps"]), (["layers", "fetch", "image"], ["done", "running", "pending"]))
        self.assertEqual(rec["state"], "running")

class TestTaskVerdictsBecomeExitCodes(TaskTest):
    """One exit code per recorded state, and the note that goes with it."""

    def _task(self, end=None, log_age=None, abort=1800, pid=None):
        log = self.tmp / "build.log"
        log.write_text("[9/4200] cc\n")
        os.utime(log, (self.clock.now() - (log_age or 0), self.clock.now() - (log_age or 0)))
        t = self.begin("here", "ws1", "wk build ws1 --kill", str(log), "compile", abort=abort)
        t.pid(pid or os.getpid())
        if end is not None:
            t.end(end)
        recs, worst = self.reported("ws1")
        return recs[0], worst, "\n".join(n["text"] for n in recs[0].get("notes", []))

    def test_running_is_2_and_says_what_it_reached(self):
        rec, worst, notes = self._task()
        self.assertEqual((rec["state"], worst), ("running", 2))
        self.assertIn("alive: [9/4200] (last output 0s ago)", notes)

    def test_silent_inside_the_deadline_is_busy_and_names_the_log(self):
        rec, worst, notes = self._task(log_age=600)
        self.assertEqual((rec["state"], worst, rec["log_age"]), ("silent", 2, "600"))
        self.assertIn("no log output for 600s", notes)
        self.assertIn("counted as busy, since nothing", notes)
        self.assertIn("tail -f", notes)

    def test_silent_past_the_deadline_is_a_record_with_no_writer(self):
        rec, worst, notes = self._task(log_age=4000)
        self.assertEqual((rec["state"], worst), ("silent", 4))
        self.assertIn("past the 1800s this build recorded as its watchdog's deadline", notes)
        self.assertIn("watchdog is gone", notes)

    def test_failed_stalled_oom_and_died(self):
        for end, state, code, note in ((1, "failed", 1, "exit 1"), ("stalled", "stalled", 3, "killed after no output"),
                                       ("oom", "oom", 3, "killed for memory"), ("gave-up", "gave-up", 1, "build gave up")):
            with self.subTest(end=end):
                rec, worst, notes = self._task(end=end)
                self.assertEqual((rec["state"], worst), (state, code))
                self.assertIn(note, notes)
        rec, worst, notes = self._task(pid=4194304)
        self.assertEqual((rec["state"], worst), ("died", 4))
        self.assertIn("died without recording an exit", notes)

class TestHealthRecords(unittest.TestCase):
    """What a machine is apart from its workspaces, each from the evidence handed in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-health-"))
        self.env = {"WK_STORE": str(self.tmp / "store"), "XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp),
                    "WK_HOST_SECRETS": str(self.tmp / "store" / "secrets"), "WK_IN_VM": "1"}
        (self.tmp / "store" / "base" / "20260101T000000Z").mkdir(parents=True)
        self.store = Store(self.env)

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_disk_reads_the_filesystem_under_the_store(self):
        rec = status.disk_record(self.store, "here", True, 2)
        self.assertEqual((rec["store"], rec["where"], rec["snapshots"], rec["reclaimable"]), (str(self.tmp / "store"), "in the podman VM", "1", "2"))
        self.assertTrue(int(rec["total_mb"]) > 0 and 0 <= int(rec["used_pct"]) <= 100)
        self.assertEqual(rec["notes"][0]["text"], "wk gc would reclaim 2 snapshot(s)")
        self.assertIsNone(status.disk_record(Store({"WK_STORE": "/nonexistent"}), "here", False, 0))

    def test_a_disk_that_cannot_be_measured_says_so_rather_than_leaving_the_sizes_out(self):
        with mock.patch("shutil.disk_usage", side_effect=PermissionError(13, "Permission denied")):
            rec = status.disk_record(self.store, "here", False, 0)
        self.assertNotIn("free_mb", rec)
        self.assertIn("cannot measure the disk under %s: [Errno 13] Permission denied" % (self.tmp / "store"),
                      [n["text"] for n in rec["notes"] if n["level"] == "warn"])

    def test_a_bench_task_that_cannot_be_read_is_a_row_rather_than_left_out(self):
        task = self.tmp / "store" / "ws" / "w" / "bench" / "20260101T000000Z-t"
        task.mkdir(parents=True)
        (task / "task.json").write_text("{not json")
        (rec,) = status.bench_records(self.store, "here", lambda pid: False)
        self.assertEqual((rec["kind"], rec["task"], rec["state"]), ("bench", "20260101T000000Z-t", "broken"))
        self.assertIn(str(task), rec["notes"][0]["text"])

    def test_the_push_row_counts_keys_and_never_reports_a_switch_position(self):
        held = self.tmp / "store" / "push-keys"
        held.mkdir(parents=True)
        env = dict(self.env)
        env.pop("WK_IN_VM")
        store = Store(env)
        self.assertEqual(status.push_record(store, "m", ["fork", "forkwpe"], False)["state"], "no keys")
        (held / "build_key_fork").write_text("not-a-key\n")
        rec = status.push_record(store, "m", ["fork", "forkwpe"], False)
        self.assertEqual((rec["name"], rec["state"]), ("push credentials", "some keys held"))
        self.assertIn("1 deploy key(s), 1 absent, no API token -- 'wk key push status' says whether they are loaded", rec["detail"])
        (held / "build_key_forkwpe").write_text("not-a-key\n")
        (held / "github-pat").write_text("ghp_notatoken\n")
        rec = status.push_record(store, "m", ["fork", "forkwpe"], False)
        self.assertEqual(rec["state"], "keys held")
        self.assertIn("an API token", rec["detail"])
        self.assertNotIn(rec["state"], ("on", "off"))

    def test_the_podman_vm_reports_no_push_row_at_all(self):
        self.assertIsNone(status.push_record(self.store, "m", ["fork"], True))

    def test_quiesce_reads_the_one_directory_cmd_quiesce_writes(self):
        q = self.tmp / "quiesce"
        q.mkdir()
        self.assertIsNone(status.quiesce_record(str(q), "m"))
        (q / "caffeinate.pid").write_text("1\n")
        (q / "raiser.pid").write_text("2\n")
        rec = status.quiesce_record(str(q), "m")
        self.assertEqual((rec["name"], rec["state"], rec["detail"]), ("quiesce", "on", "caffeinate raiser"))
        self.assertIn("wk quiesce off", rec["notes"][0]["text"])
        self.assertEqual(status.quiesce_dir(self.store), str(self.tmp / "state" / "wk" / "quiesce"))
        self.assertEqual(status.quiesce_dir(Store(dict(self.env, WK_QUIESCE_STATE="/x"))), "/x")

    def test_a_lock_whose_holder_is_gone_reads_stale(self):
        lockdir = self.tmp / "locks"
        lockdir.mkdir()
        os.symlink("pid=4194304 tok=deadbeef at=2020-01-01T00:00:00Z cmd=stale", lockdir / "demo@host.lock")
        os.symlink("pid=%d tok=beef at=2026-01-01T00:00:00Z cmd=wk build" % os.getpid(), lockdir / "live@host.lock")
        store = Store(dict(self.env, WK_LOCK_DIR=str(lockdir)))
        recs = status.lock_records(store, "m", status.Local().alive)
        by = {r["resource"]: r for r in recs}
        self.assertEqual((by["demo@host"]["alive"], by["demo@host"]["cmd"], by["demo@host"]["at"]), (False, "stale", "2020-01-01T00:00:00Z"))
        self.assertEqual((by["live@host"]["alive"], by["live@host"]["pid"]), (True, str(os.getpid())))
        out = render([machine_rec("m")] + recs).stdout
        self.assertIn("stale", out)
        self.assertIn("held", out)

    def test_the_broker_is_read_from_the_process_table_not_its_status_file(self):
        brdir = self.tmp / "state" / "wk" / "broker"
        (brdir / "r1").mkdir(parents=True)
        (brdir / "r2").mkdir()
        (brdir / "r1" / "status").write_text("state=running\npid=%d\nstage=building\n" % os.getpid())
        (brdir / "r2" / "status").write_text("state=running\npid=4194304\nstage=gone\n")
        rec = status.broker_record(self.store, "m", status.Local().alive)
        self.assertEqual((rec["name"], rec["state"]), ("request broker", "closed"))
        self.assertIn("./setup --stage broker", rec["fix"])
        self.assertEqual([n["text"] for n in rec["notes"]], ["in flight: r1 -- building"])
        self.assertIsNone(status.broker_record(Store(dict(self.env, XDG_STATE_HOME=str(self.tmp / "nostate"))), "m", lambda p: False))

    def test_services_are_named_and_asked_whether_they_are_stale(self):
        self.assertEqual(status.unit_program(REPO, "wk-proxy.service"), "container/proxy/wk-proxy.py")
        calls = []

        def run(argv):
            calls.append(argv)
            return status.Local().run(["false"]) if "is-active" in argv else status.Local().run(["true"])
        import shutil
        if shutil.which("systemctl"):
            recs = status.service_records(REPO, "m", run)
            self.assertEqual([r["state"] for r in recs], ["stopped", "stopped"])
            self.assertIn("systemctl --user start wk-proxy   (workspaces have no network without it)", recs[0]["fix"])


class TestTheWalkProbesAMachineOnce(unittest.TestCase):
    """One remote place in the walk: the driver object is the walk's, so its probe is paid once and capacity,
    delegation and tooling read the memo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-walk-"))
        (self.tmp / "hosts").mkdir()
        (self.tmp / "hosts" / "box.conf").write_text("kind=build\nhost=box.example\nroot=/home/u/wk\n")
        self.env = {"HOME": str(self.tmp), "XDG_STATE_HOME": str(self.tmp / "state"), "WK_STORE": str(self.tmp / "store"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_PLACE": "box", "WK_IN_VM": "1",
                    "PATH": os.environ.get("PATH", "")}
        self.fake = SshFake()
        self.fake.answer_remote("uname -s", out=LINUX_PROBE)
        self.fake.answer_remote("test -f $HOME/.wk-remote", rc=0)
        self.fake.answer_remote("tools/wk", out="sha=abc\ndirty=no\n")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def test_one_ssh_probe_for_the_whole_walk(self):
        reg = places.Registry(REPO, env=self.env, machine=self.fake)
        walk = status.Walk(REPO, fleet=True, devices=False, env=self.env, reg=reg)
        with mock.patch.object(status.Walk, "reach", return_value=("", "")):
            recs = [r for r in walk.records() if r.get("kind") not in ("plan", "flush", "exit")]
        self.assertIs(walk.driver("box"), walk.driver("box"))
        self.assertEqual(len(self.fake.ssh_calls("uname -s")), 1)
        self.assertEqual(len(self.fake.ssh_calls("test -f $HOME/.wk-remote")), 1)
        wk_calls = [c[-1] for c in self.fake.ssh_calls("tools/wk ")]
        self.assertEqual([c.split("tools/wk ", 1)[1].split(" 2>&1")[0].rstrip("'") for c in wk_calls],
                         ["status --no-fleet --records", "doctor --probe-tools", "key fingerprints"])
        self.assertEqual(len(self.fake.ssh_calls()), 5)
        cap = [r for r in recs if r["kind"] == "capacity"]
        self.assertEqual(len(cap), 1)
        self.assertEqual((cap[0]["machine"], cap[0]["cores"], cap[0]["free_mb"], cap[0]["load"]), ("box", "8", "20000", "0"))
        self.assertNotIn("mem_mb", cap[0])
        self.assertIn("box", {r["name"] for r in recs if r["kind"] == "machine"})


class TestSdkDecision(unittest.TestCase):
    """current / behind / unknown from the local pull and the registry's tag list."""

    LOCAL = {"image": "ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000", "created": "2026-08-01"}

    def test_a_registry_that_never_answers_is_unknown_with_the_ceiling(self):
        rec = status.sdk_record("m", self.LOCAL, None, 1)
        self.assertEqual((rec["tag"], rec["pulled"], rec["unknown"]), ("2.53-v9-abc0000", "2026-08-01", "registry did not answer within 1s"))
        self.assertNotIn("upstream", rec)

    def test_the_registry_echoing_the_local_tag_is_current(self):
        rec = status.sdk_record("m", self.LOCAL, ["2.53-v9-abc0000"], 4)
        self.assertEqual(rec["upstream"], rec["tag"])
        self.assertNotIn("unknown", rec)

    def test_a_newer_tag_upstream_is_behind(self):
        rec = status.sdk_record("m", self.LOCAL, ["2.53-v9-abc0000", "2.53-v11-def0000", "24.04_arm32"], 4)
        self.assertEqual(rec["upstream"], "2.53-v11-def0000")

    def test_a_tag_outside_the_scheme_is_current_only_when_listed(self):
        local = {"image": "ghcr.io/igalia/wkdev-sdk:24.04_arm32", "created": "2026-08-01"}
        self.assertEqual(status.sdk_record("m", local, ["24.04_arm32"], 4)["upstream"], "24.04_arm32")
        self.assertEqual(status.sdk_record("m", local, ["latest"], 4)["unknown"], "registry has no tag matching 24.04_arm32")
        self.assertIsNone(status.sdk_record("m", {}, [], 4))


class TestBenchTaskLine(unittest.TestCase):
    def _records(self):
        return [machine_rec("moose", host_self=True),
                {"kind": "bench", "machine": "moose", "task": "20260830T120000Z-wpe-pr1725", "path": "/store/ws/w/bench/20260830T120000Z-wpe-pr1725",
                 "state": "running", "summary": "3/10 runs ended, 3 ok, 0 failed, 1 round usable; now speedometer2.1 rpi3 pr1725",
                 "subject": "A/B wpe:1725: afa2ed9e70 vs base 04abe09851 · rpi3 · speedometer2.1 · 5 rounds"},
                {"kind": "bench", "machine": "moose", "task": "20260830T130000Z-rpi4-base-vs-pr1725", "path": "/store/ws/w/bench/20260830T130000Z-rpi4-base-vs-pr1725",
                 "state": "incomplete", "summary": "2/6 runs ended, 1 ok, 1 failed, 0 rounds usable",
                 "subject": "base vs pr1725 · rpi4 · speedometer2.1 · 3 rounds"}]

    def test_text_names_every_task_with_state_and_summary(self):
        out = render(self._records(), "text").stdout
        for text in ("20260830T120000Z-wpe-pr1725", "running", "now speedometer2.1 rpi3 pr1725", "20260830T130000Z-rpi4-base-vs-pr1725",
                     "incomplete", "/store/ws/w/bench/20260830T120000Z-wpe-pr1725"):
            self.assertIn(text, out)

    def test_json_carries_both_records(self):
        found = json.dumps(json.loads(render(self._records(), "json").stdout)["machines"])
        self.assertIn("20260830T120000Z-wpe-pr1725", found)
        self.assertIn("20260830T130000Z-rpi4-base-vs-pr1725", found)


class TestRendersPartial(unittest.TestCase):
    """The renderer draws what it has: an empty health block is silent, a row without its extra fields still appears, a machine that did not answer reads unreachable."""

    def test_a_workspace_with_only_a_name_and_state_is_a_row(self):
        out = render([machine_rec("m"), {"kind": "workspace", "machine": "m", "method": "container", "name": "bare", "state": "running", "ws": "present"}]).stdout
        self.assertRegex(out, r"(?m)^\s+bare\s+running\s+-\s+\?\s+clean")

    def test_an_unreachable_machine_is_a_line_not_an_empty_place(self):
        out = render([machine_rec("box"), {"kind": "raw", "machine": "box", "text": "box: unreachable over ssh: Connection refused"}]).stdout
        self.assertIn("box: unreachable over ssh: Connection refused", out)
        self.assertNotIn("no workspaces on it", out)

    def test_a_machine_with_nothing_says_so_once(self):
        out = render([machine_rec("empty")]).stdout
        self.assertEqual(out.count("(no workspaces on it)"), 1)
        self.assertNotIn("disk", out)
        self.assertNotIn("load", out)

    def test_json_and_html_are_the_one_merged_document(self):
        recs = [machine_rec("m"), {"kind": "workspace", "machine": "m", "method": "container", "name": "ws", "state": "running", "ws": "present"},
                {"kind": "exit", "code": 2}]
        doc = json.loads(render(recs, "json").stdout)
        self.assertEqual(doc["exit"], 2)
        page = Path(render(recs, "html").stdout.strip()).read_text()
        self.assertIn(json.dumps(doc), page)

    def test_an_unreadable_record_is_reported_and_the_rest_still_render(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs = list(statusview.records_from_lines(['{"kind":"machine","name":"m"}', "{not json", "", '{"kind":"exit","code":1}']))
        self.assertEqual([r["kind"] for r in recs], ["machine", "exit"])
        self.assertIn("unreadable record", err.getvalue())


class TestFleetIsOne(unittest.TestCase):
    """Two views of one workspace merge to one row when they agree, and the worst state anywhere -- a
    disagreement included -- is what the exit code carries, whichever record said it."""

    def _recs(self, state_a, state_b, exit_code=0):
        return [machine_rec("box"),
                {"kind": "workspace", "machine": "box", "method": "native", "name": "ws", "state": state_a, "ws": state_a},
                {"kind": "workspace", "machine": "box", "method": "native", "name": "ws", "state": state_b, "ws": state_b},
                {"kind": "exit", "code": exit_code}]

    def test_two_views_agreeing_are_one_row(self):
        out = render(self._recs("present", "present")).stdout
        self.assertEqual(out.count(" ws "), 1)
        self.assertNotIn("disagree", out)

    def test_two_views_disagreeing_name_both_states_once_and_exit_4(self):
        out = render(self._recs("present", "absent")).stdout
        self.assertEqual(out.count(" ws "), 1)
        self.assertIn("disagree (present vs absent)", out)
        self.assertEqual(json.loads(render(self._recs("present", "absent"), "json").stdout)["exit"], 4)
        self.assertEqual(json.loads(render(self._recs("present", "present"), "json").stdout)["exit"], 0)


class TestHelpLeadsWithTheSelfLine(unittest.TestCase):
    def test_help_leads_with_role_and_mode(self):
        lines = run("help").stdout.splitlines()
        self.assertEqual(lines[0], status.self_line(str(REPO), clean_env(wk_root=False), False))

class TestTheSelfLineIsSpacedOneWay(unittest.TestCase):
    def test_the_stream_leads_with_the_self_line_and_then_its_block(self):
        recs = [{"kind": "fleet", "machine": "here", "self": True, "role": "workstation", "mode": "host"},
                {"kind": "machine", "name": "here", "self": True}, {"kind": "exit", "code": 0}]
        merger = statusview.Merger()
        for r in recs:
            merger.feed(r)
        out = io.StringIO()
        statusview.render_text_stream(iter(recs), out, False)
        lines = out.getvalue().split("\n")
        self.assertEqual(lines[0], statusview.self_line_text("here", "workstation", "host", False))
        self.assertEqual(lines[1:3], statusview.render_machine_block(merger.doc["machines"][0], False)[:2])




class TestTheServedPage(unittest.TestCase):
    def test_port_and_interval_come_from_the_flag_else_the_env_else_any_port_every_20s(self):
        cmd = load_status_cmd()
        walk = types.SimpleNamespace(records=lambda markers=True: iter(()), worst=0)
        for argv, env, want in (([], {}, ("0", "20")), ([], {"WK_STATUS_PORT": "8080", "WK_STATUS_INTERVAL": "5"}, ("8080", "5")),
                                (["--port=9", "--interval=7"], {"WK_STATUS_PORT": "8080"}, ("9", "7"))):
            with mock.patch.object(cmd, "Walk", return_value=walk), mock.patch.object(cmd.statusview, "serve", return_value=0) as serve, \
                    mock.patch.dict(os.environ, env):
                self.assertEqual(0, cmd.main(["--web"] + argv))
            self.assertEqual(serve.call_args[0][2:], want, argv)


if __name__ == "__main__":
    unittest.main()
