"""The coding agents a workspace is made with: container/agents.sh run against stand-in curl, node and npm, and
`wk new`'s install of them, typed into a world whose workspace answers the script and whose LiteLLM endpoint lists models."""
import io
import json
import os
import shlex
import shutil
import subprocess
import unittest
import urllib.error
from unittest import mock

from tests.fakes import FlowTest, Result
from tests.support import NO_GITHUB, REPO
from tests.test_wk_workspace import Flow, World, said

SCRIPT = "container/agents.sh"

LINK = 'mkdir -p "$HOME/.local/bin" && ln -s "$OK_BIN" "$HOME/.local/bin/%s"'
CLAUDE_INSTALLER = ('v="$1"; mkdir -p "$HOME/.local/share/claude/versions" "$HOME/.local/bin" && cp "$OK_BIN" "$HOME/.local/share/claude/versions/$v" '
                    '&& ln -s "$HOME/.local/share/claude/versions/$v" "$HOME/.local/bin/claude"')
STUBS = {
    "curl": ('echo curl >> "$LOG"\nwhile [ "$1" != -o ]; do shift; done\n'
             'if [ -n "$CURL_REFUSES" ]; then echo "$CURL_REFUSES" > "$2"; echo "curl: (22) 401" >&2; exit 22; fi\n'
             'sleep "${CURL_SLEEP:-0}"\n[ "${CURL_INSTALLS:-1}" = 1 ] && echo \'%s\' > "$2"\nexit 0\n' % CLAUDE_INSTALLER),
    "node": 'case "$1" in -v) echo "$NODE_V" ;; -e) [ "$NODE_OK" = 1 ] ;; esac\n',
    "npm": 'echo "npm $*" >> "$LOG"\n[ "${NPM_OK:-1}" = 1 ] || exit 1\n%s\n' % (LINK % "pi"),
}


