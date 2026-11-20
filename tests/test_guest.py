"""A macOS guest's life on a Mac: `wk start` booting and converging it, `wk stop`, the host's daemons for guests and the
credentials they serve, typed into a world of guests whose agent answers. At the bottom: the driver conformance, the live
tier, real bytes, and what no command line reaches."""
import os
import plistlib
import re
import shlex
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import Result, bench_lib
from tests.support import REPO, WkTest, guest_step, live_selected, owed, stub_path
from tests.test_doctor_wall import HEALTHY, Walled, report
from tests.test_places_flows import GuestFlow, Guests
from tests.test_shell_path import BUILD_DIR, _run_install_fn, compile_commands_dir
from tests.test_wk_places import DriverConformance
from tests.test_wk_secrets import SECRETFILE, World
from tests.test_wk_workspace import said

ADDR = "192.168.2.1"
SERVES = {"com.wk.proxy": "WK_PROXY_TCP", "com.wk.push": "WK_PUSH_SOCKET", "com.wk.github-inject": "WK_INJECT_SOCK"}
HELD = ("read-github-pat", "push-github-pat", "push-bugzilla-api-key")


class Mac(Guests):
    """Guests booted filtered, podman beside tart. The Softnet bridge has `bridge` once a guest runs; launchd keeps the
    `loaded` labels and the `serving` sockets (setup's injector among them); the push service listens. The guest agent
    answers each command (kept with its stdin in `asked`) from `guest_said` by prefix, its clock `skew` seconds behind
    this host's; a forward's far socket exists once its ssh is spawned (unless not `far_ends`); a pid in `dying` is alive for
    one more look."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        (self.tmp / "bin" / "podman").write_text("")
        (self.tmp / "bin" / "podman").chmod(0o755)
        del self.env["WK_VM_UNFILTERED"]
        self.vmdir = self.env["WK_VM_STORE"] + "/vm"
        self.loaded, self.serving, self.sockets, self.asked, self.dying = set(), {self.vmdir + "/github-inject.sock"}, set(), [], set()
        self.bridge, self.bench, self.skew, self.listens, self.last_input, self.guest_said, self.far_ends = ADDR, False, 0, True, "", {}, True
        self.answer([self.tart, "get"], out='{"CPU": 4, "Memory": 8192}')
        self.react(["ifconfig"], lambda a, f: Result(0, "\tinet 10.0.0.2 netmask 0xffffff00\n" + (
            "\tinet %s netmask 0xffffff00\n" % f.bridge if [s for n, s in f.vms.items() if n != "wk-base" and s == "running"] else "")))
        self.answer(["test", "-x"])
        self.answer(["sh", "-c", 'printf %s "$HOME"'], out=self.env["HOME"])
        self.react(["sh", "-c"], lambda a, f: World._sh(f, a, f))
        self.react(["python3", SECRETFILE, "read"], lambda a, f: Result(0, f.files.get(a[3], "")))
        self.react(["python3", SECRETFILE, "present"], lambda a, f: Result(0 if f.files.get(a[3]) else 1))
        self.react(["python3", "-c"], lambda a, f: Result(0 if a[-1] in f.serving else 1))
        self.react(["/usr/bin/python3", "-c"], lambda a, f: Result(0 if f.listens else 1))
        self.react(["bash", "-c"], lambda a, f: bench_lib(tuple(a)))
        self.react(["launchctl", "print"], lambda a, f: Result(0 if a[-1].rsplit("/", 1)[1] in f.loaded else 113))
        self.react(["launchctl", "bootout"], lambda a, f: f.loaded.discard(a[-1].rsplit("/", 1)[1]) or Result(0))
        self.react(["launchctl", "bootstrap"], lambda a, f: f._launch(plistlib.loads(f.files[a[-1]].encode()), "RunAtLoad"))
        self.react(["launchctl", "kickstart"], lambda a, f: f._launch(f.agent(a[-1].rsplit("/", 1)[1]), "Label"))
        self.react([self.tart, "exec"], self._guest)

    def _path(self, rest):
        return shlex.split(rest)[0]

    def agent(self, label):
        return plistlib.loads(self.files[self.env["HOME"] + "/Library/LaunchAgents/%s.plist" % label].encode())

    def _launch(self, doc, starts):
        self.loaded.add(doc["Label"])
        if doc[starts]:
            self.serving.add(doc["EnvironmentVariables"].get(SERVES.get(doc["Label"])))
        return Result(0)

    def run(self, argv, input=None, timeout=None, stream=False):
        self.last_input = input or ""
        return super().run(argv, input=input, timeout=timeout, stream=stream)

    def _guest(self, argv, f):
        cmd = argv[-1]
        self.asked.append((cmd, self.last_input))
        if cmd == "date -u +%s":
            return Result(0, "%d\n" % (self.clock.now() - self.skew))
        if cmd.startswith("test -S "):
            return Result(0 if cmd.split()[-1] in self.sockets else 1)
        said = [r for k, r in self.guest_said.items() if cmd.startswith(k) or k in self.last_input]
        return said[0] if said else Result(0 if self.bench or not cmd.startswith("test -f /etc/wk-image") else 1)

    def alive(self, pid):
        if pid in self.dying:
            self.dying.discard(pid)
            self.pids.discard(pid)
            return True
        return super().alive(pid)

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        if pid and "-R" in argv and self.far_ends:
            self.sockets.add(argv[argv.index("-R") + 1].split(":")[0])
        return pid

    def held_by(self, resource, pid):
        """`resource`'s lock, held by `pid`, which dies after one more look."""
        self.dying.add(pid)
        self.pids.add(pid)
        self.files[self.env["WK_LOCK_DIR"] + "/%s@here.lock" % resource] = "pid=%d tok=beef at=2026-09-24T00:00:00Z cmd=wk start" % pid
        self.links.add(self.env["WK_LOCK_DIR"] + "/%s@here.lock" % resource)

    def did(self):
        """What the start asked of tart, the guest, launchd and the push service, and what it wrote, in order: an act's run is
        its act, and a read of the guest is named as one."""
        out, last = [], None
        for e in self.work():
            if e[0] == "run" and last == ("act", e[1]):
                continue
            last = e
            a = e[1] if e[0] in ("act", "run", "spawn") else ()
            if e[0] in ("act", "run") and a[:2] == (self.tart, "exec"):
                said = "write " + shlex.split(a[-1])[-1] if a[-1].startswith("sh -c 'set -e") else a[-1].split("\n")[0][:48]
                out.append(("guest " if e[0] == "act" else "guest? ") + said)
            elif e[0] == "act" and a[0] == "/usr/bin/python3":
                out.append("push service: listen " + a[-1])
            elif e[0] == "act" and a[0] == "launchctl":
                out.append("launchctl %s %s" % (a[1], os.path.basename(a[-1])))
            elif e[0] == "act":
                out.append(self.rel(" ".join(a[:3] if a[0] != self.tart else a[1:])))
            elif e[0] == "spawn":
                out.append("boot " + a[-1] if a[0] == "env" else "ssh -R " + self.rel(a[a.index("-R") + 1]))
            elif e[0] in ("write", "kill"):
                out.append("%s %s" % (e[0], self.rel(e[1])))
        return out

    def pidfile(self, what):
        return "%s/ws.%s-forward.pid" % (self.vmdir, what)


