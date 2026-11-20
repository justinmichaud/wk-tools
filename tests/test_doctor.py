"""`wk doctor` typed into worlds: a host ./setup provisioned, Linux or macOS, read section by section as the rows a person
reads, each fault turned in it the one miss naming its fix; `wk doctor <machine>` and `--all` asking the fleet; and
`--probe-store`. What no world reaches -- the fleet probe of a bench machine -- is at the bottom."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import fakes
from tests.fakes import FakeRegistry, Result
from tests.support import NO_REGISTRY, REPO, SYSTEM_DIRS
from tests.test_doctor_wall import report
from tests.test_wk_places import LINUX_PROBE
from tests.test_wk_workspace import Flow, said

LINUX = os.uname_result(("Linux", "here", "6.8.0", "", "x86_64"))
MAC = os.uname_result(("Darwin", "here", "25.0.0", "", "arm64"))
NAME, EMAIL = "A Person", "a@example.com"
SDK = "ghcr.io/igalia/wkdev-sdk"
SECRETFILE = str(REPO / "lib" / "secretfile.py")
HAVE = ("sh", "-c", 'command -v "$1" >/dev/null', "sh")
TOOLS = ("podman", "zsh", "cage", "wlr-randr", "nmap", "jq", "git-lfs", "gh", "git", "systemctl", "zed", "tailscale")
BOOT = "/usr/local/libexec/wk-boot-priv"
HELPERS = ("/usr/local/libexec/wk-quiesce-priv", "/usr/local/libexec/wk-card-priv", BOOT)
UNITS = {"wk-proxy.service": "container/proxy/wk-proxy.py", "wk-github-inject.service": "container/proxy/github-inject.py",
         "wk-push.service": "container/push/wk-push.py"}
BROKER, INJECT = "/run/user/1000/wk/broker.sock", "/vmstore/vm/github-inject.sock"
IMAGE = "ostree-remote-image:fedora:docker://quay.io/podman/machine-os:5.8"
VM_PROBE = "mirror=ok\nbase=ok\nproxy=ok\npihosts=ok\nbroker=ok\ngit.name=%s\ngit.email=%s\ngit.fsmonitor=true\ngit.manyfiles=true\n" % (NAME, EMAIL)
PEERS = {"Peer": {"a": {"DNSName": "pi-rescue.tail.ts.net.", "TailscaleIPs": ["100.1.1.1"], "Online": True},
                  "b": {"DNSName": "pi-bench.tail.ts.net.", "TailscaleIPs": ["100.1.1.2"], "Online": False},
                  "c": {"DNSName": "box.tail.ts.net.", "TailscaleIPs": ["100.1.1.3"], "Online": True}}}
REFUSED = "ssh: connect to host box.example port 22: Connection refused\n"


class Host(fakes.World):
    """A Linux host ./setup provisioned: every tool, link, helper, daemon and store fact present, each one an attribute a
    test turns. Its PATH is an empty directory, so what wk runs on this host itself rather than through the world finds no
    program. `on_mac()` makes it a Mac whose podman machine is `vm`, answering inside from `in_vm`."""

    def __init__(self, tmp):
        super().__init__(tmp, XDG_RUNTIME_DIR="/run/user/1000", WK_MIRROR_BRANCHES="main")
        self.home, self.store = self.env["HOME"], self.env["WK_STORE"]
        self.env.update(WK_MACHINES_DIR=str(self.tmp / "machines"), PATH=str(self.tmp / "bin"), XDG_CONFIG_HOME=self.home + "/.config")
        for d in ("machines", "bin"):
            os.makedirs(str(self.tmp / d))
        self.tools, self.gh_auth, self.sudo = set(TOOLS) - {"zed", "tailscale"}, True, Result(0, "wk's helpers run without a password\n")
        self.linked = {self.home + "/.claude/settings.json": str(REPO / "claude" / "settings-host.json"),
                       self.home + "/.claude/CLAUDE.md": str(REPO / "claude" / "CLAUDE-host.md")}
        self.git = {"user.name": NAME, "user.email": EMAIL, "core.fsmonitor": "true", "feature.manyFiles": "true"}
        self.heads, self.sockets, self.active = {"main"}, {BROKER}, set(UNITS)
        for path in (self.home + "/.zshrc", self.p("base/b1/sha"), self.p("pi-hosts"), self.p("push-keys/build_key_fork")):
            self.write_own(path, "source ~/wk-tools/shell/bashrc\n")
        self.dirs.add(self.p("git/WebKit.git"))
        for i, prog in enumerate(UNITS.values()):
            self.write_own(str(REPO / prog), "")
            self.write_own("/proc/%d" % (41 + i), "")
            self.mtimes["/proc/%d" % (41 + i)] = 2.0
        self.sdk, self.tags = "2.53-v11-b", ["2.53-v9-a", "2.53-v11-b", "2.52-v12-c"]
        self.executable, self.listing = set(HELPERS), "".join("    (root) NOPASSWD: %s\n" % h for h in HELPERS)
        self.groups, self.peers, self.box_up, self.marker = "u render", PEERS, True, None
        self.vm, self.in_vm = None, {}
        for prefix, fn in ((HAVE, lambda a, f: Result(0 if a[-1] in f.tools else 1)),
                           (["gh", "api", "user"], lambda a, f: Result(0 if f.gh_auth else 1)),
                           (["python3", SECRETFILE], lambda a, f: Result(0, f.files.get(a[3], "")) if a[2] == "read" else Result(0 if a[3] in f.files else 1)),
                           (["python3", "-c"], lambda a, f: Result(0 if f.rel(a[-1]) in f.sockets else 1)),
                           ([str(REPO / "wk"), "key", "sudo", "status", "--quiet"], lambda a, f: f.sudo),
                           (["readlink"], lambda a, f: Result(0, f.linked.get(a[1], "") + "\n")),
                           (["git", "config", "--get"], lambda a, f: Result(0 if f.git.get(a[3]) else 1, f.git.get(a[3], "") + "\n")),
                           (["git", "config", "--file", str(REPO / "dotfiles" / "gitconfig"), "--get"],
                            lambda a, f: Result(0, {"user.name": NAME, "user.email": EMAIL}[a[-1]] + "\n")),
                           (["git", "-C", self.p("git/WebKit.git"), "rev-parse"], lambda a, f: Result(0 if a[-1][11:] in f.heads else 1)),
                           (["systemctl", "--user", "cat"], lambda a, f: Result(0 if a[-1] in UNITS else 1)),
                           (["systemctl", "--user", "is-active"], lambda a, f: Result(0 if a[-1] in f.active else 3)),
                           (["systemctl", "--user", "show"], lambda a, f: Result(0, "%d\n" % (41 + list(UNITS).index(a[-1])) if a[-1] in f.active else "0\n")),
                           (["podman"], self._podman),
                           (["podman", "machine", "inspect", "wk"], lambda a, f: Result(0, json.dumps([{"Name": "wk", "State": f.vm}])) if f.vm
                            else Result(125, "", "Error: wk: VM does not exist")),
                           (["podman", "machine", "ssh", "wk", "--"], self._in_vm),
                           (["test", "-x"], lambda a, f: Result(0 if a[2] in f.executable else 1)),
                           (["sudo", "-n", "-l"], lambda a, f: Result(0, f.listing)),
                           (["id", "-nG"], lambda a, f: Result(0, f.groups + "\n")),
                           (["tailscale", "status", "--json"], lambda a, f: Result(0, json.dumps(f.peers))),
                           (["ssh"], self._ssh)):
            self.react(prefix, fn)
        self.reg = FakeRegistry(self.env, self)

    def p(self, rel):
        return os.path.join(self.store, rel)

    def on_mac(self):
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        self.tools = (self.tools | {"zed", "tailscale"}) - {"systemctl"}
        self.sockets.add(INJECT)
        self.answer(["xcode-select", "-p"], out="/Library/Developer/CommandLineTools\n")
        self.write_own(self.home + "/.config/wk/push-keys/build_key_fork", "key\n")
        self.vm, self.in_vm = "running", {"rpm-ostree": IMAGE, "--probe-store": VM_PROBE, "tailscale ip": "100.64.0.1"}

    def tart(self, guests):
        """tart on PATH, its guests {name: (state, what `git config` answers in it)}."""
        bin = self.tmp / "bin" / "tart"
        bin.write_text("")
        bin.chmod(0o755)
        t = os.path.realpath(str(bin))
        for n in guests:
            self.write_own(self.env["WK_VM_STORE"] + "/ws/%s/.wk-ready" % n, "")
        self.react([t, "list"], lambda a, f: Result(0, json.dumps([{"Name": "wk-" + n, "State": s, "Source": "local"} for n, (s, _) in guests.items()])))
        self.react([t, "exec"], lambda a, f: Result(0, guests[a[3][3:]][1]) if guests[a[3][3:]][1] else Result(1))

    def _podman(self, a, f):
        a = a[3:] if a[1:3] == ["-c", "wk"] else a[1:]
        if a[0] == "images":
            return Result(0, "%s:%s\n" % (SDK, f.sdk))
        if a[:2] == ["image", "inspect"]:
            return Result(0, "2026-11-01 10:00:00\n")
        if a[0] == "search":
            return Result(0, "NAME TAG\n" + "".join("%s %s\n" % (SDK, t) for t in f.tags)) if f.tags is not None else Result(125, "", "timed out")
        return Result(125, "", "podman: no answer for %s" % a)

    def _in_vm(self, a, f):
        out = next((v for k, v in f.in_vm.items() if k in a[-1]), None)
        return Result(1, "", "no answer in the podman machine") if out is None else Result(0, out + "\n")

    def _ssh(self, a, f):
        if not f.box_up:
            return Result(255, "", REFUSED)
        if ".wk-remote" in a[-1]:
            return Result(0, f.marker) if f.marker else Result(1)
        return Result(0, "family=debian\n") if a[-1].endswith("'bash -s'") else Result(0, LINUX_PROBE)

    def conf(self, name, text):
        (self.tmp / "machines" / (name + ".conf")).write_text(text)


def sections(err):
    """Each section's rows as a person reads them (tests.test_doctor_wall.report), by the section's title."""
    out, title = {}, None
    for line in err.splitlines():
        if line and line[0] != " " and not line.startswith(("==>", "warning:")) and "Warning: " not in line:
            title = line
            out[title] = []
        elif title:
            out[title] += report(line)
    return out


def misses(secs):
    return [r for rows in secs.values() for r in rows if r.startswith("--")]


class DoctorTest(Flow):
    """`wk doctor` on a Linux host (`mac`: a Mac)."""
    world_class = Host
    mac = False

    def setUp(self):
        p = mock.patch("os.uname", return_value=MAC if self.mac else LINUX)
        p.start()
        self.addCleanup(p.stop)
        super().setUp()
        if self.mac:
            self.w.on_mac()

    def doctor(self, *argv, w=None):
        """(exit status, rows by section, what the person reads last)."""
        rc, err = self.typed("doctor", *argv, w=w)
        return rc, sections((w or self.w).rel(err)), said(err)

    def section(self, title, *argv):
        return self.doctor(*argv)[1][title]

    def asked(self, *head):
        return [e[1] for e in self.w.effects if e[0] == "run" and e[1][:len(head)] == head]


HOST_TOOLS = ["ok podman", "ok zsh", "ok cage (benchmark kiosk)", "ok wlr-randr (session off)", "ok nmap (wk machine probe)",
              "ok jq (claude hook)", "ok git-lfs", "ok gh", "ok gh authenticated"]
CREDENTIALS = ["?? %s: nothing stored -- wk key set %s -> wk key setup" % (n, n)
               for n in ("github-pat", "bugzilla-api-key", "claude", "claude-login", "litellm", "tailnet", "tailnet-api")]
STORE = ["ok fork push key", "ok WebKit mirror", "ok snapshot", "ok egress proxy running", "ok pi-hosts populated",
         "ok fleet-request broker reachable from workspaces", "ok SDK image 2.53-v11-b, pulled 2026-11-01, is upstream's newest"]
QUIESCE = "./setup --stage quiesce  (interactive sudo)"


def dropped(*paths):
    return lambda w: [w._drop(w.p(p)) for p in paths]


# A fault turned in the provisioned host, and the one row that names it.
FAULTS = (
    *((lambda w, t=t: w.tools.discard(t), "-- %s -> ./setup --stage tools" % what)
      for t, what in (("podman", "podman"), ("zsh", "zsh"), ("cage", "cage (benchmark kiosk)"), ("wlr-randr", "wlr-randr (session off)"),
                      ("jq", "jq (claude hook)"), ("git-lfs", "git-lfs"))),
    (lambda w: w.tools.discard("gh"), "-- gh -> install gh, then: gh auth login"),
    (lambda w: setattr(w, "gh_auth", False), "-- gh authenticated -> gh auth login   (then: wk key deploy)"),
    (lambda w: setattr(w, "sudo", Result(1, "a password is required, but sudo keeps a timestamp\n")),
     "-- sudo: a password is required, but sudo keeps a timestamp -> wk key sudo setup"),
    (lambda w: w.linked.pop(w.home + "/.claude/settings.json"), "-- ~/.claude/settings.json is the HOST settings -> ./setup --stage claude"),
    (lambda w: w._drop(w.home + "/.zshrc"), "-- shell rc sources shell/bashrc -> ./setup --stage dotfiles"),
    (lambda w: w.git.update({"user.name": ""}), "-- this machine: git user.name is not set there -> ./setup --stage dotfiles"),
    (lambda w: w.git.update({"user.email": "other@example.com"}),
     "-- this machine: git user.email there is 'other@example.com', not this repo's 'a@example.com' -> ./setup --stage dotfiles"),
    (lambda w: w.git.update({"core.fsmonitor": ""}), "-- this machine: git speed settings (fsmonitor, manyFiles) -> ./setup --stage dotfiles"),
    (dropped("push-keys/build_key_fork"), "-- fork push key -> wk key deploy  (needs gh auth)"),
    (dropped("git/WebKit.git"), "-- WebKit mirror -> wk sync"),
    (lambda w: w.env.update(WK_MIRROR_BRANCHES="main webkitglib/2.52"), "-- WebKit mirror carries no webkitglib/2.52 -> wk sync --mirror"),
    (dropped("base"), "-- snapshot -> wk sync"),
    (dropped("pi-hosts"), "-- pi-hosts populated -> wk machine setup rpi5 / rpi4  (skip if no Pi devices)"),
    (lambda w: w.sockets.clear(), "-- fleet-request broker reachable from workspaces -> ./setup --stage broker  (macOS: it also publishes the socket into the VM)"),
    (lambda w: w.tags.append("2.53-v12-c"), "-- SDK image 2.53-v11-b is behind upstream's 2.53-v12-c -> a newer SDK_PIN (lib/wk/project.json): the fork's wk branch on upstream's 2.53-v12-c"),
    (lambda w: w.mtimes.update({str(REPO / UNITS["wk-push.service"]): 3.0}), "-- push service running -> ./setup   (restarts it on the program in the tree now)"),
    (lambda w: w.executable.discard(BOOT), "-- wk-boot-priv (wk boot (arming the firmware, restarting a machine)) -> " + QUIESCE),
    (lambda w: setattr(w, "listing", w.listing.replace(BOOT, BOOT + "-old")),
     "-- wk-boot-priv: installed, and sudo -n still asks for a password -> /etc/sudoers.d/zzz-wk-boot has to be the last match "
     "'sudo -l' shows, naming %s exactly -- ./setup --stage quiesce" % BOOT),
    (lambda w: setattr(w, "groups", "u"), "-- render group configured -> ./setup --stage tools, then log out and in"),
    (lambda w: w.write_own(w.env["XDG_STATE_HOME"] + "/wk/quiesce/raiser.pid", "1\n"), "-- this machine is quiesced (raiser) -> wk quiesce off"),
)


class TestAProvisionedLinuxHost(DoctorTest):
    def test_every_check_passes_and_the_exit_is_0(self):
        rc, secs, last = self.doctor()
        self.assertEqual((rc, last, misses(secs)), (0, "==> everything checkable is provisioned", []))
        self.assertEqual((secs["host tools"], secs["credentials"], secs["config (./setup owns these)"][3:], secs["workspaces store"], secs["daemons"]), (
            HOST_TOOLS, CREDENTIALS, ["ok this machine: git user.name = A Person", "ok this machine: git user.email = a@example.com",
                                      "ok this machine: git speed settings (fsmonitor, manyFiles)"],
            STORE, ["ok egress proxy running", "ok credential injector running", "ok push service running"]))

    def test_each_fault_is_the_one_miss_naming_its_fix_and_the_exit_is_1(self):
        for turn, row in FAULTS:
            with self.subTest(row=row):
                w = self.make_world()
                turn(w)
                rc, secs, _ = self.doctor(w=w)
                self.assertEqual((rc, misses(secs)), (1, [row]))

    def test_a_stopped_proxy_is_missing_in_the_store_and_as_a_daemon(self):
        self.w.active.discard("wk-proxy.service")
        rc, err = self.typed("doctor")
        self.assertIn("\nwarning: 2 item(s) missing -- each line above names its fix; the order that\n", err)
        self.assertEqual((rc, misses(sections(err))), (1, ["-- egress proxy running -> ./setup --stage sdk, then systemctl --user status wk-proxy",
                                                  "-- egress proxy stopped: workspaces have no network without it -> systemctl --user start wk-proxy"]))

    def test_what_is_unknown_is_said_and_is_not_missing(self):
        for turn, title, row in ((lambda w: w.tools.discard("nmap"), "host tools", "?? nmap absent -- only 'wk machine probe' needs it -> ./setup  (host/linux/apt.txt)"),
                                 (lambda w: w.tools.discard("git"), "config (./setup owns these)", "?? this machine: git not installed there"),
                                 (lambda w: setattr(w, "tags", None), "workspaces store", "?? SDK image 2.53-v11-b: the registry did not answer within 4s -> wk doctor, again"),
                                 (lambda w: setattr(w, "sdk", "odd"), "workspaces store", "?? SDK image odd: the registry lists no tag like it -> wk doctor, again"),
                                 (lambda w: (setattr(w, "sdk", "odd"), w.tags.append("odd")), "workspaces store", "ok SDK image odd, pulled 2026-11-01, is upstream's newest"),
                                 (lambda w: setattr(w, "sdk", "2.53-v12-d"), "workspaces store", "ok SDK image 2.53-v12-d, pulled 2026-11-01, is upstream's newest")):
            with self.subTest(row=row):
                w = self.make_world()
                turn(w)
                rc, secs, last = self.doctor(w=w)
                self.assertEqual((rc, last, secs[title][-1] if "SDK" in row else row in secs[title]),
                                 (0, "==> everything checkable is provisioned", row if "SDK" in row else True))

    def test_git_lfs_off_path_is_ok_on_either_host(self):
        for mac in (False, True):
            with self.subTest(mac=mac), mock.patch("os.uname", return_value=MAC if mac else LINUX):
                w = self.make_world()
                w.tools.discard("git-lfs")
                w.executable.add(w.home + "/.local/bin/git-lfs")
                self.assertIn("ok git-lfs", self.doctor(w=w)[1]["host tools"])

    def test_a_service_not_installed_has_no_row(self):
        self.w.answer(["systemctl", "--user", "cat", "wk-push.service"], rc=1)
        self.assertEqual(self.section("daemons"), ["ok egress proxy running", "ok credential injector running"])

    def test_no_helper_grant_is_read_from_a_listing_that_does_not_name_its_exact_path(self):
        head = "User u may run the following commands on here:\n    (ALL) ALL\n"
        for listing, mark in ((head + "    (root) NOPASSWD: " + BOOT, "ok"), (head + "    (root) NOPASSWD: /usr/local/libexec/wk-quiesce-priv", "--"),
                              ("    (ALL) ALL", "--"), ("    (root) NOPASSWD: %s-old" % BOOT, "--"), ("", "--")):
            with self.subTest(listing=listing):
                self.w.listing = listing + "\n"
                self.assertEqual(self.section("privileged helpers")[-1][:2], mark)


class TestCredentials(DoctorTest):
    """Each row is the verdict on what the store holds; the issuers' APIs are tests.support's closed port."""

    def creds(self, rel, value):
        self.w.write_own(rel.replace("~", self.w.home).replace("/store", self.w.store), value + "\n")
        rc, secs, _ = self.doctor()
        return rc, secs["credentials"], self.w.effects

    def test_a_credential_that_breaks_its_rule_is_missing_with_where_to_mint_one(self):
        rc, rows, _ = self.creds("~/.config/wk/tailscale-authkey", "tskey-api-k1-abc")
        self.assertEqual((rc, rows[5]), (1, "-- tailnet: that is an API access token, which administers the whole tailnet, not an auth key -> "
                                            "https://login.tailscale.com/admin/settings/keys -- Generate auth key: tagged tag:wk, Reusable on, "
                                            "Ephemeral OFF, longest expiry -- then: wk key set tailnet --replace"))

    def test_an_acceptable_credential_says_what_it_can_do(self):
        rc, rows, _ = self.creds("~/.config/wk/tailscale-authkey", "tskey-auth-abc")
        self.assertEqual((rc, rows[5].split(";")[0]), (0, "ok tailnet -- an auth key, so it can enroll a node and nothing else"))

    def test_one_no_issuer_answered_for_is_unknown(self):
        rc, rows, _ = self.creds("/store/push-keys/github-pat", "github_pat_11ABC_x")
        self.assertEqual((rc, rows[0].split(" (")[0], rows[0].split("); ")[-1]), (0, "?? github-pat: could not reach http://127.0.0.1:1",
                                                                     "'wk doctor' asks again -> wk key check"))

    def test_what_is_stored_is_never_printed(self):
        secret = "sk-ant-oat01-do-not-print-this"
        self.w.write_own(self.w.p("secrets/claude-token"), secret + "\n")
        _, err = self.typed("doctor")
        self.assertNotIn(secret, err)


class TestMachineLocalState(DoctorTest):
    TITLE = "machine-local state (everything a rebuild cannot get from this repo)"

    def rows(self, start):
        return [r for r in self.section(self.TITLE) if r[3:].startswith(start)]

    def test_the_machine_overlay_is_backed_up(self):
        self.assertEqual(self.rows("~/.config/wk/machines"), ["?? ~/.config/wk/machines (absent) -> backed-up: hand-written machine confs for this device only"])
        self.w.dirs.add(self.w.home + "/.config/wk/machines")
        self.assertEqual(self.rows("~/.config/wk/machines"), ["ok ~/.config/wk/machines (backed-up) -- hand-written machine confs for this device only"])

    def test_each_workspace_holding_benchmark_runs_is_backed_up(self):
        self.w.dirs.update({self.w.p("ws"), self.w.p("ws/w/bench"), self.w.p("ws/bare")})
        self.assertEqual(self.rows("/store/ws/"), ["ok /store/ws/w/bench (backed-up) -- benchmark runs: a rerun is a different measurement; "
                                                   "wk bench export <task> copies one out"])

    def test_tasks_outside_any_workspace_are_backed_up_while_any_is_there(self):
        self.w.dirs.add(self.w.p("bench"))
        self.assertEqual(self.rows("/store/bench"), [])
        self.w.write_own(self.w.p("bench/t/task.json"), "{}")
        self.assertEqual(self.rows("/store/bench"), ["ok /store/bench (backed-up) -- bench tasks outside any workspace: wk gc names each one's move"])

    def test_the_rpi5_declares_its_key_and_kernel_build_and_another_machine_neither(self):
        for ssh, want in (("here", ["?? %s/host/linux/rpi5/%s (absent) -> backed-up: " % (REPO, f) for f in ("rpi5.conf", "id_ed25519")] + ["?? ~/kbuild (absent) -> backed-up: "]),
                          ("elsewhere", [])):
            with self.subTest(ssh=ssh):
                self.w.conf("rpi5", "kind=board\nssh=%s\n" % ssh)
                self.assertEqual([r[:r.index("backed-up: ") + 11] for r in self.section(self.TITLE) if "rpi5" in r or "kbuild" in r], want)


class TestTheProbeOfTheStore(DoctorTest):
    """`wk doctor --probe-store`: the store's facts as key=value lines, for the host that asks the podman VM."""

    def probe(self):
        rc, out, _ = self.wk(self.w, "doctor", "--probe-store")
        return rc, out.splitlines()

    def test_a_provisioned_store_says_so_and_carries_this_machines_git(self):
        self.assertEqual(self.probe(), (0, ["mirror=ok", "base=ok", "proxy=ok", "pihosts=ok", "broker=ok", "git.name=A Person",
                                            "git.email=a@example.com", "git.fsmonitor=true", "git.manyfiles=true"]))

    def test_an_empty_store_and_a_machine_without_git(self):
        self.w._drop(self.w.store)
        self.w.tools.discard("git")
        self.w.active.clear()
        self.w.sockets.clear()
        self.assertEqual(self.probe(), (0, ["mirror=no", "base=no", "proxy=no", "pihosts=no", "broker=no"]))

    def test_a_declared_branch_the_mirror_lacks_is_named(self):
        self.w.env["WK_MIRROR_BRANCHES"] = "main webkitglib/2.52"
        self.assertEqual(self.probe()[1][0], "mirror=gap webkitglib/2.52")


