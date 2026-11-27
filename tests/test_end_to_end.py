"""The lanes as one program: every real lane (the drivers, the pueue client, the agents, provisioning's front and rows)
typed over fake machines, for a Linux container server, a Mac, the controller and a workstation.
Only the machines as the playbook leaves them, the daemon behind pueue, the credentials, the rotation and HTTP are
scripted."""

import json
import shlex
import unittest

from tests import ROOT, provision
from tests.credsworld import TOKEN, URL, Http, login
from tests.pueue import LINUX, MAC, FakePueue, added
from tests.world import CONFS, World
from wk import tasks, workspace
from wk.agent import session
from wk.core.machine import Result
from wk.tasks import sizing
from wk.workspace import mirror
from wk.workspace.drivers import ENTER, INSIDE

HOME = "/home/u/wk"
WS = HOME + "/ws/x"
SRC, OUT = WS + "/src", WS + "/build"
MIRROR = HOME + "/git/WebKit.git"
SECRETS = HOME + "/secrets"
PUEUE = "%s/cache/pueue-4.0.5/pueue --config %s/pueue/pueue.yml" % (HOME, HOME)
IMAGE = "ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493"
ANSWERS = {("GET", "http://vault:3128/"): (200, b"wk injector\nlogin held\n")}
PLAYBOOK = "[vault] ansible-playbook -i %s/ansible/inventory.py" % ROOT
SITE = " %s/ansible/site.yml" % ROOT
CLAUDE_ENV = {"CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login", "DISABLE_AUTOUPDATER": "1", "HTTPS_PROXY": "http://vault:3128",
              "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem", "https_proxy": "http://vault:3128"}
CLAUDE = session.claude_bin(HOME)
PLACEHOLDER, CA = HOME + "/claude-login/.credentials.json", HOME + "/injector-ca.pem"
DELIVERED = {SECRETS + "/deploy": "drwx------", SECRETS + "/env.sh": "-rw-------", SECRETS + "/ssh_config": "-rw-------",
             SECRETS + "/rotate-key": "-rw-------", "/home/u/.pi/agent/models.json": "-rw-------"}
KEYS = {"justinmichaud/WebKit": "k\n"}


def inside(w, name, inner):
    """What a command run in a workspace on `name` answers: the sizing probe."""
    return (MAC if w.confs[name].kind == "mac" else LINUX) if inner == sizing.PROBE else None


def machine(w, name):
    """The server `name` as the playbook leaves it: each role's files (a path `ls -ld` is asked about holds the mode it
    prints), and each tool answering from the fake's files, as the real one answers from its disk."""
    m, kind = w.machines[name], w.confs[name].kind
    f = m.files
    provision.server(m)
    provision.provisioned(m)
    w.pueue[name] = daemon = FakePueue(m, w.clock)
    daemon.save(dict(daemon.state, groups=dict(daemon.state["groups"], build={"status": "Running", "parallel_tasks": 1})))
    f.update({HOME + "/cache/pueue-4.0.5/pueue": "", HOME + "/cache/pueue-4.0.5/pueued": "", MIRROR + "/HEAD": "ref: refs/heads/main\n",
              MIRROR + "/config": mirror.CONFIG, MIRROR + "/refs/heads/main": "5e1f\n", CLAUDE: "claude", session.pi_bin(HOME): "pi",
              "/home/u/.claude/CLAUDE.md": "", "/home/u/.claude/settings.json": "{}", PLACEHOLDER: "-rw-------", CA: "-rw-r--r--"})
    f.update(DELIVERED)
    m.on(("id", "-un"), "u\n")
    m.on(("node", "--version"), "v22.19.0\n")
    m.on(("ssh", "-i", SECRETS + "/rotate-key"), json.dumps(KEYS))
    m.on(("git", "clone"), lambda call: f.update({call.argv[-1] + "/.git/HEAD": "ref: refs/heads/main\n"}))
    m.on(("git", "-C", MIRROR, "rev-parse"), lambda call: Result(0 if MIRROR + "/refs/heads/main" in f else 1))
    m.on(("sh", "-c", 'cd "$0" && exec "$@"'), lambda call: inside(w, name, list(call.argv[4:])))
    if kind == "linux":
        f["/etc/subuid"] = f["/etc/subgid"] = "u:165536:65536\n"
        f["/podman/image"] = IMAGE
        m.on(("podman", "--version"), "podman version 5.4.0\n")
        m.on(("podman", "image", "exists"), lambda call: Result(0 if "/podman/image" in f else 1))
        m.on(("podman", "container", "exists"), lambda call: Result(0 if "/podman/" + call.argv[3] in f else 1))
        m.on(("podman", "container", "inspect"), lambda call: f["/podman/" + call.argv[5]])
        m.on(("podman", "run"), lambda call: f.update({"/podman/" + call.argv[call.argv.index("--name") + 1]: "running\n"}))
        m.on(("podman", "rm"), lambda call: f.pop("/podman/" + call.argv[4], None) and None)
        m.on(("podman", "exec"), lambda call: inside(w, name, list(call.argv[call.argv.index("wk-x") + 1:])))