class GuestStart(GuestFlow):
    world_class = Mac

    def setUp(self):
        super().setUp()
        self.w.make("ws")

    def start(self, *argv, cmd="start"):
        rc, _, err = self.typed(cmd, "ws", *argv)
        return rc, err

    def stopped(self, **env):
        self.w.vms["wk-ws"] = "stopped"
        self.w.env.update(env)
        return self.start()

    def booted(self):
        return [e[1] for e in self.w.effects if e[0] == "spawn" and e[1][0] == "env"]

    def asked(self, word):
        return [c for c, _ in self.w.asked if word in c]

    def launched(self, label):
        return [d for d in self.w.did() if d.startswith("launchctl") and label in d]

    def lines(self, err, start):
        return [l for l in err.splitlines() if l.startswith(start)]


STEPS = ["guest? test -f /etc/wk-image", "guest? write /Users/admin/.wk-workspace", "guest bash -s /Users/admin/wk-tools",
         "guest sh -c 'cat > \"$HOME/.lldbinit\"'", "guest sh -c 'mkdir -p \"$HOME/.config/helix\" && cp -R \"",
         "guest? date -u +%s", "guest env WK_ADDR=192.168.2.1 WK_PORT=3128 bash -s",
         "guest sh -c 'umask 077 && mkdir -p \"$HOME/$1\" && cat >", "guest git config --global core.sshCommand /Users/admin",
         "guest git config --global ssh.variant ssh", "guest sh -c 'rm -f \"$HOME/$1\"' sh .wk-agent-token clau",
         "guest sh -c 'rm -f \"$HOME/$1\"' sh .wk-litellm-key lite", "guest env WK_SRC=/Users/admin/WebKit WK_MIRROR=/Volume",
         "guest bash -lc 'export WK_AGENTS_CACHE=$HOME/.cache/wk", "guest bash -lc 'export WK_AGENTS_CACHE=$HOME/.cache/wk",
         "guest bash -lc 'python3 /Users/admin/wk-tools/claude/w", "guest mkdir -p /Users/admin/.claude",
         "guest ln -sfn /Users/admin/wk-tools/claude/settings.js",
         "write /home/Library/LaunchAgents/com.wk.push.plist", "launchctl bootstrap com.wk.push.plist", "push service: listen ws",
         "guest rm -f /Users/admin/.wk-push.sock", "ssh -R /Users/admin/.wk-push.sock:/vmstore/vm/ws.push.sock",
         "write /vmstore/vm/ws.push-forward.pid", "guest? test -S /Users/admin/.wk-push.sock",
         "guest rm -f /Users/admin/.wk-broker.sock", "ssh -R /Users/admin/.wk-broker.sock:/state/wk/broker.sock",
         "write /vmstore/vm/ws.broker-forward.pid", "guest? test -S /Users/admin/.wk-broker.sock",
         "guest bash -s", "guest? bash -s"]
PROXY = ["chmod 0700 /home/.config/wk/claude-login", "write /home/Library/LaunchAgents/com.wk.proxy.plist",
         "launchctl bootstrap com.wk.proxy.plist", "launchctl kickstart com.wk.proxy"]
ALIAS = ["chmod 0700 /home/.ssh/config.d", "write /home/.ssh/config.d/wk"]


