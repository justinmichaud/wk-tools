"""The lanes as one program: every real lane (the drivers, the pueue client, delivery, the agents, provisioning) typed
on the client over fake machines, for a Linux container server, a Mac and a borrowed box. Only the machines, the
daemon behind pueue, the store and HTTP are scripted."""

import hashlib
import json
import shlex
import unittest

from tests import ROOT, provision
from tests.credsworld import CA_PEM, MODELS, TOKEN, URL, Http, login, private_key
from tests.pueue import LINUX, MAC, FakePueue, added
from tests.world import CONFS, World
from wk import tasks, workspace
from wk.agent import install
from wk.agent.wrapper import wrap
from wk.core.machine import Result
from wk.creds import deliver
from wk.tasks import hooks as pueue_setup
from wk.tasks import sizing
from wk.workspace.drivers import INSIDE

HOME = "/home/u/wk"
WS = HOME + "/ws/x"
SRC, OUT = WS + "/src", WS + "/build"
MIRROR = HOME + "/git/WebKit.git"
SECRETS = HOME + "/secrets"
PUEUE = "%s/cache/pueue-4.0.5/pueue --config %s/pueue/pueue.yml" % (HOME, HOME)
IMAGE = "ghcr.io/igalia/wkdev-sdk:latest"
UNAME = {"linux": "Linux x86_64\n", "borrowed": "Linux aarch64\n", "mac": "Darwin arm64\n"}
SEEN = "HOME=/home/u\0USER=u\0PATH=/usr/bin:/bin\0TERM=dumb\0SSH_AUTH_SOCK=/tmp/agent\0GH_TOKEN=leaked\0"
RELEASE = {pueue_setup.URL % (pueue_setup.VERSION, tool, target): digests[tool]
           for target, digests in pueue_setup.ASSETS.values() for tool in digests}
ANSWERS = {MODELS: (200, {"data": [{"id": "big"}]}), ("GET", "http://vault:3128/"): (200, b"wk injector\nlogin held\n")}
CLIENT = {"BAO_ADDR": "https://vault:8200", "BAO_TOKEN": "s.token"}
STAGES = {"lin": ["wk", "pueue", "pueued", "build group", "mirror", "build environment", "claude", "pi", "build-tool shims",
                  "agent config", "credentials"],
          "mac": ["wk", "pueue", "pueued", "build group", "mirror", "claude", "pi", "build-tool shims", "agent config", "credentials"],
          "box": ["wk", "pueue", "pueued", "build group", "mirror", "claude", "pi", "build-tool shims", "agent config"],
          "vault": ["wk", "injector"]}
CLAUDE_ENV = {"HTTPS_PROXY": "http://vault:3128", "https_proxy": "http://vault:3128", "NODE_EXTRA_CA_CERTS": HOME + "/injector-ca.pem",
              "CLAUDE_SECURESTORAGE_CONFIG_DIR": HOME + "/claude-login", "DISABLE_AUTOUPDATER": "1", "HOME": "/home/u", "USER": "u",
              "PATH": "%s/shims:%s/bin:/usr/bin:/bin" % (HOME, HOME), "TERM": "dumb", "WK_AGENT": "1", "WK_WORKSPACE": "x"}
