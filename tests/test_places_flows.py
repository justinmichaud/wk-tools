"""A place's driver as a person meets it: `wk new`, `wk rm`, `wk ls`, `wk start` and `wk stop` typed into a world whose
places are the real drivers over fake podman, tart and ssh."""
import hashlib
import json
import os
import shlex
import unittest
from unittest import mock

from tests import fakes
from tests.fakes import FakeRegistry, FlowTest, Result
from tests import test_wk_workspace as wsworld
from tests.test_wk_places import LINUX_PROBE

SDK = "ghcr.io/igalia/wkdev-sdk"
SEARCH = ("podman", "search", "--list-tags")


def tags(*names):
    return Result(0, "NAME\tTAG\n" + "".join("%s\t%s\n" % (SDK, t) for t in names))


class World(wsworld.World):
    """Podman also answers `ps` from the containers (each "Up 2 hours" unless `status` says otherwise), `stop`, `start`,
    `logs` (`last_words`), the SDK's published tags (`published`, None for a registry that does not answer) and whether
    the SDK layer is built (`layers`); a `podman build` makes the layer."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        self.status, self.layers, self.published, self.last_words = {}, set(), None, ""
        self.react(["podman", "ps"], lambda a, f: Result(0, "".join("%s\t%s\n" % (c, f.status.get(c, "Up 2 hours")) for c in sorted(f.containers))))
        for verb in ("stop", "start"):
            self.answer(["podman", verb])
        self.react(["podman", "logs"], lambda a, f: Result(0, f.last_words))
        self.react(list(SEARCH), lambda a, f: Result(1, "", "timed out") if f.published is None else tags(*f.published))
        self.react(["podman", "image", "exists"], lambda a, f: Result(0 if a[-1] in f.layers else 1))
        self.react(["podman", "build"], lambda a, f: (f.layers.add(a[a.index("-t") + 1]), Result(0))[1])
        self.answer(["sysctl", "-n", "hw.ncpu"], out="8\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")

    def _in_ws(self, argv, f):
        return Result(0) if "for t in" in argv[-1] else super()._in_ws(argv, f)   # every layout path asked for is there

    def tmp_path(self, rel):
        return str(self.tmp) + rel

    def asked_to(self, head=None):
        """Each argv handed to act_run, its scratch directory taken out."""
        return [self.rel(e[1]) for e in self.effects if e[0] == "act" and (head is None or os.path.basename(e[1][0]) == head or any(os.path.basename(a) == head for a in e[1]))]


def flag_pairs(argv):
    flags = argv[argv.index("--additional-flags") + 1].split()
    return list(zip(flags[0::2], flags[1::2]))


class Flow(wsworld.WorkspaceTest):
    world_class = World
    KIND = "container"

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"fakebox": self.KIND})

    def run_wk(self, *argv):
        """(exit status, the first line of stderr with the scratch directory taken out)."""
        rc, out, err = self.wk(self.w, *argv)
        return rc, self.w.rel(err.splitlines()[0]) if err else ""

    def last_line(self, *argv):
        rc, _, err = self.wk(self.w, *argv)
        return rc, self.w.rel(err.splitlines()[-1]) if err else ""

    def created(self, *argv):
        """`wk new ws --_detached` on the default place: what the spawned half does."""
        return self.wk(self.w, "new", "ws", "--_detached", *argv)


class TestAContainerIsListedStartedAndStopped(Flow):
    def test_ls_names_a_container_without_its_prefix_by_podmans_word_once_its_ready_marker_is_down(self):
        self.w.make("a")
        self.w.make("b", marker=False)
        self.w.status["wk-a"] = "Exited (0) 3 days ago"
        rc, out, _ = self.wk(self.w, "ls")
        self.assertEqual((rc, [l.split()[:3] for l in out.splitlines()[1:]]), (0, [["a", "fakebox", "running"], ["b", "fakebox", "creating"]]))

    def test_ls_reads_a_container_removed_by_hand_as_broken_and_one_with_no_base_yet_as_creating(self):
        self.w.make("gone")
        self.w.containers.discard("wk-gone")
        os.makedirs(self.w.ws_dir("gone"))   # the store's directory listing is read from this host's disk
        self.w.make("young", base=False)
        self.w.containers.add("wk-orphan")
        rc, out, _ = self.wk(self.w, "ls")
        self.assertEqual([l.split()[:3] for l in out.splitlines()[1:]], [["gone", "fakebox", "broken"], ["orphan", "fakebox", "broken"], ["young", "fakebox", "creating"]])

    def test_ls_shows_the_arch_recorded_at_creation(self):
        self.w.make("ws")
        self.w.files[os.path.join(self.w.ws_dir("ws"), "arch")] = "armhf\n"
        self.assertEqual(self.wk(self.w, "ls")[1].splitlines()[1].split()[:5], ["ws", "fakebox", "running", "?", "armhf"])

    def test_stop_and_start_are_podmans_stop_and_start(self):
        self.w.make("ws")
        self.w.effects = []
        self.assertEqual(self.run_wk("stop", "ws"), (0, "==> stopped ws"))
        self.assertEqual(self.run_wk("start", "ws"), (0, "==> ws is up"))
        self.assertEqual(self.w.asked_to(), [("podman", "stop", "--time", "30", "wk-ws"), ("podman", "start", "wk-ws")])


class TestThePresetATestRunsByDefault(Flow):
    def suite(self, w):
        os.environ.pop("WK_PRESET", None)   # the dispatcher exports the last call's preset into this process
        rc, _, err = self.wk(w, "test", "ws", "--dry-run")
        return rc, [l for l in err.splitlines() if l.startswith("  suite:")]

    def test_is_the_last_builds_else_the_platforms(self):
        self.w.make("ws")
        self.assertEqual(self.suite(self.w), (0, ["  suite:     jsc (jsc-release)"]))
        self.w.begin("build", "ws").set("preset", "gtk-debug")
        for p, text in self.w.files.items():   # the record is read from this host's disk
            if p.startswith(str(self.w.tmp / "store" / "task")):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(text)
        self.assertEqual(self.suite(self.w), (0, ["  suite:     jsc (gtk-debug)"]))
        w = self.make_world(kinds={"fakebox": "vm"})
        w.make("ws")
        self.assertEqual(self.suite(w), (0, ["  suite:     jsc (mac-release)"]))


class TestAContainerIsCreated(Flow):
    def wkdev_create(self):
        (argv,) = self.w.asked_to("wkdev-create")
        return argv

    def test_wkdev_create_is_handed_every_flag_a_workspace_needs_and_the_base_id_lands_last(self):
        rc, _, err = self.created()
        self.assertEqual(rc, 0, err)
        argv, d = self.wkdev_create(), "/store/ws/ws"
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[argv.index("--network"):argv.index("--name") + 2][:4], ("--network", "none", "--isolated", "--image"))
        self.assertEqual(argv[argv.index("--name"):argv.index("--name") + 4], ("--name", "wk-ws", "--shell", "/bin/bash"))
        self.assertEqual(argv[argv.index("--home") + 1], d + "/home")
        pairs = flag_pairs(argv)
        for pair in (("--volume", "/store/base/main-1/WebKit:/src/WebKit:O,upperdir=%s/changes,workdir=%s/overlay-work" % (d, d)),
                     ("--volume", "/opt/wk-tools:/opt/wk-tools:ro"), ("--memory", "20480m"), ("--cpus", "7"),
                     ("--volume", "/store/secrets/view/container:/secrets:ro"), ("--env", "WK_WORKSPACE=ws"),
                     ("--env", "NO_PROXY=localhost,127.0.0.1,::1"), ("--env", "WK_ARCH=native")):
            self.assertIn(pair, pairs)
        self.assertEqual([self.w.rel(e[1]) for e in self.w.effects if e[0] == "mkdir" and e[1].startswith(self.w.ws_dir())],
                         [d, d + "/home", d + "/ccache", d + "/changes", d + "/overlay-work", d + "/build"])
        self.assertEqual(self.w.asked_to("install"), [("install", "-m", "0755", os.path.join(str(wsworld.REPO), "container", "firstrun.sh"), d + "/home/.wkdev-firstrun")])
        mine = [self.w.rel(e) for e in self.w.effects if e[0] == "write" and e[1].startswith(self.w.ws_dir()) or e[0] == "act" and e[1][0] == "install"]
        self.assertEqual([e[0] for e in mine], ["write", "act", "write"])
        self.assertEqual((mine[0][1], mine[2][1]), (d + "/arch", d + "/base-id"))
        self.assertIn(tuple(argv), [self.w.rel(a) for a in self.w.streamed], "`unit machine.streams_long_effects`")

    def test_the_agents_cache_is_mounted_read_only_and_the_ccache_is_the_workspaces_own(self):
        """`unit places.shared_mounts_readonly`: nothing another workspace reads is mounted writable."""
        self.created()
        pairs = flag_pairs(self.wkdev_create())
        dests = [v.split(":")[1:] for k, v in pairs if k == "--volume"]
        self.assertEqual(([["/cache/agents", "ro"]], []), ([d for d in dests if "agents" in d[0]], [d for d in dests if d[0] in ("/ccache", "/skills")]))
        fills = [a for a in self.w.asked_to("bash") if a[1] == "-lc"]
        self.assertEqual([(a[2].split("\n")[0], a[-1]) for a in fills], [("export WK_AGENTS_CACHE=/store/cache/agents WK_CLAUDE_VERSION=" +
                                                                          a[2].split("WK_CLAUDE_VERSION=")[1].split("\n")[0], "fetch") for a in fills])
        self.assertEqual(len(fills), 1, "the cache is filled here, from the store's side")
        for pair in (("--env", "CCACHE_DIR=/var/lib/wk/ws/ws/ccache"), ("--volume", "/store/ws/ws:/var/lib/wk/ws/ws"), ("--env", "CCACHE_MAXSIZE=40G")):
            self.assertIn(pair, pairs)

    def test_the_ccache_is_sized_by_wk_ccache_maxsize(self):
        self.w.env["WK_CCACHE_MAXSIZE"] = "5G"
        self.created()
        self.assertIn(("--env", "CCACHE_MAXSIZE=5G"), flag_pairs(self.wkdev_create()))

    def test_the_gpu_is_passed_to_a_native_workspace_and_an_armhf_one_gets_the_arm_image_and_none(self):
        self.w.bash["gpu_flags"] = Result(1)
        self.created()
        self.assertEqual([p for p in flag_pairs(self.wkdev_create()) if p[0] == "--device"], [])
        w = self.w = self.make_world()
        w.bash["gpu_flags"] = Result(0, "--device /dev/dri --device nvidia.com/gpu=all\n")
        self.created()
        self.assertEqual([p for p in flag_pairs(self.wkdev_create()) if p[0] == "--device"], [("--device", "/dev/dri"), ("--device", "nvidia.com/gpu=all")])
        w = self.w = self.make_world()
        w.bash["gpu_flags"] = Result(0, "--device /dev/dri\n")
        self.created("--arch", "armhf")
        argv = self.wkdev_create()
        self.assertEqual(argv[argv.index("--arch"):argv.index("--arch") + 3], ("--arch", "arm", "--image"))
        self.assertIn("arm32", argv[argv.index("--image") + 1])
        self.assertEqual(([], True), ([p for p in flag_pairs(argv) if p[0] == "--device"], ("--env", "WK_ARCH=armhf") in flag_pairs(argv)))

    def test_an_sdk_tag_upstream_has_not_published_is_refused_naming_the_newest_of_its_series_before_anything_is_made(self):
        self.w.published = ("2.55-v1-abc_amd64", "2.54-v9-aaa", "2.55-v1-1111111", "2.55-v10-2222222", "2.55-v2-3333333")
        rc, _, err = self.created()
        self.assertEqual(rc, 1)
        self.assertIn("%s:2.55-v1-abc, which upstream has not published" % SDK, err)
        self.assertIn("WK_SDK_IMAGE=%s:2.55-v10-2222222 wk new" % SDK, err)
        self.assertEqual([], [e for e in self.w.effects if e[0] in ("mkdir", "write") and e[1].startswith(self.w.ws_dir())])

    def test_the_sdk_layer_is_built_once_whether_or_not_the_registry_answers(self):
        for published in (("2.55-v1-abc",), None):
            self.w.published = published
            self.assertEqual(self.created()[0], 0)
            self.w.make("ws")
            self.assertEqual(self.wk(self.w, "rm", "ws", "--yes")[0], 0)
        built = [a[2:6] for a in self.w.asked_to("podman") if a[1] == "build"]
        (layer,) = {a[-1] for a in built}
        self.assertEqual(built, [("--build-arg", "BASE=%s:2.55-v1-abc" % SDK, "-t", layer)])
        self.assertTrue(layer.startswith("localhost/wk-sdk:2.55-v1-abc-"))
        self.assertEqual({a[a.index("--image") + 1] for a in self.w.asked_to("wkdev-create")}, {layer})

    def test_an_image_override_skips_the_registry(self):
        self.w.env["WK_SDK_IMAGE"] = "ghcr.io/x/sdk:tag"
        self.created()
        self.assertEqual([e for e in self.w.effects if e[0] == "run" and e[1][:2] == ("podman", "search")], [])
        self.assertIn("ghcr.io/x/sdk:tag", self.wkdev_create())

    def test_a_wkdev_create_that_fails_writes_no_base_id(self):
        self.w.wkdev_create = lambda a, f: Result(125, "", "no such image\n")
        rc, _, err = self.created()
        self.assertEqual((rc, "no such image" in err), (125, True))
        self.assertNotIn(self.w.ws_dir() + "/base-id", self.w.files)

    def test_a_firstrun_that_does_not_install_refuses_naming_wk_rm_and_writes_no_base_id(self):
        self.w.answer(["install", "-m"], rc=1, err="install: cannot stat: No such file or directory\n")
        rc, _, err = self.created()
        self.assertEqual(rc, 1)
        self.assertIn("installing firstrun.sh into 'ws' failed", err)
        self.assertIn("wk rm ws", err)
        self.assertNotIn(self.w.ws_dir() + "/base-id", self.w.files)

    def test_the_sdk_checkout_is_refreshed_first_streamed_and_a_failed_refresh_refuses(self):
        self.created()
        script = os.path.join(str(wsworld.REPO), "container", "sdk-refresh.sh")
        (refresh,) = self.w.asked_to("sdk-refresh.sh")
        self.assertEqual((refresh[:3], len(refresh)), (("bash", script, "/opt/webkit-container-sdk"), 5))
        self.assertIn(refresh, self.w.streamed)
        w = self.w = self.make_world()
        w.answer(["bash", script], rc=1, err="fetch failed\n")
        rc, _, err = self.created()
        self.assertEqual((rc, "fetch failed" in err, "refreshing the webkit-container-sdk checkout failed" in err), (1, True, True))
        self.assertEqual(w.asked_to("wkdev-create"), [])

    def test_the_front_makes_the_store_and_a_private_keyring_and_detaches_into_a_log_under_it(self):
        self.w.answer(["find", self.w.tmp_path("/store/secrets"), "-maxdepth"])   # not yet 0700
        rc, _, err = self.wk(self.w, "new", "ws", "--no-wait")
        self.assertEqual(rc, 0, err)
        made = [self.w.rel(e[1]) for e in self.w.effects if e[0] == "mkdir"]
        for d in ("git", "base", "ws", "cache/yocto/downloads", "cache/buildroot/ccache", "cache/bench", "cache/agents", "log"):
            self.assertIn("/store/" + d, made)
        self.assertIn(("chmod", "0700", "/store/secrets"), self.w.asked_to("chmod"))
        (spawn,) = [self.w.rel(e) for e in self.w.effects if e[0] == "spawn"]
        self.assertEqual(spawn[2], "/store/log/new-ws.log")


class TestAContainerIsWaitedFor(Flow):
    def initialises(self, after=None, words=""):
        def create(argv, f):
            f.containers.add(argv[argv.index("--name") + 1])
            f.dirs.add(argv[argv.index("--home") + 1])
            return Result(0)
        self.w.wkdev_create, self.w.last_words, sleep = create, words, self.w.clock.sleep

        def firstrun(seconds):
            sleep(seconds)
            if len(self.w.clock.slept) == after:
                self.w.files[self.w.ws_dir() + "/home/.wk-ready"] = ""
        self.w.clock.sleep = firstrun

    def test_its_creation_polls_the_marker_on_the_clock_until_it_is_down(self):
        self.initialises(after=3)
        rc, _, err = self.created()
        self.assertEqual((rc, self.w.clock.slept, wsworld.said(err)), (0, [2, 2, 2], "==> workspace 'ws' created"))

    def test_its_creation_gives_up_at_wk_ready_wait_showing_the_containers_last_words(self):
        self.initialises(words="one\n\ntwo\n")
        self.w.env["WK_READY_WAIT"] = "4"
        rc, _, err = self.created()
        self.assertEqual((rc, self.w.clock.slept, wsworld.said(err)), (1, [2, 2], "error: 'ws' was created but never finished initialising (above):"))
        self.assertIn("    one\n    two\nerror:", err)
        self.assertEqual([e[1] for e in self.w.effects if e[0] == "run" and e[1][:2] == ("podman", "logs")], [("podman", "logs", "wk-ws")])

    def test_a_mirror_under_the_containers_home_has_its_mountpoint_made_in_the_workspaces_home_as_the_user(self):
        self.w.env.update({"WK_CONTAINER_USER": "u", "WK_STORE": "/home/u/.local/share/wk"})
        self.w.driver = self.w.reg.load("fakebox")
        self.w.publish("main-1")
        self.w.dirs.add(self.w.driver.store.mirror_dir())
        rc, _, err = self.created()
        made = [e[1] for e in self.w.effects if e[0] == "mkdir" and e[1].startswith("/home/u/.local/share/wk/ws/ws/home/")]
        self.assertEqual((rc, made), (0, ["/home/u/.local/share/wk/ws/ws/home/.local/share/wk/git"]))
        self.assertIn(("--volume", "/home/u/.local/share/wk/git:/home/u/.local/share/wk/git:ro"), flag_pairs(self.w.asked_to("wkdev-create")[0]))


class TestAContainerIsDestroyed(Flow):
    def test_rm_removes_the_container_then_its_directory_and_its_creation_log(self):
        self.w.make("ws")
        self.w.files[self.w.driver.create_log("ws")] = "log\n"
        self.w.effects = []
        self.assertEqual(self.last_line("rm", "ws", "--yes"), (0, "==> workspace 'ws' destroyed"))
        self.assertEqual(self.w.asked_to("podman"), [("podman", "rm", "-f", "wk-ws"), ("podman", "unshare", "rm", "-rf", "/store/ws/ws")])
        self.assertEqual([self.w.rel(e) for e in self.w.work() if e[0] == "remove"], [("remove", "/store/ws/ws"), ("remove", "/store/log/new-ws.log")])
        self.assertEqual(self.w.left(), {})


class MacHost(World):
    """A Mac whose containers are in the podman machine `wk`: `podman -c wk` is its podman, and a shell there reads its files and its wk lists."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        del self.env["WK_IN_VM"]
        self.react(["podman", "-c", "wk"], lambda a, f: f._answer(["podman"] + a[3:]))
        self.react(["podman", "machine", "ssh", "wk", "--"], self._in_machine)
        self.answer(["podman", "machine", "inspect", "wk"], out=json.dumps([{"Name": "wk", "State": "running", "Resources": {"CPUs": 8, "Memory": 16384}}]))

    def _in_machine(self, argv, f):
        cmd = argv[-1]
        if cmd.startswith(("test -d ", "test -e ")):
            return Result(0 if f.exists(cmd[8:]) else 1)
        if cmd.endswith("/wk ls --json"):
            return Result(0, json.dumps({"workspaces": [{"name": c[3:], "place": "fakebox", "state": "running"} for c in sorted(f.containers)]}))
        return Result(0)