class TestAStart(GuestStart):
    def test_a_running_guest_has_its_proxy_started_then_each_step_converged_in_order_and_is_not_booted(self):
        rc, err = self.start()
        self.assertEqual((rc, said(err), self.w.did()), (0, "  zed ssh://wk-ws/Users/admin/WebKit", PROXY + STEPS + ALIAS))
        self.assertEqual((self.lines(err, "==> ws is up"), err.count("the guest's own window logs in as admin / admin")),
                         (["==> ws is up at 192.168.64.9 (ssh alias wk-ws)"], 1))

    def test_a_stopped_guest_is_booted_filtered_with_the_mirror_alone_then_its_agent_is_asked_before_any_step(self):
        rc, err = self.stopped()
        (run,) = self.booted()
        self.assertEqual((rc, run[3:], run[1].split(":")[0]), (0, ("run", "--net-softnet", "--net-softnet-block=0.0.0.0/0", "--net-softnet-allow=%s/32" % ADDR,
                                                                   "--dir=mirror:%s/state/wk/git:ro,tag=wk-mirror" % self.w.tmp, "wk-ws"), "PATH=/usr/local/bin"))
        self.assertEqual(self.w.did()[:7], ["boot wk-ws"] + PROXY + ["guest? true", STEPS[0]])
        self.assertNotIn(self.w.vmdir + "/ws.unfiltered", self.w.files)

    def test_an_unfiltered_guest_is_marked_and_gets_no_softnet_proxy_nor_egress_address(self):
        rc, err = self.stopped(WK_VM_UNFILTERED="1")
        self.assertEqual((rc, self.lines(err, "warning: WK_VM"), self.booted()[0][4], self.w.did()[:3]),
                         (0, ["warning: WK_VM_UNFILTERED=1 -- this guest gets the open network, with no egress filter"],
                          "--dir=mirror:%s/state/wk/git:ro,tag=wk-mirror" % self.w.tmp, ["write /vmstore/vm/ws.unfiltered", "boot wk-ws", "guest? true"]))
        self.assertEqual(self.asked("WK_ADDR"), ["env WK_ADDR= WK_PORT=3128 bash -s"])

    def test_a_host_running_as_many_vms_as_it_permits_refuses_the_boot(self):
        self.w.make("mac")
        rc, err = self.stopped(WK_VM_MAX="1")
        self.assertEqual((rc, said(err), self.booted()), (1, "error: 1 VM(s) are already running on this host:", []))

    def test_a_guest_that_does_not_fit_beside_the_running_ones_is_refused_unless_shared(self):
        self.w.make("mac")
        self.w.answer([self.w.tart, "get", "wk-mac"], out='{"CPU": 4, "Memory": 20480}')
        rc, err = self.stopped()
        self.assertEqual((rc, said(err), self.booted()), (1, "error: not enough memory to start 'wk-ws'.", []))
        rc, err = self.stopped(WK_VM_SHARE="1")
        self.assertEqual((rc, self.lines(err, "warning: 'wk-ws'"), len(self.booted())), (0, [
            "warning: 'wk-ws' (8192MB) on top of 0MB podman + 20480MB of running guests exceeds the 20480MB envelope -- continuing because WK_VM_SHARE is set"], 1))

    def test_a_host_short_of_disk_is_refused_naming_the_disk_a_guest_believes_it_has(self):
        self.w.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 10485760 1% /\n")
        rc, err = self.stopped(WK_VM_DISK_GB="100")
        self.assertEqual((rc, said(err), err.splitlines()[1], self.booted()),
                         (1, "error: only 10 GB free on the host.", "    A macOS guest believes it has a 100 GB disk, but every byte it writes has to come", []))

    def test_no_softnet_refuses_before_tart_runs(self):
        self.w.answer(["test", "-x"], rc=1)
        rc, err = self.stopped()
        self.assertEqual((rc, said(err), self.booted()), (1, "error: softnet is not installed, so this guest's egress would not be filtered.", []))

    def test_a_guest_that_never_comes_up_names_its_empty_run_log(self):
        self.w.answer([self.w.tart, "ip"], rc=1)
        rc, err = self.stopped()
        self.assertEqual((rc, said(err), self.w.rel(err.splitlines()[-1])),
                         (1, "error: wk-ws did not come up within 180s. Its run log says:", "    nothing -- /vmstore/vm/ws.run.log is empty"))

    def test_a_failed_step_is_named_and_the_start_goes_on(self):
        self.w.guest_said["bash -s /Users/admin/wk-tools"] = Result(1)
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: could not wire"), self.w.did()[-len(ALIAS) - 1]),
                         (0, ["warning: could not wire ws's shell; 'wk' will not be on PATH in there"], "guest? bash -s"))

    def test_a_desktop_something_covers_refuses_the_start_and_wk_vm_force_hands_it_over(self):
        self.w.guest_said["printf 'securityagent="] = Result(0, "securityagent=up\nconsole_user=admin\nscreenlock=off\n")
        rc, err = self.start()
        lines = err.splitlines()
        self.assertEqual((rc, said(err), lines[lines.index(said(err)) + 1], self.w.did()[-1]),
                         (1, "error: 'ws' is not usable: something is in front of its desktop.", "      an authentication sheet (SecurityAgent) is up", "guest? bash -s"))
        self.w.env["WK_VM_FORCE"] = "1"
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: WK_VM_FORCE")), (0, ["warning: WK_VM_FORCE=1 -- 'ws' is handed over with this in front of its desktop:"]))

    def test_an_unknown_guest_is_refused_by_name_and_tart_is_asked_nothing_else(self):
        rc, _, err = self.typed("start", "nope")
        self.assertEqual((rc, err.splitlines()[0], [e for e in self.w.effects if e[0] != "run"]), (2, "warning: no such workspace: nope -- 'wk ls' lists them", []))

    def test_a_dry_run_names_every_step_and_changes_nothing(self):
        before = (dict(self.w.files), set(self.w.loaded), set(self.w.serving))
        rc, err = self.start("--dry-run")
        self.assertIn("would run on wk-ws: env WK_ADDR=%s WK_PORT=3128 bash -s" % ADDR, err.splitlines())
        self.assertEqual((rc, (self.w.files, self.w.loaded, self.w.serving)), (0, before))

    def test_a_dry_run_of_a_stopped_guest_ends_at_its_boot(self):
        self.w.vms["wk-ws"] = "stopped"
        before = dict(self.w.files)
        rc, err = self.start("--dry-run")
        self.assertEqual((rc, [r[-1] for r in self.booted()], self.w.asked, self.w.files), (0, ["wk-ws"], [], before))
        self.assertEqual([e for e in self.w.effects if e[0] == "run" and e[1][1:2] == ("ip",)], [])


class TestOneLockPerGuest(GuestStart):
    def test_start_and_stop_wait_for_the_guests_lock_and_ask_nothing_under_anothers(self):
        for cmd in ("start", "stop"):
            self.w.held_by("guest-ws", 4242)
            rc, err = self.start(cmd=cmd)
            taken = next(i for i, e in enumerate(self.w.effects) if e[0] == "rename" and "guest-ws" in e[2])
            first = next(i for i, e in enumerate(self.w.effects) if e[0] in ("run", "act") and e[1][:2] == (self.w.tart, "exec"))
            self.assertEqual((rc, err.splitlines()[0], taken < first), (0, "==> waiting for the guest-ws lock (held by pid 4242)", True), cmd)

    def test_a_dead_holder_is_no_holder(self):
        self.w.files[self.w.env["WK_LOCK_DIR"] + "/guest-ws@here.lock"] = "pid=4243 tok=beef at=2026-09-24T00:00:00Z cmd=wk start"
        self.w.links.add(self.w.env["WK_LOCK_DIR"] + "/guest-ws@here.lock")
        rc, err = self.start()
        self.assertEqual((rc, [l for l in err.splitlines() if "waiting" in l]), (0, []))


