"""Dispatcher, declaration and help behaviour -- port of the dispatcher-shaped
checks from cmd/selftest's `quick` section. Each docstring is the
phrase of the behaviour it checks.

Run: python3 -m unittest tests.test_dispatcher -v
"""
import os
import subprocess
import unittest

from tests.support import (
    REAL_REGISTRY, REPO, WkTest, fake_workspace, rand_suffix, run, stub_path,
    where_values,
)


class TestHelpAndDeclarations(WkTest):
    def test_help_lists_every_cmd_entry(self):
        """`wk help` lists every cmd/* entry"""
        # A bare `wk` prints the listing (exit 2); `wk help` prints README.md.
        help_out = run().stdout
        missing = []
        for c in sorted((REPO / "cmd").iterdir()):
            if not (c.is_file() and __import__("os").access(c, __import__("os").X_OK)):
                continue
            import re

            if not re.search(rf"(?m)^  {re.escape(c.name)}( |$)", help_out):
                missing.append(c.name)
        self.assertEqual(missing, [], f"not listed by 'wk help': {missing}")

    def test_every_command_declares_itself_to_the_dispatcher(self):
        """every command under cmd/ declares itself to the dispatcher"""
        import os

        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            n = f.name
            lines = f.read_text(errors="replace").splitlines()
            head = lines[:15]
            line3 = lines[2] if len(lines) > 2 else ""
            if line3.endswith("."):
                bad.append(f"{n}: synopsis summary ends in a period")
            elif not (
                line3.startswith(f"# wk {n} -- ") or (line3.startswith(f"# wk {n} ") and " -- " in line3)
            ):
                bad.append(f"{n}: line 3 is not a one-line synopsis")

            decl_lines = [l for l in head if l.startswith("# wk:")]
            if not decl_lines:
                bad.append(f"{n}: no '# wk:' declaration line in the first 15 lines")
                continue
            has_where = has_group = False
            where_val = ""
            for line in decl_lines:
                rest = line[len("# wk:"):]
                if rest.startswith(" sub ") or rest.startswith(" flag "):
                    continue
                for tok in rest.split():
                    if tok.startswith("where="):
                        has_where = True
                        where_val = tok[len("where="):]
                    elif tok.startswith("group="):
                        has_group = True
            if not has_where:
                bad.append(f"{n}: '# wk:' has no where=")
            if not has_group:
                bad.append(f"{n}: '# wk:' has no group=")
            if where_val not in ("",) + where_values():
                bad.append(f"{n}: where={where_val} is not one of {'|'.join(where_values())}")
        self.assertEqual(bad, [], f"commands that do not declare themselves: {bad}")

    def test_explain_every_command_answers_without_running_anything(self):
        """every command answers `--explain` without running anything"""
        import os

        bad = []
        for c in sorted((REPO / "cmd").iterdir()):
            if not (c.is_file() and os.access(c, os.X_OK)):
                continue
            n = c.name
            cp = run(n, "--explain")
            if cp.returncode != 0:
                bad.append(f"{n}(exit {cp.returncode})")
                continue
            out = cp.stdout
            if "  changes things: " not in out:
                bad.append(f"{n}(no-role)")
            if "what it does" not in out:
                bad.append(f"{n}(no-header)")
            idx = out.find("what it does")
            if idx != -1 and len(out[idx:].splitlines()) < 4:
                bad.append(f"{n}(empty-header)")
        self.assertEqual(bad, [], f"'wk <cmd> --explain' is not usable for: {bad}")

    def test_unknown_command_prints_usage_and_exits_2(self):
        """an unknown command prints the usage and exits 2"""
        cp = run("nosuchcommand")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("unknown command", cp.stdout + cp.stderr)

    def test_unknown_target_names_the_conf_to_write(self):
        """an unconfigured name is refused, and the error prints the conf to write"""
        # The real registry: the message names the conf to write in it, and
        # that path is what this checks (the suite is otherwise pointed at an
        # empty one -- tests.support.NO_REGISTRY).
        cp = run("ls", env={"WK_TARGET": "nosuchtarget-selftest",
                            "WK_TARGET_REGISTRY": str(REAL_REGISTRY)})
        self.assertNotEqual(cp.returncode, 0, "an unknown target was accepted")
        self.assertIn(
            "targets/hosts/nosuchtarget-selftest.conf", cp.stdout + cp.stderr
        )


