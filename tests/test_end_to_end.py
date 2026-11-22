"""The lanes as one program: every real lane (the drivers, the pueue client, the agents, provisioning's front and rows)
typed on the client over fake machines, for a Linux container server, a Mac, a borrowed box and the credential server.
Only the machines as the playbook leaves them, the daemon behind pueue, the store and HTTP are scripted."""

import shlex
import unittest

from tests import ROOT, provision
from tests.credsworld import TOKEN, URL, Http, login, private_key
from tests.pueue import LINUX, MAC, FakePueue, added
from tests.world import CONFS, World
from wk import tasks, workspace
from wk.agent import session
from wk.agent.wrapper import wrap
from wk.core.machine import Result
from wk.core.vars import VARS
from wk.tasks import sizing
from wk.workspace import mirror
from wk.workspace.drivers import INSIDE

HOME = "/home/u/wk"
WS = HOME + "/ws/x"
SRC, OUT = WS + "/src", WS + "/build"
MIRROR = HOME + "/git/WebKit.git"
SECRETS = HOME + "/secrets"
PUEUE = "%s/cache/pueue-4.0.5/pueue --config %s/pueue/pueue.yml" % (HOME, HOME)
IMAGE = "ghcr.io/igalia/wkdev-sdk:2.55-v5-94a7493"
SEEN = "HOME=/home/u\0USER=u\0PATH=/usr/bin:/bin\0TERM=dumb\0SSH_AUTH_SOCK=/tmp/agent\0GH_TOKEN=leaked\0"
ANSWERS = {("GET", "http://vault:3128/"): (200, b"wk injector\nlogin held\n"),
           ("GET", "http://vault:8200/v1/sys/seal-status"): (200, {"initialized": True, "sealed": False})}
PLAYBOOK = "[desk] ansible-playbook -i %s/ansible/inventory.py" % ROOT
SITE = " %s/ansible/site.yml" % ROOT
PORT = 24583       # 20000 + crc32("ai claude x") % 10000
SESSION = {"NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem", "CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login", "DISABLE_AUTOUPDATER": "1",
           "HOME": "/home/u", "USER": "u", "PATH": "%s/shims:%s/bin:/usr/bin:/bin" % (HOME, HOME), "TERM": "dumb", "WK_AGENT": "1",
           "WK_WORKSPACE": "x"}
CLAUDE_ENV = dict(SESSION, HTTPS_PROXY="http://vault:3128", https_proxy="http://vault:3128")
FORWARDED = dict(SESSION, HTTPS_PROXY="http://127.0.0.1:%d" % PORT, https_proxy="http://127.0.0.1:%d" % PORT)
CLAUDE = session.claude_bin(HOME)
PLACEHOLDER, CA = HOME + "/claude-login/.credentials.json", HOME + "/injector-ca.pem"
DELIVERED = {SECRETS + "/deploy": "drwx------", SECRETS + "/env.sh": "-rw-------", SECRETS + "/ssh_config": "-rw-------",
             "/home/u/.pi/agent/models.json": "-rw-------"}


def inside(w, name, inner):
    """What a command run in a workspace on `name` answers: the sizing probe, the environment, and the wrapper's stand-in."""
    if inner == sizing.PROBE:
        return MAC if w.confs[name].kind == "mac" else LINUX
    if inner == ["env", "-0"]:
        return SEEN
    if inner[0] in ("bwrap", "sandbox-exec") or inner[:2] == ["env", "-i"]:
        return "" if "ls" in inner else "HOME=/home/u\0PWD=%s\0" % SRC
    return None


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
    f.update({"%s/shims/%s" % (HOME, tool): "" for tool in VARS["wk_build_tools"]})
    f.update({} if kind == "borrowed" else DELIVERED)
    m.on(("id", "-un"), "u\n")
    m.on(("node", "--version"), "v22.19.0\n")
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
    """The fleet as `wk machine setup` leaves each server: the client's checkout is clean and pushed, and the store
    holds every credential."""
    w = World(confs=dict(CONFS, desk="kind=client\nlitellm_url=%s" % URL), seams={"driver": workspace.driver, "tasks": tasks.tasks})
    w.pueue = {}
    provision.checkout(w.machines["desk"])
    provision.client(w.machines["desk"])
    w.machines["desk"].on(("ansible-playbook", "-i"), "")
    for name in ("lin", "mac", "box"):
        machine(w, name)
    w.machines["vault"].on(("true",), "")
    w.machines["vault"].files[HOME + "/cache/openbao-2.7.3/bao"] = ""
    w.secrets.values.update({"github-user": "me", "github-token": TOKEN, "bugzilla-user": "me@example.test", "bugzilla-password": "pw",
                             "litellm-key": "sk-1", "claude-login": login(), "deploy-key/Me/WebKit": private_key()})
    return w


