"""Dispatcher, declaration and help behaviour -- port of the dispatcher-shaped
checks from cmd/selftest's `quick` section. Each docstring is the
phrase of the behaviour it checks.

Run: python3 -m unittest tests.test_dispatcher -v
"""
import os
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
        for c in ("sync", "gc", "vm", "pi", "session", "quiesce"):
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
        "build", "claude", "enter", "gui", "logs", "pick", "pr", "profile",
        "remotes", "run", "status", "sync", "test", "verify", "zed",
    )

    def test_every_command_refuses_a_name_no_workspace_answers_to(self):
        """an unknown workspace name is refused, with the synopsis, exit 2"""
        name = "nosuchws-" + rand_suffix()
        for c in self.COMMANDS:
            with self.subTest(cmd=c):
                cp = run(c, name)
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 2, f"'wk {c} {name}' exited {cp.returncode}:\n{out}")
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
            # and closes with `}` in column 0; anything between the two is
            # resolved when that function is called, not where it is written.
            defined = {}
            body_of = [None] * len(lines)
            cur = None
            for i, line in enumerate(lines):
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{\s*$", line)
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
