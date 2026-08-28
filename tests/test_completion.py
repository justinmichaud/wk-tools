"""`wk --declarations` and `wk completion` -- the machine-readable command
dump and the shell completion scripts built on it. Each docstring is the
phrase of the behaviour it checks.

Run: python3 -m unittest tests.test_completion -v
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.support import REPO, WK, WkTest, run

VALID_WHERE = ("host", "local", "workspace", "dynamic")


class TestDeclarations(WkTest):
    def test_declarations_lists_every_cmd_entry_with_a_valid_where(self):
        """`wk --declarations` lists every cmd/* entry with a valid where"""
        cp = run("--declarations")
        self.assertEqual(cp.returncode, 0, cp.stdout)

        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        seen = {}
        for line in lines:
            fields = line.split("\t")
            self.assertEqual(len(fields), 5, f"not 5 tab-separated fields: {line!r}")
            name, where, dname, group, syn = fields
            seen[name] = (where, dname, group, syn)
            self.assertIn(where, VALID_WHERE, f"{name}: where={where!r} is not one of {VALID_WHERE}")
            self.assertIn(dname, ("required", "optional", "none"), f"{name}: name={dname!r}")
            self.assertTrue(syn.startswith(name), f"{name}: synopsis {syn!r} does not start with the command name")

        on_disk = {
            f.name
            for f in (REPO / "cmd").iterdir()
            if f.is_file() and os.access(f, os.X_OK)
        }
        missing = on_disk - seen.keys()
        self.assertEqual(missing, set(), f"on disk but not in --declarations: {missing}")
        extra = seen.keys() - on_disk
        self.assertEqual(extra, set(), f"in --declarations but not on disk: {extra}")

    def test_declarations_includes_completion_itself(self):
        """`wk --declarations` includes completion itself"""
        cp = run("--declarations")
        names = [l.split("\t")[0] for l in cp.stdout.splitlines() if l.strip()]
        self.assertIn("completion", names)


class TestCompletionScripts(WkTest):
    def test_bash_completion_output_parses_under_bash_dash_n(self):
        """`wk completion bash` output parses under `bash -n`"""
        cp = run("completion", "bash")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self._assert_parses(["bash", "-n"], cp.stdout)

    def test_zsh_completion_output_parses_under_zsh_dash_n_if_zsh_exists(self):
        """`wk completion zsh` output parses under `zsh -n` if zsh exists"""
        zsh = shutil.which("zsh")
        if not zsh:
            self.skipTest("zsh not installed")
        cp = run("completion", "zsh")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self._assert_parses([zsh, "-n"], cp.stdout)

    def test_zsh_completion_registers_wk_where_no_rc_ran_compinit(self):
        """`wk completion zsh` registers `wk` in a zsh whose rc never ran compinit"""
        zsh = shutil.which("zsh")
        if not zsh:
            self.skipTest("zsh not installed")
        cp = run("completion", "zsh")
        self.assertEqual(cp.returncode, 0, cp.stdout)

        # `zsh -f` is the machine whose rc never ran compinit: no rc file at
        # all, so the script has to start the completion system itself or
        # `complete -F` dies in bashcompinit's compdef call. HOME is a scratch
        # directory because compinit writes a dumpfile into it.
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
        """`wk completion` refuses a shell it does not know"""
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
        """Source the generated bash function in a subshell, set COMP_WORDS
        the way bash's programmable completion would, call it directly, and
        report what it put in COMPREPLY -- exactly the mechanics `complete -F`
        drives at TAB, without needing a real interactive readline session.
        """
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

    def test_wk_bu_completes_to_build(self):
        """completing `wk bu` offers `build`"""
        reply = self._complete([str(WK), "bu"], 1)
        self.assertEqual(reply, ["build"])

    def test_completing_the_command_word_offers_every_command(self):
        """completing the bare command word offers every command name"""
        reply = self._complete([str(WK), ""], 1)
        self.assertIn("build", reply)
        self.assertIn("new", reply)
        self.assertIn("completion", reply)

    def test_completing_a_flag_offers_that_commands_own_flags(self):
        """completing `--` after a command offers that command's own flags"""
        reply = self._complete([str(WK), "build", "somews", "--"], 3)
        self.assertIn("--list", reply)

    def test_workspace_slot_reads_local_state_not_wk_ls(self):
        """workspace-name completion reads local state, not `wk ls`"""
        tmp = tempfile.mkdtemp(prefix="wk-completion-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # No registry: a workspace's own store is what completion reads
        # (cmd/completion, list_workspaces_local) -- container is the one
        # built-in kind whose store is plain $WK_STORE.
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))

        reply = self._complete([str(WK), "build", ""], 2, env={"WK_STORE": tmp})
        self.assertIn("demo-ws", reply)


if __name__ == "__main__":
    unittest.main()