PODMAN, PLAIN = ["podman", "exec", "--interactive", "--workdir", SRC, "wk-x"], ["sh", "-c", 'cd "$0" && exec "$@"', SRC]
EXEC = {"lin": PODMAN, "mac": PLAIN, "box": PLAIN}
EXEC_TTY = dict(EXEC, lin=PODMAN[:3] + ["--tty"] + PODMAN[3:])
RUN = ("[%s] podman run --detach --replace --pull=never --name wk-x --init --userns=keep-id --uts=host --pids-limit=-1 "
       "--security-opt seccomp=unconfined --volume /home/u:/home/u --volume {h}:{h} --volume {m}:{m}:ro --env HOME=/home/u "
       "--env WK_WORKSPACE=x --workdir {s} {i} sleep infinity").format(h=HOME, m=MIRROR, s=SRC, i=IMAGE)
SKIP = ["--dangerously-skip-permissions"]
MODE = {"lin": SKIP, "mac": SKIP, "box": ["--permission-mode", "auto"]}
BUILD = {"lin": ["24576", "1", "env", "WK_BUILD=1", "CC=clang"], "mac": ["30720", "0", "env", "WK_BUILD=1", "WEBKIT_OUTPUTDIR=" + OUT + "/Release"],
         "box": ["12288", "0", "nice", "-n", "19", "ionice", "-c", "3", "env", "WK_BUILD=1", "CC=clang"]}


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
        made = self.ok(w.wk("new", "x", "--on", name))
        self.assertEqual(made.effects[0], "[%s] git clone --shared --no-tags -c fetch.writeCommitGraph=false -c gc.writeCommitGraph=false %s %s"
                         % (name, MIRROR, SRC))
        self.assertEqual(made.effects[6:], ["[%s] mkdir -p %s" % (name, OUT)] + [RUN % name] * (w.confs[name].driver == "container")
                         + ["[%s] write %s/.ready" % (name, WS)])
        self.assertEqual(self.ok(w.wk("ls")).out.split(), ["x", name, w.confs[name].driver, "ready"])
        built = self.ok(w.wk("build", "x", "jsc-release", "--detach"))
        self.assertEqual(built.effects[:-1], ["[%s] write %s/guard.py" % (name, OUT)])
        self.assertIn("%s add --print-task-id --group build --label x:build:jsc-release " % PUEUE, built.effects[-1])
        line = added(w, name)[0]
        self.assertEqual(line[:len(EXEC[name]) + 3 + len(BUILD[name])], EXEC[name] + ["python3", "-I", OUT + "/guard.py"] + BUILD[name])
        status = self.ok(w.wk("status", "x"), 2)
        self.assertEqual(status.out, "running  x:build:jsc-release            0m00s\n")
        return line

    def agent(self, w, name, env=CLAUDE_ENV):
        """`wk ai claude x`: measured, then started under the kind's wrapper with the injector's environment."""
        run = self.ok(w.wk("ai", "claude", "x"))
        start = EXEC_TTY[name] + wrap(w.confs[name].kind, [CLAUDE] + MODE[name] + ["--remote-control", "x"], [SECRETS], env)
        self.assertEqual(run.effects, ["[%s] write /home/u/.claude.json" % name, "[%s] %s" % (name, shlex.join(start))])
        for hidden in ("GH_TOKEN", "GITHUB_COM_TOKEN", "SSH_AUTH_SOCK", "leaked"):
            self.assertNotIn(hidden, run.effects[-1])
        self.assertEqual(env["PATH"].split(":")[:2], [HOME + "/shims", HOME + "/bin"])
        return run


class TestProvisioning(EndToEnd):
    def test_setup_of_any_server_is_one_ansible_playbook_limited_to_it(self):
        w = world()
        for name in ("vault", "lin", "mac", "box"):
            self.played(self.ok(w.wk("machine", "setup", name)), " --limit " + name)
        self.played(self.ok(w.wk("machine", "setup", "box", "--stage", "borrowed-claude")), " --limit box --tags borrowed-claude")

    def test_a_dry_run_of_setup_is_ansibles_check_mode_and_no_effect_of_wks(self):
        w = world()
        before = w.state()
        dry = self.ok(w.wk("--dry-run", "machine", "setup", "lin"))
        self.assertEqual((dry.effects, dry.plan, w.state()), ([], [], before))
        self.assertIn(PLAYBOOK + " --limit lin --check --diff" + SITE, dry.calls)

    def test_key_sync_is_the_credentials_role(self):
        w = world()
        self.played(self.ok(w.wk("key", "sync", "lin")), " --limit lin --tags credentials")
        self.played(self.ok(w.wk("key", "sync")), " --tags credentials")


