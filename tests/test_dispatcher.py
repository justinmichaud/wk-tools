"""Dispatcher, declaration and help behaviour -- port of the dispatcher-shaped
checks from cmd/selftest's `quick` section. Each docstring is the
phrase of the behaviour it checks.

Run: python3 -m unittest tests.test_dispatcher -v
"""
import unittest

from tests.support import REPO, WkTest, fake_workspace, run


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
            if where_val not in ("", "host", "local", "workspace", "dynamic"):
                bad.append(f"{n}: where={where_val} is not one of host|local|workspace|dynamic")
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
        cp = run("ls", env={"WK_TARGET": "nosuchtarget-selftest"})
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