def world():
    """The fleet as `wk machine setup` leaves each server: the controller's checkout is clean and pushed, and it holds
    every credential."""
    w = World(confs=dict(CONFS, vault="kind=controller\nlitellm_url=%s" % URL), seams={"driver": workspace.driver, "tasks": tasks.tasks})
    w.pueue = {}
    vault = w.machines["vault"]
    provision.checkout(vault)
    provision.controller(vault)
    vault.on(("ansible-playbook", "-i"), "")
    vault.on(("ls", "-ld"), "drwx------ 2 u u 64 Nov 22 16:07 %s\n" % SECRETS)
    for name in ("lin", "mac", "box"):
        machine(w, name)
    w.secrets.values.update({"github-user": "me", "github-token": TOKEN, "bugzilla-user": "me@example.test", "bugzilla-password": "pw",
                             "litellm-key": "sk-1", "claude-login": login()})
    return w


PODMAN, PLAIN = ["podman", "exec", "--interactive", "--workdir", SRC, "wk-x"], ["sh", "-c", 'cd "$0" && exec "$@"', SRC]
EXEC = {"lin": PODMAN, "mac": PLAIN, "box": PLAIN}
EXEC_TTY = dict(EXEC, lin=PODMAN[:3] + ["--tty"] + PODMAN[3:])
RUN = ("[%s] podman run --detach --replace --pull=never --name wk-x --init --userns=keep-id --uts=host --pids-limit=-1 "
       "--security-opt seccomp=unconfined --volume /home/u:/home/u --volume {h}:{h} --volume {m}:{m}:ro --env HOME=/home/u "
       "--env WK_WORKSPACE=x --workdir {s} {i} sleep infinity").format(h=HOME, m=MIRROR, s=SRC, i=IMAGE)
MODE = ["--dangerously-skip-permissions"]
BUILD = {"lin": ["env", "CC=clang"], "mac": ["env", "WEBKIT_OUTPUTDIR=" + OUT + "/Release"]}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        patch = Http(ANSWERS).patch()
        patch.start()
        self.addCleanup(patch.stop)

    def ok(self, run, code=0):
        self.assertEqual(run.code, code, run.err + run.out)
        return run

    def played(self, run, words):
        """The command was one ansible-playbook, after the questions the front asks first."""
        self.assertEqual((run.effects, [line for line in run.calls if line.startswith(PLAYBOOK)]), ([PLAYBOOK + words + SITE],) * 2)

    def started(self, w, name):
        """`wk new x`, a detached build and its status, on the server `name`; returns the build's line."""
        made = self.ok(w.wk("new", "x", on=name))
        self.assertEqual(made.effects[0], "[%s] git clone --shared --no-tags -c fetch.writeCommitGraph=false -c gc.writeCommitGraph=false %s %s"
                         % (name, MIRROR, SRC))
        self.assertEqual(made.effects[6:], ["[%s] mkdir -p %s" % (name, OUT)] + [RUN % name] * (w.confs[name].driver == "container")
                         + ["[%s] write %s/.ready" % (name, WS)])
        self.assertEqual(self.ok(w.wk("ls")).out.split(), ["x", name, w.confs[name].driver, "ready"])
        built = self.ok(w.wk("build", "x", "jsc-release", "--detach", on=name))
        self.assertIn("%s add --print-task-id --group build --label x:build:jsc-release " % PUEUE, built.effects[-1])
        line = added(w, name)[0]
        self.assertEqual(line[:len(EXEC[name]) + len(BUILD[name])], EXEC[name] + BUILD[name])
        status = self.ok(w.wk("status", "x", on=name), 2)
        self.assertEqual(status.out, "running  x:build:jsc-release            0m00s\n")
        return line

    def agent(self, w, name):
        """`wk ai claude x`: started in the login shell `wk enter` opens, with the injector's environment."""
        run = self.ok(w.wk("ai", "claude", "x", tty=True, on=name))
        inner = ["env", "WK_WORKSPACE=x"] + ["%s=%s" % kv for kv in sorted(CLAUDE_ENV.items())] + [CLAUDE] + MODE + ["--remote-control", "x"]
        entered = ["sh", "-c", ENTER, SRC, SECRETS + "/env.sh", "-c", shlex.join(inner)]
        start = EXEC_TTY[name] + ["env", "SHELL=/bin/bash"] + entered if name == "lin" else entered
        self.assertEqual(run.effects, ["[%s] write /home/u/.claude.json" % name, run.effects[1], "[%s] write %s/deploy/justinmichaud--WebKit"
                                       % (name, SECRETS), "[%s] %s" % (name, shlex.join(start))])
        self.assertTrue(run.effects[1].startswith("[%s] ssh -i %s/rotate-key " % (name, SECRETS)), run.effects[1])
        return run


