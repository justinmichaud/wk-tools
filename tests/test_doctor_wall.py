"""`wk doctor <workspace>`: the wall's report on one workspace, typed into a world holding container workspace `ws` (or a
macOS guest) whose probes are answered from HEALTHY, keyed by a substring of the command each runs."""
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from tests.fakes import Result
from tests.test_wk_workspace import Flow, World, said

PERSON = "/opt/wk-tools/container/bin/wk-person"
LOGIN = '{"claudeAiOauth": {"accessToken": "%s", "refreshToken": "wk-injects-this"}}'
# Most specific first: the first key found in the command answers it; a `person:` key answers a person's run of it.
HEALTHY = [
    ("sk-ant-o[ar]t", ""),
    ("/.credentials.json", LOGIN % "wk-injects-this"),
    ("api.anthropic.com/v1/models", 'HTTP/1.1 200 OK\r\n\r\n{"data": []}\n200'),
    ("hosts.yml", ""),
    ("https://github.com/ 2", "200"),
    ("example.com", "curl: (56) Received HTTP code 403 from proxy after CONNECT"),
    ("192.168.1.1", "403"),
    ("1.1.1.1", "000"),
    ("swscan", "curl: (56) Received HTTP code 403\ncurl: (56) Received HTTP code 403"),
    ("/proc/net/dev", "lo "),
    ("command -v bwrap", "WKBWRAP"),
    ("mktemp -d /tmp/wk-wall", "BLOCKED\nWROTE"),
    ("GITHUB_COM_TOKEN", "wk-injects-this"),
    ("GH_TOKEN", "wk-injects-this"),
    ("BUGS_WEBKIT_ORG_PASSWORD", "wk-injects-this"),
    ("person:git ls-remote", "0123abcd\trefs/heads/main"),
    ("git ls-remote", "error: this caller descends from a run of wk's own (pid 9: bash -lc git ls-remote), not from a shell of yours\n"
                      "    a publish is made from a shell of your own:  wk enter ws"),
    ("git config --global --get core.sshCommand", "/opt/wk-tools/container/push/wk-push-client.py"),
    ("https://api.github.com/ 2", "200"),
    ("api.github.com/user", "200"),
    ("person:/pulls", "422"),
    ("/pulls", "412"),
    ("/git/refs", "422"),
    ("claude auth status", '{"loggedIn": true, "authMethod": "claude.ai"}'),
    ("webkitscmpy.setup", "true"),
    ("rest/version", "200"),
    ("http_code}' -X POST -H", "412"),
    ("gpu-probe.sh", Result(0, "renderer=NVIDIA Tegra | vendor=NVIDIA\n")),
    ("touch /opt/wk-tools/.wk-write-probe", "touch: cannot touch '/opt/wk-tools/.wk-write-probe': Read-only file system"),
]