class TestAMacHost(DoctorTest):
    """The store lives in the podman machine, which doctor asks and never starts."""
    mac = True

    def test_every_check_passes_and_the_container_machines_git_is_read_inside_it(self):
        rc, secs, last = self.doctor()
        self.assertEqual((rc, last, misses(secs), secs["host tools"][:4]), (0, "==> everything checkable is provisioned", [], [
            "ok Xcode command line tools", "ok podman", "ok zed", "ok tailscale"]))
        self.assertEqual(secs["workspaces store"][:2] + secs["workspaces store"][7:], [
            "ok fork push key", "ok podman machine 'wk' runs docker://quay.io/podman/machine-os:5.8", "ok container machine: git user.name = A Person",
            "ok container machine: git user.email = a@example.com", "ok container machine: git speed settings (fsmonitor, manyFiles)", STORE[-1]])
        self.assertEqual((secs["privileged helpers"], secs["macOS VM place (optional -- Apple ports)"]), (
            ["ok wk-quiesce-priv (wk quiesce)", "ok wk-boot-priv (wk boot (arming the firmware, restarting a machine))"],
            ["?? tart not installed -> README.md, Setup -- only needed for Apple-port builds"]))

    def guests(self, **guests):
        """The rows after tart's, softnet's and the golden base's, with `guests` in tart; and the guests asked."""
        self.w.tart(guests)
        rows = self.section("macOS VM place (optional -- Apple ports)")
        return rows[3:], sorted({a[3][3:] for a in self.asked(os.path.realpath(str(self.w.tmp / "bin" / "tart")), "exec")})

    def test_each_running_guests_git_is_read_and_a_stopped_one_is_not_asked(self):
        blob = "git.name=%s\ngit.email=%s\ngit.fsmonitor=true\ngit.manyfiles=true\n" % (NAME, EMAIL)
        self.assertEqual(self.guests(rel=("running", blob), off=("stopped", blob)), (
            ["ok rel (tart guest): git user.name = A Person", "ok rel (tart guest): git user.email = a@example.com",
             "ok rel (tart guest): git speed settings (fsmonitor, manyFiles)"], ["rel"]))

    def test_a_guest_whose_identity_is_unset_names_its_start_and_one_that_does_not_answer_is_unknown(self):
        start = " -> wk start rel (the include is written on every start)"
        self.assertEqual(self.guests(rel=("running", "git.name=\ngit.email=\ngit.fsmonitor=\ngit.manyfiles=\n"), mute=("running", "")), (
            ["-- rel (tart guest): git user.name is not set there" + start, "-- rel (tart guest): git user.email is not set there" + start,
             "-- rel (tart guest): git speed settings (fsmonitor, manyFiles)" + start, "?? mute (tart guest): git config did not answer -> wk doctor mute"],
            ["mute", "rel"]))

    def test_zed_on_path_or_as_a_bundles_cli_is_ok_and_a_bare_bundle_is_not(self):
        cli = "/Applications/Zed.app/Contents/MacOS/cli"
        for case, turn, row in (("bundle cli", lambda w: (w.tools.discard("zed"), w.executable.add(cli)), "ok zed"),
                                ("bare bundle", lambda w: w.tools.discard("zed"), "-- zed -> https://zed.dev/download")):
            with self.subTest(case=case):
                w = self.make_world()
                w.on_mac()
                turn(w)
                self.assertEqual(self.doctor(w=w)[1]["host tools"][2], row)

    def test_a_stopped_machine_is_unknown_and_nothing_is_started_or_asked_inside(self):
        self.w.vm = "stopped"
        rc, secs, _ = self.doctor()
        self.assertEqual((rc, secs["workspaces store"]), (0, ["ok fork push key", "?? podman machine 'wk' is stopped -> wk start, then re-run wk doctor for the store checks"]))
        self.assertIn("?? /store/read-github-pat (podman VM) -- not visible while the podman machine is stopped -> regenerable: ./setup and wk key set "
                      "write it from the push keys", secs[TestMachineLocalState.TITLE])
        self.assertEqual({a[:3] for a in self.asked("podman", "machine")}, {("podman", "machine", "inspect")})

    def test_no_machine_is_missing_with_the_stage_that_makes_one(self):
        self.w.vm = None
        rc, secs, _ = self.doctor()
        self.assertEqual((rc, misses(secs)), (1, ["-- podman machine 'wk' -> ./setup --stage machine"]))

    def test_a_machine_on_another_image_is_missing_with_its_remake(self):
        self.w.in_vm["rpm-ostree"] = "ostree-remote-image:fedora:docker://quay.io/podman/machine-os:5.6"
        self.assertEqual(misses(self.doctor()[1]), ["-- podman machine 'wk' runs docker://quay.io/podman/machine-os:5.8 -> 'wk rm' its workspaces, "
                                                    "then:  podman machine rm wk && ./setup --stage machine"])

    def test_the_container_machines_own_git_is_missing_with_the_include_that_sets_it(self):
        self.w.in_vm["--probe-store"] = VM_PROBE.replace("git.name=" + NAME, "git.name=")
        self.assertEqual(misses(self.doctor()[1]), ["-- container machine: git user.name is not set there -> podman machine ssh wk -- "
                                                    "git config --global include.path /opt/wk-tools/dotfiles/gitconfig"])

    def test_a_machine_whose_tooling_answers_nothing_is_unknown(self):
        del self.w.in_vm["--probe-store"]
        self.assertEqual(self.section("workspaces store")[-1], "?? store inside the VM -> /opt/wk-tools missing in the VM? run ./setup --stage sdk")

    def test_a_daemon_launchd_keeps_that_answers_no_connect_is_missing_with_its_stage(self):
        self.w.sockets.clear()
        self.assertEqual(self.section("daemons"), ["-- credential injector answers on this Mac -> ./setup --stage inject",
                                                   "-- request broker answers on this Mac -> ./setup --stage broker"])