class TestWorkspaceRefusals(WkTest):
    """Verifies the dispatcher's where=host / where=workspace boundary: a
    host-only command must refuse inside a workspace, and it must name the
    reason rather than fail some other way."""

    def test_wk_verify_refuses_inside_a_workspace(self):
        """`wk verify` refuses inside a workspace"""
        with fake_workspace() as ws:
            cp = ws.run("verify", "selftest-ws")
        self.assertNotEqual(cp.returncode, 0, "wk verify succeeded inside a workspace")
        self.assertIn("acts on a host", cp.stdout + cp.stderr)

    def test_host_only_commands_refuse_inside_a_workspace(self):
        """host-only commands refuse inside a workspace"""
        for c in ("gc", "vm", "pi", "session", "quiesce"):
            with self.subTest(cmd=c):
                with fake_workspace() as ws:
                    cp = ws.run(c)
                self.assertNotEqual(cp.returncode, 0, f"'wk {c}' was accepted inside a workspace")
                self.assertIn(
                    "acts on a host",
                    cp.stdout + cp.stderr,
                    f"'wk {c}' refused for some other reason: {cp.stdout + cp.stderr}",
                )

    def test_a_host_refusal_names_the_invocation_for_outside(self):
        """the refusal prints the exact command to type on the host, arguments and all"""
        with fake_workspace() as ws:
            cp = ws.run("pi", "boot-order", "rpi4", "--dry-run")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("From the host:  wk pi boot-order rpi4 --dry-run", cp.stdout + cp.stderr)
        with fake_workspace() as ws:
            cp = ws.run("gc")
        self.assertIn("From the host:  wk gc", cp.stdout + cp.stderr)

    def test_bridge_is_host_only_and_ls_starts_nothing(self):
        """is refused inside a workspace and on a shared build machine"""
        with fake_workspace() as ws:
            cp = ws.run("bridge", "ls")
        self.assertNotEqual(cp.returncode, 0, "not refused inside a workspace")
        self.assertIn(
            "acts on a host",
            cp.stdout + cp.stderr,
            f"refused, but not as a host-only command: {cp.stdout + cp.stderr}",
        )

    def test_build_arg_forms_in_workspace_vs_on_host(self):
        """`wk build <config>` inside, `wk build <ws> <config>` outside"""
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, f"in-workspace 'wk build <config>' failed: {cp.stdout + cp.stderr}")
            self.assertIn("workspace: selftest-ws", cp.stdout)

            cp2 = ws.run("build", "otherws", "jsc-release", "--dry-run")
            self.assertNotEqual(cp2.returncode, 0, "'wk build <ws> <config>' was accepted inside a workspace")
            self.assertIn("no name", cp2.stdout + cp2.stderr)

        # The host form typed outside with a bare argument: must ask for the
        # config rather than guess. WK_IN_VM=1 pins this to argument parsing
        # so a bare host does not go boot a podman VM to find out.
        cp3 = run("build", "jsc-release", env={"WK_IN_VM": "1"})
        self.assertEqual(cp3.returncode, 2, f"host 'wk build <config>' exited {cp3.returncode}, expected 2")
        self.assertIn("usage: wk build <workspace> <config>", cp3.stdout + cp3.stderr)

    def test_broker_door_is_narrow(self):
        """names the socket and the stage that opens it"""
        with fake_workspace() as ws:
            cp = ws.run("pi", "setup", "some-host", env={"WK_BROKER_SOCKET": str(ws.tmp / "no-such-broker.sock")})
        self.assertNotEqual(cp.returncode, 0, "'wk pi setup' was accepted inside a workspace")
        self.assertIn("acts on a host", cp.stdout + cp.stderr)

        with fake_workspace() as ws:
            cp2 = ws.run(
                "boot", "rpi4", "--status",
                env={"WK_BROKER_SOCKET": str(ws.tmp / "no-such-broker.sock")},
            )
        self.assertNotEqual(cp2.returncode, 0, "'wk boot' answered inside a workspace with no broker")
        self.assertIn(
            "setup --stage broker",
            cp2.stdout + cp2.stderr,
            "the refusal does not name the stage that opens the door",
        )