class TestTheSteps(GuestStart):
    def test_a_workspace_guest_is_given_its_marker_and_a_bench_image_keeps_none(self):
        self.start()
        (marker,) = [i for c, i in self.w.asked if c.endswith(" sh /Users/admin/.wk-workspace")]
        self.assertIn("name=ws\nplace=vm\nsrc=/Users/admin/WebKit\n", marker)
        self.w.bench, self.w.asked = True, []
        self.start()
        self.assertEqual(self.asked(".wk-workspace"), ["rm -rf /Users/admin/.wk-workspace"])

    def test_the_agents_are_installed_as_every_driver_installs_them(self):
        self.start()
        script = (REPO / "container" / "agents.sh").read_text()
        got = [shlex.split(c) for c in self.asked("WK_AGENTS_CACHE")]
        self.assertEqual([(a[:2], a[2].split("\n", 1)[1], a[3:]) for a in got], [(["bash", "-lc"], script, ["container/agents.sh", "fetch"]),
                                                                                (["bash", "-lc"], script, [])])
        self.assertTrue(got[0][2].startswith("export WK_AGENTS_CACHE=$HOME/.cache/wk/agents WK_CLAUDE_VERSION="))
        self.assertEqual(self.asked("workspace-config"), ["bash -lc 'python3 /Users/admin/wk-tools/claude/workspace-config.py /Users/admin/WebKit'"])

    def test_a_container_and_a_macos_guest_each_get_their_own_helix_languages(self):
        """`unit helix.macos_languages`: firstrun gives a container glibc's --malloc-trim and jsc-release's build; a guest, neither."""
        with tempfile.TemporaryDirectory() as home:
            _run_install_fn("_install_helix", "x86_64", home, curl="exit 0\n", sudo="exit 0\n", tar='mkdir -p "$3"\n')
            linux = Path(home, ".config/helix/languages.toml").read_text()
        self.start()
        (mac,) = [i for c, i in self.w.asked if 'cat > "$HOME/.config/helix/languages.toml"' in c]
        args = [tomllib.loads(t)["language-server"]["clangd"]["args"] for t in (linux, mac)]
        self.assertEqual([(True, "--compile-commands-dir=/src/WebKit/" + BUILD_DIR), (False, "--compile-commands-dir=/Users/admin/WebKit")],
                         [("--malloc-trim" in a, compile_commands_dir(a)) for a in args])

    def test_a_credential_file_its_reader_refuses_is_not_delivered(self):
        self.w.answer(["python3", SECRETFILE, "read"], rc=2, err="wk: refusing to read ...\n")
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: could not write the agent"), self.asked(".wk-litellm-key")),
                         (0, ["warning: could not write the agent credentials into ws; an agent in there will ask you to log in"], []))

    def test_the_push_forward_listens_for_this_guest_over_the_transport_and_ends_at_the_socket_a_workspace_dials(self):
        self.start()
        (fwd,) = [e[1] for e in self.w.effects if e[0] == "spawn" and "/Users/admin/.wk-push.sock:%s/ws.push.sock" % self.w.vmdir in e[1]]
        self.assertIn("ProxyCommand=%s/container/ssh-transport vm ws" % REPO, fwd)
        self.assertEqual((self.launched("com.wk.push"), [i for _, i in self.w.asked if "KEY:" in i]), (["launchctl bootstrap com.wk.push.plist"], []))

    def test_no_push_forward_is_made_when_the_service_would_not_listen(self):
        self.w.listens = False
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: could not reach the push"), [d for d in self.w.did() if "push.sock" in d]),
                         (0, ["warning: could not reach the push service from ws; a push from in there is refused"], []))


class TestTheClock(GuestStart):
    def set(self, skew, **env):
        self.w.skew = skew
        self.w.env.update(env)
        rc, err = self.start()
        return rc, self.lines(err, "==> ws's clock"), [c.rsplit(" ", 1)[0] for c in self.asked("sudo -n date")]

    def test_a_guest_behind_or_ahead_of_this_host_is_set_from_it(self):
        for skew in (11 * 86400, -3600):
            rc, said_, sudo = self.set(skew)
            self.assertEqual((rc, re.sub(r"\d+s", "Ns", said_[0]), sudo), (0, "==> ws's clock was Ns out; set from this host", ["sudo -n date -u"]), skew)
            self.assertAlmostEqual(int(re.search(r"(\d+)s", said_[0]).group(1)), abs(skew), delta=2)
            self.w.asked = []

    def test_a_guest_in_time_is_left_alone(self):
        self.assertEqual(self.set(0), (0, [], []))

    def test_a_guest_that_refuses_the_write_is_named(self):
        self.w.guest_said["sudo -n date"] = Result(1)
        self.w.skew = 86400
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: could not set")), (0, ["warning: could not set ws's clock; TLS in there will fail as CERT_NOT_YET_VALID"]))

    def test_wk_vm_clock_skew_sets_the_tolerance(self):
        self.assertEqual(self.set(300, WK_VM_CLOCK_SKEW="600")[2], [])
        self.assertEqual(self.set(300, WK_VM_CLOCK_SKEW="")[2], ["sudo -n date -u"])