class TestOnAMacHost(Flow):
    world_class = MacHost

    def setUp(self):
        super().setUp()
        self.w.make("w")
        p = mock.patch("os.uname", return_value=os.uname_result(("Darwin", "mac", "25.0", "", "arm64")))
        p.start()
        self.addCleanup(p.stop)

    def test_ls_is_the_podman_machines_own_wk_ls(self):
        rc, out, _ = self.wk(self.w, "ls")
        self.assertEqual((rc, [l.split()[:3] for l in out.splitlines()[1:]]), (0, [["w", "fakebox", "running"]]))
        self.assertIn(("podman", "machine", "ssh", "wk", "--", "WK_IN_VM=1 WK_HOST_SELF=1 WK_NO_DELEGATE=1 WK_ROW_LABEL=here /opt/wk-tools/wk ls --json"),
                      [e[1] for e in self.w.effects if e[0] == "run"])

    def test_a_container_workspace_is_found_in_the_machines_store_and_entered_by_the_machines_own_wk(self):
        rc, _, err = self.wk(self.w, "enter", "w", "--", "git", "push")
        self.assertEqual((rc, [e[1] for e in self.w.effects if e[0] == "exec"]),
                         (0, [("podman", "machine", "ssh", "wk", "--", "WK_IN_VM=1 WK_HOST_SELF=1 WK_ROW_LABEL=here /opt/wk-tools/wk enter w -- git push")]))
        self.assertIn(("podman", "machine", "ssh", "wk", "--", "test -d %s" % self.w.ws_dir("w")), [e[1] for e in self.w.effects if e[0] == "run"])


    def test_a_run_is_entered_by_the_machines_own_wk_as_its_own_and_a_dry_run_is_not_handed_on(self):
        for argv in (("run", "w", "--", "x.js"), ("run", "w", "--dry-run", "--", "x.js")):
            os.environ.pop("WK_DRY_RUN", None)
            self.w.effects = []
            rc, _, err = self.wk(self.w, *argv)
            line = [e[1][-1] for e in self.w.effects if e[0] == "exec"] or [l for l in err.splitlines() if "/opt/wk-tools/wk enter" in l]
            self.assertEqual((rc, "/opt/wk-tools/wk enter w --own -- bash -lc " in line[0], "WK_DRY_RUN" in line[0]), (0, True, False), argv)


