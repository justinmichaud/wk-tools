"""Option consistency across cmd/*: --quiet (dispatcher-level, once), --json"""
import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support import REPO, WkTest, run, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch  # noqa: E402


def _clean(env):
    full_env = dict(os.environ)
    full_env.pop("WK_MARKER", None)
    full_env.pop("XDG_STATE_HOME", None)
    full_env.pop("WK_STORE", None)
    if env:
        full_env.update(env)
    return full_env


def run_impl(name, *args, env=None, timeout=30, split=False):
    if split:
        return subprocess.run(
            [str(REPO / "cmd" / name), *args],
            cwd=str(REPO), env=_clean(env),
            capture_output=True, text=True, timeout=timeout,
        )
    cp = subprocess.run(
        [str(REPO / "cmd" / name), *args],
        cwd=str(REPO), env=_clean(env),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )
    cp.stderr = ""
    return cp


class TestQuietFlag(WkTest):

    def test_quiet_suppresses_info_and_log_but_not_warn(self):
        cp = self.bash(
            '. lib/common.sh\n'
            'info "an info line"\n'
            'log "a log line"\n'
            'warn "a warning"\n',
            env={"WK_QUIET": "1"},
        )
        self.assertNotIn("an info line", cp.stderr)
        self.assertNotIn("a log line", cp.stderr)
        self.assertIn("a warning", cp.stderr)
        self.assertIn("a log line", self.bash('. lib/common.sh\nlog "a log line"\n').stderr)


    def test_a_readonly_command_with_quiet_still_exits_zero_with_no_narration(self):
        empty = self.tmp / "machines"
        empty.mkdir()
        cp = run("machine", "ls", "--quiet", env={"WK_MACHINES_DIR": str(empty)})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("==>", cp.stdout)


class TestLsJson(WkTest):
    def test_ls_json_is_one_valid_document(self):
        with temp_store() as store:
            cp = run_impl("ls", "--json", split=True,
                           env={"WK_STORE": store["WK_STORE"], "WK_TARGET": "container"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(len(cp.stdout.strip().splitlines()), 1, cp.stdout)
        doc = json.loads(cp.stdout)
        self.assertIsInstance(doc, dict)
        self.assertIsInstance(doc["workspaces"], list)
        for row in doc["workspaces"]:
            self.assertEqual(
                set(row.keys()),
                {"name", "target", "state", "base", "snap", "arch", "changes"},
            )


    def test_json_merge_list_merges_concatenated_documents(self):
        with temp_store() as store:
            d = Path(store["WK_STORE"])
            (d / "a.json").write_text('{"workspaces": [{"name": "a"}]}\n')
            (d / "b.json").write_text(
                '{"workspaces": [{"name": "b"}]}{"workspaces": [{"name": "c"}]}\n'
            )
            (d / "empty.json").write_text("")
            doc = dispatch.json_merge_list("workspaces", [
                str(d / "a.json"), str(d / "b.json"),
                str(d / "empty.json"), str(d / "does-not-exist.json")])
        names = sorted(w["name"] for w in doc["workspaces"])
        self.assertEqual(names, ["a", "b", "c"])