class TestTheProxy(GuestStart):
    def proxy(self):
        return self.w.agent("com.wk.proxy")["EnvironmentVariables"]

    def test_it_is_started_on_demand_once_the_bridge_has_its_address_and_left_alone_while_it_serves(self):
        self.start()
        env, home = self.proxy(), self.w.env["HOME"]
        self.assertEqual((env["WK_PROXY_TCP"], env["WK_INJECT_SOCK"], env["WK_INJECT_READ_SOCK"], env["WK_PUSH_SOCKET"]),
                         ("%s:3128" % ADDR, self.w.vmdir + "/github-inject.sock", self.w.vmdir + "/github-inject-read.sock", self.w.env["XDG_STATE_HOME"] + "/wk/push.sock"))
        self.assertEqual((self.w.agent("com.wk.proxy")["RunAtLoad"], self.launched("inject")), (False, []))
        self.start()
        self.assertEqual(self.launched("com.wk.proxy"), [])

    def test_its_address_is_read_off_the_live_interface_on_the_guest_subnet_else_it_is_the_subnets_dot_1(self):
        self.w.env["WK_VM_SUBNET"] = "10.0.0"
        self.start()
        self.assertEqual(self.proxy()["WK_PROXY_TCP"], "10.0.0.2:3128")
        self.w.bridge = "172.16.9.1"
        self.stopped(WK_VM_SUBNET="172.16.9")
        self.assertEqual(self.booted()[0][6], "--net-softnet-allow=172.16.9.1/32")

    def test_each_override_reaches_what_it_names(self):
        self.w.bridge = "203.0.113.9"
        rc, err = self.stopped(WK_VM_PROXY_ADDR="203.0.113.9", WK_VM_PROXY_PORT="9999", WK_SOFTNET_BIN="/opt/softnet")
        run = self.booted()[0]
        self.assertEqual((rc, run[1].split(":")[0], run[6], self.proxy()["WK_PROXY_TCP"], self.asked("WK_ADDR")),
                         (0, "PATH=/opt", "--net-softnet-allow=203.0.113.9/32", "203.0.113.9:9999", ["env WK_ADDR=203.0.113.9 WK_PORT=9999 bash -s"]))
        self.assertIn(("run", ("test", "-x", "/opt/softnet")), self.w.effects)

    def test_an_injector_that_does_not_answer_is_named_with_its_setup_stage(self):
        self.w.serving.clear()
        rc, err = self.start()
        self.assertEqual((rc, self.lines(err, "warning: the credential injector"), err.splitlines()[1]),
                         (0, ["warning: the credential injector is not running on this Mac, so 'git-webkit pr' and Claude in a guest get no"],
                          "  credential:  ./setup --stage inject"))

    def test_the_push_service_is_kept_by_launchd_and_ensured_once(self):
        self.start()
        self.start()
        env = self.w.agent("com.wk.push")["EnvironmentVariables"]
        self.assertEqual((self.launched("com.wk.push"), env["WK_PUSH_GUEST_DIR"]), ([], self.w.vmdir))

    def test_a_command_waiting_for_a_running_guest_starts_a_dead_proxy_only(self):
        self.start()
        for case, state, serving, starts in (("dead proxy", "running", False, 1), ("live proxy", "running", True, 0), ("stopped guest", "stopped", False, 0)):
            self.w.vms["wk-ws"] = state
            self.w.serving.discard("%s:3128" % ADDR) if not serving else self.w.serving.add("%s:3128" % ADDR)
            self.typed("enter", "ws", "--", "true")
            self.assertEqual(self.launched("kickstart com.wk.proxy"), ["launchctl kickstart com.wk.proxy"] * starts, case)


class TestTheForwards(GuestStart):
    def forwards(self):
        return [d for d in self.w.did() if d.startswith("ssh -R")]

    def test_a_second_start_while_they_are_alive_forwards_nothing_again(self):
        self.start()
        self.start()
        self.assertEqual((self.forwards(), int(self.w.files[self.w.pidfile("push")]) in self.w.pids), ([], True))

    def test_a_pidfile_whose_pid_is_gone_is_started_again(self):
        self.w.files[self.w.pidfile("push")] = "4194304\n"
        self.start()
        self.assertEqual(self.forwards(), ["ssh -R /Users/admin/.wk-push.sock:/vmstore/vm/ws.push.sock", "ssh -R /Users/admin/.wk-broker.sock:/state/wk/broker.sock"])

    def test_start_and_stop_take_a_forwards_lock(self):
        for cmd in ("start", "stop"):
            self.w.held_by("vm-push-forward-ws", 4242)
            rc, err = self.start(cmd=cmd)
            self.assertEqual((rc, self.lines(err, "==> waiting")), (0, ["==> waiting for the vm-push-forward-ws lock (held by pid 4242)"]), cmd)


class TestAStop(GuestStart):
    def test_ends_every_forward_then_syncs_the_guests_disk_before_tart_cuts_its_power(self):
        self.start()
        pids = [int(self.w.files[self.w.pidfile(what)]) for what in ("push", "broker")]
        rc, err = self.start(cmd="stop")
        self.assertEqual((rc, said(err), self.w.did()), (0, "==> stopped ws", ["kill %d" % pids[0], "kill %d" % pids[1], "guest sync", "stop wk-ws"]))
        self.assertEqual([self.w.pidfile(what) in self.w.files for what in ("push", "broker")], [False, False])

    def test_a_guest_that_is_not_running_is_said_so_and_tart_is_asked_nothing(self):
        self.w.vms["wk-ws"] = "stopped"
        rc, err = self.start(cmd="stop")
        self.assertEqual((rc, said(err), self.w.did()), (0, "==> ws is not running", []))