class TestInsideAWorkspace(Flow):
    KIND = "local"

    def test_a_workspace_neither_creates_destroys_nor_stops_itself_and_names_the_host_command(self):
        for argv, words in ((("new",), "a workspace cannot create a workspace -- run 'wk new ws' on the host"),
                            (("rm", "--yes"), "a workspace cannot destroy itself -- run 'wk rm ws' on the host"),
                            (("stop",), "a workspace cannot stop itself -- run 'wk stop ws' on the host")):
            self.assertEqual(self.run_wk(*argv), (1, "error: " + words))
        self.assertEqual([e for e in self.w.mutations() if e[0] != "mkdir"], [])


class TestAJobInAWorkspaceIsSizedByIt(Flow):
    KIND = "local"

    def sized(self, sysname, cpu=None, mem=None):
        os.environ.pop("WK_DRY_RUN", None)
        self.w.files["/proc/meminfo"] = "MemTotal:       32806140 kB\nMemAvailable:   20480000 kB\n"
        cgroup = {"/sys/fs/cgroup/cpu.max": cpu, "/sys/fs/cgroup/memory.max": mem}
        self.w.files = {p: t for p, t in self.w.files.items() if p not in cgroup} | {p: t for p, t in cgroup.items() if t}
        with mock.patch("os.uname", return_value=os.uname_result((sysname, "ws", "6.1", "", "x86_64"))):
            rc, _, err = self.wk(self.w, "test", "--dry-run")
        return rc, err.splitlines()[0]

    def test_on_linux_by_its_cgroup_else_the_machine(self):
        self.assertEqual(self.sized("Linux", "200000 100000\n", "4294967296\n"), (0, "resources: 2 jobs (cores=2 avail=4096MB @ 1536MB/job)"))
        self.assertEqual(self.sized("Linux", "max 100000\n", "max\n"), (0, "resources: 8 jobs (cores=8 avail=20000MB @ 1536MB/job)"))

    def test_an_unreadable_or_garbled_cgroup_limit_is_refused_not_replaced_by_the_machines(self):
        self.assertEqual(self.sized("Linux", None, "max\n"), (1, "error: cannot read /sys/fs/cgroup/cpu.max (no such file: /sys/fs/cgroup/cpu.max): "
                                                                  "a build in a container is sized by its cgroup's limits"))
        self.assertEqual(self.sized("Linux", "lots\n", "max\n"), (1, "error: /sys/fs/cgroup/cpu.max reads 'lots', which is not what cgroup v2 writes there"))

    def test_a_guest_has_no_cgroup_and_is_the_machines_size(self):
        self.assertEqual(self.sized("Darwin"), (0, "resources: 6 jobs (cores=8 avail=20480MB @ 3072MB/job)"))