class Walled(World):
    """A host whose podman is rootless with the egress proxy up; a guest is reached with `tart exec`. A value in `said` may
    be a function of the command, for a probe the test runs for real."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.said = dict(HEALTHY)
        self.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], out="true\n")
        self.answer(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"])
        if getattr(self, "tart", None):
            self.react([self.tart, "exec"], lambda a, f: f._in_ws(["exec", a[3][3:]] + shlex.split(a[-1]), f))

    def _in_ws(self, argv, f):
        if argv[-2:-1] not in (["-lc"], ["-c"]):
            return Result(1, "", "no answer in the workspace for: %s" % argv[2:])
        cmd, person = argv[-1], PERSON in argv
        found = [v for k, v in self.said.items() if (k[7:] in cmd and person if k.startswith("person:") else k in cmd)]
        out = found[0] if found else ""
        out = out(cmd) if callable(out) else out
        # curl's `-D -` through the proxy prints its CONNECT reply first, unless suppressed.
        connect = "HTTP/1.0 200 Connection established\r\n\r\n" if "curl" in cmd and "-D -" in cmd and "--suppress-connect-headers" not in cmd else ""
        return out if isinstance(out, Result) else Result(0, connect + out)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def report(err):
    """Each row as a person reads it: its mark (ok, --, ?? or .. for a note) and its first line, the remedy after ->."""
    rows = []
    for line in (ANSI.sub("", l) for l in err.splitlines()):
        m = re.match(r"  (ok|--|\?\?)    (.*)$", line)
        if m:
            rows.append(m.group(1) + " " + re.sub(r" +-> ", " -> ", m.group(2), count=1))
        elif line.startswith("        "):
            rows.append(".. " + line.strip())
    return rows


class WallTest(Flow):
    world_class = Walled
    kinds = None

    def setUp(self):
        super().setUp()
        if self.kinds:
            self.w = self.make_world(kinds=self.kinds)
        self.w.make("ws")

    def doctor(self, said=(), *flags):
        """`wk doctor ws <flags>` with HEALTHY changed by `said`: (exit status, rows, the commands asked in the workspace)."""
        self.w.said = {**dict(HEALTHY), **dict(said)}
        self.w.env["WK_NAME"] = "ws"   # what the dispatcher exports, read back from the registry's environment
        rc, err = self.typed("doctor", "ws", *flags)
        asked = [e[1][-1] for e in self.w.effects if e[0] == "run" and "-lc" in e[1]]
        return rc, report(err), [shlex.split(c)[-1] if c.startswith(("bash -lc ", "sh -c ")) else c for c in asked]

    def misses(self, rows):
        return [r for r in rows if r.startswith(("--", "??"))]


INJECT = "systemctl --user status wk-github-inject; the CA is /run/wk/wk-github-ca.pem"
JOURNAL = "the push service's journal on the host: journalctl --user -u wk-push"
REMAKE = "a container is given the placeholder when it is made ('wk rm ws' and 'wk new'), a guest at every 'wk start ws'"
OWN_WRITE = ("a 401 is an injector still running older code, which 'wk status' reports and './setup' on that machine restarts; "
             "anything else is the write socket serving what is not a person's shell: the push service and the egress proxy")
PERSON_WRITE = ("expected GitHub's 422; a 412 is the push service not taking it for a person's, so 'git-webkit pr' fails for a "
                "person too -> the push service where the workspace's place runs it ('./setup' converges it)")
BZ_WRITE = ("expected 412, the injector's own refusal -> Bugzilla's own 'log in first' is an injector still running older code, "
            "which 'wk status' reports and './setup' on that machine restarts; anything else is the write socket serving what is "
            "not a person's shell")
OUTAGE = ".. %s did not answer through the injector (HTTP %s) -- an upstream outage, not the sandbox"
FAULT = "HTTP/1.1 502 Bad Gateway\r\nX-Wk-Injector: SSLCertVerificationError\r\nContent-Length: 5\r\n\r\n502"
FAULTED = ("-- %s: the injector failed to verify or resolve the host (SSLCertVerificationError) -- a fault in the injector, not "
           "the upstream -> " + INJECT)
TOKEN_SET = ("-- $CLAUDE_CODE_OAUTH_TOKEN is set in this workspace as well as the placeholder claude.ai login, and the token "
             "wins: every session authenticates as an inference-only credential and remote control refuses to start -> nothing "
             "should put it here (/opt/wk-tools/shell/bashrc exports it only where the delivery column sends it); 'wk rm ws' and "
             "'wk new' remake the workspace without it")

# HEALTHY changed by `said`, `wk doctor ws` with `flags`: its exit status, and every row it misses on.
FAILS = (
    ({"https://github.com/ 2": "000"}, (), 1, ["-- github unreachable through the proxy (got '000') -> systemctl --user status wk-proxy"]),
    ({"example.com": ""}, (), 1, ["-- example.com was NOT refused -> the allowlist is not being enforced"]),
    ({"192.168.1.1": "200"}, (), 1, ["-- reaching the LAN gateway returned '200' -> the local network must be refused"]),
    ({"1.1.1.1": "200"}, (), 1, ["-- direct egress succeeded, bypassing the proxy: '200' -> everything must go through the proxy"]),
    ({"/proc/net/dev": "lo eth0 "}, (), 1, ["-- workspace has network interfaces: lo eth0 -> a workspace has loopback only"]),
    ({"/proc/net/dev": ""}, (), 1, ["-- could not enumerate interfaces inside the workspace -> /proc/net/dev did not answer"]),
    ({"command -v bwrap": ""}, (), 1, ["-- no bwrap in the workspace -> 'wk ai claude' cannot wall off commits and refuses to start"]),
    ({"mktemp -d /tmp/wk-wall": "COMMITTED\nWROTE"}, (), 1,
     ["-- commit wall did NOT block a commit under bwrap: 'COMMITTED WROTE' -> lib/wk/wall.py, commit_wall_prefix"]),
    ({"mktemp -d /tmp/wk-wall": "BLOCKED\nNOWRITE"}, (), 1, ["-- commit wall blocks an ordinary write too -> too much: lib/wk/wall.py, commit_wall_prefix"]),
    ({"PRIVATE KEY": "/home/u/.ssh/id_fork"}, (), 1, ["-- private key material inside the workspace: /home/u/.ssh/id_fork -> remove it"]),
    ({"hosts.yml": "/home/u/.config/gh/hosts.yml"}, (), 1, ["-- GitHub credential inside the workspace: /home/u/.config/gh/hosts.yml -> "
                                                            "rm ~/.config/gh/hosts.yml; unset the token, before an agent runs"]),
    ({"test -r /secrets/claude-token": "yes"}, (), 1,
     ["-- /secrets/claude-token is readable in 'ws' and the delivery table gives claude to no container place -> what a workspace "
      "mounts holds only what it is given (Secrets.publish_view, lib/wk/secrets.py)"]),
    ({"test -S /run/wk/ssh-agent.sock": "agent"}, (), 3, ["-- an ssh-agent reaches this workspace (agent) -> no key may: nothing but the push service holds one"]),
    ({"test -S /run/wk/ssh-agent.sock": "env-keys"}, (), 3, ["-- an ssh-agent reaches this workspace (env-keys) -> no key may: nothing but the push service holds one"]),
    ({"git config --global --get core.sshCommand": ""}, (), 3,
     ["-- git's ssh transport in 'ws' is not the push client (core.sshCommand) -> 'wk rm ws' and 'wk new' remake the workspace "
      "with it; a guest is rewritten at every 'wk start ws'"]),
    ({"git ls-remote": "Permission denied (publickey).", "person:git ls-remote": "Permission denied (publickey)."}, (), 3,
     ["-- a push from wk's own run was not refused: Permission denied (publickey). -> the push service on the host: 'wk doctor' "
      "there says whether it answers", "-- a push from a person's shell was refused or failed: Permission denied (publickey). -> " + JOURNAL]),
    ({"git ls-remote": "error: this caller is in no workspace\n    ask the host", "person:git ls-remote": "error: this caller is in no workspace"}, (), 3,
     ["-- the push service refused wk's own run for 'this caller is in no workspace' before deciding who it is, so whether its "
      "push is refused is unmeasured -> " + JOURNAL,
      "-- a push from a person's shell was refused or failed: error: this caller is in no workspace -> " + JOURNAL]),
    ({"person:git ls-remote": "error: no shell of yours is behind this caller (pid 9)"}, (), 3,
     ["-- a push from a person's shell was refused or failed: error: no shell of yours is behind this caller (pid 9) -> " + JOURNAL]),
    ({"git ls-remote": ""}, (), 3, ["-- a push from wk's own run was not refused: nothing said -> the push service on the host: "
                                    "'wk doctor' there says whether it answers"]),
    ({"api.github.com/user": "000"}, (), 1, ["-- a read answered '000' rather than 200 or 401 -> the injector is in the path but not answering for api.github.com/user"]),
    ({"api.github.com/user": "000", "https://api.github.com/ 2": "000"}, (), 1,
     ["-- api.github.com answered '000' -- the injector is not in the path -> " + INJECT]),
    ({"/pulls": "422"}, (), 3, ["-- a GitHub write from wk's own run answered '422' -- expected 412, the injector's own refusal -> " + OWN_WRITE]),
    ({"person:/pulls": "412"}, (), 3, ["-- a write from a person's shell answered '412' -- " + PERSON_WRITE]),
    ({"/pulls": "", "person:/pulls": ""}, (), 3, ["-- a GitHub write from wk's own run answered 'nothing' -- expected 412, the injector's own refusal -> " + OWN_WRITE,
                                                  "-- a write from a person's shell answered 'nothing' -- " + PERSON_WRITE]),
    ({"claude auth status": '{"loggedIn": false}'}, (), 1,
     ["-- 'claude auth status' in 'ws' says it is not logged in, so every session there stops at /login -> " + REMAKE]),
    ({"claude auth status": ""}, (), 1,
     ["-- 'claude auth status' in 'ws' answered something that is not a report, so whether an agent there can authenticate is "
      "unmeasured. It answered (first 80 bytes): b'' -> the CLI is too old to have the subcommand (it self-updates; 'wk enter ws' "
      "then 'claude --version'), or what it wrote is not the JSON this reads"]),
    ({"claude auth status": "wk-no-claude-cli"}, (), 1,
     ["-- no 'claude' on $PATH in 'ws', so no session there can run at all -> 'wk enter ws' then 'claude --version'. Whether an "
      "agent there can authenticate is unmeasured"]),
    ({"CLAUDE_CODE_OAUTH_TOKEN:+set": "set", "claude auth status": '{"loggedIn": true, "authMethod": "oauth_token"}'}, (), 1,
     [TOKEN_SET, "-- the agent in 'ws' authenticates as oauth_token, where a container place is given placeholder claude.ai login "
                 "and must report claude.ai -> two credentials reach it, or the wrong one does"]),
    *(({"/.credentials.json": login}, (), 1, ["-- ~/.wk-claude/.credentials.json in 'ws' is not the placeholder login -> " + REMAKE])
      for login in (LOGIN % "sk-ant-oat01-x", "", "[]")),
    ({"sk-ant-o[ar]t": "/run/wk/.credentials.json"}, (), 1,
     ["-- a claude.ai token is readable in 'ws': /run/wk/.credentials.json -> the injector is its one holder: remove it, and "
      "'wk key check' outside says whether the login still works"]),
    ({"api.anthropic.com/v1/models": 'HTTP/1.1 401 Unauthorized\r\n\r\n{"type": "error"}\n401'}, (), 1,
     ["-- Anthropic refused the claude.ai login the injector put on a request (HTTP 401) -> 'wk key check'; 'wk key set "
      "claude-login --replace' stores a new one"]),
    ({"api.anthropic.com/v1/models": "\n000"}, (), 1, ["-- api.anthropic.com answered '000' -- the injector is not in the path for it -> " + INJECT]),
    *(({"webkitscmpy.setup": v}, (), 1, ["-- 'git-webkit setup' has not completed in 'ws' (webkitscmpy.setup is not true): `git-webkit "
                                          "pr` prompts or refuses -> 'wk sync ws --fix' re-asserts the remotes and runs it"]) for v in ("", "false")),
    ({"rest/version": "000"}, (), 1, ["-- bugs.webkit.org answered '000' -- the injector is not in the path for it -> " + INJECT]),
    ({"http_code}' -X POST -H": "410"}, (), 3, ["-- POST /rest/bug from wk's own run answered '410' -- " + BZ_WRITE]),
    ({"gpu-probe.sh": Result(1, "renderer=llvmpipe\n")}, ("--gpu",), 1,
     ["-- only software rendering inside the workspace -> a benchmark on it would measure the software renderer"]),
    ({"gpu-probe.sh": Result(2)}, ("--gpu",), 1, ["-- no usable EGL inside the workspace (probe exit 2) -> a benchmark on it would measure the software renderer"]),
    ({"api.github.com/user": FAULT}, (), 1, [FAULTED % "GitHub"]),
    ({"rest/version": FAULT}, (), 1, [FAULTED % "Bugzilla"]),
    ({"http_code}' -X POST -H": FAULT}, (), 3, [FAULTED % "Bugzilla"]),
)
# HEALTHY changed by `said`: nothing missed, and these rows among what it reads.
PASSES = (
    ({"1.1.1.1": ""}, ["ok no direct egress with the proxy bypassed"]),
    ({"test -S /run/wk/ssh-agent.sock": "env"},
     ["ok $SSH_AUTH_SOCK names the machine's own ssh-agent, which holds no identity: no key reaches this workspace"]),
    ({"gpu-probe.sh": Result(1, "renderer=llvmpipe\n")},
     [".. renderer=llvmpipe", ".. only software rendering inside the workspace -- benchmarks would measure llvmpipe"]),
    ({"gpu-probe.sh": Result(2)}, [".. no usable EGL inside the workspace (probe exit 2)"]),
    ({"api.github.com/user": "401"}, [".. reads are unauthenticated (HTTP 401): no standing read token where the injector reads one, so "
                                      "a workspace sees public GitHub at 60 requests an hour ('wk key set github-pat', then './setup')"]),
    ({"https://api.github.com/ 2": "401", "api.github.com/user": "401"},
     [".. GitHub refused the standing read token the injector holds (HTTP 401), so every API read in here is refused with it -- "
      "'gh', and 'git-webkit pr', which reports it as a token of its own being out of date. A token this machine holds and GitHub "
      "still accepts ('wk key check github-pat') means the injector was handed an older one: 'wk start ws' converges a guest's copy "
      "and './setup' the podman machine's. One refused there too is 'wk key set github-pat --replace'"]),
    ({"api.github.com/user": "HTTP/1.1 502 Bad Gateway\r\nContent-Length: 5\r\n\r\n502"}, [OUTAGE % ("GitHub", 502)]),
    ({"api.github.com/user": "504"}, [OUTAGE % ("GitHub", 504)]),
    ({"/pulls": "504"}, [OUTAGE % ("GitHub", 504)]),
    ({"rest/version": "504"}, [OUTAGE % ("Bugzilla", 504)]),
    ({"http_code}' -X POST -H": "504"}, [OUTAGE % ("Bugzilla", 504)]),
    ({"api.anthropic.com/v1/models": "HTTP/1.1 401 Unauthorized\r\n\r\nthe wk credential injector put no claude.ai login on this "
                                     "request: this machine holds no claude.ai login\n401"},
     [".. no claude.ai session here can authenticate: the wk credential injector put no claude.ai login on this request: this "
      "machine holds no claude.ai login", ".. 'wk key check' says what this machine holds"]),
    ({"api.anthropic.com/v1/models": "HTTP/1.1 504 Gateway Timeout\r\n\r\napi.anthropic.com did not answer\n504"}, [OUTAGE % ("Anthropic", 504)]),
    ({"claude auth status": '\x1b7\x1b[r\x1b8\x1b[?25h{\r\r\n\x1b[3G"loggedIn":\x1b[15Gtrue,\r\r\n\x1b[3G"authMethod":\x1b[17G'
                            '"claude.ai"\r\r\n}\r\r\n\x1b[?25h\x1b[?1006l\x1b(B\x0f\x1b[>4m\x1b[<u\x1b[?1004l\x1b7\x1b[r\x1b8\x1b[?25h\n'},
     ["ok the agent in 'ws' reports itself logged in (claude.ai) from the placeholder claude.ai login credential this machine "
      "delivered -- no /login, no browser; whether Anthropic accepts it is the injector row's measurement"]),
)

HEALTHY_CONTAINER = [
    "ok workspace running", "ok github reachable through the proxy (HTTP 200)", "ok a host outside the allowlist is refused",
    "ok the local network is refused", "ok no direct egress with the proxy bypassed", "ok no network interface but loopback (lo )",
    "ok no host home, runtime directory or D-Bus socket", "ok commit wall blocks a commit; an ordinary write still works (bwrap)",
    "ok the claude.ai login in here is the placeholder the injector swaps",
    "ok no claude.ai token is readable in here (the login, ~/.claude, /secrets, /run/wk)",
    "ok the placeholder login is authenticated through the injector (HTTP 200), from a login no workspace holds",
    "ok no private key material in the workspace (~/.ssh, ~/.claude, the top of home, /secrets, /run/wk)",
    "ok GITHUB_COM_TOKEN in the workspace is the placeholder", "ok GH_TOKEN in the workspace is the placeholder",
    "ok BUGS_WEBKIT_ORG_PASSWORD in the workspace is the placeholder", "ok no stored GitHub credential inside: gh has only the placeholder",
    "ok no credential this kind is not given is readable in here (claude)",
    "ok no ssh-agent socket and no $SSH_AUTH_SOCK: no key reaches this workspace",
    "ok git's ssh transport is the push client, so a push is the host's service's to allow",
    "ok a push from wk's own run is refused: this caller descends from a run of wk's own (pid 9: bash -lc git ls-remote), not from a shell of you",
    "ok a push from a person's shell is served by the push service",
    "ok a read is authenticated (HTTP 200) from a token this workspace never holds",
    "ok a write from wk's own run is refused by the injector (HTTP 412)",
    "ok a write from a person's shell reaches GitHub (HTTP 422 for an empty request): the injector writes for it",
    "ok bugs.webkit.org reachable through the injector (HTTP 200)", "ok a Bugzilla write from wk's own run is refused by the injector (HTTP 412)",
    "ok git-webkit is set up in 'ws' (hooks, fork remote verified)",
    "ok the agent in 'ws' reports itself logged in (claude.ai) from the placeholder claude.ai login credential this machine delivered "
    "-- no /login, no browser; whether Anthropic accepts it is the injector row's measurement",
    ".. renderer=NVIDIA Tegra | vendor=NVIDIA", "ok GPU acceleration available",
    "ok podman is rootless", "ok egress proxy running", "ok /opt/wk-tools is read-only"]
PROBE = "GIT_TERMINAL_PROMPT=0 git ls-remote --heads git@%s.git 2>&1 | head -3"
PULLS = "curl -sS -m 40 --suppress-connect-headers -D - -o /dev/null -w '%{http_code}' -X POST -d '{}' https://api.github.com/repos/justinmichaud/WebKit/pulls 2>/dev/null"


class TestAContainersReport(WallTest):
    def test_a_healthy_container_passes_every_check_in_order(self):
        rc, rows, asked = self.doctor()
        self.assertEqual((rc, rows), (0, HEALTHY_CONTAINER))

    def test_each_wrong_answer_is_the_one_miss_naming_it_and_its_remedy(self):
        """One miss per wrong answer, a check asked as two publishers missing once for each; 3 is something could publish."""
        for said, flags, status, missed in FAILS:
            with self.subTest(said=said, flags=flags):
                rc, rows, _ = self.doctor(said, *flags)
                self.assertEqual((rc, self.misses(rows)), (status, missed))

    def test_each_answer_the_sandbox_is_not_at_fault_for_passes_and_says_so(self):
        for said, says in PASSES:
            with self.subTest(said=said):
                rc, rows, _ = self.doctor(said)
                self.assertEqual((rc, self.misses(rows), [r for r in says if r in rows]), (0, [], says))

    def test_each_host_path_is_probed_and_one_that_answers_is_named(self):
        rc, rows, asked = self.doctor({"ls -d": "/host/home"})
        self.assertEqual((rc, [r for r in rows if "host home" in r or "host path" in r]),
                         (1, ["-- host path visible inside the workspace: /host/home -> nothing of the host is mounted in"] * 4))
        self.assertEqual([c for c in asked if c.startswith("ls -d")], ["ls -d %s 2>/dev/null | head -1" % p for p in
                                                                      ("/host/home", "/host/run", "/run/user/*/bus", "/run/dbus/system_bus_socket")])

    def test_the_commit_wall_probe_walls_the_paths_an_agent_session_is_walled_from(self):
        probe = next(c for c in self.doctor()[2] if "mktemp" in c)
        self.assertIn('W="bwrap --dev-bind / / %s --"' % " ".join("--ro-bind-try $D/.git/{0} $D/.git/{0}".format(p) for p in
                      ("objects", "refs", "logs", "HEAD", "packed-refs", "index.lock", "ORIG_HEAD")), probe)

    def test_an_unset_placeholder_names_what_exports_it_and_a_real_token_is_never_printed(self):
        for var in ("GITHUB_COM_TOKEN", "GH_TOKEN", "BUGS_WEBKIT_ORG_PASSWORD"):
            with self.subTest(var=var):
                self.assertEqual(self.misses(self.doctor({var: ""})[1]), [
                    "-- %s is unset in here, so nothing that reads it sends a credential for the injector to replace -> "
                    "container/proxy/ensure-bridge.sh exports all three, from /secrets/github-user, /secrets/bugzilla-user and the "
                    "injector's CA; './setup' rewrites /secrets" % var])
                rc, rows, _ = self.doctor({var: "ghp-a-real-one"})
                self.assertEqual((rc, self.misses(rows)), (1, ["-- %s in the workspace is not the placeholder: something put a real "
                                                               "credential in here -> find it and remove it" % var]))
                self.assertNotIn("ghp-a-real-one", "\n".join(rows))

    def test_the_claude_login_is_sent_as_the_placeholder_and_the_token_scan_reads_no_old_mount(self):
        asked = self.doctor()[2]
        self.assertIn("-H 'Authorization: Bearer wk-injects-this'", next(c for c in asked if "api.anthropic.com" in c))
        scan = next(c for c in asked if "sk-ant-o[ar]t" in c)
        self.assertEqual([old for old in ("/agent-rw", "My Shared Files") if old in scan], [])

    def test_the_pr_tool_setup_is_asked_of_the_checkout(self):
        self.assertIn("git -C /src/WebKit config --get webkitscmpy.setup", self.doctor()[2])

    def test_an_arch_with_no_gpu_says_so_and_fails_only_with_gpu(self):
        self.w.files[os.path.join(self.w.ws_dir(), "arch")] = "armhf\n"
        rc, rows, _ = self.doctor()
        self.assertEqual((rc, [r for r in rows if "GPU" in r]), (0, [".. no GPU: an armhf workspace gets none (the NVIDIA userspace is aarch64-only)"]))
        rc, rows, _ = self.doctor((), "--gpu")
        self.assertEqual((rc, self.misses(rows)), (1, ["-- --gpu on an armhf workspace, which cannot have one -> a native workspace"]))


class TestPublishers(WallTest):
    def test_a_container_is_asked_as_wks_own_run_then_as_a_persons_shell_and_no_stand_in_agent_runs(self):
        self.doctor()
        asked = [(e[1][-1], PERSON in e[1]) for e in self.w.effects if e[0] == "run" and "-lc" in e[1]]
        self.assertEqual([a for a in asked if "git ls-remote" in a[0]], [(PROBE % "github-webkit:justinmichaud/WebKit", p) for p in (False, True)])
        self.assertEqual([a for a in asked if "/pulls" in a[0]], [(PULLS, False), (PULLS, True)])
        self.assertEqual([a for a, _ in asked if '"$d/claude"' in a], [])

    def test_the_one_repository_an_agent_publishes_is_served_to_wks_own_run_and_has_no_commit_wall_or_pr_tool(self):
        self.w.files[os.path.join(self.w.ws_dir(), "home", ".wk-workspace")] = "repo=wk-tools\n"
        self.w.answer(["git", "-c", "safe.directory=*", "-C", "/opt/wk-tools", "remote", "get-url", "origin"], out="https://github.com/u/wk-tools.git\n")
        rc, rows, asked = self.doctor({"git ls-remote": "0123abcd\trefs/heads/main"})
        self.assertEqual((rc, [r for r in rows if "a push of" in r]), (0, [
            "ok a push of u/wk-tools from wk's own run is served: the one repository an agent publishes",
            "ok a push of u/wk-tools from a person's shell is served: the one repository an agent publishes"]))
        self.assertEqual([c for c in asked if "git ls-remote" in c][0], PROBE % "github-wk-tools:u/wk-tools")
        self.assertEqual([c for c in asked if "webkitscmpy" in c or "mktemp" in c], [])


class TestTheHostSide(WallTest):
    def test_rootless_podman_and_the_proxy_each_fail_on_their_own(self):
        self.w.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], out="false\n")
        self.assertEqual(self.misses(self.doctor()[1]), ["-- podman is NOT rootless -> an escape from this container is an escape as root"])
        self.w.answer(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], rc=125)
        self.w.answer(["systemctl", "--user", "is-active", "--quiet", "wk-proxy.service"], rc=3)
        self.assertEqual(self.misses(self.doctor()[1]), ["-- podman is NOT rootless -> an escape from this container is an escape as root",
                                                         "-- egress proxy is not running -> systemctl --user status wk-proxy"])

    def test_the_write_probe_runs_after_every_check_and_a_writable_mount_fails_and_is_cleaned(self):
        self.assertEqual(self.doctor()[2][-1], "touch /opt/wk-tools/.wk-write-probe 2>&1")
        rc, rows, asked = self.doctor({"touch /opt/wk-tools/.wk-write-probe": ""})
        self.assertEqual((rc, self.misses(rows), asked[-2:]), (1, ["-- /opt/wk-tools is writable from inside the workspace -> it is mounted "
                                                                   "read-only (lib/wk/places.py's Container)"],
                                                           ["touch /opt/wk-tools/.wk-write-probe 2>&1", "rm -f /opt/wk-tools/.wk-write-probe"]))

    def test_every_check_runs_at_once(self):
        """Six probes, the first of six checks, each wait for the other five: run one after another, each would die waiting."""
        met = threading.Barrier(6, timeout=5)
        keys = ("https://github.com/ 2", "example.com", "192.168.1.1", "/proc/net/dev", "command -v bwrap", "gpu-probe.sh")
        self.assertEqual(self.doctor({k: lambda cmd, said=dict(HEALTHY)[k]: (met.wait(), said)[1] for k in keys})[:2], (0, HEALTHY_CONTAINER))

    def test_a_check_that_dies_is_unmeasured_not_passed(self):
        def dies(cmd):
            raise OSError("ssh went away")
        rc, rows, _ = self.doctor({"gpu-probe.sh": dies})
        self.assertEqual((rc, self.misses(rows)), (1, ["-- the 'gpu' probe died before it reported anything (ssh went away), so what it "
                                                       "measures is unmeasured -> run it again to ask a second time"]))

    def test_a_stopped_workspace_fails_and_nothing_is_asked_in_it(self):
        self.w.react(["podman", "inspect"], lambda a, f: Result(0, "exited\n"))
        self.assertEqual(self.doctor(), (1, ["-- workspace state: exited -> wk start ws"], []))


class TestTheKeyScanRunsForReal(WallTest):
    """The scan run by bash against a real home shaped like a guest's, whose checkout (with WebKit's PEM fixtures) is inside it:
    what it does not walk is the point."""

    PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot a real key\n"

    def scan(self, *files):
        home = Path(tempfile.mkdtemp(dir=str(self.tmp)))
        for rel, text in (("WebKit/Source/test/cert.pem", self.PEM), (".ssh/known_hosts", "")) + files:
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_text(text)
        run = lambda cmd: subprocess.run(["bash", "-c", "HOME=%s\n%s" % (home, cmd)], capture_output=True, text=True).stdout
        rows = self.doctor({"PRIVATE KEY": run})[1]
        return [r.replace(str(home) + "/", "~/") for r in rows if "private key material" in r]

    def test_a_pem_in_the_checkout_is_not_reported(self):
        self.assertEqual(self.scan(), ["ok no private key material in the workspace (~/.ssh, ~/.claude, the top of home, /secrets, /run/wk)"])

    def test_a_key_in_dot_ssh_or_the_top_of_home_or_under_dot_claude_is(self):
        self.assertEqual(self.scan((".ssh/id_fork", self.PEM), ("leaked_key", self.PEM), (".claude/deep/k", self.PEM)),
                         ["-- private key material inside the workspace: ~/.ssh/id_fork ~/.claude/deep/k ~/leaked_key -> remove it"])

    def test_a_transcript_quoting_the_words_is_not_reported_and_one_holding_a_key_is(self):
        quoted = (".claude/projects/quoted.jsonl", json.dumps({"text": "KEY_SCAN greps 'PRIVATE KEY'\n" + self.PEM + "   300\tPEM = " + repr(self.PEM)}) + "\n")
        self.assertEqual(self.scan(quoted)[0][:3], "ok ")
        head, body = self.PEM.splitlines()[0], "A" * 64
        for name, text in (("cat", head + "\n" + body), ("read", "     1\t" + head + "\n     2\t" + body)):
            with self.subTest(name=name):
                self.assertEqual(self.scan(quoted, (".claude/projects/%s.jsonl" % name, json.dumps({"text": text}) + "\n")),
                                 ["-- private key material inside the workspace: ~/.claude/projects/%s.jsonl -> remove it" % name])


GUEST = ["ok workspace running", "-- no golden base VM 'wk-base' to clone a guest from -> wk sysimage build macos-guest-base   (hours)",
         "?? 'ws' did not answer the desktop probe -> wk doctor ws", "?? 'ws' did not answer the load probe -> wk doctor ws"]
HEALTHY_GUEST = GUEST + HEALTHY_CONTAINER[1:5] + [
    "ok the softwareupdate scan path is refused, so nothing offers this guest an upgrade"] + HEALTHY_CONTAINER[8:19] + [
    "ok a push from an agent in the guest is refused: this caller descends from a run of wk's own (pid 9: bash -lc git ls-remote), not from a shell of you",
    "ok a read is authenticated (HTTP 200) from a token this workspace never holds",
    "ok a write from an agent in the guest is refused by the injector (HTTP 412)", "ok bugs.webkit.org reachable through the injector (HTTP 200)",
    "ok a Bugzilla write from an agent in the guest is refused by the injector (HTTP 412)"] + HEALTHY_CONTAINER[26:28] + [
    ".. a guest's GPU is Virtualization.framework's, reached through Metal, and this probe is EGL: what a benchmark in there gets "
    "is measured by the benchmark, and its desktop rows above say whether its window is covered",
    "ok a write from a person's shell reaches GitHub (HTTP 422 for an empty request): the injector writes for it"]


class TestAGuestsReport(WallTest):
    """A macOS guest booted filtered: no /proc for the push service to read, so a stand-in agent runs beside each publish."""
    kinds = {"fakebox": "vm"}
    CLIENT = {"git config --global --get core.sshCommand": "/Users/admin/wk-tools/container/push/wk-push-client.py"}

    def setUp(self):
        super().setUp()
        self.unfiltered = self.w.files.pop(os.path.join(self.w.driver.vm_dir(), "ws.unfiltered"))

    def test_a_healthy_guest_is_asked_every_credential_check_and_its_egress_and_no_container_check(self):
        """Its own rows (the golden base, its desktop and load) come first: this world answers neither probe."""
        self.assertEqual(self.doctor(self.CLIENT)[:2], (1, HEALTHY_GUEST))

    def test_a_softwareupdate_path_that_answers_fails_naming_why_no_guest_turns_it_off(self):
        rc, rows, _ = self.doctor({**self.CLIENT, "swscan": "curl: (56) Received HTTP code 403"})
        self.assertEqual(self.misses(rows)[3:], [
            "-- swscan/gdmf are reachable from in here: 'curl: (56) Received HTTP code 403' -> softwareupdated finds an upgrade and "
            "Setup Assistant puts its pane in front of the window; no guest can turn the check off (vm/desktop.sh)"])

    def test_each_publish_runs_beside_a_stand_in_agent_and_a_persons_write_is_asked_after_the_last(self):
        asked = self.doctor(self.CLIENT)[2]
        push = next(c for c in asked if "git ls-remote" in c)
        self.assertEqual(push[:push.index("; kill")], 'd=$(mktemp -d); cp "$(command -v sh)" "$d/claude"; "$d/claude" -c "while :; do sleep 1; '
                                                      'done" & p=$!; sleep 0.3; ' + PROBE % "github-webkit:justinmichaud/WebKit")
        last_agent = max(i for i, c in enumerate(asked) if '"$d/claude"' in c)
        self.assertEqual([i > last_agent for i, c in enumerate(asked) if "/git/refs" in c], [True])

    def test_a_persons_write_answering_412_names_the_push_service(self):
        rc, rows, _ = self.doctor({**self.CLIENT, "/git/refs": "412"})
        self.assertEqual(self.misses(rows)[3:], ["-- a write from a person's shell answered '412' -- " + PERSON_WRITE])

    def test_a_persons_write_is_unmeasured_while_an_agent_runs(self):
        rc, rows, asked = self.doctor({**self.CLIENT, "ps -Ao": "4242"})
        self.assertEqual((rows[-1], [c for c in asked if "/git/refs" in c]),
                         (".. a person's write is not measured: an agent runs in 'ws', so the injector refuses it as it should", []))

    def test_an_unfiltered_guest_fails_and_is_asked_no_egress(self):
        self.w.files[os.path.join(self.w.driver.vm_dir(), "ws.unfiltered")] = self.unfiltered
        rc, rows, _ = self.doctor(self.CLIENT)
        self.assertEqual(rows[:2] + [r for r in rows if "github reachable" in r or "softwareupdate" in r], [
            "ok workspace running", "-- this guest was booted with WK_VM_UNFILTERED, so it has the open network -> wk stop ws && wk "
            "start ws   (without that variable)"])

    def test_a_stopped_guest_fails_with_its_own_rows_and_nothing_is_asked_in_it(self):
        self.w.vms["wk-ws"] = "stopped"
        self.assertEqual(self.doctor(self.CLIENT), (1, ["-- workspace state: stopped -> wk start ws", GUEST[1],
                                                         "?? 'ws' is not running, so its desktop and its load cannot be read -> wk start ws"], []))


class TestWhatIsAsked(WallTest):
    def setUp(self):
        super().setUp()
        (self.tmp / "machines").mkdir()
        (self.tmp / "machines" / "pi.conf").write_text("kind=board\n")
        self.w.env["WK_MACHINES_DIR"] = str(self.tmp / "machines")

    def test_each_question_is_refused_where_it_does_not_belong(self):
        for argv, line in ((["pi", "--all"], "error: --all is not asked of a machine: wk doctor pi"),
                           (["--gpu"], "error: --gpu is a workspace's question: wk doctor <workspace> --gpu"),
                           (["ws", "--all"], "error: --all is this machine's question, not a workspace's: drop the workspace name")):
            with self.subTest(argv=argv):
                self.w.env["WK_NAME"] = "ws" if argv[0] == "ws" else ""
                rc, err = self.typed("doctor", *argv)
                self.assertEqual((rc, said(err), self.w.effects), (1, line, []))

    def test_inside_a_workspace_no_flag_is_asked(self):
        rc, err = self.typed("doctor", "--all", w=self.make_world(kinds={"fakebox": "local"}))
        self.assertEqual((rc, said(err)), (1, "error: --all is not asked inside a workspace: 'wk doctor' here checks this one"))

    def test_an_absent_workspace_is_refused_by_name(self):
        self.w.env["WK_NAME"] = "nope"
        rc, err = self.typed("doctor", "nope")
        self.assertEqual((rc, err.splitlines()[0], set(self.w.effects)), (2, "warning: no such workspace: nope -- 'wk ls' lists them",
                                                                  {("run", ("podman", "inspect", "wk-nope", "--format", "{{.State.Status}}"))}))


class TestABuildBox(WallTest):
    kinds = {"fakebox": "remote"}

    def test_a_remote_place_has_no_sandbox_and_is_refused(self):
        self.w.env["WK_NAME"] = "ws"
        rc, err = self.typed("doctor", "ws")
        self.assertEqual((rc, said(err), [e for e in self.w.effects if "-lc" in e[1]]),
                         (1, "error: 'wk doctor ws' proves a sandbox holds, and a remote place has none:", []))


# Not typed as `wk doctor`: inside a workspace the wall asks this host's own /proc (pushgate.Proc() of os.getpid()) and its
# own uname (LocalWorkspace.os), and the disk section walks this host's files; the rest has no command line that reaches it.
import io  # noqa: E402
import sys  # noqa: E402
from unittest import mock  # noqa: E402

from tests.support import REPO, load_cmd  # noqa: E402
from tests import fakes  # noqa: E402
from tests.fakes import Fake, FakeRegistry  # noqa: E402
from tests.test_push_service import Proc  # noqa: E402

sys.path.insert(0, str(REPO / "lib"))
import credcheck  # noqa: E402
from wk import doctor, places, pushgate, wall  # noqa: E402

ENSURE_BRIDGE = "container/proxy/ensure-bridge.sh"
CONMON, ENTRY, EXEC, BRIDGE_PID = 400, 500, 600, 601


class _Wall(fakes.FlowTest):
    """The workspace's process table is a fake /proc: conmon, a person's entry under it, and this process -- `me`, the wk
    command a check inside a workspace runs in -- under that. Each exec's bridge is added to it as ensure-bridge.sh starts
    one. tests/test_ai.py and tests/test_ai_inside.py build on it."""
    kind = "container"
    me = ["python3", "/opt/wk-tools/wk", "ai", "claude"]

    def setUp(self):
        super().setUp()
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_VM_STORE": str(self.tmp / "vmstore"),
                    "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1", "WK_MARKER": str(self.tmp / "marker"),
                    "PATH": os.environ.get("PATH", "")}
        (self.tmp / "marker").write_text("name=demo\nsrc=/src/WebKit\n")
        self.fake, self.answers, self.asked = fakes.World(), dict(HEALTHY), []
        for prefix in (["env"], ["bash", "-lc"], [os.path.join(str(REPO), ENSURE_BRIDGE)]):
            self.fake.react(prefix, self._exec)
        for verb, said in (("present", ""), ("read", "stored-value")):
            self.fake.answer(["python3", os.path.join(str(REPO), "lib", "secretfile.py"), verb], out=said)
        self.proc, self.proc_lock = Proc(self.tmp / "proc"), threading.Lock()
        self.proc.chain([(CONMON, 1, "conmon", ["/usr/bin/conmon"]), (ENTRY, CONMON, "bash", ["/bin/bash", "/opt/wk-tools/" + pushgate.PERSON, "/bin/bash", "--login"]),
                         (os.getpid(), ENTRY, "python3", self.me)])
        real = pushgate.Proc
        for p in (mock.patch.object(pushgate, "Proc", lambda: real(str(self.proc.root))), mock.patch.object(credcheck, "verdict", return_value="ok\tit works"),
                  mock.patch("wk.store.Store.macos_host", new_callable=mock.PropertyMock, return_value=True)):
            p.start()
            self.addCleanup(p.stop)
        self.reg = places.Registry(str(REPO), env=self.env, machine=self.fake)
        self.driver = self.reg.load(self.kind)

    def _exec(self, argv, fake):
        """A container's exec ends in the command; a workspace-local one quotes it after `exec`."""
        person = self._bridges_a_person(argv)
        argv = argv[1:] if argv[0].endswith(ENSURE_BRIDGE) else argv
        return self._answer(shlex.split(argv[-1])[-1] if argv[:2] == ["bash", "-lc"] else argv[-1], bool(person))

    def _bridges_a_person(self, argv):
        """Whether the push service takes the bridge this exec starts for a person's: it is the child of the command
        ensure-bridge.sh execs into, which is conmon's child for a container's exec and this process's for one inside."""
        i = next((i for i, a in enumerate(argv) if a.endswith(ENSURE_BRIDGE)), None)
        if i is None:
            return None
        with self.proc_lock:
            self.proc.add(EXEC, CONMON if self.kind == "container" else os.getpid(), "bash", argv[i + 1:])
            self.proc.add(BRIDGE_PID, EXEC, "python3", ["python3", "/opt/wk-tools/container/proxy/bridge.py", str(EXEC)])
            try:
                pushgate.person(pushgate.Proc(), BRIDGE_PID, "demo")
                return True
            except pushgate.Denied:
                return False

    def _direct(self, ws, argv, tty=False, timeout=None, person=False):
        return self._answer(argv[-1], person)

    def _answer(self, cmd, person=False):
        """A `person:` key answers a person's run of that command ahead of the plain key."""
        self.asked.append(cmd)
        found = [v for k, v in self.answers.items() if k.startswith("person:") and person and k[len("person:"):] in cmd]
        found += [v for k, v in self.answers.items() if not k.startswith("person:") and k in cmd]
        return found[0] if found and isinstance(found[0], Result) else Result(0, found[0] if found else "")

    def set(self, key, value):
        self.answers[key] = value

    def wall(self):
        return wall.Wall(str(REPO), self.driver, "demo", self.fake)