class TestTheFleet(DoctorTest):
    """`wk doctor <machine>` and `wk doctor --all`: a build box `box`, a board `pi` and this machine, a board `here`."""

    def setUp(self):
        super().setUp()
        self.w.conf("box", "kind=build\nhost=box.example\nroot=/r\n")
        self.w.conf("pi", "kind=board\nssh=pi-rescue\nbench_ssh=pi-bench\n")
        self.w.conf("here", "kind=board\n")

    def test_without_all_no_machine_is_asked_for_its_provisioning_or_probed(self):
        rc, secs, _ = self.doctor()
        self.assertEqual(([t for t in secs if "machine:" in t], [a for a in self.asked("ssh") if "bash -s" in a[-1] or ".wk-remote" in a[-1]]), ([], []))

    def test_with_all_every_build_machine_and_every_bench_machine_but_this_one_is_asked(self):
        rc, secs, _ = self.doctor("--all")
        self.assertEqual([t for t in secs if "machine:" in t], ["build machine: box", "bench machine: pi"])

    def test_a_build_machine_that_does_not_answer_is_unknown_never_missing(self):
        self.w.box_up = False
        rc, secs, _ = self.doctor("--all")
        self.assertEqual((rc, secs["build machine: box"]), (1, ["?? box did not answer -> ssh box true  -- then re-run; nothing was changed"]))

    def test_a_build_machine_is_judged_by_the_inputs_its_provisioning_recorded(self):
        inputs = subprocess.run([sys.executable, str(REPO / "lib" / "treehash.py"), str(REPO / "remote")], capture_output=True, text=True).stdout[:16]
        for marker, row in ((None, "-- provisioning on box predates its inputs: no ~/.wk-remote there, so nothing has provisioned it -> wk machine setup box"),
                            ("place=box\n", "-- provisioning on box predates its inputs: provisioned before this record existed -> wk machine setup box"),
                            ("inputs=0123456789abcdef\n", "-- provisioning on box predates its inputs: remote/provision.sh or remote/deps.sh has "
                                                          "changed since it ran -> wk machine setup box"),
                            ("inputs=%s\n" % inputs, "ok provisioned from this tree's remote/provision.sh + remote/deps.sh")):
            with self.subTest(marker=marker):
                self.w.marker = marker
                self.assertEqual(self.doctor("--all")[1]["build machine: box"], [row])

    def test_a_build_machine_is_asked_whether_it_answers(self):
        self.assertEqual(self.doctor("box")[:2], (0, {}))
        self.w.box_up = False
        rc, err = self.typed("doctor", "box")
        self.assertEqual((rc, report(err)), (1, ["ok box on the tailnet at 100.1.1.3", "-- box does not answer: connect to host box.example port 22: "
                                                 "Connection refused -> wk machine probe box"]))

    def test_a_boards_tailnet_names_are_asked_and_a_name_the_tailnet_lacks_is_unknown(self):
        rc, err = self.typed("doctor", "pi")
        self.assertEqual((rc, report(err)[:2]), (1, ["ok pi-rescue on the tailnet at 100.1.1.1", "-- pi-bench on the tailnet at 100.1.1.2 -> "
                                                     "power it on, or: wk machine probe pi"]))
        self.w.peers = {}
        rc, err = self.typed("doctor", "pi")
        self.assertEqual(report(err)[0], "?? pi-rescue is not a tailnet node -> wk machine probe pi")

    def test_a_board_whose_fleet_probe_fails_is_unknown_and_asked_nothing_further(self):
        rc, err = self.typed("doctor", "pi")
        self.assertEqual((rc, report(err)[2][:39], report(err)[2][-23:], self.asked("ssh")),
                         (1, "?? pi: the fleet probe failed: [Errno 2", " -> wk machine probe pi", []))