class Fleet(fakes.World):
    """This host and the machines its `confs` name: `box` a build box at box.example keeping its workspaces under
    /home/u/wk with a wk of its own, `pal` a peer workstation whose wk is /opt/wk-tools/wk. Each far shell answers what
    `far` says (or works out, given the world) for the first needle its command line holds; one in `down` answers ssh
    with that error."""
    CONFS = {"box": "kind=build\nhost=box.example\nroot=/home/u/wk\n", "pal": "kind=peer\nhost=pal.example\ntools=/opt/wk-tools\n"}

    def __init__(self, tmp, confs=None):
        super().__init__(tmp, XDG_STATE_HOME="/st")
        (self.tmp / "machines").mkdir()
        for name, text in (confs or self.CONFS).items():
            (self.tmp / "machines" / (name + ".conf")).write_text(text)
        self.env["WK_MACHINES_DIR"] = str(self.tmp / "machines")
        self.answer(["podman"], rc=125, err="Error: no such object\n")   # no container anywhere, on any host's podman
        self.answer(["podman", "ps"])
        self.down, self.far = {}, {"test -f $HOME/.wk-remote": Result(0), "test -x": Result(0),
                                   "wk/tools/wk ls --json": Result(0, json.dumps({"workspaces": []})),
                                   "/opt/wk-tools/wk ls --json": Result(0, json.dumps({"workspaces": []}))}
        self.react(["ssh"], self._ssh)
        self.reg = FakeRegistry(self.env, self)

    def _ssh(self, argv, f):
        host = next(a for a in argv if a.endswith(".example"))[:-len(".example")]
        if host in f.down:
            return f.down[host]
        text = argv[-1]
        if "uname -s" in text:
            return Result(0, LINUX_PROBE)
        r = next((r for needle, r in f.far.items() if needle in text), Result(127, "", "no far answer for: " + text[:60]))
        return r(f) if callable(r) else r

    def asked(self, host):
        """The far command lines `host` was handed, in order."""
        out = []
        for e in self.effects:
            if e[0] == "run" and e[1][0] == "ssh" and host + ".example" in e[1]:
                text = shlex.split(e[1][-1])[-1]
                out.append(shlex.split(text)[-1] if text.startswith("sh -c ") else text)
        return out

    def lists(self, *rows):
        return Result(0, json.dumps({"workspaces": [dict(zip(("name", "place", "state"), r)) for r in rows]}))


class FleetFlow(FlowTest):
    def setUp(self):
        super().setUp()
        self.w = Fleet(self.tmp)

    def rows(self):
        rc, out, _ = self.wk(self.w, "ls")
        return rc, [l.split()[:3] for l in out.splitlines()[1:]]


class TestTheRegistry(FleetFlow):
    def test_an_unknown_place_is_refused_naming_every_conf(self):
        rc, _, err = self.wk(self.w, "new", "x", "--on", "nosuch")
        self.assertEqual((rc, err.splitlines()[:3]), (1, ["error: unknown place 'nosuch'.", "    The built-in ones are container, vm, remote and local.",
                                                          "    The machines here: box pal"]))

    def test_a_conf_named_after_this_machine_is_not_asked_as_a_peer(self):
        (self.w.tmp / "machines" / "HERE.conf").write_text("kind=build\nhost=self.example\n")
        self.rows()
        self.assertEqual((self.w.asked("self"), len(self.w.asked("box")) > 0), ([], True))

    def test_inside_the_podman_vm_no_machine_is_asked(self):
        self.w.env["WK_IN_VM"] = "1"
        self.assertEqual(self.rows(), (0, []))
        self.assertEqual([e for e in self.w.effects if e[1][0] == "ssh"], [])

    def test_on_a_build_box_its_own_place_is_the_default_and_is_read_here(self):
        (self.w.tmp / "wk-remote").write_text("place=box\nroot=/home/u/wk\n")
        (self.w.tmp / "machines" / "box.conf").write_text(self.w.CONFS["box"] + "hostname=here\n")
        self.w.env["WK_REMOTE_MARKER"] = str(self.w.tmp / "wk-remote")
        self.w.react(["sh", "-c"], lambda a, f: Result(0, LINUX_PROBE if "uname -s" in a[2] else ""))   # the box itself, in a shell here
        self.w.far["/opt/wk-tools/wk ls --json"] = self.w.lists(("pw", "pal", "running"))
        rc, out, err = self.wk(self.w, "ls")
        self.assertEqual((rc, out.splitlines()[1:], err.splitlines()[-1]), (0, [], "(no workspaces here -- from the workstation: wk new <name> --on box)"))
        self.assertEqual((self.w.asked("box"), self.w.asked("pal")), ([], []))

    def test_inside_a_workspace_the_place_is_local_and_lists_only_itself(self):
        (self.w.tmp / "marker").write_text("name=ws\nsrc=/src/WebKit\narch=armhf\n")
        self.w.env["WK_MARKER"] = str(self.w.tmp / "marker")
        rc, out, _ = self.wk(self.w, "ls")
        self.assertEqual((rc, [l.split() for l in out.splitlines()[1:]]), (0, [["ws", "local", "running", "n/a", "armhf", "n/a"]]))