class TestTheGuestsCredentials(GuestStart):
    def held(self, pat="ghp-held", bz="bz-held"):
        held = self.w.env["HOME"] + "/.config/wk/push-keys/"
        for name, value in (("github-pat", pat), ("bugzilla-api-key", bz)):
            self.w.files.pop(held + name, None) if value is None else self.w.files.__setitem__(held + name, value + "\nsecond line\n")
        rc, err = self.start()
        return rc, err, [self.w.files.get(self.w.vmdir + "/" + f) for f in HELD]

    def test_each_start_writes_every_file_the_injector_reads_private_to_this_user(self):
        rc, _, got = self.held()
        self.assertEqual((rc, got), (0, ["ghp-held\n", "ghp-held\n", "bz-held\n"]))
        self.assertEqual(len([e for e in self.w.effects if e[0] == "act" and e[1][:2] == ("sh", "-c") and e[1][2].startswith("umask 077 && cat > ")]), 3)

    def test_one_withdrawn_here_is_withdrawn_there_at_the_next_start(self):
        self.held()
        self.assertEqual(self.held(pat=None, bz=None)[2], [None, None, None])

    def test_a_start_that_finds_the_proxy_up_still_delivers_todays_token(self):
        self.held(pat="ghp-yesterdays")
        got = self.held(pat="ghp-todays")[2][0]
        self.assertEqual((self.launched("proxy"), got), ([], "ghp-todays\n"))

    def test_a_far_side_that_refuses_is_named(self):
        self.w.react(["sh", "-c"], lambda a, f: Result(1))
        rc, err, _ = self.held()
        self.assertEqual(self.w.rel(self.lines(err, "warning: could not converge")[0]),
                         "warning: could not converge the tokens in /vmstore/vm; a read from a guest answers 401")


class WalledMac(Walled, Mac):
    """A Mac whose guest answers the wall's probes from HEALTHY (tests/test_doctor_wall.py)."""


class TestTheWall(GuestStart):
    world_class = WalledMac

    def test_a_guests_rows_name_each_daemons_remedy(self):
        self.w.files.pop(self.w.vmdir + "/ws.unfiltered")
        self.w.said = {**dict(HEALTHY), "https://github.com/ 2": "000", "https://api.github.com/ 2": "000", "rest/version": "000"}
        self.w.env["WK_NAME"] = "ws"
        rc, _, err = self.typed("doctor", "ws")
        self.assertEqual((rc, [r for r in report(err) if "'000'" in r]), (3, [
            "-- github unreachable through the proxy (got '000') -> wk start ws",
            "-- api.github.com answered '000' -- the injector is not in the path -> ./setup --stage inject (launchd keeps the injector)",
            "-- bugs.webkit.org answered '000' -- the injector is not in the path for it -> ./setup --stage inject (launchd keeps the injector)"]))


class TestTheWaits(GuestStart):
    """Each wait of a start gives up after the seconds it declares, on the world's clock."""

    def test_a_guest_whose_agent_never_answers_is_refused_after_two_minutes_of_asking(self):
        self.w.guest_said["true"] = Result(255)
        rc, err = self.stopped()
        self.assertEqual((rc, said(err), self.w.did(), sum(self.w.clock.slept)),
                         (1, "error: wk-ws is up at 192.168.64.9 but its tart guest agent never answered. Its run log says:",
                          ["boot wk-ws"] + PROXY + ["guest? true"] * 61, 120))

    def test_a_bridge_that_never_gets_its_address_starts_no_proxy_after_fifteen_seconds(self):
        self.w.bridge = "10.9.9.9"
        rc, err = self.start()
        push = STEPS.index("write /home/Library/LaunchAgents/com.wk.push.plist")
        self.assertEqual((rc, self.lines(err, "warning: the guest bridge"), self.w.effects.count(("run", ("ifconfig",))), self.w.did(), sum(self.w.clock.slept)),
                         (0, ["warning: the guest bridge never got address 192.168.2.1; not starting the proxy"], 32,
                          STEPS[:push] + PROXY[:1] + STEPS[push:] + ALIAS, 15))

    def test_a_forward_whose_guest_end_never_serves_is_ended_after_four_seconds(self):
        self.w.far_ends = False
        rc, err = self.start()
        far = {"push": "/Users/admin/.wk-push.sock", "broker": "/Users/admin/.wk-broker.sock"}
        self.assertEqual((rc, [self.w.rel(l) for l in self.lines(err, "warning: the push forward")], [d for d in self.w.did() if d.startswith(("ssh -R", "guest? test -S", "kill"))]),
                         (0, ["warning: the push forward into 'ws' did not come up; see /vmstore/vm/ws.push-forward.log"],
                          [d for pid, what, near in ((1001, "push", "/vmstore/vm/ws.push.sock"), (1002, "broker", "/state/wk/broker.sock"))
                           for d in ["ssh -R %s:%s" % (far[what], near)] + ["guest? test -S " + far[what]] * 21 + ["kill %d" % pid]]))
        self.assertEqual(([self.w.pidfile(what) in self.w.files for what in far], round(sum(self.w.clock.slept), 6)), ([False, False], 8))


# The driver conformance, the live tier and what no command line reaches name lib modules.
from wk import daemons, doctor, guest, places, wall  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.store import GUEST_BROKER_SOCKET, Store  # noqa: E402


class TestVmConformance(GuestStart, DriverConformance):
    cls, down, platform = places.Vm, "stopped", "macos"

    def stopped(self):
        self.w.vms["wk-ws"] = "stopped"
        return self.w.driver

    def brought_up(self, t):
        return self.w.vms["wk-ws"] == "running"


def _a_running_guest():
    """(vm place, name) of a guest up on this host, or None; nothing is started to find one."""
    if not live_selected() or sys.platform != "darwin":
        return None
    try:
        vm = places.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"]
    except (LookupError, Refused):
        return None
    return (vm, up[0]) if up else None


class _LiveGuest(unittest.TestCase):
    """Read-only against a guest already running on this host; it starts nothing."""
    wk_tier = "live"

    def setUp(self):
        self.found = _a_running_guest()
        if self.found is None:
            self.skipTest("live tier not selected, or no macOS guest is running on this host")