class TestProvisioning(EndToEnd):
    def test_setup_of_any_server_is_one_ansible_playbook_limited_to_it(self):
        w = world()
        for name in ("vault", "lin", "mac"):
            self.played(self.ok(w.wk("machine", "setup", name, on="vault")), " --limit " + name)

    def test_a_dry_run_of_setup_is_ansibles_check_mode_and_no_effect_of_wks(self):
        w = world()
        before = w.state()
        dry = self.ok(w.wk("--dry-run", "machine", "setup", "lin", on="vault"))
        self.assertEqual((dry.effects, dry.plan, w.state()), ([], [], before))
        self.assertIn(PLAYBOOK + " --limit lin --check --diff" + SITE, dry.calls)

    def test_delivering_the_credentials_is_the_credentials_stage_of_setup(self):
        self.played(self.ok(world().wk("machine", "setup", "lin", "--stage", "credentials", on="vault")), " --limit lin --tags credentials")


class TestYourServers(EndToEnd):
    def journey(self, name, removed):
        w = world()
        self.started(w, name)
        self.assertEqual(self.agent(w, name).err, "==> rotated the push keys of justinmichaud/WebKit\n==> starting claude in x\n")
        for on in (name, "vault"):
            doctor = self.ok(w.wk("doctor", on=on))
            self.assertEqual([line for line in doctor.out.splitlines() if not line.startswith(" ")], [on])
            self.assertEqual([line for line in doctor.out.splitlines() if line.startswith(" ") and not line.startswith("  ok  ")], [])
        self.assertEqual(self.ok(w.wk("doctor", "x", on=name)).out, "x (workspace on %s)\n" % name)
        self.assertIn("error: workspace x has a running task (x:build:jsc-release)", self.ok(w.wk("rm", "x", "--yes", on=name), 2).err)
        self.ok(w.wk("build", "x", "--kill", on=name))
        self.assertEqual(self.ok(w.wk("rm", "x", "--yes", on=name)).effects, ["[%s] remove %s/.ready" % (name, WS)] + removed + ["[%s] remove %s" % (name, WS)])
        gc = self.ok(w.wk("gc", on=name))
        self.assertEqual((gc.out, gc.effects), ("nothing to reclaim\n", []))
        w.machines[name].files[HOME + "/cache/pueue-3.4.1"] = "old"
        self.assertEqual(self.ok(w.wk("gc", "--yes", on=name)).effects, ["[%s] remove %s/cache/pueue-3.4.1" % (name, HOME)])

    def test_a_linux_server_from_provisioned_to_a_build_and_an_agent_in_its_container_and_back(self):
        self.journey("lin", ["[lin] podman rm --force --ignore wk-x"])

    def test_a_mac_server_from_provisioned_to_a_build_and_an_agent_in_its_checkout_and_back(self):
        self.journey("mac", [])

    def test_the_controller_holds_no_workspace_and_reports_its_secrets_the_injector_and_its_tools(self):
        w = world()
        self.assertIn("error: wk new runs on a Linux server or a Mac server (box, lin, mac), and this is vault, the controller",
                      self.ok(w.wk("new", "x", on="vault"), 1).err)
        self.assertEqual([line.split()[1] for line in self.ok(w.wk("doctor", on="vault")).out.splitlines()[1:]],
                         ["secrets", "injector", "ssh", "git", "tailscale", "ansible", "xorriso", "qemu-nbd", "websocat"])

    def test_the_workstation_lists_every_servers_workspaces_and_opens_one_in_zed(self):
        w = world()
        self.ok(w.wk("new", "x", on="mac"))
        self.assertEqual(self.ok(w.wk("ls")).out.split(), ["x", "mac", "plain", "ready"])
        self.assertEqual(self.ok(w.wk("zed", "x")).effects, ["[desk] zed ssh://mac%s" % SRC])

    def test_wk_typed_inside_the_container_queues_a_build_the_servers_daemon_starts(self):
        w = world()
        self.ok(w.wk("new", "x", on="lin"))
        w.machines["lin"].files[INSIDE] = ""
        run = self.ok(w.wk("build", "x", "jsc-release", "--detach", on="lin", env={"WK_WORKSPACE": "x"}))
        self.assertEqual([call.split(" 'if ")[0] for call in run.calls if "podman" in call.split(" add ")[0] or "sh -c" in call],
                         ["[lin] sh -c 'cd \"$0\" && exec \"$@\"' %s sh -c" % SRC])
        self.assertEqual(added(w, "lin")[0][:len(EXEC["lin"])], EXEC["lin"])