class TestABuildBox(FleetFlow):
    def test_ls_probes_it_once_then_asks_its_own_wk_with_the_flags_as_environment(self):
        self.w.far["wk/tools/wk ls --json"] = self.w.lists(("a", "box", "present"))
        self.assertEqual(self.rows(), (0, [["a", "box", "present"]]))
        asked = self.w.asked("box")
        self.assertEqual((len(asked), "uname -s" in asked[0]), (3, True))
        self.assertEqual(asked[1:], ["test -f $HOME/.wk-remote && test -x /home/u/wk/tools/wk", "cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk ls --json"])
        ssh = [i for i, e in enumerate(self.w.effects) if e[0] == "run" and e[1][0] == "ssh"]
        self.assertEqual({self.w.effects[i - 1] for i in ssh}, {("mkdir", "/st/wk/ssh")}, "each ssh makes its ControlPath directory first")

    def test_its_tools_are_under_wk_in_the_far_home_unless_its_conf_names_them_relative_to_it(self):
        self.w = Fleet(self.tmp, {"bare": "kind=build\nhost=bare.example\n", "own": "kind=build\nhost=own.example\ntools=my/tools\n"})
        self.rows()
        self.assertEqual((self.w.asked("bare")[1], self.w.asked("own")[1]), ("test -f $HOME/.wk-remote && test -x /home/u/wk/tools/wk",
                                                                            "test -f $HOME/.wk-remote && test -x /home/u/my/tools/wk"))

    def test_one_with_no_wk_of_its_own_is_listed_from_here_by_its_directories_and_each_ones_marker(self):
        self.w.far.update({"test -f $HOME/.wk-remote": Result(1), "ls -1A /home/u/wk/ws": Result(0, "a\nb\n.stray\n"),
                           "ws/a ]": Result(0, "present\n"), "ws/b ]": Result(0, "creating\n")})
        self.assertEqual(self.rows(), (0, [["a", "box", "present"], ["b", "box", "creating"]]))
        self.assertEqual([a for a in self.w.asked("box") if "wk ls" in a], [])

    def test_one_that_does_not_answer_is_named_with_why_after_one_ssh(self):
        for rc, err, why in ((255, "ssh: Could not resolve hostname box.example: nodename nor servname provided, or not known\n\n",
                              "Could not resolve hostname box.example: nodename nor servname provided, or not known"),
                             (255, "", "ssh exited 255 and said nothing"), (124, "", "timed out after 20s"),
                             (255, "kex_exchange_identification: Connection closed by remote host\n", "Connection closed by remote host")):
            self.w.effects = []
            self.w.down["box"] = Result(rc, "", err)
            rc_, _, err_ = self.wk(self.w, "ls")
            self.assertEqual(rc_, 0)
            self.assertIn("warning: box: unreachable over ssh: %s, so its workspaces are not listed" % why, err_.splitlines())
            self.assertEqual(len(self.w.asked("box")), 1, why)

    def test_new_on_one_that_does_not_answer_is_refused_since_what_is_there_cannot_be_known(self):
        self.w.down["box"] = Result(255, "", "ssh: connect to host box.example port 22: Connection refused\n")
        rc, _, err = self.wk(self.w, "new", "x", "--on", "box")
        self.assertEqual((rc, [l for l in err.splitlines() if l.startswith(("error:", "warning:"))]),
                         (1, ["warning: could not ask box over ssh: connect to host box.example port 22: Connection refused -- what is there is not in this answer",
                              "error: 'new' acts on a workspace on box, and box did not answer."]))

    def test_a_place_with_no_host_is_refused_naming_its_conf_and_nothing_is_asked_over_ssh(self):
        self.w.effects = []
        rc, _, err = self.wk(self.w, "new", "x", "--on", "remote")
        self.assertEqual((rc, err.splitlines()[0]), (1, "error: place 'remote' has no host to reach: set host= in %s/machines/remote.conf" % self.w.tmp))

    def test_start_and_stop_say_a_box_has_nothing_to_bring_up_or_down(self):
        self.w.far.update({"test -f $HOME/.wk-remote": Result(1), "ws/a ]": Result(0, "present\n")})
        self.assertEqual(self.wk(self.w, "start", "a")[::2], (0, "==> 'box' has no notion of starting a single workspace -- nothing to bring up for 'a'\n"))
        self.assertEqual(self.wk(self.w, "stop", "a")[::2], (1, "error: the 'box' place has no notion of stopping a single workspace -- 'a' is left running\n"))
        self.assertEqual([e for e in self.w.mutations() if e[0] == "act"], [])


class TestABoxWorkspaceIsMadeFromHere(FleetFlow):
    """`box` has no wk of its own, so its workspace is made from here: `touch` of the marker makes `there` present; agents install."""

    def setUp(self):
        super().setUp()
        self.there = {"half": "creating", "there": "present"}
        self.w.far = {"test -f $HOME/.wk-remote": Result(1), "/home/u/wk/ws/a ]": lambda f: Result(0, self.there.get("a", "absent") + "\n"),
                      "/home/u/wk/ws/half ]": Result(0, "creating\n"), "/home/u/wk/ws/there ]": Result(0, "present\n"),
                      "git init --bare": Result(0, "mirror-fetch origin ok\nmirror-fetch wpe FAILED\n"), "git clone": Result(0),
                      "git remote": Result(0), "ccache.conf": Result(0), "touch": lambda f: (self.there.update(a="present"), Result(0))[1],
                      "WK_AGENTS_CACHE=": Result(0, "claude=installed\npi=installed\n"), "workspace-config.py": Result(0)}

    def acts(self):
        return [t for t in self.w.asked("box") if not any(n in t for n in ("uname -s", "echo absent", "cd /home/u/wk/ws/a/WebKit && "))]

    def new(self, name="a", *argv):
        self.w.effects = []
        rc, _, err = self.wk(self.w, "new", name, "--on", "box", "--_detached", *argv)
        return rc, err

    def test_the_mirror_is_refreshed_first_and_the_ready_marker_goes_down_last(self):
        rc, err = self.new()
        self.assertEqual((rc, wsworld.said(err)), (0, "==> workspace 'a' created"))
        acts = self.acts()
        self.assertEqual([a.split("\n")[0] for a in acts], ["set -e", "git clone --quiet --shared -b main /home/u/wk/mirror /home/u/wk/ws/a/WebKit",
                                                             "set -e", "[ -f /home/u/wk/cache/ccache/ccache.conf ] || printf %s 'max_size = 40G",
                                                             "touch /home/u/wk/ws/a/.wk-ready"])
        self.assertIn("M=/home/u/wk/mirror\n", acts[0])
        self.assertIn("git remote add mirror /home/u/wk/mirror", acts[2])
        self.assertIn("  origin   ok\n  wpe      FAILED\n", err)
        self.assertIn(("mkdir", "/st/wk/remote/box/ws/a"), self.w.effects, "its record is kept here")

    def test_a_failed_round_trip_ends_the_creation_where_it_stood(self):
        self.w.far["git clone"] = Result(128, "", "fatal: not a git repository\n")
        rc, err = self.new()
        self.assertEqual((rc, wsworld.said(err), len(self.acts())), (1, "error: could not create the checkout on box.example", 2))
        self.w.far.update({"git clone": Result(0), "touch": Result(255)})
        rc, err = self.new()
        self.assertEqual((rc, wsworld.said(err)), (1, "error: could not mark 'a' ready on box.example -- treat it as half-made and " "re-run 'wk new a --on box'"))

    def test_remotes_that_do_not_wire_only_warn_and_the_marker_still_goes_down(self):
        self.w.far["git remote"] = Result(1)
        rc, err = self.new()
        self.assertEqual((rc, self.acts()[-1]), (0, "touch /home/u/wk/ws/a/.wk-ready"))
        self.assertIn("warning: could not wire the remotes in /home/u/wk/ws/a/WebKit", "\n".join(err.splitlines()))

    def test_a_dry_run_reads_the_box_and_runs_nothing_there(self):
        rc, err = self.new("a", "--dry-run")
        self.assertEqual((rc, self.acts()), (0, []))
        self.assertIn("would run on box.example: sh -c", err)
        self.assertIn("touch /home/u/wk/ws/a/.wk-ready", err)


