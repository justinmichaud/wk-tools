"""rr: `wk run --rr` records jsc and `wk gui --rr` MiniBrowser into the workspace's ~/wk-rr, `wk run --replay`
replays the latest recording under lldb; a non-Linux place, an Apple CPU, a missing rr and closed perf events are
refused. Typed into a world holding workspace `ws` on a Linux x86_64 host."""
import unittest
from unittest import mock

from tests.test_wk_gui import GuiTest, Result, Seat, uname

TRACE = "export _RR_TRACE_DIR=/home/u/wk-rr"
PRIV = "/usr/local/libexec/wk-quiesce-priv"


class Recorder(Seat):
    """The workspace (a container's user is `u`) answers whether rr is installed (`rr`) and perf_event_paranoid
    (`paranoid`); this host has the quiesce helper when `helper`, and its perf-on lowers paranoid to 1 when sudo lets it (`lowers`)."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.env["WK_CONTAINER_USER"] = "u"
        self.rr, self.paranoid, self.helper, self.lowers = True, "1", False, True
        self.react(["sh", "-c", 'command -v "$1" >/dev/null', "sh", PRIV], lambda a, f: Result(0 if f.helper else 1))
        self.react(["sudo", "-n", PRIV, "perf-on"], lambda a, f: (setattr(f, "paranoid", "1" if f.lowers else f.paranoid), Result(0))[1])

    def _in_ws(self, argv, f):
        if argv[-1] == "rr":
            return Result(0 if f.rr else 1)
        if argv[2:] == ["cat", "/proc/sys/kernel/perf_event_paranoid"]:
            return Result(0, f.paranoid + "\n")
        return super()._in_ws(argv, f)


class RrTest(GuiTest):
    world_class = Recorder

    def run_(self, *argv, w=None):
        return self.typed("run", "--preset", "gtk-release", *argv, w=w)

    def probes(self):
        return [e for e in self.w.effects if e[0] == "run" and e[1][-1] in ("rr", "/proc/sys/kernel/perf_event_paranoid")]


class TestRecordAndReplay(RrTest):
    def test_run_rr_records_jsc_into_the_workspace_trace_directory_without_a_tty(self):
        rc, _, cmd = self.run_("--rr", "--", "x.js")
        self.assertEqual((rc, cmd.split("; ")[1]), (0, TRACE))
        self.assertRegex(cmd, r'^export LD_LIBRARY_PATH=.*; exec rr record \S+/GTK/Release/bin/jsc --validateOptions=1 x.js$')
        self.assertIn("--no-tty", self.w.effects[-1][1])

    def test_replay_runs_the_latest_trace_under_lldb_on_a_tty_with_the_tail_for_rr(self):
        rc, _, cmd = self.typed("run", "--replay", "--", "-p", "1234")
        self.assertEqual((rc, cmd.splitlines()[-1]), (0, TRACE + '; exec rr replay -d "$LLDB" -p 1234'))
        self.assertNotIn("--no-tty", self.w.effects[-1][1])

    def test_gui_rr_prefixes_minibrowser_with_the_recorder(self):
        rc, _, cmd = self.typed("gui", "--rr", "--preset", "gtk-release")
        self.assertEqual((rc, cmd.split(" && ")[1]), (0, "WPE_BROWSER=minibrowser WEBKIT_MINI_BROWSER_PREFIX='rr record' "
                                                          "Tools/Scripts/run-minibrowser --gtk --release -- --full-screen about:blank "))
        self.assertTrue(cmd.startswith(TRACE + "; cd /src/WebKit"))

    def test_a_dry_run_asks_the_workspace_nothing_and_prints_the_recording(self):
        self.w.rr, self.w.paranoid = False, "3"
        rc, line, cmd = self.run_("--dry-run", "--rr", "--", "x.js")
        self.assertEqual((rc, cmd, self.probes()), (0, None, []))
        self.assertRegex(line, r"^would run: env .* exec rr record \S+/bin/jsc --validateOptions=1 x.js'$")


class TestRefusals(RrTest):
    def test_a_macos_target_is_refused_naming_why(self):
        with mock.patch("os.uname", uname("Darwin", "arm64")):
            w = self.world("vm")
            for argv in (["run", "--rr"], ["run", "--replay"], ["gui", "--rr"]):
                self.assertEqual(self.typed(*argv, w=w), (1, "error: rr records Linux processes only: it replays from ptrace "
                                                             "and the CPU's perf counters,", None), argv)

    def test_a_container_on_an_apple_silicon_mac_is_refused_and_one_on_an_arm_server_is_not(self):
        for host, in_vm, refused in ((("Darwin", "arm64"), "1", True), (("Linux", "aarch64"), "1", True),
                                     (("Darwin", "x86_64"), "1", False), (("Linux", "aarch64"), None, False)):
            if in_vm is None:
                self.w.env.pop("WK_IN_VM")
            with mock.patch("os.uname", uname(*host)):
                rc, line, _ = self.run_("--rr", "--", "x.js")
            self.assertEqual((rc, line.startswith("error: rr does not support Apple Silicon CPUs")), (int(refused), refused), host)

    def test_rr_missing_is_refused(self):
        self.w.rr = False
        self.assertEqual(self.run_("--rr"), (1, "error: rr is not installed in 'ws'. It is provisioning, not a per-run step:", None))

    def test_closed_perf_events_are_refused_with_the_remedy_where_it_applies(self):
        self.w.paranoid = "2"
        with mock.patch("os.uname", uname("Darwin", "x86_64")):
            rc, _, err = self.wk(self.w, "run", "ws", "--rr")
        self.assertEqual(rc, 1)
        self.assertIn("error: perf_event_paranoid is 2 in 'ws', and rr needs 1 or less.\n", err)
        self.assertIn("\n        echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid\n", err)
        rc, _, err = self.wk(self.w, "run", "ws", "--rr")
        self.assertIn("The privileged helper that can lower it is not installed on this host.\n    Remedy:  ./setup --stage quiesce", err)
        self.w.helper, self.w.lowers = True, False
        rc, _, err = self.wk(self.w, "run", "ws", "--rr")
        self.assertIn("Asking the quiesce helper ('%s perf-on') did not bring it down --" % PRIV, err)

    def test_a_linux_host_lowers_closed_perf_events_through_the_quiesce_helper_and_records(self):
        self.w.paranoid, self.w.helper = "2", True
        rc, _, cmd = self.run_("--rr", "--", "x.js")
        self.assertEqual((rc, ("act", ("sudo", "-n", PRIV, "perf-on")) in self.w.effects), (0, True))
        self.assertIn("exec rr record", cmd)

    def test_rr_runs_on_its_own(self):
        for argv, drop in ((["--rr", "--lldb"], "--rr runs on its own; drop --lldb"), (["--replay", "--until-crash"], "--replay runs on its own; drop --until-crash"),
                           (["--rr", "--replay"], "--rr runs on its own; drop --replay")):
            self.assertEqual(self.typed("run", *argv), (1, "error: " + drop, None))
        self.assertEqual(self.typed("gui", "--rr", "--lldb", "ui"), (1, "error: --rr records the browser and --lldb debugs it live; pick one", None))


if __name__ == "__main__":
    unittest.main()
