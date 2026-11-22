import os
import tempfile
import unittest

from tests.world import CONFS, World
from tests.ws import HOME, MAKE, MIRROR, inv, server
from wk.core import seams
from wk.core.errors import Refused
from wk.core.machine import Local, Result
from wk.workspace import driver
from wk.workspace.drivers import Container, Plain
from wk.workspace.place import place

IMAGE = "ghcr.io/igalia/wkdev-sdk:latest"
X = seams.Workspace("x", "lin", HOME + "/ws/x", "container", seams.READY)
EXISTS = "[lin] podman container exists wk-x"
STATUS = "[lin] podman container inspect --format '{{.State.Status}}' wk-x"
RUN = ("[lin] podman run --detach --replace --pull=never --name wk-x --init --userns=keep-id --pids-limit=-1 "
       "--security-opt seccomp=unconfined --volume {ws}:{ws} --volume {m}:{m}:ro --volume {h}/secrets:{h}/secrets:ro "
       "--workdir {ws}/src %s sleep infinity" % IMAGE).format(ws=X.path, m=MIRROR, h=HOME)


def podman(machine, status):
    """Script podman: the container wk-x is absent (None) or in `status`."""
    machine.on(("podman", "container", "exists"), Result(1 if status is None else 0))
    machine.on(("podman", "container", "inspect"), "%s\n" % status)


class TestFactory(unittest.TestCase):
    def test_a_linux_server_gets_a_container_driver_on_the_projects_sdk_image(self):
        w = World()
        d = driver(inv(w), "container", w.machines["lin"])
        self.assertEqual((type(d), d.kind, d.home, d.image), (Container, "container", HOME, IMAGE))

    def test_a_machine_conf_names_another_image(self):
        w = World(confs=dict(CONFS, lin="kind=linux\nsdk_image=localhost/sdk:1"))
        self.assertEqual(driver(inv(w), "container", w.machines["lin"]).image, "localhost/sdk:1")

    def test_a_mac_or_borrowed_server_gets_the_plain_driver(self):
        w = World()
        d = driver(inv(w), "plain", w.machines["mac"])
        self.assertEqual((type(d), d.kind), (Plain, "plain"))

    def test_wk_home_is_this_machines_environment_here_and_under_the_home_elsewhere(self):
        w = World()
        i = inv(w, "lin", {"WK_HOME": "/srv/wk"})
        self.assertEqual((place(i, w.machines["lin"]), place(i, w.machines["mac"])),
                         ((w.confs["lin"], "/srv/wk"), (w.confs["mac"], "/home/u/wk")))