CLAUDE = install.claude_bin(HOME)


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
    """Script the bare machine `name`: each tool answers from the fake's files, as the real one answers from its disk."""
    m, kind = w.machines[name], w.confs[name].kind
    f, m.dirs = m.files, {}
    provision.server(m, shell="/bin/zsh" if kind == "mac" else "/bin/bash")

    def held(call):
        out = {}
        for path in call.argv[4:]:
            out[path] = ([m.dirs[path], None] if path in m.dirs else
                         [m.modes[path], hashlib.sha256(f[path].encode()).hexdigest()] if path in f else None)
        return json.dumps(out)

    def install_d(call):
        for path in call.argv[4:]:
            f[path], m.dirs[path] = provision.LISTED[call.argv[3]], call.argv[3]

    def move(call):
        for path in [p for p in f if p.startswith(call.argv[4] + "/")]:
            f[call.argv[5] + path[len(call.argv[4]):]] = f.pop(path)

    def place(call):
        part, path, _ = call.argv[4:]
        f[path] = f.pop(part)
    m.on(("python3", "-I", "-c", deliver.STATUS), held)
    m.on(("install", "-d", "-m"), install_d)
    m.on(("id", "-un"), "u\n")
    if w.confs[name].role:
        def systemctl(call):
            verb = call.argv[2]
            if verb in ("enable", "restart"):
                f["/systemd/" + verb] = ""
            return Result(0 if verb not in ("is-enabled", "is-active") or "/systemd/" + {"is-enabled": "enable", "is-active": "restart"}[verb] in f else 1)
        m.on(("systemctl", "--user"), systemctl)
        m.on(("loginctl", "enable-linger"), lambda call: f.update({"/var/lib/systemd/linger/u": ""}))
        return
    w.pueue[name] = FakePueue(m, w.clock, up=False)
    m.on(("uname", "-sm"), UNAME[kind])
    m.on(("python3", "-I", "-c", pueue_setup.SHA256), lambda call: f.get(call.argv[4]) or Result(1, "", "No such file"))
    m.on(("python3", "-I", "-c", pueue_setup.PLACE), place)
    m.on(("sh", "-c", pueue_setup.START), lambda call: w.pueue[name].start())
    m.on(("curl",), lambda call: f.update({call.argv[4]: RELEASE.get(call.argv[5], "installer")}))
    m.on(("git", "init"), lambda call: f.update({call.argv[-1] + ("/HEAD" if "--bare" in call.argv else "/.git/config"): ""}))
    m.on(("git", "clone"), lambda call: f.update({call.argv[-1] + "/.git/HEAD": "ref: refs/heads/main\n"}))
    m.on(("git", "-C", MIRROR, "rev-parse"), lambda call: Result(0 if MIRROR + "/refs/heads/main" in f else 1))
    m.on(("git", "-C", MIRROR, "fetch"), lambda call: f.update({MIRROR + "/refs/heads/main": "5e1f\n"}))
    m.on(("bash",), lambda call: f.update({"%s/.local/share/claude/versions/%s" % (call.env["HOME"], call.argv[2]): "claude"}))
    m.on(("sh", "-c", install.MOVE), move)
    m.on(("node", "--version"), "v22.19.0\n")
    m.on(("npm",), lambda call: f.update({call.argv[4] + "/node_modules/.bin/pi": "pi"}))
    m.on(("sh", "-c", 'cd "$0" && exec "$@"'), lambda call: inside(w, name, list(call.argv[4:])))
    if kind == "linux":
        f["/etc/subuid"] = f["/etc/subgid"] = "u:165536:65536\n"
        m.on(("podman", "--version"), "podman version 5.4.0\n")
        m.on(("podman", "image", "exists"), lambda call: Result(0 if "/podman/image" in f else 1))
        m.on(("podman", "pull"), lambda call: f.update({"/podman/image": call.argv[2]}))
        m.on(("podman", "container", "exists"), lambda call: Result(0 if "/podman/" + call.argv[3] in f else 1))
        m.on(("podman", "container", "inspect"), lambda call: f["/podman/" + call.argv[5]])
        m.on(("podman", "run"), lambda call: f.update({"/podman/" + call.argv[call.argv.index("--name") + 1]: "running\n"}))
        m.on(("podman", "rm"), lambda call: f.pop("/podman/" + call.argv[4], None) and None)
        m.on(("podman", "exec"), lambda call: inside(w, name, list(call.argv[call.argv.index("wk-x") + 1:])))


def world():
    """The fleet with nothing set up: the client's checkout is clean, and the store holds every credential."""
    w = World(confs=dict(CONFS, desk="kind=client\nlitellm_url=%s" % URL), seams={"driver": workspace.driver, "tasks": tasks.tasks})
    w.pueue = {}
    provision.checkout(w.machines["desk"])
    provision.client(w.machines["desk"])
    w.machines["desk"].files[ROOT + "/lib/wk/creds/injector.py"] = "the injector's source\n"
    for name in ("lin", "mac", "box", "vault"):
        machine(w, name)
    w.secrets.values.update({"github-user": "me", "github-token": TOKEN, "bugzilla-user": "me@example.test", "bugzilla-password": "pw",
                             "litellm-key": "sk-1", "claude-login": login(), "deploy-key/Me/WebKit": private_key()})
    return w