class TestTheLiveGuest(_LiveGuest):
    def ask(self, script):
        vm, ws = self.found
        return vm.exec(ws, ["bash", "-lc", script], timeout=60)

    def test_vm_egress(self):
        """`live vm.egress[<check>]`: nothing reaches out bypassing the proxy, PyPI does through it, and every
        rc file sources the egress block."""
        vm, ws = self.found
        if not vm.egress_filtered(ws):
            self.skipTest("'%s' was booted with WK_VM_UNFILTERED" % ws)
        checks = {"direct": ("curl -s -o /dev/null --max-time 5 --noproxy '*' https://pypi.org/simple/", False),
                  "pypi": ("curl -sf -o /dev/null --max-time 30 https://pypi.org/simple/pip/", True),
                  "zprofile": ("grep -qF .wk-egress \"$HOME/.zprofile\"", True)}
        for check, (script, reaches) in checks.items():
            with self.subTest(check=check):
                r = self.ask(script)
                self.assertEqual(reaches, r.ok, r.out + r.err)

    @owed("live inject.claude_login[vm]: the CLI accepting the placeholder and the host injector's swap are measured only "
          "against the real CLI and api.anthropic.com")
    def test_inject_claude_login(self):
        """`live inject.claude_login[vm]`: the guest holds the placeholder and no token, and a session answers through the
        host's injector."""
        vm, ws = self.found
        rows = wall.Wall(str(REPO), vm, ws, vm.machine).claude_login()
        self.assertEqual([], [r for r in rows if r[0] != doctor.OK], rows)
        r = self.ask("claude -p 'Reply with the single word OK.'")
        self.assertIn("OK", r.out, r.err)

    def test_vm_shared_mirror(self):
        """`live vm.shared_mirror`: the checkout is --shared off the mirror share, and its alternates resolve."""
        vm, ws = self.found
        r = self.ask("cat %s/.git/objects/info/alternates && git -C %s cat-file -e HEAD"
                     % (vm.src(ws), vm.src(ws)))
        self.assertTrue(r.ok, r.out + r.err)
        self.assertIn(vm.mirror_dir() + "/objects", r.out)


class TestTheLiveRemount(_LiveGuest):
    def test_sync_guest_remount(self):
        """`live sync.guest_remount`: after the remount the guest reads the host mirror's main as the host does."""
        vm, ws = self.found
        host = vm.machine.run(["git", "-C", vm.store.mirror_dir(), "rev-parse", "refs/heads/main"])
        self.assertEqual(vm.remount_mirror(ws), "")
        r = vm.exec(ws, ["git", "-C", vm.mirror_dir(), "rev-parse", "refs/heads/main"], timeout=60)
        self.assertEqual((r.ok, r.out.strip()), (True, host.out.strip()), r.err)


class TestTheLiveWayIn(_LiveGuest):
    """`tart exec` as the one way into a real guest, and what rides it; it writes only under /tmp in there."""

    def test_vm_tart_exec(self):
        """`live vm.tart_exec`: a command runs as the guest's user with its home, its status comes back, a binary file
        crosses both ways byte for byte, and a detached job outlives the exec that started it."""
        vm, ws = self.found
        g = vm.guest(ws)
        r = g.run(["sh", "-c", 'echo "$(id -un) $HOME"; exit 3'])
        self.assertEqual((r.rc, r.out.strip()), (3, "%s %s" % (vm.user(), vm.home())), r.err)
        with tempfile.TemporaryDirectory() as d:
            data = bytes(range(256)) * 64
            with open(os.path.join(d, "in"), "wb") as f:
                f.write(data)
            g.copy_in(os.path.join(d, "in"), "/tmp/wk-live-copy")
            g.copy_out("/tmp/wk-live-copy", os.path.join(d, "out"))
            with open(os.path.join(d, "out"), "rb") as f:
                self.assertEqual(f.read(), data)
        pid = g.spawn(["sleep", "30"], "/tmp/wk-live-spawn.log")
        self.assertTrue(g.alive(pid), "a detached job died with its tart exec")
        g.kill(pid)

    def test_vm_mirror_tag_mount(self):
        """`live vm.mirror_tag_mount`: the base's mirror job mounted the mirror's own tag at boot, where mirror_dir is."""
        vm, ws = self.found
        r = vm.exec(ws, ["sh", "-c", "mount | grep -F %s && test -d %s" % (shlex.quote(" on %s (" % places.GUEST_MIRROR_MOUNT),
                                                                        shlex.quote(vm.mirror_dir()))], timeout=60)
        self.assertTrue(r.ok, r.out + r.err)

    def test_vm_guest_jobs(self):
        """`live vm.guest_jobs[<job>]`: the three jobs the base renders for itself (daemons.guest_rows) are loaded in a
        running guest, nosleep and mirror in launchd's system domain and display in the user's, running what the row says."""
        vm, ws = self.found
        for word, row in daemons.guest_rows(vm.home(), guest.display(vm.env)).items():
            with self.subTest(job=word):
                sudo, domain = ("sudo -n ", "system") if row.domain == "system" else ("", 'gui/$(id -u)')
                r = vm.exec(ws, ["sh", "-c", "%slaunchctl print %s/com.wk.%s" % (sudo, domain, word)], timeout=60)
                self.assertTrue(r.ok, r.out + r.err)
                self.assertIn(" ".join(row.argv), " ".join(r.out.split()))

    def test_vm_ssh_transport(self):
        """`live vm.ssh_transport`: ssh over the guest's sshd on tart exec answers, as the editor's alias and the
        socket forwards use it."""
        vm, ws = self.found
        r = vm.machine.run(vm.ssh_argv(ws) + ["id", "-un"], timeout=60)
        self.assertEqual((r.rc, r.out.strip()), (0, vm.user()), r.err)

    def test_vm_broker_forward(self):
        """`live vm.broker_forward`: `wk sync`'s broker client in a guest reaches the host's broker."""
        vm, ws = self.found
        r = vm.exec(ws, ["env", "WK_BROKER_SOCKET=%s/%s" % (vm.home(), GUEST_BROKER_SOCKET), "python3",
                         vm.tools(ws) + "/container/broker/wk-broker-client.py", "capabilities"], timeout=60)
        self.assertTrue(r.ok, r.out + r.err)