class TestThisMachineAsABox(FleetFlow):
    def test_every_round_trip_of_a_creation_is_a_local_shell_never_ssh(self):
        rr, there = str(self.w.tmp / "rr"), {}
        (self.w.tmp / "machines" / "me.conf").write_text("kind=build\nlocal=1\nroot=%s\n" % rr)

        def sh(argv, f):
            text = argv[2]
            there.update(a="present\n") if text.startswith("touch ") else None
            return Result(0, LINUX_PROBE if "uname -s" in text else there.get("a", "absent\n") if "echo absent" in text else "claude=installed\npi=installed\n")
        self.w.react(["sh", "-c"], sh)
        rc, _, err = self.wk(self.w, "new", "a", "--on", "me", "--_detached")
        acts = [e[1][2].split("\n")[0] for e in self.w.effects if e[0] == "act" and e[1][:2] == ("sh", "-c")][:5]
        self.assertEqual((rc, acts[1], acts[4]), (0, "git clone --quiet --shared -b main %s/mirror %s/ws/a/WebKit" % (rr, rr), "touch %s/ws/a/.wk-ready" % rr))
        self.assertEqual([e for e in self.w.effects if e[0] == "run" and e[1][0] == "ssh"], [])

    def test_a_command_on_a_terminal_runs_here_in_the_checkout(self):
        (self.w.tmp / "machines" / "me.conf").write_text("kind=build\nlocal=1\nroot=%s\n" % (self.w.tmp / "rr"))
        self.w.react(["sh", "-c"], lambda a, f: Result(0, LINUX_PROBE if "uname -s" in a[2] else "present\n"))
        self.w.answer(["bash", "-lc"])
        rc, _, err = self.wk(self.w, "run", "a", "--profile=bytecode", "--", "x.js")
        tty = next(e for e in self.w.effects if e[0] == "run_tty")
        self.assertEqual((rc, tty[1][:2], tty[2]), (0, ("bash", "-lc"), str(self.w.tmp / "rr" / "ws" / "a" / "WebKit")))


class TestABoxsOwnWkIsHandedTheCommand(FleetFlow):
    """Only `wk rm` here: a hand-over the dispatcher makes (`wk build`, `wk status`) replaces this process with ssh, and
    --force leaves a process-wide record of the barrier it crossed."""

    def setUp(self):
        super().setUp()
        self.there = "present"
        self.w.far.update({"/home/u/wk/ws/a ]": lambda f: Result(0, self.there + "\n"), "wk rm a": lambda f: (setattr(self, "there", "absent"), Result(0))[1]})
        self.w.answer(["git", "-C", str(wsworld.REPO), "rev-parse", "HEAD"], out="abc1234def\n")
        self.tools_at("0000stale000")

    def tools_at(self, sha):
        self.w.far["/wk doctor --probe-tools"] = Result(0, "sha=%s\ndirty=no\n" % sha)

    def rm(self, *argv):
        for v in self.UNSET:   # the dispatcher exports --yes and --dry-run into this process
            os.environ.pop(v, None)
        self.w.effects = []
        rc, _, err = self.wk(self.w, "rm", "a", *argv)
        return rc, err, [a for a in self.w.asked("box") if "wk rm" in a or "probe-tools" in a]

    def test_one_at_another_wk_tools_commit_is_refused_naming_the_sync_and_asked_again_each_time(self):
        rc, err, asked = self.rm("--yes")
        self.assertEqual((rc, wsworld.said(err), asked), (1, "error: 'rm' is not handed to box: wk-tools on box is at 0000stale000, and this "
                                                            "workstation's at abc1234def.", ["cd $HOME && WK_YES=1 /home/u/wk/tools/wk doctor --probe-tools"]))
        self.assertIn("    Bring it level:  wk sync --tools box", err.splitlines())
        self.tools_at("abc1234")
        rc, err, asked = self.rm("--yes")
        self.assertIn("==> 'a' destroyed on box.example, by that machine's own wk", err.splitlines())
        self.assertEqual((rc, wsworld.said(err), asked), (0, "==> workspace 'a' destroyed",
                                                          ["cd $HOME && WK_YES=1 /home/u/wk/tools/wk doctor --probe-tools",
                                                           "cd $HOME && WK_YES=1 WK_EXPORTS_READ=1 WK_ROW_LABEL=box /home/u/wk/tools/wk rm a"]))

    def test_a_dry_run_names_the_hand_over_and_runs_nothing_there(self):
        self.tools_at("abc1234")
        rc, err, asked = self.rm("--dry-run")
        self.assertEqual((rc, asked), (0, ["cd $HOME && WK_DRY_RUN=1 /home/u/wk/tools/wk doctor --probe-tools"]))
        (line,) = [l for l in err.splitlines() if l.startswith("would run: ssh")]
        self.assertIn("WK_DRY_RUN=1 WK_EXPORTS_READ=1 WK_ROW_LABEL=box /home/u/wk/tools/wk rm a", line)


class TestAPeer(FleetFlow):
    def test_ls_asks_its_own_wk_and_never_for_a_box_marker(self):
        self.w.far["/opt/wk-tools/wk ls --json"] = self.w.lists(("pw", "pal", "running"), ("half", "pal", "creating"))
        self.assertEqual(self.rows(), (0, [["pw", "pal", "running"], ["half", "pal", "creating"]]))
        asked = self.w.asked("pal")
        self.assertEqual(asked[1:], ["test -x /opt/wk-tools/wk", "cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=pal /opt/wk-tools/wk ls --json"])

    def test_one_with_no_wk_tools_of_its_own_is_refused_the_command(self):
        self.w.far["test -x"] = Result(1)
        rc, _, err = self.wk(self.w, "new", "x", "--on", "pal")
        self.assertEqual((rc, err.splitlines()[0], [e for e in self.w.mutations() if e != ("mkdir", "/st/wk/ssh")]),
                         (1, "error: 'new' acts on a workspace on pal, which has no wk-tools of its own to", []))

    def test_rm_hands_its_workspace_to_its_own_wk_and_a_refusal_there_changes_nothing_here(self):
        self.w.far["/opt/wk-tools/wk ls --json"] = self.w.lists(("pw", "pal", "running"))
        self.w.far["wk rm pw"] = lambda f: (f.far.update({"/opt/wk-tools/wk ls --json": f.lists()}), Result(0))[1]
        rc, _, err = self.wk(self.w, "rm", "pw", "--yes")
        self.assertEqual((rc, err.splitlines()[0]), (0, "==> 'pw' destroyed on pal.example, by that machine's own wk"))
        self.assertEqual([a for a in self.w.asked("pal") if "wk rm" in a], ["cd $HOME && WK_YES=1 WK_ROW_LABEL=pal /opt/wk-tools/wk rm pw"])
        self.w.far.update({"/opt/wk-tools/wk ls --json": self.w.lists(("pw", "pal", "running")), "wk rm pw": Result(1, "error: has work running in it\n")})
        self.w.effects = []
        rc, _, err = self.wk(self.w, "rm", "pw", "--yes")
        self.assertEqual((rc, err.splitlines()[-1]), (1, "error: pal.example did not destroy 'pw' (above); nothing here was changed -- re-run 'wk rm pw'"))
        self.assertEqual([e for e in self.w.work() if e[0] == "remove"], [])


GUEST_FILES = tuple("/vmstore/vm/mac." + f for f in ("run.log", "unfiltered", "push-forward.log", "broker-forward.log"))
BASE_INPUTS = ("vm/provision-base.sh", "vm/desktop.sh", "vm/mount-mirror.sh", "bench/mac-pyobjc.sh")
BASE_IMAGE = "ghcr.io/cirruslabs/macos-tahoe-xcode@sha256:f441eb487a18b4588c096adcff5eb48fddca550909e01c472580872b48c166b0"


def sealed_inputs():
    """What `wk sysimage build macos-guest-base` records of this tree's provisioning inputs when it seals the base."""
    text = b"".join((wsworld.REPO / rel).read_bytes() for rel in BASE_INPUTS) + ("image=%s\nuser=admin\n" % BASE_IMAGE).encode()
    return hashlib.sha256(text + b"WK_MOUNT_MIRROR=/usr/local/libexec/wk-mount-mirror").hexdigest()[:16]