class TestYourServers(EndToEnd):
    def journey(self, name, wrapper, removed):
        w = world()
        self.started(w, name)
        self.assertEqual(self.agent(w, name).err, "==> starting claude in x: credentials are hidden by %s\n" % wrapper)
        doctor = self.ok(w.wk("doctor"))
        self.assertEqual([line for line in doctor.out.splitlines() if not line.startswith(" ")], ["desk", "box", "lin", "mac", "vault"])
        self.assertEqual([line for line in doctor.out.splitlines() if line.startswith(" ") and not line.startswith("  ok  ")], [])
        self.assertEqual(self.ok(w.wk("doctor", "x")).out, "x (workspace on %s)\n  ok       credential wrapper credentials are hidden by %s\n"
                         % (name, wrapper))
        self.assertIn("error: workspace x has a running task (x:build:jsc-release)", self.ok(w.wk("rm", "x", "--yes"), 2).err)
        self.ok(w.wk("build", "x", "--kill"))
        self.assertEqual(self.ok(w.wk("rm", "x", "--yes")).effects, ["[%s] remove %s/.ready" % (name, WS)] + removed + ["[%s] remove %s" % (name, WS)])
        gc = self.ok(w.wk("gc"))
        self.assertEqual((gc.out, gc.effects), ("nothing to reclaim\n", []))
        w.machines[name].files[HOME + "/cache/pueue-3.4.1"] = "old"
        self.assertEqual(self.ok(w.wk("gc", "--yes")).effects, ["[%s] remove %s/cache/pueue-3.4.1" % (name, HOME)])

    def test_a_linux_server_from_provisioned_to_a_build_and_an_agent_in_its_container_and_back(self):
        self.journey("lin", "bwrap", ["[lin] podman rm --force --ignore wk-x"])

    def test_a_mac_server_from_provisioned_to_a_build_and_an_agent_in_its_checkout_and_back(self):
        self.journey("mac", "a Seatbelt profile", [])

    def test_the_credential_server_holds_no_workspace_and_is_asked_only_for_the_injector_and_the_store(self):
        w = world()
        self.assertIn("error: vault holds no workspace: it is the credential server", self.ok(w.wk("new", "x", "--on", "vault"), 1).err)
        self.assertIn("vault\n  ok       reachable\n  ok       injector       it holds the claude.ai login\n"
                      "  ok       openbao        HTTP 200, initialized True, sealed False\n", self.ok(w.wk("doctor")).out)

    def test_wk_typed_inside_the_container_queues_a_build_the_servers_daemon_starts(self):
        w = world()
        self.ok(w.wk("new", "x", "--on", "lin"))
        w.machines["lin"].files[INSIDE] = ""
        run = self.ok(w.wk("build", "x", "jsc-release", "--detach", on="lin", env={"WK_WORKSPACE": "x"}))
        self.assertEqual([call.split(" 'if ")[0] for call in run.calls if "podman" in call.split(" add ")[0] or "sh -c" in call],
                         ["[lin] sh -c 'cd \"$0\" && exec \"$@\"' %s sh -c" % SRC])
        self.assertEqual(added(w, "lin")[0][:len(EXEC["lin"])], EXEC["lin"])


class TestABorrowedBox(EndToEnd):
    def test_it_builds_niced_and_claude_started_from_the_client_reaches_the_injector_through_the_forward_with_no_credential_there(self):
        w = world()
        self.started(w, "box")
        run = self.agent(w, "box", FORWARDED)
        self.assertEqual(run.err, "==> starting claude in x: no credential is delivered to a borrowed box, so the wrapper is the minimal "
                                  "environment alone\n")
        box = w.machines["box"]
        handed = [call for call in box.calls if call.argv[:2] == ("wk", "ai")][0]
        self.assertEqual((handed.forward, handed.env), ((PORT, "vault:3128"), {"WK_FORWARD": str(PORT)}))
        self.assertEqual([path for path in box.files if path.startswith(SECRETS + "/") or path.startswith("/home/u/.pi")], [])
        self.assertIn("  ok       claude login\n", self.ok(w.wk("doctor")).out)

    def test_claude_typed_on_the_box_itself_is_refused_naming_the_client(self):
        w = world()
        self.ok(w.wk("new", "x", "--on", "box"))
        r = self.ok(w.wk("ai", "claude", "x", on="box"), 4)
        self.assertIn("    start it from the client: wk ai claude x\n", r.err)
