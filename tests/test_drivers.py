import dataclasses
import os
import tempfile
import unittest

from tests.world import CONFS, World
from tests.ws import HOME, MAKE, MIRROR, inv, server
from wk.core import seams
from wk.core.errors import Refused
from wk.core.machine import Local, Result
from wk.workspace import driver
from wk.workspace.drivers import ENTER, INSIDE, Container, Plain
from wk.workspace.place import place

IMAGE = "ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493"
X = seams.Workspace("x", "lin", HOME + "/ws/x", "container", seams.READY)
EXISTS = "[lin] podman container exists wk-x"
STATUS = "[lin] podman container inspect --format '{{.State.Status}}' wk-x"
RUN = ("[lin] podman run --detach --replace --pull=never --name wk-x --init --userns=keep-id --uts=host --pids-limit=-1 "
       "--security-opt seccomp=unconfined --volume /home/u:/home/u --volume {h}:{h} --volume {m}:{m}:ro "
       "--env HOME=/home/u --env WK_WORKSPACE=x --workdir {ws}/src %s sleep infinity" % IMAGE).format(ws=X.path, m=MIRROR, h=HOME)
ENV_SH = HOME + "/secrets/env.sh"


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

    def test_a_mac_server_gets_the_plain_driver(self):
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
        self.m.files[ENV_SH] = ""
        self.d = Container(self.m, HOME, IMAGE)

    def lines(self):
        return [line for line, _ in self.w.journal.entries]

    def test_create_runs_an_idle_container_with_the_logins_home_the_wk_home_and_the_mirror_mounted_under_the_hosts_name(self):
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
        del self.m.files[ENV_SH]
        self.assertRaisesRegex(Refused, r"lin has no /home/u/wk/secrets/env.sh for a shell in the container to source\n    `wk machine setup lin --stage credentials`, typed on the controller, delivers it", self.d.create, X)
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
        self.assertEqual(self.d.exec_argv(X, ["claude"], tty=True)[:4], ["podman", "exec", "--interactive", "--tty"])

    def test_the_persons_shell_is_the_one_wrapper_run_by_bash_in_the_container_on_the_terminal_when_there_is_one(self):
        inside = ["env", "SHELL=/bin/bash", "sh", "-c", ENTER, X.path + "/src", ENV_SH]
        self.assertEqual(self.d.enter_argv(X, tty=True), ["podman", "exec", "--interactive", "--tty", "--workdir", X.path + "/src", "wk-x"] + inside)
        self.assertEqual(self.d.enter_argv(X, ["git", "push", "a b"]),
                         ["podman", "exec", "--interactive", "--workdir", X.path + "/src", "wk-x"] + inside + ["-c", "git push 'a b'"])

    def test_typed_inside_a_container_the_persons_shell_is_the_wrapper_with_no_podman(self):
        self.m.files[INSIDE] = ""
        self.assertEqual(self.d.enter_argv(X, ["true"])[4:], ["env", "SHELL=/bin/bash", "sh", "-c", ENTER, X.path + "/src", ENV_SH, "-c", "true"])

    def test_a_task_or_a_plain_command_never_sources_the_delivered_environment(self):
        for d in (self.d, Plain(HOME)):
            self.assertNotIn("env.sh", " ".join(d.exec_argv(X, ["make"]) + d.exec_argv(X, ["make"], task=True)))

    def test_typed_inside_a_container_its_own_workspace_is_running_and_no_other_can_be_asked(self):
        self.m.files[INSIDE] = ""
        d = Container(self.m, HOME, IMAGE, own="x")
        self.assertEqual((d.state(X), d.state(dataclasses.replace(X, name="y"))), (seams.READY, seams.UNKNOWN))
        self.assertEqual([c for c in self.lines() if "podman" in c], [])

    def test_typed_inside_a_container_a_command_runs_directly_and_only_a_task_is_a_podman_exec(self):
        self.m.files[INSIDE] = ""
        self.assertEqual(self.d.exec_argv(X, ["make"]), ["sh", "-c", 'cd "$0" && exec "$@"', X.path + "/src", "make"])
        self.assertEqual(self.d.exec_argv(X, ["make"], task=True), ["podman", "exec", "--interactive", "--workdir", X.path + "/src", "wk-x", "make"])

    def test_wk_new_and_wk_rm_drive_the_real_driver(self):
        w = World(seams={"driver": driver})
        m = server(w)
        m.files[ENV_SH] = ""
        podman(m, None)
        self.assertEqual(w.wk("new", "x", on="lin").effects, MAKE[:-2] + [RUN, MAKE[-1]])
        podman(m, "running")
        self.assertEqual(w.wk("ls", on="lin").out.split(), ["x", "lin", "container", "ready"])
        self.assertEqual(w.wk("rm", "x", "--yes", on="lin").effects[1], "[lin] podman rm --force --ignore wk-x")

    def test_wk_ls_typed_inside_a_container_knows_its_own_workspace_and_calls_the_others_unknown(self):
        w = World(here="lin", seams={"driver": driver})
        w.workspace("x")
        w.workspace("y")
        w.machines["lin"].files[INSIDE] = ""
        self.assertEqual(w.wk("ls", env={"WK_WORKSPACE": "x"}).out.split(), ["x", "lin", "container", "ready", "y", "lin", "container", "unknown"])