EXEC = {"lin": ["podman", "exec", "--interactive", "--workdir", SRC, "wk-x"], "mac": ["sh", "-c", 'cd "$0" && exec "$@"', SRC],
        "box": ["sh", "-c", 'cd "$0" && exec "$@"', SRC]}
EXEC_TTY = dict(EXEC, lin=["podman", "exec", "--interactive", "--tty", "--workdir", SRC, "wk-x"])
RUN = ("[lin] podman run --detach --replace --pull=never --name wk-x --init --userns=keep-id --uts=host --pids-limit=-1 "
       "--security-opt seccomp=unconfined --volume /home/u:/home/u --volume {h}:{h} --volume {m}:{m}:ro --env HOME=/home/u "
       "--env WK_WORKSPACE=x --workdir {s} {i} sleep infinity").format(h=HOME, m=MIRROR, s=SRC, i=IMAGE)
MODE = {"lin": ["--dangerously-skip-permissions"], "mac": ["--dangerously-skip-permissions"], "box": ["--permission-mode", "auto"]}
BUILD = {"lin": ["24576", "1", "env", "WK_BUILD=1", "CC=clang"], "mac": ["30720", "0", "env", "WK_BUILD=1", "WEBKIT_OUTPUTDIR=" + OUT],
         "box": ["12288", "0", "nice", "-n", "19", "ionice", "-c", "3", "env", "WK_BUILD=1", "CC=clang"]}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        patch = Http(ANSWERS).patch()
        patch.start()
        self.addCleanup(patch.stop)

    def ok(self, run, code=0):
        self.assertEqual(run.code, code, run.err + run.out)
        return run

    def set_up(self, w, name):
        """Setup runs every lane's stage in order and changes each; typed again it changes nothing."""
        first = self.ok(w.wk("machine", "setup", name))
        self.assertEqual([line.rsplit(None, 1)[0].rstrip() for line in first.out.splitlines() if line.endswith("  changed")],
                         STAGES[name])
        self.assertEqual(first.out.splitlines()[-1], "%s: %d changed" % (name, len(STAGES[name])))
        if name == "vault":
            w.machines["vault"].files[HOME + "/injector/ca.pem"] = CA_PEM       # the running injector makes its CA
        again = self.ok(w.wk("machine", "setup", name))
        self.assertEqual((again.effects, again.out.splitlines()[-1]), ([], "%s: no changes" % name))
        self.assertNotIn("  changed", again.out)

    def fleet(self):
        w = world()
        for name in ("vault", "lin", "mac", "box"):
            self.set_up(w, name)
        return w

    def started(self, w, name):
        """`wk new x`, a detached build and its status, on the server `name`; returns the build's line."""
        made = self.ok(w.wk("new", "x", "--on", name))
        self.assertEqual(made.effects[0], "[%s] git clone --shared --no-tags -c fetch.writeCommitGraph=false -c gc.writeCommitGraph=false %s %s"
                         % (name, MIRROR, SRC))
        self.assertEqual(made.effects[6:], ["[%s] mkdir -p %s" % (name, OUT)] + [RUN] * (name == "lin") + ["[%s] write %s/.ready" % (name, WS)])
        self.assertEqual(self.ok(w.wk("ls")).out.split(), ["x", name, w.confs[name].driver, "ready"])
        built = self.ok(w.wk("build", "x", "jsc-release", "--detach"))
        self.assertEqual(built.effects[:-1], ["[%s] write %s/guard.py" % (name, OUT)])
        self.assertIn("%s add --print-task-id --group build --label x:build:jsc-release " % PUEUE, built.effects[-1])
        line = added(w, name)[0]
        self.assertEqual(line[:len(EXEC[name]) + 3 + len(BUILD[name])], EXEC[name] + ["python3", "-I", OUT + "/guard.py"] + BUILD[name])
        status = self.ok(w.wk("status", "x"), 2)
        self.assertEqual(status.out, "running  x:build:jsc-release            0m00s\n")
        return line

    def agent(self, w, name):
        """`wk ai claude x`: measured, then started under the kind's wrapper with the injector's environment."""
        run = self.ok(w.wk("ai", "claude", "x"))
        start = EXEC_TTY[name] + wrap(w.confs[name].kind, [CLAUDE] + MODE[name] + ["--remote-control", "x"], [SECRETS], CLAUDE_ENV)
        self.assertEqual(run.effects, ["[%s] write /home/u/.claude.json" % name, "[%s] %s" % (name, shlex.join(start))])
        for hidden in [var for var, _ in deliver.ENV] + ["SSH_AUTH_SOCK", "leaked"]:
            self.assertNotIn(hidden, run.effects[-1])
        self.assertEqual(CLAUDE_ENV["PATH"].split(":")[:2], [HOME + "/shims", HOME + "/bin"])
        return run