class TestFromInside(_Wall):
    """`wk doctor` in a workspace, its probes run as whatever this process is to the push service: an agent session (the
    usual case), a person's own shell (whose push is served, which is not publishing), and the agent of a wk-tools
    workspace (the one repository an agent publishes, so no commit wall and its push served)."""
    kind = "local"
    CLIENT = str(REPO / "container" / "push" / "wk-push-client.py")
    PUBLISHING = {"/pulls": "422", "http_code}' -X POST -H": "410", "git ls-remote": "", "test -S /run/wk/ssh-agent.sock": "agent"}
    # who: (what a healthy pass says, the answers each of which is publishing, whether a Linux checkout is walled)
    CASES = {"agent": (("no ssh-agent socket", "a push from an agent session is refused",
                        "a write from an agent session is refused by the injector"), tuple(PUBLISHING), True),
             "person": (("a push from a person's shell is served", "a write from a person's shell reaches GitHub",
                         "a Bugzilla write is not measured from a person's shell"), (), True),
             "tools": (("a push of u/wk-tools from an agent session is served: the one repository an agent publishes",
                        "a write from an agent session is refused by the injector"),
                       ("/pulls", "http_code}' -X POST -H", "test -S /run/wk/ssh-agent.sock"), False)}

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(places.LocalWorkspace, "os", lambda _: "linux")
        patch.start()
        self.addCleanup(patch.stop)

    def as_(self, who):
        """CASES in order: each starts from the healthy answers, and the wk-tools workspace comes last."""
        self.answers = dict(HEALTHY)
        self.set("git config --global --get core.sshCommand", self.CLIENT)
        self.proc.chain([(os.getpid(), ENTRY, "python3", ["python3", "/opt/wk-tools/wk", "doctor"] if who == "person" else self.me)])
        if who == "tools":
            (self.tmp / "marker").write_text("name=demo\nsrc=/src/wk-tools\nrepo=wk-tools\n")
            self.driver = self.reg.load("local")
            self.fake.answer(["git", "-c", "safe.directory=*", "-C", self.driver.tools_src(), "remote", "get-url", "origin"],
                             out="https://github.com/u/wk-tools.git\n")
            self.set("git ls-remote", "0123abcd\trefs/heads/main")
        return self.CASES[who]

    def results(self):
        out = io.StringIO()
        rep = doctor.Report(out)
        return wall.from_inside(str(REPO), self.driver, "demo", self.fake, rep), rep, out.getvalue()

    def test_a_healthy_workspace_passes_and_nothing_can_publish(self):
        for who in self.CASES:
            with self.subTest(who=who):
                says = self.as_(who)[0]
                publishing, rep, out = self.results()
                self.assertEqual((False, 0), (publishing, rep.missing), out)
                for phrase in says:
                    self.assertIn(phrase, out)

    def test_each_way_to_publish_is_publishing(self):
        for who in self.CASES:
            for key in self.as_(who)[1]:
                with self.subTest(who=who, key=key):
                    held = self.answers.get(key)
                    self.set(key, self.PUBLISHING[key])
                    self.assertTrue(self.results()[0])
                    self.set(key, held)

    def test_the_commit_wall_is_probed_where_bwrap_is(self):
        for who in self.CASES:
            walled = self.as_(who)[2]
            for os_name in ("linux", "macos"):
                with self.subTest(who=who, os=os_name), mock.patch.object(self.driver, "os", return_value=os_name):
                    self.assertEqual(walled and os_name == "linux", "commit-wall" in [n for n, _ in self.wall().from_inside()])

    def test_a_read_that_fails_is_the_sandbox_not_publishing(self):
        self.as_("agent")
        self.set("rest/version", "000")
        publishing, rep, _ = self.results()
        self.assertEqual((False, 1), (publishing, rep.missing))

    def test_the_host_half_is_not_asked(self):
        names = [n for n, _ in self.wall().from_inside()]
        for n in ("secrets-view", "isolation", "gpu", "agent-credential"):
            self.assertNotIn(n, names)

    def test_inside_the_exit_status_is_0_intact_1_broken_3_publishing(self):
        def sim(missing, publishing):
            def fake_from_inside(root, driver, ws, machine, rep):
                rep.missing = missing
                return publishing
            reg = FakeRegistry({}, Fake(), lambda name, env: mock.Mock(ws_name="demo", kind="local"), in_workspace=lambda: True)
            with mock.patch.object(DOCTOR_CMD.wall, "from_inside", side_effect=fake_from_inside):
                return self.stderr(lambda: DOCTOR_CMD.inside(reg))[0]
        self.assertEqual([0, 1, 3], [sim(0, False), sim(2, False), sim(2, True)])