class TestContainer(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")
        self.m = self.w.machines["lin"]
        self.m.files[HOME + "/secrets/github-token"] = "t"
        self.d = Container(self.m, HOME, IMAGE)

    def lines(self):
        return [line for line, _ in self.w.journal.entries]

    def test_create_runs_an_idle_container_with_the_workspace_the_mirror_and_the_secrets_mounted(self):
        podman(self.m, None)
        self.d.create(X)
        self.assertEqual((self.lines(), self.w.journal.effects()), ([EXISTS, RUN], [RUN]))

    def test_create_leaves_a_running_container_alone(self):
        podman(self.m, "running")
        self.d.create(X)
        self.assertEqual(self.lines(), [EXISTS, STATUS])

    def test_create_replaces_a_container_that_is_not_running(self):
        podman(self.m, "exited")
        self.d.create(X)
        self.assertEqual(self.w.journal.effects(), [RUN])

    def test_create_without_delivered_secrets_is_refused_naming_the_delivery(self):
        podman(self.m, None)
        del self.m.files[HOME + "/secrets/github-token"]
        self.assertRaisesRegex(Refused, r"lin has no /home/u/wk/secrets to mount in the container\n    `wk key sync` delivers", self.d.create, X)
        self.assertEqual(self.w.journal.effects(), [])

    def test_a_run_that_fails_is_refused_with_what_podman_said(self):
        podman(self.m, None)
        self.m.on(("podman", "run"), Result(125, "", "Error: image not known\n"))
        self.assertRaisesRegex(Refused, r"\(exit 125\): Error: image not known\n    `wk doctor` names what the container needs; `wk machine setup` pulls the image",
                               self.d.create, X)

    def test_destroy_removes_the_container_and_an_absent_one_is_no_error(self):
        self.d.destroy(X)
        self.assertEqual(self.w.journal.effects(), ["[lin] podman rm --force --ignore wk-x"])

    def test_state_is_podmans_word(self):
        for status, state in ((None, seams.ABSENT), ("running", seams.READY), ("exited", seams.BROKEN), ("created", seams.BROKEN)):
            podman(self.m, status)
            self.assertEqual(self.d.state(X), state, status)

    def test_a_podman_that_does_not_answer_is_not_an_absent_container(self):
        self.m.on(("podman", "container", "exists"), Result(125, "", "Error: cannot connect\n"))
        self.assertRaisesRegex(Refused, r"podman did not answer on lin \(exit 125\): Error: cannot connect\n    `wk doctor`", self.d.state, X)

    def test_a_command_is_a_podman_exec_from_the_checkout_or_the_directory_named(self):
        self.assertEqual(self.d.exec_argv(X, ["make", "-j4"]),
                         ["podman", "exec", "--interactive", "--workdir", X.path + "/src", "wk-x", "make", "-j4"])
        self.assertEqual(self.d.exec_argv(X, ["ls"], cwd=X.path + "/build")[3:5], ["--workdir", X.path + "/build"])

    def test_a_shell_is_an_interactive_login_shell_in_the_checkout(self):
        self.assertEqual(self.d.enter_argv(X), ["podman", "exec", "--interactive", "--tty", "--workdir", X.path + "/src", "wk-x", "/bin/bash", "-l"])

    def test_wk_new_and_wk_rm_drive_the_real_driver(self):
        w = World(seams={"driver": driver})
        m = server(w)
        m.files[HOME + "/secrets/github-token"] = "t"
        podman(m, None)
        self.assertEqual(w.wk("new", "x", "--on", "lin").effects, MAKE[:-2] + [RUN, MAKE[-1]])
        podman(m, "running")
        self.assertEqual(w.wk("ls").out.split(), ["x", "lin", "container", "ready"])
        self.assertEqual(w.wk("rm", "x", "--yes").effects[1], "[lin] podman rm --force --ignore wk-x")


class TestPlain(unittest.TestCase):
    def setUp(self):
        self.d, self.ws = Plain(), seams.Workspace("x", "mac", HOME + "/ws/x", "plain", seams.READY)

    def test_there_is_nothing_to_create_destroy_or_find_broken(self):
        self.assertEqual((self.d.create(self.ws), self.d.destroy(self.ws), self.d.state(self.ws)), (None, None, seams.READY))

    def test_a_command_and_a_shell_start_in_the_checkout(self):
        self.assertEqual(self.d.exec_argv(self.ws, ["make"]), ["sh", "-c", 'cd "$0" && exec "$@"', HOME + "/ws/x/src", "make"])
        self.assertEqual(self.d.enter_argv(self.ws), ["sh", "-c", 'cd "$0" && exec "$SHELL" -l "$@"', HOME + "/ws/x/src"])

    def test_the_argv_really_runs_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(tmp + "/src/sub")
            ws = seams.Workspace("x", "mac", tmp, "plain", seams.READY)
            self.assertEqual(Local().run(self.d.exec_argv(ws, ["pwd"])).out, tmp + "/src\n")
            self.assertEqual(Local().run(self.d.exec_argv(ws, ["sh", "-c", "pwd; exit 3"], cwd=tmp + "/src/sub")).rc, 3)
            r = Local().run(self.d.enter_argv(ws) + ["-c", "pwd"], env={"SHELL": "/bin/sh"})
            self.assertEqual((r.rc, r.out), (0, tmp + "/src\n"))