class TestYourServers(EndToEnd):
    def journey(self, name, wrapper, removed):
        w = self.fleet()
        synced = self.ok(w.wk("key", "sync", name))
        self.assertEqual((synced.effects, [line.split()[1] for line in synced.out.splitlines()]), ([], ["unchanged"] * 8))
        self.started(w, name)
        self.assertEqual(self.agent(w, name).err, "==> starting claude in x: credentials are hidden by %s\n" % wrapper)
        doctor = self.ok(w.wk("doctor", env=CLIENT))
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

    def test_a_linux_server_from_bare_to_a_build_and_an_agent_in_its_container_and_back(self):
        self.journey("lin", "bwrap", ["[lin] podman rm --force --ignore wk-x"])

    def test_a_mac_server_from_bare_to_a_build_and_an_agent_in_its_checkout_and_back(self):
        self.journey("mac", "a Seatbelt profile", [])

    def test_wk_typed_inside_the_container_queues_a_build_the_servers_daemon_starts(self):
        w = self.fleet()
        self.ok(w.wk("new", "x", "--on", "lin"))
        w.machines["lin"].files[INSIDE] = ""
        run = self.ok(w.wk("build", "x", "jsc-release", "--detach", on="lin", env={"WK_WORKSPACE": "x"}))
        self.assertEqual([call.split(" 'if ")[0] for call in run.calls if "podman" in call.split(" add ")[0] or "sh -c" in call],
                         ["[lin] sh -c 'cd \"$0\" && exec \"$@\"' %s sh -c" % SRC])
        self.assertEqual(added(w, "lin")[0][:len(EXEC["lin"])], EXEC["lin"])

    def test_a_dry_run_of_setup_on_a_bare_server_prints_every_stage_s_plan_and_changes_nothing(self):
        w = world()
        before = w.state()
        dry = self.ok(w.wk("--dry-run", "machine", "setup", "lin"))
        self.assertEqual((dry.effects, w.state()), ([], before))
        self.assertEqual(dry.plan, [effect.split("\n")[0] for effect in self.ok(world().wk("machine", "setup", "lin")).effects])
        self.assertIn("[lin] %s group add build --parallel 1" % PUEUE, dry.plan)


class TestABorrowedBox(EndToEnd):
    def test_it_is_set_up_builds_niced_and_runs_an_agent_under_the_minimal_environment_with_nothing_delivered(self):
        w = self.fleet()
        self.assertEqual(self.ok(w.wk("key", "sync", "box")).out.split()[:2], ["box", "skipped"])
        self.started(w, "box")
        self.assertEqual(self.agent(w, "box").err,
                         "==> starting claude in x: no credential is delivered to a borrowed box, so the wrapper is the minimal environment alone\n")
        box = w.machines["box"]
        self.assertEqual([path for path in box.files if path.startswith(SECRETS + "/") or "claude-login" in path or path.endswith(".pem")], [])