# `tart`: one running guest called wk-demo, and the guest itself as a directory. `tart exec` hands the guest's
# command as the last argument for a login shell, and everything it writes it writes under $HOME, so running that command with HOME
# pointed at a scratch directory exercises the real umask, mkdir, redirect and rm -- not a transcript of them.
# /Users/admin is rewritten to that directory for the same reason. `tart list` mixes local VMs with cached OCI images, so
# Source matters. Every invocation is appended to $WK_TEST_LOG.
FAKE_TART = '''
case "$1" in
list) echo '[{"Name":"wk-demo","State":"running","Source":"local"}]' ;;
ip)   echo 1.2.3.4 ;;
exec)
    printf '%s\\n' "$*" >> "$WK_TEST_LOG"
    for a in "$@"; do last="$a"; done
    cmd=$(printf '%s' "$last" | sed "s|/Users/admin|$WK_TEST_GUEST|g")
    HOME="$WK_TEST_GUEST" SHELL=$(command -v bash) bash -lc "$cmd"; rc=$?
    [ -t 0 ] || cat >/dev/null
    exit $rc ;;
*)    exit 1 ;;
esac
'''


def _guest(tmp, name="demo"):
    """A scratch guest home, and the ready marker without which Vm.info says `creating`."""
    home = tmp / "guest-home"
    home.mkdir(parents=True, exist_ok=True)
    vmstore = tmp / "vmstore"
    ws = vmstore / "ws" / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".wk-ready").write_text("")
    return home, vmstore


def _stub_env(binp, home, log, store, vmstore, **extra):
    return {"PATH": f"{binp}:{os.environ['PATH']}", "WK_TEST_GUEST": str(home), "WK_TEST_LOG": str(log),
            "WK_STORE": str(store), "WK_HOST_SECRETS": str(store / "secrets"), "WK_VM_STORE": str(vmstore),
            "WK_VM_PROXY_ADDR": "192.0.2.1", **extra}


class TestTheGuestGetsTheInjectorsCa(WkTest):
    def _egress(self, home, vmstore, ca_text=None, bugzilla=None, **extra):
        log = self.tmp / "guest.log"
        log.write_text("")
        vmdir = vmstore / "vm"
        vmdir.mkdir(parents=True, exist_ok=True)
        if ca_text is not None:
            (vmdir / "wk-github-ca.pem").write_text(ca_text)
        with stub_path({"tart": FAKE_TART}) as binp:
            env = _stub_env(binp, home, log, self.tmp / "store", vmstore, XDG_STATE_HOME=str(self.tmp / "state"), **extra)
            return guest_step(env, "set_guest_egress", secrets={"bugzilla_user": lambda s: bugzilla})

    def test_the_bugzilla_placeholder_goes_in_with_the_login_and_not_without(self):
        home, vmstore = _guest(self.tmp)
        ca = "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
        self._egress(home, vmstore, ca_text=ca)
        self.assertNotIn("BUGS_WEBKIT_ORG", (home / ".wk-egress").read_text())
        self._egress(home, vmstore, ca_text=ca, bugzilla="me@example.test")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("export BUGS_WEBKIT_ORG_USERNAME=me@example.test\n", rc)
        self.assertIn("export BUGS_WEBKIT_ORG_PASSWORD=wk-injects-this\n", rc)

    def test_the_ca_and_the_placeholder_go_in_with_the_proxy(self):
        home, vmstore = _guest(self.tmp)
        self._egress(home, vmstore, ca_text="-----BEGIN CERTIFICATE-----\nx\n"
                                            "-----END CERTIFICATE-----\n")
        rc = (home / ".wk-egress").read_text()
        self.assertIn("http_proxy=http://192.0.2.1:3128", rc)
        self.assertIn("GITHUB_COM_TOKEN=wk-injects-this", rc)
        self.assertIn("GITHUB_COM_USERNAME=justinmichaud", rc)
        self.assertIn("PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring", rc)
        self.assertIn("REQUESTS_CA_BUNDLE=", rc)
        self.assertIn("CURL_CA_BUNDLE=", rc)
        self.assertIn("GIT_SSL_CAINFO=", rc)
        self.assertIn("BEGIN CERTIFICATE", (home / ".wk-github-ca.pem").read_text())

    def test_an_unfiltered_guest_gets_neither(self):
        home, vmstore = _guest(self.tmp)
        (home / ".wk-github-ca.pem").write_text("stale\n")
        self._egress(home, vmstore, ca_text="x\n", WK_VM_UNFILTERED="1")
        self.assertFalse((home / ".wk-github-ca.pem").exists())
        self.assertFalse((home / ".wk-ca-bundle.pem").exists())


class TestNotReachableFromTheCli(GuestStart):
    def test_an_absent_guest_is_refused_by_the_start_itself(self):
        """Registry.present refuses a workspace whose guest tart does not list before guest.start reads tart."""
        vm = self.w.driver
        del self.w.vms["wk-ws"]
        self.assertEqual(said(self.refused(lambda: guest.start(vm, "ws"))), "error: no such workspace: ws -- 'wk ls' lists them")

    def test_the_injectors_row_names_its_files_the_login_it_holds_and_the_machine_it_publishes_into(self):
        """./setup --stage inject renders this row (python3 -m wk.daemons here)."""
        host = guest.Host(self.w.driver)
        row, login = host.daemons()["github-inject"], host.secrets.cred_path("claude-login")
        self.assertEqual(({k: self.w.rel(row.env[k]) for k in ("WK_INJECT_SOCK", "WK_INJECT_PLAIN_SOCK", "WK_INJECT_READ_SOCK", "WK_INJECT_CLAUDE_LOGIN")},
                          row.env["WK_INJECT_PUBLISH_MACHINE"], row.lock, row.boot),
                         ({"WK_INJECT_SOCK": "/vmstore/vm/github-inject.sock", "WK_INJECT_PLAIN_SOCK": "/vmstore/vm/claude-inject.sock",
                           "WK_INJECT_READ_SOCK": "/vmstore/vm/github-inject-read.sock", "WK_INJECT_CLAUDE_LOGIN": self.w.rel(login)},
                          host.vm.store.podman_machine(), login, True))

    def test_where_there_are_no_guests_nothing_is_converged(self):
        """`wk key set` reaches guest.credentials_converge only where secrets reads this host as macOS."""
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            self.assertTrue(guest.credentials_converge(str(REPO), self.w.env, self.w))
        self.assertEqual([self.w.files.get(self.w.vmdir + "/" + f) for f in HELD], [None] * 3)