class Guests(wsworld.World):
    """A Mac with tart: the base `wk-base` sealed against this tree, an image it `pulled`, a clone `tart run` boots at an address
    whose guest agent answers every exec, 200 GB free; guests boot unfiltered (WK_VM_UNFILTERED), as Softnet is test_guest's."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, kinds)
        self.vms["wk-base"] = "stopped"
        self.base_ready = os.path.join(self.env["WK_VM_STORE"], "vm", "base.ready")
        self.files[self.base_ready] = "image=%s\ninputs=%s\n" % (BASE_IMAGE, sealed_inputs())
        self.env["WK_VM_UNFILTERED"] = "1"
        self.pulled = [BASE_IMAGE]
        self.react([self.tart, "list"], lambda a, f: Result(0, json.dumps([{"Name": n, "State": s, "Source": "local"} for n, s in sorted(f.vms.items())]
                                                                          + [{"Name": i, "State": "stopped", "Source": "OCI"} for i in f.pulled])))
        self.answer([self.tart, "ip"], out="192.168.64.9\n")
        self.answer([self.tart, "exec"])
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.answer(["chmod"])

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        if "run" in argv:
            self.vms[argv[-1]] = "running"
        return pid

    def tart_did(self):
        return [e[1][1:] for e in self.effects if e[0] == "act" and e[1][0] == self.tart and e[1][1] != "exec"]


class GuestFlow(wsworld.WorkspaceTest):
    world_class = Guests

    def make_world(self, **kw):
        return super().make_world(kinds={"fakebox": "vm"}, **kw)

    def typed(self, *argv):
        self.w.effects = []
        rc, out, err = self.wk(self.w, *argv)
        return rc, out, err

    def created(self, *argv):
        return self.typed("new", "ws", "--_detached", *argv)


class TestAGuestIsCreated(GuestFlow):
    def test_new_clones_the_base_sizes_it_marks_it_ready_and_then_boots_it(self):
        rc, _, err = self.created()
        self.assertEqual((rc, wsworld.said(err)), (0, "==> workspace 'ws' created"))
        self.assertEqual(self.w.tart_did(), [("clone", "wk-base", "wk-ws"), ("set", "wk-ws", "--cpu", "9", "--memory", "20480", "--random-mac",
                                                                            "--display", "1280x800", "--display-refit")])
        self.assertEqual([a[1] for a in self.w.streamed if a[0] == self.w.tart], ["clone", "set"])
        made = [self.w.rel(e[:2]) if e[0] != "spawn" else ("spawn", e[1][-1]) for e in self.w.effects if e[0] in ("write", "spawn") and ("vmstore/ws" in e[1] or "run" in e[1])]
        self.assertEqual(made[:2], [("write", "/vmstore/ws/ws/.wk-ready"), ("spawn", "wk-ws")])
        self.assertEqual(self.w.vms["wk-ws"], "running")
        (forward,) = [e[1] for e in self.w.effects if e[0] == "spawn" and e[1][0] == "ssh"]
        self.assertEqual((forward[-1], forward[forward.index("-i") + 1]), ("wk-ws.vm.invalid", self.w.env["WK_VM_STORE"] + "/vm/id_ed25519"))
        self.assertIn("ProxyCommand=%s/container/ssh-transport vm ws" % wsworld.REPO, forward)

    def test_its_size_and_display_are_the_overrides_when_given(self):
        self.w.env.update({"WK_VM_CPUS": "4", "WK_VM_MEM_MB": "8192", "WK_VM_DISPLAY": "1920x1080"})
        self.created()
        self.assertEqual(self.w.tart_did()[1][2:], ("--cpu", "4", "--memory", "8192", "--random-mac", "--display", "1920x1080", "--display-refit"))

    def test_a_base_that_cannot_be_built_clones_nothing(self):
        del self.w.vms["wk-base"]
        self.w.pulled = []
        self.w.answer([self.w.tart, "pull"], rc=1, err="no route to ghcr.io\n")
        rc, _, err = self.created()
        self.assertEqual((rc, wsworld.said(err), self.w.tart_did()), (1, "error: tart pull failed (exit 1): what it said is above", [("pull", BASE_IMAGE)]))

    def test_a_stale_base_is_refused_naming_the_rebuild_unless_forced(self):
        self.w.files[self.w.base_ready] = "inputs=0000000000000000\n"
        rc, _, err = self.created()
        self.assertEqual((rc, wsworld.said(err), self.w.tart_did()), (1, "error: 'wk-base' predates its own provisioning inputs: vm/provision-base.sh, "
                                                                          "vm/desktop.sh, vm/mount-mirror.sh, bench/mac-pyobjc.sh, WK_VM_IMAGE or "
                                                                          "WK_VM_USER has changed since it was built.", []))
        self.assertIn("  WK_VM_FORCE=1 clones it anyway.", err.splitlines())
        self.w.env["WK_VM_FORCE"] = "1"
        rc, _, err = self.created()
        self.assertEqual((rc, self.w.tart_did()[0]), (0, ("clone", "wk-base", "wk-ws")))
        self.assertIn("warning: WK_VM_FORCE=1 -- 'ws' is cloned from a base that predates its own provisioning inputs: vm/provision-base.sh, "
                      "vm/desktop.sh, vm/mount-mirror.sh, bench/mac-pyobjc.sh, WK_VM_IMAGE or WK_VM_USER has changed since it was built", err.splitlines())

    def test_a_full_host_is_refused_counting_the_podman_machine_before_anything_is_cloned(self):
        self.w.make("mac")
        self.w.env["WK_VM_MAX"] = "2"
        self.w.answer(["podman", "machine", "inspect"], out=json.dumps([{"State": "running", "Resources": {"Memory": 8192}}]))
        rc, _, err = self.created()
        lines = err.splitlines()
        i = lines.index("error: 2 VM(s) are already running on this host:")
        self.assertEqual((rc, lines[i + 1:i + 3], self.w.tart_did()), (1, ["      mac", "      podman machine wk"], []))

    def test_the_front_makes_the_store_and_a_private_vm_directory(self):
        vm = self.w.env["WK_VM_STORE"] + "/vm"
        self.w.answer(["find", vm, "-maxdepth"])   # not yet 0700
        rc, _, err = self.typed("new", "ws", "--no-wait")
        self.assertEqual(rc, 0, err)
        made = [self.w.rel(e[1]) for e in self.w.effects if e[0] == "mkdir" and e[1].startswith(self.w.env["WK_VM_STORE"])]
        self.assertEqual(made[:3], ["/vmstore", "/vmstore/ws", "/vmstore/vm"])
        self.assertIn(("chmod", "0700", "/vmstore/vm"), [self.w.rel(e[1]) for e in self.w.effects if e[0] == "act"])

    def test_a_dry_run_names_the_clone_and_the_delete_and_changes_nothing(self):
        self.w.make("mac")
        before = (dict(self.w.files), set(self.w.dirs), dict(self.w.vms))
        rc, _, err = self.typed("new", "ws", "--dry-run")
        self.assertIn("would run: %s clone wk-base wk-ws" % self.w.tart, err.splitlines())
        rc, _, err = self.typed("rm", "mac", "--dry-run")
        self.assertIn("would run: %s delete wk-mac" % self.w.tart, err.splitlines())
        self.assertEqual((self.w.files, self.w.dirs, self.w.vms), before)


class TestAGuestIsReached(GuestFlow):
    def test_ls_lists_each_guest_by_tarts_word_once_its_marker_is_down_and_never_the_base_or_a_pulled_image(self):
        self.w.make("mac")
        self.w.make("half", marker=False)
        rc, out, _ = self.typed("ls")
        self.assertEqual((rc, [l.split()[:3] for l in out.splitlines()[1:]]), (0, [["half", "fakebox", "creating"], ["mac", "fakebox", "running"]]))

    def test_a_command_goes_through_the_guest_agent_never_an_address(self):
        self.w.make("mac")
        rc, _, err = self.typed("enter", "mac", "--", "echo", "hi")
        self.assertEqual((rc, [e[1] for e in self.w.effects if e[0] == "exec"]),
                         (0, [(self.w.tart, "exec", "-i", "wk-mac", "/bin/zsh", "-lc", "cd /Users/admin/WebKit 2>/dev/null; echo hi")]))

    def test_a_profiled_run_gets_a_pty_through_the_guest_agent(self):
        self.w.make("mac")
        rc, _, err = self.typed("run", "mac", "--profile=instruments", "--", "x.js")
        tty = next(e for e in self.w.effects if e[0] == "run_tty")
        self.assertEqual((rc, tty[1][:5], tty[1][-1].split(" && ")[0], tty[2]),
                         (0, (self.w.tart, "exec", "-i", "-t", "wk-mac"), "cd /Users/admin/WebKit 2>/dev/null; bash -lc 'cd /Users/admin/WebKit", None))

    def test_the_editors_alias_reaches_sshd_through_the_transport_and_a_stopped_guest_has_no_route(self):
        self.w.make("mac")
        rc, out, _ = self.typed("zed", "mac", "--url")
        self.assertEqual((rc, out), (0, "ssh://wk-mac/Users/admin/WebKit\n"))
        text = self.w.files[self.w.env["HOME"] + "/.ssh/config.d/wk"]
        self.assertIn("Host wk-mac\n    # generated by wk -- removed by wk rm\n    HostName wk-mac.vm.invalid\n", text)
        self.assertIn("ProxyCommand %s/container/ssh-transport vm mac" % wsworld.REPO, text)
        self.assertIn("IdentityFile %s/vm/id_ed25519" % self.w.env["WK_VM_STORE"], text)
        self.w.vms["wk-mac"] = "stopped"
        rc, _, err = self.typed("zed", "mac", "--url")
        self.assertEqual((rc, wsworld.said(err)), (1, "error: 'mac' is not running, so there is no route to it."))

    def test_a_stopped_guest_is_refused_naming_its_start(self):
        self.w.make("mac")
        self.w.vms["wk-mac"] = "stopped"
        for argv in (("scp", "mac", ":a", str(self.tmp / "b")), ("enter", "mac", "--", "true")):
            rc, _, err = self.typed(*argv)
            self.assertEqual((rc, wsworld.said(err)), (1, "error: 'mac' is not running (wk start mac)"), argv)
        self.assertEqual([e for e in self.w.effects if e[0] in ("exec", "act")], [])

    def test_a_guest_being_created_is_waited_for_on_the_clock_until_its_creation_ends(self):
        self.w.make("mac", marker=False)
        creation = self.w.begin(name="mac", pid=4242)
        self.w.pids.add(4242)
        sleep = self.w.clock.sleep

        def ends(seconds):
            sleep(seconds)
            if len(self.w.clock.slept) == 2:
                self.w.files[self.w.ws_dir("mac") + "/.wk-ready"] = ""
                creation.end(0)
        self.w.clock.sleep = ends
        rc, _, err = self.typed("enter", "mac", "--", "true")
        self.assertEqual((rc, self.w.clock.slept, err.splitlines()[0], wsworld.said(err)), (0, [2, 2], "==> waiting for 'mac' to finish being created", "==> 'mac' is ready"))

    def test_without_a_store_of_its_own_the_place_refuses_to_be_driven(self):
        self.w.make("mac")
        del self.w.env["WK_VM_STORE"]
        for argv in (("ls",), ("new", "ws")):
            rc, _, err = self.typed(*argv)
            self.assertEqual((rc, wsworld.said(err)), (1, "error: the vm place has no store of its own on this machine -- set WK_VM_STORE apart from WK_STORE"))

    def resources(self):
        os.environ.pop("WK_DRY_RUN", None)
        rc, _, err = self.typed("test", "g", "--dry-run")
        return rc, err.splitlines()[0]

    def test_as_tart_has_it_configured_else_as_this_host_would_size_one(self):
        self.w.make("g")
        self.w.answer([self.w.tart, "get", "wk-g"], out='{"CPU": 6, "Memory": 12288}')
        self.assertEqual(self.resources(), (0, "resources: 4 jobs (cores=6 avail=12288MB @ 3072MB/job)"))
        self.w.answer([self.w.tart, "get", "wk-g"], rc=1)
        self.w.env.update({"WK_VM_CPUS": "3", "WK_VM_MEM_MB": "4096"})
        self.assertEqual(self.resources(), (0, "resources: 1 jobs (cores=3 avail=4096MB @ 3072MB/job)"))


class TestOffAMac(fakes.FlowTest):
    def test_the_vm_place_holds_no_name_and_refuses_to_be_driven(self):
        with mock.patch("os.uname", return_value=os.uname_result(("Darwin", "mac", "25.0", "", "arm64"))):
            w = Guests(self.tmp, kinds={"fakebox": "vm"})
            w.make("mac")
        with mock.patch("os.uname", return_value=os.uname_result(("Linux", "box", "6.1", "", "x86_64"))):
            for argv, line in ((("new", "ws"), "error: the vm place holds macOS guests, so it exists only on a macOS host -- run this on the Mac that holds them"),
                               (("enter", "mac"), "warning: no such workspace: mac -- 'wk ls' lists them")):
                w.effects = []
                rc, _, err = self.wk(w, *argv)
                self.assertEqual((rc != 0, err.splitlines()[0], w.mutations()), (True, line, []))


class TestAGuestIsDestroyed(GuestFlow):
    def test_rm_deletes_the_guest_kills_its_runner_and_removes_every_file_of_it_directory_last(self):
        self.w.make("mac")
        self.w.pids.add(4242)
        self.w.react(["pgrep"], lambda a, f: Result(0, "4242\n") if 4242 in f.pids else Result(1))
        rc, _, err = self.typed("rm", "mac", "--yes")
        self.assertEqual((rc, wsworld.said(err)), (0, "==> workspace 'mac' destroyed"))
        acts = [self.w.rel(e if e[0] != "act" else e[1][1:]) for e in self.w.work()
                if e[0] == "kill" or e[0] == "remove" and "/vmstore/" in e[1] or e[0] == "act" and e[1][0] == self.w.tart]
        self.assertEqual(acts, [("stop", "wk-mac"), ("delete", "wk-mac"), ("kill", 4242, 15)] + [("remove", f) for f in GUEST_FILES]
                         + [("remove", "/vmstore/ws/mac"), ("remove", "/vmstore/log/new-mac.log")])
        self.assertEqual((self.w.left("mac"), "still alive" in err), ({}, False))

    def test_a_runner_that_outlives_the_delete_is_named_with_its_kill(self):
        self.w.make("mac")
        self.w.answer(["pgrep"], out="4242\n")
        rc, _, err = self.typed("rm", "mac", "--yes")
        self.assertIn("warning: a 'tart run' for 'wk-mac' is still alive (pid 4242), holding a VM slot:  kill -9 4242", err.splitlines())

    def test_a_guest_deleted_by_hand_has_only_its_files_cleared(self):
        self.w.make("mac")
        del self.w.vms["wk-mac"]
        rc, _, err = self.typed("rm", "mac", "--yes")
        self.assertEqual((rc, self.w.tart_did()), (0, []))
        self.assertEqual([self.w.rel(e) for e in self.w.work() if e[0] == "remove" and "/vmstore/" in e[1]],
                         [("remove", f) for f in GUEST_FILES] + [("remove", "/vmstore/ws/mac"), ("remove", "/vmstore/log/new-mac.log")])

    def test_the_golden_base_is_never_destroyed(self):
        rc, _, err = self.typed("rm", "base", "--yes")
        self.assertEqual((rc, wsworld.said(err), self.w.tart_did()), (1, "error: refusing to delete the golden base (wk sysimage build macos-guest-base --rm)", []))


if __name__ == "__main__":
    unittest.main()
