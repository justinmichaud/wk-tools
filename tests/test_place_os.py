"""A place's driver as a person meets it: `wk new`, `wk rm`, `wk ls`, `wk start` and `wk stop` typed into a world whose
places are the real drivers over fake podman, tart and ssh."""
import json
import os
import shlex
import unittest

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
        return [self.rel(e[1]) for e in self.effects if e[0] == "act" and (head is None or os.path.basename(e[1][0]) == head
                                                                            or any(os.path.basename(a) == head for a in e[1]))]


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
        self.assertEqual([l.split()[:3] for l in out.splitlines()[1:]], [["gone", "fakebox", "broken"], ["orphan", "fakebox", "broken"],
                                                                        ["young", "fakebox", "creating"]])

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
        self.assertEqual(self.w.asked_to("install"), [("install", "-m", "0755", os.path.join(str(wsworld.REPO), "container", "firstrun.sh"),
                                                    d + "/home/.wkdev-firstrun")])
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
        for pair in (("--env", "CCACHE_DIR=/var/lib/wk/ws/ws/ccache"), ("--volume", "/store/ws/ws:/var/lib/wk/ws/ws"),
                     ("--env", "CCACHE_MAXSIZE=40G")):
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


class TestAContainerIsDestroyed(Flow):
    def test_rm_removes_the_container_then_its_directory_and_its_creation_log(self):
        self.w.make("ws")
        self.w.files[self.w.driver.create_log("ws")] = "log\n"
        self.w.effects = []
        self.assertEqual(self.last_line("rm", "ws", "--yes"), (0, "==> workspace 'ws' destroyed"))
        self.assertEqual(self.w.asked_to("podman"), [("podman", "rm", "-f", "wk-ws"), ("podman", "unshare", "rm", "-rf", "/store/ws/ws")])
        self.assertEqual([self.w.rel(e) for e in self.w.work() if e[0] == "remove"], [("remove", "/store/ws/ws"), ("remove", "/store/log/new-ws.log")])
        self.assertEqual(self.w.left(), {})


class TestInsideAWorkspace(Flow):
    KIND = "local"

    def test_a_workspace_neither_creates_destroys_nor_stops_itself_and_names_the_host_command(self):
        for argv, words in ((("new",), "a workspace cannot create a workspace -- run 'wk new ws' on the host"),
                            (("rm", "--yes"), "a workspace cannot destroy itself -- run 'wk rm ws' on the host"),
                            (("stop",), "a workspace cannot stop itself -- run 'wk stop ws' on the host")):
            self.assertEqual(self.run_wk(*argv), (1, "error: " + words))
        self.assertEqual([e for e in self.w.mutations() if e[0] != "mkdir"], [])


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
        self.assertEqual(asked[1:], ["test -f $HOME/.wk-remote && test -x /home/u/wk/tools/wk",
                                     "cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=box /home/u/wk/tools/wk ls --json"])
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


class TestAPeer(FleetFlow):
    def test_ls_asks_its_own_wk_and_never_for_a_box_marker(self):
        self.w.far["/opt/wk-tools/wk ls --json"] = self.w.lists(("pw", "pal", "running"), ("half", "pal", "creating"))
        self.assertEqual(self.rows(), (0, [["pw", "pal", "running"], ["half", "pal", "creating"]]))
        asked = self.w.asked("pal")
        self.assertEqual(asked[1:], ["test -x /opt/wk-tools/wk", "cd $HOME && WK_NO_DELEGATE=1 WK_ROW_LABEL=pal /opt/wk-tools/wk ls --json"])

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


if __name__ == "__main__":
    unittest.main()