class TestTheGitLfsFilter(unittest.TestCase):
    def test_the_shared_gitconfig_runs_the_filter_and_a_place_without_git_lfs_still_adds(self):
        tmp = tempfile.mkdtemp(prefix="wk-test-lfs-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "gitconfig"), "w") as f:
            f.write("[include]\n\tpath = %s\n" % (REPO / "dotfiles" / "gitconfig"))
        bare = os.path.join(tmp, "bin")
        os.mkdir(bare)
        os.symlink(shutil.which("git", path=os.pathsep.join(SYSTEM_DIRS)), os.path.join(bare, "git"))   # a workspace's PATH leads with a wrapper that is no git
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.path.join(tmp, "gitconfig"), GIT_CONFIG_NOSYSTEM="1", PATH=bare + ":/bin")
        git = lambda *a: subprocess.run(["git", "-C", tmp + "/r"] + list(a), env=env, capture_output=True, text=True)
        subprocess.run(["git", "init", "-q", tmp + "/r"], env=env, check=True)
        self.assertEqual("git-lfs clean -- %f", git("config", "filter.lfs.clean").stdout.strip())
        Path(tmp, "r", ".gitattributes").write_text("* filter=lfs\n")
        Path(tmp, "r", "a").write_text("a\n")
        self.assertEqual(0, git("add", "a").returncode)