DOCTOR_CMD = load_cmd("doctor")


class TestNoCommandLineReaches(_Wall):
    """A check or a driver answer no `wk` command asks: a build box is refused by `wk doctor` and `wk ai`; the fork is the
    tree's project.json; nothing asks a build box or a workspace's own place whether its egress is filtered."""

    def test_a_build_box_is_given_the_token(self):
        self.driver = self.reg.load("remote")
        self.driver.exec = self._direct
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "set")
        self.set("claude auth status", '{"loggedIn": true, "authMethod": "oauth_token"}')
        self.assertEqual([r[0] for r in self.wall().agent_credential()], [doctor.OK])
        self.set("CLAUDE_CODE_OAUTH_TOKEN:+set", "")
        rows = self.wall().agent_credential()
        self.assertEqual([r[0] for r in rows], [doctor.MISS, doctor.OK])
        self.assertIn("no $CLAUDE_CODE_OAUTH_TOKEN", rows[0][1])
        self.assertIn("usable claude", rows[0][2])

    def test_a_guests_person_write_is_unmeasured_with_no_fork(self):
        self.driver = self.reg.load("vm")
        with mock.patch.object(wall.secrets, "forks", return_value=[]):
            self.assertIn("no fork is configured", self.wall().person_write()[0][1])

    def test_the_drivers_egress_and_remedy(self):
        self.assertEqual([self.reg.load(k).egress_filtered("demo") for k in ("remote", "local")], [False, False])
        self.assertIn("usable litellm", self.reg.load("vm").agent_secret_remedy("demo", "litellm"))


if __name__ == "__main__":
    unittest.main()