class TestTheScript(FlowTest):
    def setUp(self):
        super().setUp()
        self.home, self.bin, self.sys = self.tmp / "home", self.tmp / "bin", self.tmp / "sys"
        for d in (self.home, self.bin, self.sys):
            d.mkdir()
        for tool in ("bash", "mkdir", "ln", "cp", "mv", "sleep", "rm", "head", "python3"):
            (self.sys / tool).symlink_to(shutil.which(tool))
        self.ok = self.tmp / "ok"
        self.stub(self.ok, "exit 0\n")
        for name, body in STUBS.items():
            self.stub(self.bin / name, body)
        self.env = {"HOME": str(self.home), "PATH": "%s:%s" % (self.bin, self.sys), "LOG": str(self.tmp / "log"),
                    "OK_BIN": str(self.ok), "WK_AGENTS_CACHE": str(self.tmp / "cache"), "WK_CLAUDE_VERSION": "9.9.9", "NODE_V": "v22.19.0", "NODE_OK": "1"}

    def stub(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def script(self, *args, **env):
        return subprocess.run([shutil.which("bash"), str(REPO / SCRIPT), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def run_script(self, *args, **env):
        cp = self.script(*args, **env) if args else (self.script("fetch", **env), self.script(**env))[1]
        said = dict(l.split("=", 1) for l in cp.stdout.splitlines() if "=" in l)
        return cp.returncode, said

    def log(self):
        p = self.tmp / "log"
        return p.read_text().splitlines() if p.exists() else []

    def test_a_fresh_workspace_gets_both_and_a_rerun_finds_them(self):
        self.assertEqual((0, {"claude": "installed", "pi": "installed"}), self.run_script())
        self.assertEqual((0, {"claude": "present", "pi": "present"}), self.run_script())
        self.assertEqual(2, len(self.log()), self.log())

    def test_pi_goes_into_the_users_own_prefix_without_install_scripts(self):
        self.run_script()
        npm = [l for l in self.log() if l.startswith("npm ")]
        self.assertEqual(["npm install -g --ignore-scripts --prefer-offline --prefix %s/.local @earendil-works/pi-coding-agent" % self.home], npm)

    def artifact(self):
        return self.tmp / "cache" / "claude" / "9.9.9"

    def curls(self):
        return [l for l in self.log() if l == "curl"]

    def test_the_first_install_fetches_into_the_artifact_and_the_next_workspace_makes_no_network_call(self):
        self.assertEqual("installed", self.run_script()[1]["claude"])
        self.assertTrue((self.artifact() / ".local/share/claude/versions/9.9.9").is_file())
        self.assertFalse((self.tmp / "cache" / "claude" / ".stage").exists())
        (other := self.tmp / "home2").mkdir()
        self.assertEqual("installed", self.run_script(HOME=str(other))[1]["claude"])
        self.assertEqual(1, len(self.curls()), self.log())
        self.assertTrue(os.access(other / ".local/bin/claude", os.X_OK))
        self.assertEqual(str(other / ".local/share/claude/versions/9.9.9"), os.readlink(other / ".local/bin/claude"))

    def test_two_creations_at_once_fetch_once_and_the_second_waits_for_the_artifact(self):
        (other := self.tmp / "home2").mkdir()
        procs = [subprocess.Popen([shutil.which("bash"), str(REPO / SCRIPT), "fetch"], env=dict(self.env, CURL_SLEEP="1", HOME=h),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for h in (str(self.home), str(other))]
        self.assertEqual([("", 0), ("", 0)], [(p.communicate(timeout=60)[0], p.returncode) for p in procs])
        self.assertEqual(1, len(self.curls()), self.log())

    def test_a_failed_fetch_leaves_no_artifact_for_the_next_try_to_trust(self):
        self.script("fetch", CURL_INSTALLS="0")
        self.assertEqual([], [p for p in (self.artifact(), self.tmp / "cache" / "claude" / ".stage") if p.exists()])
        self.assertEqual("installed", self.run_script()[1]["claude"])

    def test_the_install_only_reads_the_cache_and_pi_goes_through_the_workspaces_own_npm_cache(self):
        """`unit places.shared_mounts_readonly`: a workspace installs both from a cache it cannot write, which it leaves as it was."""
        self.stub(self.bin / "npm", 'echo "npm_config_cache=$npm_config_cache" >> "$LOG"\n' + LINK % "pi" + "\n")
        self.script("fetch")
        subprocess.run(["chmod", "-R", "a-w", str(self.tmp / "cache")], check=True)
        self.addCleanup(subprocess.run, ["chmod", "-R", "u+w", str(self.tmp / "cache")])
        self.assertEqual((0, {"claude": "installed", "pi": "installed"}), self.run_script())
        self.assertEqual(["curl", "npm_config_cache="], self.log())

    def test_a_claude_on_path_that_does_not_run_is_replaced(self):
        self.stub(self.bin / "claude", "exit 1\n")
        self.assertEqual("installed", self.run_script()[1]["claude"])

    def test_an_installer_that_leaves_nothing_that_runs_fails(self):
        rc, said = self.run_script(CURL_INSTALLS="0")
        self.assertEqual((1, "failed", False), (rc, said["claude"], "pi" in said))

    def test_a_refused_download_shows_its_reason_and_runs_nothing(self):
        cp = self.script("fetch", CURL_REFUSES="echo ran > $LOG.ran; the injector says why")
        self.assertEqual((1, "claude=failed\n"), (cp.returncode, cp.stdout))
        self.assertIn("the injector says why", cp.stderr)
        self.assertFalse((self.tmp / "log.ran").exists())

    def test_too_old_a_node_is_no_pi_not_a_failure(self):
        rc, said = self.run_script(NODE_V="v20.11.1", NODE_OK="0")
        self.assertEqual((0, "no-node v20.11.1"), (rc, said["pi"]))
        self.assertFalse([l for l in self.log() if l.startswith("npm ")])

    def test_no_node_at_all_is_no_pi(self):
        (self.bin / "node").unlink()
        self.assertEqual("no-node absent", self.run_script()[1]["pi"])

    def test_the_node_verb_answers_whether_pi_can_be_installed(self):
        self.assertEqual(0, self.script("node").returncode)
        self.assertEqual(1, self.script("node", NODE_V="v20.11.1", NODE_OK="0").returncode)
        (self.bin / "node").unlink()
        self.assertEqual(1, self.script("node").returncode)

    def test_a_node_in_the_users_own_prefix_is_found(self):
        (self.bin / "node").rename(self.home / "node")
        (self.home / ".local" / "bin").mkdir(parents=True)
        (self.home / "node").rename(self.home / ".local" / "bin" / "node")
        self.assertEqual("installed", self.run_script()[1]["pi"])

    def test_an_npm_install_that_fails_fails(self):
        rc, said = self.run_script(NPM_OK="0")
        self.assertEqual((1, "failed"), (rc, said["pi"]))

    def test_every_workspace_reads_the_machines_one_models_file(self):
        """`unit agents.models_per_machine`: two workspaces on one agents cache read the one file, and a rewrite reaches both."""
        other, pi = self.tmp / "other", self.tmp / "cache" / "pi"
        for d in (self.home / ".pi" / "agent", other, pi):
            d.mkdir(parents=True)
        (self.home / ".pi" / "agent" / "models.json").write_text("its own")
        self.assertEqual([0, 0], [self.run_script(HOME=str(h))[0] for h in (self.home, other)])
        for text in ("first", "second"):
            (pi / "models.json").write_text(text)
            self.assertEqual([text, text], [(h / ".pi" / "agent" / "models.json").read_text() for h in (self.home, other)])

    def test_find_names_the_one_that_runs_and_fails_without_one(self):
        self.assertEqual(1, self.script("find", "claude").returncode)
        self.run_script()
        cp = self.script("find", "claude")
        self.assertEqual((0, "%s/.local/bin/claude\n" % self.home), (cp.returncode, cp.stdout))
        self.assertEqual([], [l for l in self.log() if "find" in l])


FETCH = "export WK_AGENTS_CACHE=%s WK_CLAUDE_VERSION=2.1.308"
CONFIG = "python3 /opt/wk-tools/claude/workspace-config.py /src/WebKit"
ENDPOINT = NO_GITHUB + "/v1"   # WK_LITELLM_API, as tests.support sets it
FAILED = "error: could not install the coding agents in 'ws' (claude=failed pi=?)."


class Endpoint:
    """The LiteLLM endpoint: /model/info lists `listed`, a completion answers 200 for a model in `callable` and 404 for
    another; `down` is no answer at all. Each request lands in `asked` as (method, path, bearer)."""

    def __init__(self):
        self.listed, self.callable, self.down, self.asked = [], set(), False, []

    def open(self, req, timeout=None):
        path = req.full_url[len(ENDPOINT):]
        self.asked.append((req.get_method(), path, req.get_header("Authorization")))
        if self.down:
            raise urllib.error.URLError("refused")
        if path != "/model/info" and json.loads(req.data)["model"] not in self.callable:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
        r = io.BytesIO(json.dumps({"data": [{"model_name": m, "model_info": {"mode": "chat"}} for m in self.listed]}).encode())
        r.status, r.headers = 200, {}
        return r


class Installs(World):
    """`wk new`'s machine: the agents cache is filled on the store's side as `fill` says, the script in the workspace
    answers `install` and Claude's start-up answers `config`; a LiteLLM key is stored when `key` is set."""

    def __init__(self, tmp, **kw):
        self.fill, self.install, self.config, self.key = Result(0), Result(0, "claude=installed\npi=installed\n"), Result(0), None
        super().__init__(tmp, **kw)
        self.react(["bash", "-lc"], lambda a, f: f.fill)
        self.react(["python3", os.path.join(str(REPO), "lib", "secretfile.py")],
                   lambda a, f: Result(0, f.key) if f.key else Result(int(a[2] == "present")))
        self.bash.update({"rm -f ": Result(0), "umask 077": Result(0)})

    def _in_ws(self, argv, f):
        return f.install if argv[-1].startswith("export WK_AGENTS_CACHE=") else f.config if argv[-1] == CONFIG else super()._in_ws(argv, f)


class AgentsTest(Flow):
    world_class = Installs

    def setUp(self):
        super().setUp()
        self.endpoint = Endpoint()
        p = mock.patch("urllib.request.build_opener", lambda *handlers: self.endpoint)
        p.start()
        self.addCleanup(p.stop)
        self.cache = os.path.join(self.w.env["WK_STORE"], "cache", "agents")
        self.models = '"%s/pi/models.json"' % self.cache

    def asked(self):
        """The install's runs: the store side's fill by its first line and verb, its models.json writes, the workspace's by first line."""
        runs = [e[1] for e in self.w.effects if e[0] == "run"]
        return ([("fill", a[2].split("\n")[0]) + a[3:] if a[:2] == ("bash", "-lc") else ("store", a[2]) if a[:2] == ("bash", "-c") else ("in ws", a[-1].split("\n")[0])
                 for a in runs if a[:2] == ("bash", "-lc") or a[:2] == ("bash", "-c") and "/pi/models.json" in a[2] or a[0] == "env" and (CONFIG == a[-1] or "WK_AGENTS_CACHE" in a[-1])])


class TestTheInstall(AgentsTest):
    def test_claude_is_fetched_on_the_stores_side_installed_from_its_cache_and_its_answers_recorded_after(self):
        rc, err = self.detach()
        self.assertEqual((rc, self.asked()[:3]), (0, [("fill", FETCH % self.cache, SCRIPT, "fetch"), ("in ws", FETCH % "/cache/agents"), ("in ws", CONFIG)]))
        self.assertIn("==> pi installed in 'ws'", err.splitlines())

    def test_a_failed_fetch_or_install_refuses_with_the_scripts_own_words_and_names_the_rerun(self):
        self.w.install = Result(1, "claude=failed\ncurl said so\n", "curl: (22) The requested URL returned error: 401\n")
        rc, err = self.detach()
        lines = err.splitlines()
        self.assertEqual((rc, said(err), lines[lines.index(FAILED) + 1:lines.index(FAILED) + 3], [a[0] for a in self.asked()]),
                         (1, FAILED, ["    curl said so", "    curl: (22) The requested URL returned error: 401"], ["fill", "in ws"]))
        self.assertIn("    'wk new ws' destroys the half-made workspace and tries again.", lines)
        self.w.fill = Result(1, "claude=failed\n")
        rc, err = self.detach()
        self.assertEqual((rc, said(err), [a[0] for a in self.asked()]), (1, FAILED, ["fill"]))

    def test_answers_that_cannot_be_recorded_refuse_with_why(self):
        self.w.config = Result(1, "", "not readable as JSON")
        rc, err = self.detach()
        self.assertEqual((rc, said(err), "    not readable as JSON" in err.splitlines()),
                         (1, "error: could not record Claude's start-up answers in 'ws', and every session there", True))

    def test_too_old_a_node_is_said_and_is_not_a_refusal(self):
        self.w.install = Result(0, "claude=present\npi=no-node v20.11.1\n")
        rc, err = self.detach()
        self.assertEqual((rc, "  no pi in 'ws': it needs node >= 22.19 and npm, and found node v20.11.1" in err.splitlines()), (0, True))

    def test_a_dry_run_prints_the_install_and_runs_nothing_in_the_workspace(self):
        rc, err = self.new("--dry-run")
        self.assertEqual((rc, [a for a in self.asked() if a[0] == "in ws"]), (0, []))
        self.assertIn("would run in ws: bash -lc '%s'" % CONFIG, err.splitlines())


class TestPisModels(AgentsTest):
    """`unit agents.models_per_machine`: each `wk new` recomputes the machine's one pi models.json from the stored LiteLLM key."""

    def written(self, key="sk-placeholder\n"):
        self.w.key = key
        rc, err = self.detach()
        self.assertEqual(rc, 0, err)
        return [a[1] for a in self.asked() if a[0] == "store"], err.splitlines()

    def test_pi_is_pointed_at_the_models_the_key_may_call_the_callable_first(self):
        self.endpoint.listed, self.endpoint.callable = ["glm-5p3-flash", "gpt-oss-120b"], {"gpt-oss-120b"}
        (write,), lines = self.written()
        words = shlex.split(write)
        self.assertEqual(json.loads(words[words.index("printf") + 2]), {"providers": {"litellm": {
            "baseUrl": ENDPOINT, "api": "openai-completions", "apiKey": "$LITELLM_API_KEY", "models": [{"id": "gpt-oss-120b"}, {"id": "glm-5p3-flash"}]}}})
        self.assertEqual(write.split(" && ")[:2] + write.split(" && ")[-1:], ['mkdir -p "%s/pi"' % self.cache, "umask 077", "mv -f %s.$$ %s" % (self.models, self.models)])
        self.assertEqual(self.endpoint.asked, [("GET", "/model/info", "Bearer sk-placeholder")] + [("POST", "/chat/completions", "Bearer sk-placeholder")] * 2)

    def test_where_no_model_answers_the_file_goes_and_where_nothing_answers_it_stays(self):
        self.endpoint.listed = ["m-404"]
        writes, lines = self.written()
        self.assertEqual((writes, "    so pi has nothing to call: 'wk key check' says what the endpoint makes of the key" in lines), (["rm -f " + self.models], True))
        self.w, self.endpoint.down = self.make_world(), True
        writes, lines = self.written()
        self.assertEqual((writes, "    so pi's models.json is left as it was" in lines), ([], True))

    def test_with_no_key_the_file_goes_and_it_says_which_command_stores_one(self):
        writes, lines = self.written(key=None)
        self.assertEqual((writes, self.endpoint.asked), (["rm -f " + self.models], []))
        self.assertIn("  no LiteLLM key on this machine ('wk key set litellm'), so pi in 'ws' asks you to /login", lines)

    def test_a_present_pi_still_recomputes_the_machines_file(self):
        self.w.install = Result(0, "claude=present\npi=present\n")
        self.endpoint.listed, self.endpoint.callable = ["m"], {"m"}
        self.assertEqual(len(self.written()[0]), 1)


if __name__ == "__main__":
    unittest.main()