# Not typed as `wk doctor`: a bench machine's rows past its tailnet names come from fleet_probe (lib/wk/doctor.py), which runs
# `env ... python3 -m wk.boot.cli fleet-probe` through a Local() of its own rather than the dispatcher's machine, so no world
# answers it. tests/test_notify.py and tests/test_guest.py import `doctor`, the states, `build_doctor` and `deps` from here.
sys.path.insert(0, str(REPO / "lib"))
from wk import doctor  # noqa: E402
from tests.fakes import Fake  # noqa: E402

OK, MISS, UNK = doctor.OK, doctor.MISS, doctor.UNK


def fake_doctor(macos, env=None, machine=None):
    e = dict({"HOME": "/h", "WK_STORE": "/store", "XDG_STATE_HOME": "/h/.local/state", "PATH": os.environ["PATH"],
              "WK_MACHINES_DIR": NO_REGISTRY}, **(env or {}))
    doc = doctor.Doctor(str(REPO), env=e, machine=machine or Fake(), macos=macos)
    doc.container.is_here = lambda: not macos   # the host the test names, not the one it runs on
    return doc


def build_doctor():
    d = tempfile.mkdtemp(prefix="wk-test-doctor-machines-")
    for n in ("farbox", "box", "old", "fresh"):
        with open(os.path.join(d, n + ".conf"), "w") as f:
            f.write("kind=build\ndriver=remote\n")
    return fake_doctor(False, env={"WK_MACHINES_DIR": d, "XDG_STATE_HOME": os.path.join(d, "state")})


