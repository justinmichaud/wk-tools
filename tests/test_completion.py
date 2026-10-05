"""`wk --declarations` and `wk completion` -- the machine-readable command"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, WK, WkTest, run

sys.path.insert(0, str(REPO / "lib"))
from wk import completion as C          # noqa: E402
from wk import decl as D                 # noqa: E402
from wk.machine import Local             # noqa: E402


class TestDeclarations(WkTest):

    def test_declarations_omits_the_completion_builtin(self):
        cp = run("--declarations")
        names = [l.split("\t")[0] for l in cp.stdout.splitlines() if l.strip()]
        self.assertNotIn("completion", names)


class TestCompletionGenerator(unittest.TestCase):

    def test_completion_itself_completes_though_it_has_no_cmd_file(self):
        self.assertIn("completion", C.commands(REPO))

    def test_a_values_list_answered_by_a_store_is_not_asked_at_tab(self):
        cmds = {d.name: d for d in D.all_commands(REPO)}
        self.assertEqual(C.values_cmd(cmds["bench"]), "")
        self.assertEqual(C.values_cmd(cmds["boot"]), "--list")

    def test_workspace_listing_never_touches_a_machine(self):
        tmp = tempfile.mkdtemp(prefix="wk-completion-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))
        env = dict(os.environ, WK_STORE=tmp)
        with mock.patch.object(Local, "run", side_effect=AssertionError("probed a machine")), \
             mock.patch.object(Local, "run_tty", side_effect=AssertionError("probed a machine")):
            self.assertEqual(C.local_workspaces(REPO, env=env), ["demo-ws"])


class TestCompletionScripts(WkTest):
    def test_bash_completion_output_parses_under_bash_dash_n(self):
        cp = run("completion", "bash")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self._assert_parses(["bash", "-n"], cp.stdout)

    def test_zsh_completion_output_parses_under_zsh_dash_n_if_zsh_exists(self):
        zsh = shutil.which("zsh")
        if not zsh:
            self.skipTest("zsh not installed")
        cp = run("completion", "zsh")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self._assert_parses([zsh, "-n"], cp.stdout)

    def test_zsh_completion_registers_wk_where_no_rc_ran_compinit(self):
        zsh = shutil.which("zsh")
        if not zsh:
            self.skipTest("zsh not installed")
        cp = run("completion", "zsh")
        self.assertEqual(cp.returncode, 0, cp.stdout)

        tmp = tempfile.mkdtemp(prefix="wk-completion-zsh-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        script = os.path.join(tmp, "completion.zsh")
        with open(script, "w") as f:
            f.write(cp.stdout)
        proc = subprocess.run(
            [zsh, "-f", "-c", f"source {script}; print -r -- ${{_comps[wk]}}"],
            env={**os.environ, "HOME": tmp},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.stderr, "", f"the completion script complained:\n{proc.stderr}")
        self.assertIn("_wk_completion", proc.stdout, "`wk` was left with no completion")

    def test_completion_refuses_an_unknown_shell(self):
        cp = run("completion", "fish")
        self.assertNotEqual(cp.returncode, 0)

    def _assert_parses(self, checker, script):
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            cp = subprocess.run(checker + [path], capture_output=True, text=True, timeout=15)
            self.assertEqual(cp.returncode, 0, f"{checker[0]} -n failed:\n{cp.stderr}\n---\n{script}")
        finally:
            os.unlink(path)


class TestBashCompletionFunction(WkTest):
    def _complete(self, comp_words, cword, env=None):
        words = " ".join(f"'{w}'" for w in comp_words)
        script = f"""
set -e
source <("{WK}" completion bash)
COMP_WORDS=({words})
COMP_CWORD={cword}
_wk_completion
printf '%s\\n' "${{COMPREPLY[@]}}"
"""
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        cp = subprocess.run(
            ["bash", "-c", script],
            cwd=str(REPO),
            env=full_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return [l for l in cp.stdout.splitlines() if l]

    def test_a_prefix_completes_to_the_command_or_verb(self):
        self.assertEqual(self._complete([str(WK), "bu"], 1), ["build"])
        self.assertEqual(self._complete([str(WK), "key", "che"], 2), ["check"])

    def test_completing_the_command_word_offers_every_command(self):
        reply = self._complete([str(WK), ""], 1)
        self.assertIn("build", reply)
        self.assertIn("new", reply)
        self.assertIn("completion", reply)


    def test_config_flag_completes_build_configs(self):
        reply = self._complete([str(WK), "test", "somews", "--config", ""], 4)
        self.assertIn("jsc-release", reply)
        self.assertIn("mac-release", reply)

    def test_the_word_after_a_value_taking_flag_is_not_the_workspace(self):
        reply = self._complete([str(WK), "build", "--branch", "x", ""], 4, env=self._store())
        self.assertIn("demo-ws", reply)

    def test_the_argument_after_the_workspace_offers_the_declared_values(self):
        reply = self._complete([str(WK), "build", "somews", ""], 3)
        self.assertIn("jsc-release", reply)
        self.assertNotIn("available", reply)

    def _store(self):
        tmp = tempfile.mkdtemp(prefix="wk-completion-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))
        return {"WK_STORE": tmp}

    def test_the_workspace_place_is_the_verbs(self):
        env = self._store()
        first = self._complete([str(WK), "bench", ""], 2, env=env)
        self.assertIn("run", first)
        self.assertNotIn("demo-ws", first)
        self.assertIn("demo-ws", self._complete([str(WK), "bench", "run", ""], 3, env=env))

    def test_a_default_verb_that_takes_a_workspace_offers_it_first(self):
        reply = self._complete([str(WK), "pr", ""], 2, env=self._store())
        self.assertIn("rebase", reply)
        self.assertIn("demo-ws", reply)



    def test_every_verb_of_every_command_completes_its_declared_options(self):
        want, lines = {}, []
        for d in D.all_commands(REPO):
            for v in C.subverbs(d):
                want[f"{d.name} {v}"] = " ".join(C.flags_for(d, v))
                lines.append(f"t {d.name} {v}")
        script = ('source <("%s" completion bash)\n'
                  't() { COMP_WORDS=(wk "$1" "$2" --); COMP_CWORD=3; _wk_completion; echo "$1 $2:${COMPREPLY[*]}"; }\n%s\n'
                  % (WK, "\n".join(lines)))
        cp = subprocess.run(["bash", "-c", script], cwd=str(REPO), capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        got = dict(l.split(":", 1) for l in cp.stdout.splitlines())
        self.assertEqual(got, want)

    def test_completion_offers_its_shells(self):
        self.assertEqual(self._complete([str(WK), "completion", ""], 2), ["bash", "zsh"])


if __name__ == "__main__":
    unittest.main()