class TestPlain(unittest.TestCase):
    def setUp(self):
        self.d, self.ws = Plain(HOME), seams.Workspace("x", "mac", HOME + "/ws/x", "plain", seams.READY)

    def test_there_is_nothing_to_create_destroy_or_find_broken(self):
        self.assertEqual((self.d.create(self.ws), self.d.destroy(self.ws), self.d.state(self.ws)), (None, None, seams.READY))

    def test_a_command_starts_in_the_checkout_and_the_persons_shell_is_the_one_wrapper(self):
        self.assertEqual(self.d.exec_argv(self.ws, ["make"]), ["sh", "-c", 'cd "$0" && exec "$@"', HOME + "/ws/x/src", "make"])
        self.assertEqual(self.d.enter_argv(self.ws, tty=True), ["sh", "-c", ENTER, HOME + "/ws/x/src", ENV_SH])
        self.assertEqual(self.d.enter_argv(self.ws, ["echo", "a b"]), ["sh", "-c", ENTER, HOME + "/ws/x/src", ENV_SH, "-c", "echo 'a b'"])

    def test_the_argv_really_runs_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(tmp + "/src/sub")
            ws = seams.Workspace("x", "mac", tmp, "plain", seams.READY)
            self.assertEqual(Local().run(self.d.exec_argv(ws, ["pwd"])).out, tmp + "/src\n")
            self.assertEqual(Local().run(self.d.exec_argv(ws, ["sh", "-c", "pwd; exit 3"], cwd=tmp + "/src/sub")).rc, 3)

    def test_the_wrapper_really_sources_the_environment_where_there_is_one_and_runs_the_command_in_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(tmp + "/ws/x/src")
            d, ws = Plain(tmp), seams.Workspace("x", "mac", tmp + "/ws/x", "plain", seams.READY)
            said = ["sh", "-c", 'pwd; echo "${WK_SOURCED:-no} $1"; exit 3', "sh", "a b"]
            r = Local().run(d.enter_argv(ws, said), env={"SHELL": "/bin/sh"})
            self.assertEqual((r.rc, r.out), (3, tmp + "/ws/x/src\nno a b\n"))
            os.makedirs(tmp + "/secrets")
            with open(tmp + "/secrets/env.sh", "w") as f:
                f.write("export WK_SOURCED=yes\n")
            r = Local().run(d.enter_argv(ws, said), env={"SHELL": "/bin/sh"})
            self.assertEqual((r.rc, r.out), (3, tmp + "/ws/x/src\nyes a b\n"))
            r = Local().run(d.enter_argv(ws), env={"SHELL": "/bin/sh"}, input="echo $WK_SOURCED\n")
            self.assertEqual((r.rc, r.out), (0, "yes\n"))