def deps(**over):
    """lib/wk/machine_cmd/deps.py's build-machine questions, answered with nothing unless the test says otherwise."""
    return mock.patch.multiple(doctor.machine_deps, **dict(dict(probe=lambda driver, root: "", stale=lambda driver, root: None), **over))


def probed(mode="host", armed="", **over):
    f = dict(role="bench-device", probeable="yes", mode=mode, armed=armed, armed_by="me",
             armed_at="2099-01-01T00:00:00Z", armed_boot="", boot_id="", media="sd")
    f.update(over)
    return f


class TestNotReachableFromTheCli(unittest.TestCase):
    """`wk doctor <board>` past the tailnet rows: the fleet probe's fields, a board's governor and temperature, a Mac's SIP."""

    def rows(self, fields):
        d = tempfile.mkdtemp(prefix="wk-test-doctor-device-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        Path(d, "pi.conf").write_text("kind=board\nssh=pi-rescue\nbench_ssh=pi-bench\n")
        fake = Fake()
        fake.answer(["tailscale", "status", "--json"], out=json.dumps(PEERS))
        fake.answer(["ssh"], out="performance\n51234\n")
        return list(doctor.device(str(REPO), "pi", {"WK_MACHINES_DIR": d, "HOME": d, "PATH": os.environ["PATH"]}, fake, probe=lambda *a: fields))

    def test_each_row_of_a_board(self):
        stale = probed(armed="bench", armed_boot="b1", boot_id="b2")
        for fields, word, state in ((probed(), "answers in host mode", OK), (probed(), "not armed", OK),
                                    (probed(), "cpu governor on pi-rescue: performance", doctor.NOTE),
                                    (probed(), "temperature on pi-rescue: 51C", doctor.NOTE),
                                    (probed(mode="bench"), "cpu governor on pi-bench", doctor.NOTE),
                                    (probed(mode=""), "unreachable", MISS), (None, "did not answer", UNK),
                                    (probed(armed="bench"), "armed for bench", doctor.NOTE), (stale, "armed for bench", MISS),
                                    (probed(sip={"host": "enabled"}), "host install: enabled", OK), (probed(sip={}), "host install is unread", UNK),
                                    (probed(sip={"host": "disabled"}), "host install: disabled", MISS), (probed(sip={}), "bench install is unread", UNK),
                                    (probed(sip={"bench": "disabled"}), "bench install: disabled", OK), (probed(sip={"bench": "enabled"}), "bench install: enabled", MISS)):
            with self.subTest(fields=fields, word=word):
                self.assertEqual(state, next(r for r in self.rows(fields) if word in r[1])[0])

    def test_an_unreachable_board_is_asked_nothing_further(self):
        self.assertFalse(any(w in r[1] for r in self.rows(probed(mode="")) for w in ("governor", "SIP")))


if __name__ == "__main__":
    unittest.main()
