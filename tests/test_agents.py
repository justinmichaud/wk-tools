"""The coding agents a workspace is made with: container/agents.sh run against stand-in curl, node and npm, and
lib/wk/agents.py's install over a recorded workspace."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import WsDriver
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
import credcheck  # noqa: E402
from wk import agents  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

LINK = 'mkdir -p "$HOME/.local/bin" && ln -s "$OK_BIN" "$HOME/.local/bin/%s"'
STUBS = {
    "curl": 'echo curl >> "$LOG"\n[ "${CURL_INSTALLS:-1}" = 1 ] && echo \'%s\'\nexit 0\n' % (LINK % "claude"),
    "node": 'case "$1" in -v) echo "$NODE_V" ;; -e) [ "$NODE_OK" = 1 ] ;; esac\n',
    "npm": 'echo "npm $*" >> "$LOG"\n[ "${NPM_OK:-1}" = 1 ] || exit 1\n%s\n' % (LINK % "pi"),
}


class TestTheScript(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-agents-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home, self.bin, self.sys = self.tmp / "home", self.tmp / "bin", self.tmp / "sys"
        for d in (self.home, self.bin, self.sys):
            d.mkdir()
        for tool in ("bash", "mkdir", "ln"):
            (self.sys / tool).symlink_to(shutil.which(tool))
        self.ok = self.tmp / "ok"
        self.stub(self.ok, "exit 0\n")
        for name, body in STUBS.items():
            self.stub(self.bin / name, body)
        self.env = {"HOME": str(self.home), "PATH": "%s:%s" % (self.bin, self.sys), "LOG": str(self.tmp / "log"),
                    "OK_BIN": str(self.ok), "NODE_V": "v22.19.0", "NODE_OK": "1"}

    def stub(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def script(self, *args, **env):
        return subprocess.run([shutil.which("bash"), str(REPO / agents.SCRIPT), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60)

    def run_script(self, *args, **env):
        cp = self.script(*args, **env)
        said = dict(l.split("=", 1) for l in cp.stdout.splitlines() if "=" in l)
        return cp.returncode, said

    def log(self):
        p = self.tmp / "log"
        return p.read_text().splitlines() if p.exists() else []

    def test_a_fresh_workspace_gets_both_and_a_rerun_finds_them(self):
        self.assertEqual((0, {"claude": "installed", "pi": "installed", "models": "no"}), self.run_script())
        self.assertEqual((0, {"claude": "present", "pi": "present", "models": "no"}), self.run_script())
        self.assertEqual(2, len(self.log()), self.log())

    def test_pi_goes_into_the_users_own_prefix_without_install_scripts(self):
        self.run_script()
        npm = [l for l in self.log() if l.startswith("npm ")]
        self.assertEqual(["npm install -g --ignore-scripts --prefix %s/.local @earendil-works/pi-coding-agent" % self.home], npm)

    def test_a_claude_on_path_that_does_not_run_is_replaced(self):
        self.stub(self.bin / "claude", "exit 1\n")
        self.assertEqual("installed", self.run_script()[1]["claude"])

    def test_an_installer_that_leaves_nothing_that_runs_fails(self):
        rc, said = self.run_script(CURL_INSTALLS="0")
        self.assertEqual((1, "failed"), (rc, said["claude"]))
        self.assertNotIn("pi", said)

    def test_too_old_a_node_is_no_pi_not_a_failure(self):
        rc, said = self.run_script(NODE_V="v20.11.1", NODE_OK="0")
        self.assertEqual((0, "no-node v20.11.1"), (rc, said["pi"]))
        self.assertFalse([l for l in self.log() if l.startswith("npm ")])

    def test_no_node_at_all_is_no_pi(self):
        (self.bin / "node").unlink()
        self.assertEqual("no-node absent", self.run_script()[1]["pi"])

    def test_an_npm_install_that_fails_fails(self):
        rc, said = self.run_script(NPM_OK="0")
        self.assertEqual((1, "failed"), (rc, said["pi"]))

    def test_a_models_file_already_there_is_said(self):
        (self.home / ".pi" / "agent").mkdir(parents=True)
        (self.home / ".pi" / "agent" / "models.json").write_text("{}")
        self.assertEqual("yes", self.run_script()[1]["models"])

    def test_find_names_the_one_that_runs_and_fails_without_one(self):
        self.assertEqual(1, self.script("find", "claude").returncode)
        self.run_script()
        cp = self.script("find", "claude")
        self.assertEqual((0, "%s/.local/bin/claude\n" % self.home), (cp.returncode, cp.stdout))
        self.assertEqual([], [l for l in self.log() if "find" in l])


class Workspace:
    """`run` as agents.install calls it: every argv kept, answered by the first key found in its last word."""

    def __init__(self, answers):
        self.answers, self.argvs = answers, []

    def __call__(self, argv):
        self.argvs.append(argv)
        for key, r in self.answers.items():
            if key in argv[-1]:
                return r
        return Result(0)


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace({"claude.ai/install.sh": Result(0, "claude=present\npi=installed\nmodels=no\n")})
        self.stored = False
        for p in (mock.patch.object(agents.Secrets, "cred_stored", side_effect=lambda name: self.stored),
                  mock.patch.object(agents.Secrets, "cred_read", return_value="sk-placeholder\n")):
            p.start()
            self.addCleanup(p.stop)

    def install(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            try:
                agents.install(str(REPO), {}, Fake(), self.ws, "demo", "/opt/wk-tools", "/src/WebKit")
                status = 0
            except Refused as e:
                status = e.status
        return status, err.getvalue()

    def test_the_start_up_answers_are_recorded_for_the_checkout_after_the_install(self):
        status, err = self.install()
        self.assertEqual(0, status, err)
        self.assertIn("pi installed in 'demo'", err)
        self.assertIn("claude.ai/install.sh", self.ws.argvs[0][-1])
        self.assertEqual("python3 /opt/wk-tools/claude/workspace-config.py /src/WebKit", self.ws.argvs[1][-1])

    def test_a_failed_install_refuses_naming_the_rerun(self):
        self.ws.answers["claude.ai/install.sh"] = Result(1, "claude=failed\n")
        status, err = self.install()
        self.assertEqual(1, status, err)
        self.assertIn("could not install the coding agents in 'demo' (claude=failed pi=?)", err)
        self.assertIn("'wk new demo' destroys the half-made workspace and tries again", err)
        self.assertEqual(1, len(self.ws.argvs))

    def test_answers_that_cannot_be_recorded_refuse(self):
        self.ws.answers["workspace-config.py"] = Result(1, "", "not readable as JSON")
        status, err = self.install()
        self.assertEqual(1, status, err)
        self.assertIn("could not record Claude's start-up answers in 'demo'", err)
        self.assertIn("not readable as JSON", err)

    def test_no_node_is_said_and_is_not_a_refusal(self):
        self.ws.answers["claude.ai/install.sh"] = Result(0, "claude=present\npi=no-node v20.11.1\nmodels=no\n")
        status, err = self.install()
        self.assertEqual(0, status, err)
        self.assertIn("no pi in 'demo': it needs node >= 22.19 and npm, and found node v20.11.1", err)

    def models_written(self, served):
        self.stored = True
        with mock.patch.object(credcheck, "litellm_models", side_effect=served), \
                mock.patch.object(credcheck, "litellm_callable", side_effect=lambda key, ids: (ids[1:] or [None])[0]):
            status, err = self.install()
        return status, err, [a[-1] for a in self.ws.argvs if "> ~/.pi/agent/models.json" in a[-1]]

    def test_pi_is_pointed_at_the_models_the_key_may_call(self):
        status, err, writes = self.models_written(lambda key: ["glm-5p3-flash", "gpt-oss-120b"])
        self.assertEqual(0, status, err)
        words = shlex.split(writes[0])
        provider = json.loads(words[words.index("printf") + 2])["providers"]["litellm"]
        self.assertEqual((credcheck.LITELLM_ENDPOINT, "openai-completions", "$LITELLM_API_KEY"),
                         (provider["baseUrl"], provider["api"], provider["apiKey"]))
        self.assertEqual([{"id": "gpt-oss-120b"}, {"id": "glm-5p3-flash"}], provider["models"])
        self.assertIn("umask 077", writes[0])

    def test_an_endpoint_where_no_model_answers_writes_nothing_and_names_the_check(self):
        status, err, writes = self.models_written(lambda key: ["m-404"])
        self.assertEqual((0, []), (status, writes), err)
        self.assertIn("'wk key check'", err)

    def test_an_endpoint_that_does_not_answer_writes_nothing(self):
        def down(key):
            raise credcheck.Unreachable("URLError: refused")
        status, err, writes = self.models_written(down)
        self.assertEqual((0, []), (status, writes), err)
        self.assertIn("URLError: refused", err)

    def test_with_no_key_it_says_which_command_stores_one(self):
        status, err = self.install()
        self.assertEqual(0, status, err)
        self.assertIn("wk key set litellm", err)
        self.assertFalse([a for a in self.ws.argvs if "> ~/.pi/agent/models.json" in a[-1]])

    def test_a_models_file_already_there_is_left_alone(self):
        self.ws.answers["claude.ai/install.sh"] = Result(0, "claude=present\npi=present\nmodels=yes\n")
        _, _, writes = self.models_written(lambda key: ["m"])
        self.assertEqual([], writes)


class TestADryRun(unittest.TestCase):
    def test_it_prints_the_install_and_runs_nothing_in_the_workspace(self):
        t, err = WsDriver("container", str(REPO), {}, Fake()), io.StringIO()
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(err):
            t.install_agents("demo")
        self.assertEqual([], t.machine.effects)
        self.assertIn("would run in demo: bash -lc 'python3 /opt/wk-tools/claude/workspace-config.py /src/WebKit'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