class TestStatusDefaultView(WkTest):
    def test_status_default_view_is_text_unless_at_a_terminal(self):
        """a bare `wk status` **at a terminal opens the page**"""
        # The tty half needs a pty and is verified by hand (same carve-out
        # cmd/selftest's chk_status_default_view documents); this checks the
        # half that matters for scripting: not-a-terminal defaults to text.
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
status_default_mode; m="$WK_STATUS_DEFAULT_MODE"
[ "$m" = text ] || {{ echo "redirected stdout defaulted to '$m', not text"; exit 1; }}
( WK_STATUS_VIEW=json; status_default_mode; [ "$WK_STATUS_DEFAULT_MODE" = json ] ) \\
    || {{ echo "WK_STATUS_VIEW did not decide the view"; exit 1; }}
( CI=1; status_default_mode; [ "$WK_STATUS_DEFAULT_MODE" = text ] ) \\
    || {{ echo "CI did not fall back to the table"; exit 1; }}
'''
        cp = self.bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()


class TestUnknownWorkspaceName(WkTest):
    """A name no workspace answers to is the dispatcher's to refuse, once, for
    every command that takes one -- so no command ignores it, and none reports
    it as an argument it never expected."""

    # Every workspace command that takes a name and does not create or destroy
    # one. `new` and `rm` are the lifecycle pair: they are handed a name with
    # nothing behind it. The rest need nothing installed to reach the refusal,
    # which is why `start`/`stop` (needs podman) and `bench` (needs a plan) are
    # exercised through the declaration check below rather than by running.
    COMMANDS = (
        "build", "enter", "gui", "logs", "pick", "pr", "profile",
        "remotes", "run", "status", "sync", "test", "verify", "zed",
    )

    def test_a_name_at_another_slot_is_refused_the_same_way(self):
        """`wk ai claude <name>` -- the name is the second positional
        (name=required@2), and an unknown one is still the dispatcher's to
        refuse, with the synopsis."""
        name = "nosuchws-" + rand_suffix()
        # WK_TARGET, so the refusal is this machine's rather than the podman
        # VM's: an unknown name resolves to the container target, and a macOS
        # host forwards a container command into the VM, whose own dispatcher
        # would answer instead -- from whatever copy of wk-tools was last
        # pushed in there.
        cp = run("ai", "claude", name, env={"WK_TARGET": "vm"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 2, out)
        self.assertIn(f"no such workspace: {name}", out)
        self.assertIn("usage: wk ai", out)

    @staticmethod
    def _takes(cmd):
        """The command's own `takes=<n>` (default 0), read the same way the
        dispatcher's decl_load does: from the first non-sub, non-flag
        `# wk:` line in the header."""
        import re

        head = (REPO / "cmd" / cmd).read_text(errors="replace").splitlines()[:15]
        for line in head:
            if not line.startswith("# wk:"):
                continue
            rest = line[len("# wk:"):].strip()
            if rest.startswith("sub ") or rest.startswith("flag "):
                continue
            m = re.search(r"takes=(\d+)", rest)
            return int(m.group(1)) if m else 0
        return 0

    def test_every_command_refuses_a_name_no_workspace_answers_to(self):
        """an unknown workspace name is refused, with the synopsis, exit 2"""
        # A command with `takes=1` -- `wk pr [<workspace>] <ref>` -- reads a
        # lone positional as its own argument, not a name (see `wk` lines
        # 89-94): `wk pr <unknown>` alone is "which workspace", not a name
        # refusal. Give it a second positional so the first really is read
        # as the name.
        name = "nosuchws-" + rand_suffix()
        for c in self.COMMANDS:
            with self.subTest(cmd=c):
                takes = self._takes(c)
                extra = tuple(f"arg{i}" for i in range(takes))
                cp = run(c, name, *extra)
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 2, f"'wk {c} {name} {' '.join(extra)}' exited {cp.returncode}:\n{out}")
                self.assertIn(f"no such workspace: {name}", out)
                self.assertIn(f"usage: wk {c}", out, f"the refusal does not print the synopsis:\n{out}")

    def test_a_workspace_command_never_ignores_the_name_it_was_given(self):
        """`wk stop <unknown>` refuses rather than acting on every workspace"""
        # The failure this pins: `optional` used to mean "take the positional
        # only if it names a workspace", so a typo fell through to a command
        # that read it as "no name given" -- all of them.
        name = "nosuchws-" + rand_suffix()
        cp = run("stop", name)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, f"'wk stop {name}' was accepted:\n{out}")
        self.assertNotIn("stopping", out, f"'wk stop {name}' acted on something:\n{out}")

    def test_name_declarations_are_one_of_three_words(self):
        """every `name=` in a declaration is required, optional or none"""
        import os
        import re

        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            for line in f.read_text(errors="replace").splitlines()[:15]:
                if not line.startswith("# wk:"):
                    continue
                for tok in re.findall(r"name=(\S*)", line):
                    if tok.split("@")[0] not in ("required", "optional", "none"):
                        bad.append(f"{f.name}: name={tok}")
        self.assertEqual(bad, [], f"declarations the dispatcher cannot read: {bad}")


class TestZedNames(WkTest):
    """`wk zed` is the one command whose name may be a machine instead of a
    workspace, and the only one that resolves a name itself."""

    def test_a_workspace_name_is_refused_by_the_dispatcher(self):
        """`wk zed <unknown>` names the mistake, not a second argument"""
        name = "nosuchws-" + rand_suffix()
        cp = run("zed", name)
        out = cp.stdout + cp.stderr
        self.assertIn(f"no such workspace: {name}", out, out)
        self.assertNotIn("one name at a time", out, out)

    def test_tools_takes_a_name_the_dispatcher_does_not_answer_for(self):
        """`wk zed --tools <unknown>` is zed's own refusal, naming both kinds"""
        # `flag --tools name=none` hands the name through; before it, the
        # dispatcher consumed nothing and zed read the leftover as a second
        # name -- which is what `wk zed --tools <machine>` always hit.
        name = "nosuchws-" + rand_suffix()
        cp = run("zed", "--tools", name)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        if "zed is not installed" in out:
            self.skipTest("zed is not installed here, so the name is never reached")
        self.assertIn(f"no machine or workspace named '{name}'", out, out)

    def test_two_names_are_still_two_names(self):
        """a second name is refused by name"""
        cp = run("zed", "--tools", "one-" + rand_suffix(), "two-" + rand_suffix())
        out = cp.stdout + cp.stderr
        if "zed is not installed" in out:
            self.skipTest("zed is not installed here, so the names are never reached")
        self.assertIn("one name at a time", out, out)


class TestFlagNameOverride(WkTest):
    """`flag <--x> name=<n>` is how a flag says the name does not arrive the
    way the command's other invocations bring it -- because it is a machine
    (`wk zed --tools`), or because the flag answers without one."""

    def test_a_flag_can_answer_without_a_workspace(self):
        """`wk profile --list` prints the modes rather than asking for a name"""
        cp = run("profile", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("sampling", cp.stdout + cp.stderr)

    def test_the_override_applies_only_to_that_flag(self):
        """without the flag the same command still wants a name"""
        cp = run("profile")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("usage: wk profile", cp.stdout + cp.stderr)


class TestSubverbNeedsOverride(WkTest):
    """`sub <verbs> needs=` clears a command's top-level `needs` for the
    subverbs that do not use the thing. cmd/key declares `needs gh,gh-auth`
    because `register` and `check` call the GitHub API -- but `ensure` is
    ssh-keygen and a file, run over ssh on build machines that have no `gh`
    on them, and `tailnet` stores a credential that never reaches GitHub."""

    # `have gh` passes (the stub is on PATH) so the only thing left to
    # refuse on is gh-auth: one refusal under test, not two.
    GH_DEAD = '#!/bin/sh\nexit 1\n'

    def test_the_subverbs_that_call_github_are_refused(self):
        """`wk key check` with a gh that cannot reach the API is refused"""
        with stub_path({"gh": self.GH_DEAD}) as binp:
            cp = run("key", "check",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("gh auth login", cp.stdout)

    def test_the_subverbs_that_do_not_are_left_alone(self):
        """`wk key show` reads the store and never asks GitHub anything, so
        the same dead gh does not stop it"""
        with stub_path({"gh": self.GH_DEAD}) as binp:
            cp = run("key", "show",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotIn("gh auth login", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)


class TestTopLevelCallOrder(WkTest):
    """bash resolves a function when the call runs, so a command that calls one
    of its own functions at top level before defining it fails at that line --
    `bash -n` sees nothing wrong. `wk sync <workspace>` did exactly this."""

    def test_no_command_calls_a_function_it_has_not_defined_yet(self):
        """every function a command calls at top level is defined above it"""
        import os
        import re

        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            lines = f.read_text(errors="replace").splitlines()
            # A function body in this tree opens with `name() {` in column 0
            # -- optionally with an argument comment after the brace, which is
            # this tree's house style -- and closes with `}` in column 0;
            # anything between the two is resolved when that function is
            # called, not where it is written.
            defined = {}
            body_of = [None] * len(lines)
            cur = None
            for i, line in enumerate(lines):
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{\s*(#.*)?$", line)
                if m and cur is None:
                    cur = m.group(1)
                    defined.setdefault(cur, i)
                body_of[i] = cur
                if cur is not None and line == "}":
                    cur = None
            for i, line in enumerate(lines):
                if body_of[i] is not None or line.lstrip().startswith("#"):
                    continue
                m = re.match(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*([A-Za-z_][A-Za-z0-9_]*)\b", line)
                if not m:
                    continue
                fn = m.group(1)
                if fn in defined and defined[fn] > i:
                    bad.append(f"{f.name}:{i + 1}: calls {fn}(), defined at line {defined[fn] + 1}")
        self.assertEqual(bad, [], "functions called before they are defined: " + "; ".join(bad))


class TestWhereTheNameSitsInArgv(WkTest):
    """`takes=<n>` (the declaration) and argv_name (the dispatcher): a command
    whose own positional follows the workspace name -- `wk pr [<workspace>]
    <ref>` -- has a lone positional read as *its* argument, not as a workspace
    name. Without it a pull request was refused as a workspace typo:

        wk pr justinmichaud:eng/some-branch
        warning: no such workspace: justinmichaud:eng/some-branch
    """

    FUNCS = ("decl_load", "in_list", "sub_override", "flag_override",
             "cmd_name", "cmd_takes", "name_slot", "positional",
             "positional_count", "argv_name", "resolve_target")

    def _name(self, cmd, *args):
        import shlex
        lifted = "\n".join(
            subprocess.run(["sed", "-n", f"/^{f}() {{/,/^}}/p", str(REPO / "wk")],
                           capture_output=True, text=True).stdout
            for f in self.FUNCS)
        quoted = " ".join(shlex.quote(a) for a in args)
        cp = self.bash(
            f'. "{REPO}/lib/common.sh"\n{lifted}\n'
            f'decl_load "{REPO}/cmd/{cmd}"\n'
            f'decl=$(cmd_name {quoted}); slot=$(name_slot "$decl"); takes=$(cmd_takes {quoted})\n'
            # `none` is decided by the caller (resolve_target, main), the same
            # way the dispatcher does it -- argv_name only answers "which
            # positional".
            f'if [ "${{decl%%@*}}" = none ]; then echo NONE\n'
            f'else argv_name "$slot" "$takes" {quoted} || echo NONE; fi\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_a_lone_ref_is_the_commands_own_argument(self):
        self.assertEqual(self._name("pr", "justinmichaud:eng/some-branch"), "NONE")
        self.assertEqual(self._name("pr", "1234"), "NONE")

    def test_a_workspace_in_front_of_the_ref_is_the_name(self):
        self.assertEqual(self._name("pr", "myws", "1234"), "myws")

    def test_a_subverb_that_takes_nothing_keeps_its_name_slot(self):
        """`wk pr rebase [<ws>]` -- the name is the second positional, and a
        bare `wk pr rebase` has none (sub rebase name=optional@2 takes=0)."""
        self.assertEqual(self._name("pr", "rebase"), "NONE")
        self.assertEqual(self._name("pr", "rebase", "myws"), "myws")

    def test_a_command_with_no_takes_still_claims_its_first_positional(self):
        """The default is takes=0: `wk stop <typo>` must still be refused as a
        workspace name, not passed through as an argument -- once upon a time
        that stopped every workspace on the machine."""
        self.assertEqual(self._name("stop", "typo"), "typo")
        self.assertEqual(self._name("build", "myws", "jsc-release"), "myws")

    def test_a_command_that_takes_no_name_has_none_in_its_argv(self):
        """name=none means the positionals are all the command's: `wk sysimage
        write` is a subverb, never a workspace."""
        self.assertEqual(self._name("sysimage", "write"), "NONE")

    def test_takes_is_declared_wherever_a_positional_follows_an_optional_name(self):
        """A command with an *optional* name and a positional of its own after
        it must declare takes=, or that positional is read as a workspace name
        and refused as a typo. name=required is a different shape: from a host
        the name really is the first positional, and the `[<workspace>]` in
        those synopses means "omitted inside a workspace" (`wk pick <commit>`)."""
        import re
        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            head = "\n".join(f.read_text(errors="replace").splitlines()[:15])
            if "name=optional" not in head:
                continue
            syn = re.search(r"^# wk \S+ (.*?) -- ", head, re.M)
            if not syn or "[<workspace>]" not in syn.group(1):
                continue
            after = syn.group(1).split("[<workspace>]", 1)[1].strip()
            # An optional flag is not a positional: `[--fix]`, `[--keep-vm]`.
            after = after.lstrip("[")
            if not after or after.startswith("-"):
                continue
            if "takes=" not in head:
                bad.append(f"{f.name}: '{syn.group(1)}' takes an argument after "
                           f"the workspace but declares no takes=")
        self.assertEqual(bad, [], "; ".join(bad))


class TestTheDirectoryNamesTheWorkspaceOnABuildBox(WkTest):
    """cwd_workspace (the dispatcher): a shared build machine holds several
    workspaces side by side under one root and has no workspace marker to be
    inside of, so the directory is what says which one is meant. Without it
    the in-workspace interface -- `wk build <config>`, no name -- worked in a
    container workspace and nowhere else."""

    FUNC = "cwd_workspace"

    def _name(self, marker_root, cwd, remote=True):
        lifted = subprocess.run(
            ["sed", "-n", f"/^{self.FUNC}() {{/,/^}}/p", str(REPO / "wk")],
            capture_output=True, text=True).stdout
        assert lifted.strip(), "cwd_workspace() is gone from the dispatcher"
        stubs = (f'in_remote_host() {{ {"return 0" if remote else "return 1"}; }}\n'
                 f'wk_remote_field() {{ printf %s {marker_root!r}; }}\n')
        cp = self.bash(f'. "{REPO}/lib/common.sh"\n{stubs}{lifted}\n'
                       f'cd {cwd!r} || exit 3\n'
                       f'cwd_workspace || echo NONE\n')
        self.assertIn(cp.returncode, (0,), cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wk"
        (self.root / "ws" / "image-decoders" / "WebKit" / "Source").mkdir(parents=True)
        (self.root / "cache").mkdir()

    def test_standing_in_a_workspace_names_it(self):
        self.assertEqual(
            self._name(str(self.root), str(self.root / "ws" / "image-decoders")),
            "image-decoders")

    def test_standing_deep_inside_one_names_it_too(self):
        self.assertEqual(
            self._name(str(self.root),
                       str(self.root / "ws" / "image-decoders" / "WebKit" / "Source")),
            "image-decoders")

    def test_standing_elsewhere_under_the_root_names_nothing(self):
        self.assertEqual(self._name(str(self.root), str(self.root / "cache")), "NONE")
        self.assertEqual(self._name(str(self.root), str(self.root)), "NONE")

    def test_a_machine_that_is_not_a_build_box_is_never_asked(self):
        """A workstation's own directories are not workspaces; the marker is
        what makes the question meaningful at all."""
        self.assertEqual(
            self._name(str(self.root), str(self.root / "ws" / "image-decoders"),
                       remote=False),
            "NONE")


class TestHelpNamesEveryWhereOverride(WkTest):
    """`wk <cmd> -h`'s `runs on:` line is the top-level `where=`, which is the
    wrong answer for most invocations of a command whose subverbs or flags
    override it: `wk push` declares `where=store`, and `on`/`off`/`status`
    run here. So every override is named under it, in the prose of the one
    table both lines come from (`where_prose` in the dispatcher)."""

    @staticmethod
    def _overrides(path):
        """Every `sub`/`flag ... where=<w>` line of a command's header, as
        (verbs, where) -- read the way decl_load reads them."""
        out = []
        for line in path.read_text(errors="replace").splitlines()[:15]:
            if not line.startswith("# wk:"):
                continue
            rest = line[len("# wk:"):]
            if not (rest.startswith(" sub ") or rest.startswith(" flag ")):
                continue
            fields = rest.split(None, 1)[1].split()
            where = [t[len("where="):] for t in fields[1:] if t.startswith("where=")]
            if where:
                out.append((fields[0], where[0]))
        return out

    def _prose(self, where):
        """The dispatcher's own words for one `where=` value, lifted from
        `wk` rather than retyped here -- the point of the change is that
        there is one table, and a copy in a test is a second one."""
        lifted = subprocess.run(
            ["sed", "-n", "/^where_prose() {/,/^}/p", str(REPO / "wk")],
            capture_output=True, text=True).stdout
        self.assertTrue(lifted.strip(), "where_prose() is gone from the dispatcher")
        cp = self.bash(f'D_HERE="" D_LIFECYCLE=""\n{lifted}\nwhere_prose {where}\n')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_every_override_is_named_with_its_where(self):
        """each overriding subverb and flag, with where it runs"""
        checked = 0
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            overrides = self._overrides(f)
            if not overrides:
                continue
            text = run(f.name, "-h").stdout
            for verbs, where in overrides:
                expected = f"    {verbs.replace(',', ', ')}: {self._prose(where)}"
                with self.subTest(cmd=f.name, verbs=verbs):
                    self.assertIn(expected, text.splitlines(),
                                  f"'wk {f.name} -h' does not say where '{verbs}' runs:\n{text}")
                checked += 1
        # The commands that have one today: push, bench, sync, build, pi,
        # profile, pr. A run that checked nothing would pass silently.
        self.assertGreater(checked, 5, "no where= override was checked at all")

    def test_the_top_level_answer_is_still_there(self):
        """the command's own `where=` line comes first, then the overrides"""
        lines = run("push", "-h").stdout.splitlines()
        top = [i for i, l in enumerate(lines) if l.startswith("  runs on: ")]
        self.assertEqual(len(top), 1, lines)
        self.assertIn(self._prose("store"), lines[top[0]])
        self.assertTrue(lines[top[0] + 1].startswith("    on, off, status: "), lines)
