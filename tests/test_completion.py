"""`wk --declarations` and `wk completion` -- the machine-readable command
dump, and the shell completion script the dispatcher builtin generates from
the declarations (lib/wk/completion.py). Each docstring is the phrase of the
behaviour it checks.

Run: python3 -m unittest tests.test_completion -v
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, WK, WkTest, run, where_values

sys.path.insert(0, str(REPO / "lib"))
from wk import completion as C          # noqa: E402
from wk import decl as D                 # noqa: E402
from wk.dispatch import TOMBSTONES       # noqa: E402
from wk.machine import Local             # noqa: E402

VALID_WHERE = where_values()


class TestDeclarations(WkTest):
    def test_declarations_lists_every_cmd_entry_with_a_valid_where(self):
        """`wk --declarations` lists every cmd/* entry with a valid where"""
        cp = run("--declarations")
        self.assertEqual(cp.returncode, 0, cp.stdout)

        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        seen = {}
        for line in lines:
            fields = line.split("\t")
            self.assertGreaterEqual(len(fields), 5, f"fewer than 5 tab-separated fields: {line!r}")
            name, where, dname, group, syn = fields[:5]
            seen[name] = (where, dname, group, syn)
            self.assertIn(where, VALID_WHERE, f"{name}: where={where!r} is not one of {VALID_WHERE}")
            # `required@2` is the same declaration with the name at another
            # positional (`wk ai claude <ws>`); the slot is a suffix, not a
            # fourth kind.
            self.assertIn(dname.split("@")[0], ("required", "optional", "none"),
                          f"{name}: name={dname!r}")
            if "@" in dname:
                self.assertTrue(dname.split("@")[1].isdigit(),
                                f"{name}: name={dname!r} has no positional after the @")
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

    def test_declarations_omits_the_completion_builtin(self):
        """`wk --declarations` dumps `cmd/` files; completion has none, so it is absent"""
        cp = run("--declarations")
        names = [l.split("\t")[0] for l in cp.stdout.splitlines() if l.strip()]
        self.assertNotIn("completion", names)


class TestCompletionGenerator(unittest.TestCase):
    """The generator itself (lib/wk/completion.py), read directly -- no shell,
    no subprocess, no `-h` text."""

    def test_every_declared_command_and_its_opts_appear(self):
        """every non-tombstoned command, and every flag it declares anywhere, is in the generated script"""
        script = C.generate(REPO, "bash", TOMBSTONES)
        for d in D.all_commands(REPO):
            if d.name in TOMBSTONES:
                continue
            self.assertIn(d.name, script, f"{d.name} missing from the generated script")
            for opt in C.flags_for(d):
                self.assertIn(opt, script, f"{d.name}'s {opt} missing from the generated script")

    def test_a_tombstoned_command_does_not_complete(self):
        """a tombstoned command is never offered as a completion"""
        script = C.generate(REPO, "bash", TOMBSTONES)
        m = re.search(r"_wk_commands='([^']*)'", script)
        self.assertIsNotNone(m, script)
        offered = m.group(1).split()
        for name in TOMBSTONES:
            self.assertNotIn(name, offered, f"tombstoned command '{name}' still completes")

    def test_completion_itself_completes_though_it_has_no_cmd_file(self):
        """`completion` is a builtin with no `cmd/` file, and still offered"""
        self.assertIn("completion", C.commands(REPO, TOMBSTONES))

    def test_a_values_list_answered_by_a_store_is_not_asked_at_tab(self):
        """bench's values= list runs on its store, so completion never asks it; build's runs here"""
        cmds = {d.name: d for d in D.all_commands(REPO)}
        self.assertEqual(C.values_cmd(cmds["bench"]), "")
        self.assertEqual(C.values_cmd(cmds["build"]), "--list")

    def test_workspace_listing_never_touches_a_machine(self):
        """`local_workspaces` reads local state only: a probe of a machine (podman,
        ssh) runs through `Local.run`/`run_tty`, and neither is ever called"""
        tmp = tempfile.mkdtemp(prefix="wk-completion-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))
        env = dict(os.environ, WK_STORE=tmp)
        with mock.patch.object(Local, "run", side_effect=AssertionError("probed a machine")), \
             mock.patch.object(Local, "run_tty", side_effect=AssertionError("probed a machine")):
            self.assertEqual(C.local_workspaces(REPO, env=env), ["demo-ws"])


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
        # (lib/wk/completion.py, local_workspaces) -- container is the one
        # built-in kind whose store is plain $WK_STORE.
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))

        reply = self._complete([str(WK), "build", ""], 2, env={"WK_STORE": tmp})
        self.assertIn("demo-ws", reply)

    def test_config_flag_completes_build_configs(self):
        """`--config <TAB>` offers build configs, on a command that only shares the word"""
        reply = self._complete([str(WK), "test", "somews", "--config", ""], 4)
        self.assertIn("jsc-release", reply)
        self.assertIn("mac-release", reply)

    def test_the_word_after_a_value_taking_flag_is_not_the_workspace(self):
        """`wk build --branch x <TAB>` still completes the workspace slot"""
        tmp = tempfile.mkdtemp(prefix="wk-completion-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "ws", "demo-ws"))
        reply = self._complete([str(WK), "build", "--branch", "x", ""], 4, env={"WK_STORE": tmp})
        self.assertIn("demo-ws", reply)

    def test_the_argument_after_the_workspace_offers_the_declared_values(self):
        """`wk build ws <TAB>` offers the configs `wk build --list` prints"""
        reply = self._complete([str(WK), "build", "somews", ""], 3)
        self.assertIn("jsc-release", reply)
        self.assertNotIn("available", reply)

    def test_a_subverb_completes(self):
        """`wk key <TAB>` offers key's declared subverbs"""
        reply = self._complete([str(WK), "key", ""], 2)
        self.assertIn("sudo", reply)

    def test_completion_offers_its_shells(self):
        """`wk completion <TAB>` offers bash and zsh"""
        self.assertEqual(self._complete([str(WK), "completion", ""], 2), ["bash", "zsh"])


if __name__ == "__main__":
    unittest.main()
