"""Dispatcher, declaration and help behaviour."""
TIER = "lint"
import contextlib
import re
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import (
    REAL_MACHINES, REPO, WkTest, fake_workspace, rand_suffix, run, run_here, stub_path,
)

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch, places  # noqa: E402
from wk.machine import Fake  # noqa: E402

CMDS = [c for c in sorted((REPO / "cmd").iterdir()) if c.is_file() and os.access(c, os.X_OK)]


class TestHelpAndDeclarations(WkTest):
    def test_help_lists_every_cmd_entry(self):
        help_out = run().stdout
        missing = [c.name for c in CMDS if not re.search(rf"(?m)^  {re.escape(c.name)}( |$)", help_out)]
        self.assertEqual(missing, [], f"not listed by 'wk help': {missing}")

    def test_every_command_declares_itself_to_the_dispatcher(self):
        bad = []
        for f in CMDS:
            head = [line.rstrip("\n") for line in D.leading_block(f)]
            line3 = head[2] if len(head) > 2 else ""
            if line3.endswith(".") or not (line3.startswith(f"# wk {f.name} ") and " -- " in line3):
                bad.append(f"{f.name}: line 3 is not a one-line synopsis with no closing period")
            tokens = " ".join(l[5:] for l in head if l.startswith("# wk:") and not l[5:].lstrip().startswith(("sub ", "flag ")))
            bad += [f"{f.name}: '# wk:' has no {key}" for key in ("where=", "group=") if not re.search(r"(^|\s)" + key, tokens)]
        self.assertEqual(bad, [], f"commands that do not declare themselves: {bad}")

    def test_explain_every_command_answers_without_running_anything(self):
        bad = [c.name for c in CMDS if run(c.name, "--explain").returncode != 0]
        self.assertEqual(bad, [], f"'wk <cmd> --explain' is not usable for: {bad}")

    def test_explain_names_each_subverbs_own_destructive_override(self):
        cmd = self.tmp / "demo"
        cmd.write_text("#!/usr/bin/env python3\n#\n# wk demo a|b|c -- a demo\n"
                       "# wk: where=host name=none verbs=a,b,c destructive a,--replace\n"
                       "# wk: sub b destructive=\n# wk: sub c destructive=yes\n#\n#   wk demo a   does a\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(dispatch.Exit):
            dispatch.explain("demo", D.Decl(cmd))
        lines = out.getvalue().splitlines()
        for want in ("  destructive: " + dispatch.destructive_prose("a,--replace"),
                     "    b: " + dispatch.destructive_prose(""), "    c: " + dispatch.destructive_prose("yes")):
            self.assertIn(want, lines)

    def test_unknown_command_prints_usage_and_exits_2(self):
        cp = run("nosuchcommand")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("unknown command", cp.stdout + cp.stderr)

    def test_unknown_target_names_the_conf_to_write(self):
        cp = run("ls", env={"WK_PLACE": "nosuchtarget-selftest",
                            "WK_MACHINES_DIR": str(REAL_MACHINES)})
        self.assertNotEqual(cp.returncode, 0, "an unknown place was accepted")
        self.assertIn(
            "machines/nosuchtarget-selftest.conf", cp.stdout + cp.stderr
        )


class TestDelegationReadsTheRegistry(WkTest):

    def setUp(self):
        super().setUp()
        (self.tmp / "hosts").mkdir()
        (self.tmp / "hosts" / "peer.conf").write_text("kind=peer\npeer=1\ntools=/opt/wk-tools\n")
        (self.tmp / "hosts" / "me.conf").write_text("kind=build\nlocal=1\nroot=%s\n" % (self.tmp / "rr"))
        env = {"HOME": str(self.tmp), "XDG_STATE_HOME": str(self.tmp / "state"), "WK_STORE": str(self.tmp / "store"),
               "WK_MACHINES_DIR": str(self.tmp / "hosts"), "WK_IN_VM": "1", "PATH": os.environ.get("PATH", "")}
        self.fake = Fake("host")
        self.reg = places.Registry(REPO, env=env, machine=self.fake)

    def test_a_peer_delegates_this_machine_and_an_unknown_name_do_not(self):
        with mock.patch.object(dispatch, "_registry", self.reg):
            self.assertIs(dispatch.registry(), self.reg)
            peer = dispatch.delegate_driver("peer")
            self.assertEqual((peer.name, peer.delegates()), ("peer", True))
            self.assertIsNone(dispatch.delegate_driver("me"))
            self.assertIsNone(dispatch.delegate_driver("nosuch"))
            self.assertIsNone(dispatch.delegate_driver("container"))
        self.assertEqual(self.fake.effects, [])

    def test_a_peer_that_does_not_answer_is_refused_with_its_name(self):
        with mock.patch.object(dispatch, "_registry", self.reg):
            peer = dispatch.delegate_driver("peer")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(dispatch.Exit) as raised:
                    dispatch.delegate_run(peer, "status", ["ws"])
        self.assertEqual(raised.exception.status, 1)
        self.assertIn("'status' acts on a workspace on peer, and peer did not answer.", err.getvalue())
        self.assertIn("the workspace is that machine's own", err.getvalue())
        self.assertEqual(len([e for e in self.fake.effects if e[1][0] == "ssh"]), 1)


class TestWorkspaceRefusals(WkTest):

    def test_wk_doctor_takes_no_workspace_inside_one(self):
        with fake_workspace() as ws:
            cp = ws.run("doctor", "other-ws")
        self.assertNotEqual(cp.returncode, 0, "wk doctor <ws> was accepted inside a workspace")
        self.assertIn("no workspace argument in here", cp.stdout + cp.stderr)

    def test_host_only_commands_refuse_inside_a_workspace_even_with_the_broker_door(self):
        for argv, broker in ((("gc",), False), (("quiesce",), False), (("machine", "ls"), False),
                             (("machine", "setup", "some-host"), True)):
            with self.subTest(argv=argv), fake_workspace() as ws:
                env = {"WK_BROKER_SOCKET": str(ws.tmp / "no-such-broker.sock")} if broker else None
                cp = ws.run(*argv, env=env)
                self.assertNotEqual(cp.returncode, 0, f"'wk {' '.join(argv)}' was accepted inside a workspace")
                self.assertIn("acts on a host", cp.stdout + cp.stderr)

    def test_a_host_refusal_names_the_invocation_for_outside(self):
        with fake_workspace() as ws:
            cp = ws.run("machine", "setup", "rpi4", "--dry-run")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("wk machine setup rpi4 --dry-run", cp.stdout + cp.stderr)

    def test_build_arg_forms_in_workspace_vs_on_host(self):
        with fake_workspace() as ws:
            cp = ws.run("build", "jsc-release", "--dry-run")
            self.assertEqual(cp.returncode, 0, f"in-workspace 'wk build <preset>' failed: {cp.stdout + cp.stderr}")
            self.assertIn("workspace: selftest-ws", cp.stdout)

            cp2 = ws.run("build", "otherws", "jsc-release", "--dry-run")
            self.assertNotEqual(cp2.returncode, 0, "'wk build <ws> <preset>' was accepted inside a workspace")
            self.assertIn("no workspace argument in here", cp2.stdout + cp2.stderr)

        cp3 = run("build", "jsc-release", env={"WK_IN_VM": "1"})
        self.assertEqual(cp3.returncode, 2, f"host 'wk build <preset>' exited {cp3.returncode}, expected 2")
        self.assertIn("usage: wk build <workspace> <preset>", cp3.stdout + cp3.stderr)

    def test_the_broker_door_names_its_stage_when_shut(self):
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


class TestUnknownWorkspaceName(WkTest):

    COMMANDS = (
        "build", "enter", "gui", "pr",
        "run", "status", "sync", "test", "doctor", "zed",
    )

    def test_a_name_at_another_slot_is_refused_the_same_way(self):
        name = "nosuchws-" + rand_suffix()
        for run_as in (run, run_here):
            with self.subTest(run=run_as.__name__):
                cp = run_as("ai", "claude", name, env={"WK_PLACE": "vm"})
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 2, out)
                self.assertIn(f"no such workspace: {name}", out)
                self.assertIn("usage: wk ai", out)

    @staticmethod
    def _takes(cmd):
        takes = D.Decl(REPO / "cmd" / cmd).takes
        return 0 if takes == "*" else int(takes)

    def test_every_command_refuses_a_name_no_workspace_answers_to(self):
        self._refuses_everywhere(run)

    def test_a_machine_with_no_vm_place_refuses_the_name_the_same_way(self):
        self._refuses_everywhere(run_here)

    def _refuses_everywhere(self, run):
        name = "nosuchws-" + rand_suffix()
        for c in self.COMMANDS:
            with self.subTest(cmd=c):
                takes = self._takes(c)
                extra = tuple(f"arg{i}" for i in range(takes))
                cp = run(c, name, *extra, env={"WK_PLACE": "vm"})
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 2, f"'wk {c} {name} {' '.join(extra)}' exited {cp.returncode}:\n{out}")
                self.assertIn(f"no such workspace: {name}", out)
                self.assertIn(f"usage: wk {c}", out, f"the refusal does not print the synopsis:\n{out}")

    def test_a_workspace_command_never_ignores_the_name_it_was_given(self):
        name = "nosuchws-" + rand_suffix()
        cp = run("stop", name)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, f"'wk stop {name}' was accepted:\n{out}")
        self.assertNotIn("stopping", out, f"'wk stop {name}' acted on something:\n{out}")

    def test_name_declarations_are_one_of_the_words_the_dispatcher_reads(self):
        vocabulary = D.NAME_VALUES
        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            for line in D.leading_block(f):
                if not line.startswith("# wk:"):
                    continue
                for tok in re.findall(r"name=(\S*)", line):
                    if tok.split("@")[0] not in vocabulary:
                        bad.append(f"{f.name}: name={tok}")
        self.assertEqual(bad, [], f"declarations the dispatcher cannot read: {bad}")


class TestZedNames(WkTest):

    def test_a_workspace_name_is_refused_by_the_dispatcher(self):
        name = "nosuchws-" + rand_suffix()
        cp = run("zed", name)
        out = cp.stdout + cp.stderr
        self.assertIn(f"no such workspace: {name}", out, out)
        self.assertNotIn("one name at a time", out, out)

    def test_tools_takes_a_name_the_dispatcher_does_not_answer_for(self):
        name = "nosuchws-" + rand_suffix()
        cp = run("zed", "--tools", name)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        if "zed is not installed" in out:
            self.skipTest("zed is not installed here, so the name is never reached")
        self.assertIn(f"no machine or workspace named '{name}'", out, out)


class TestFlagNameOverride(WkTest):

    def test_a_flag_can_require_the_workspace_the_command_does_not(self):
        cp = run("status", "--log")
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("usage: wk status", cp.stdout)


class TestSubverbNeedsOverride(WkTest):

    GH_DEAD = '#!/bin/sh\nexit 1\n'

    def test_the_subverbs_that_call_github_are_refused(self):
        with stub_path({"gh": self.GH_DEAD}) as binp:
            cp = run("key", "check",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("gh auth login", cp.stdout)

    def test_the_subverbs_that_do_not_are_left_alone(self):
        with stub_path({"gh": self.GH_DEAD}) as binp:
            cp = run("key", "show",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotIn("gh auth login", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)


class TestWhereTheNameSitsInArgv(WkTest):

    def _name(self, cmd, *args):
        d = D.Decl(REPO / "cmd" / cmd)
        args = dispatch.Invocation(cmd, d, list(args)).verb_first()
        name_decl = d.name_for(args)
        if name_decl.split("@")[0] == "none":
            return "NONE"
        found = dispatch.argv_name(D.name_slot(name_decl), d.takes_for(args), args)
        return "NONE" if found is None else found

    def test_the_name_slot_of_each_shape(self):
        for argv, want in ((("pr", "justinmichaud:eng/some-branch"), "NONE"),   # a lone ref is the command's own
                           (("pr", "1234"), "NONE"),
                           (("pr", "myws", "1234"), "myws"),
                           (("pr", "rebase"), "NONE"),                          # a subverb taking nothing keeps the slot
                           (("pr", "rebase", "myws"), "myws"),
                           (("stop", "typo"), "typo"),                          # no takes= still claims the first
                           (("build", "myws", "jsc-release"), "myws")):
            with self.subTest(argv=argv):
                self.assertEqual(want, self._name(*argv))

    def test_a_required_name_needs_none_of_the_commands_own_positionals_after_it(self):
        d = D.Decl(REPO / "cmd" / "bench")
        args = dispatch.Invocation("bench", d, ["run", "myws", "--kill"]).verb_first()
        name_decl = d.name_for(args)
        self.assertEqual("myws", dispatch.name_in_argv(name_decl.split("@")[0], D.name_slot(name_decl), d.takes_for(args), args))
        self.assertIsNone(dispatch.name_in_argv("optional", 1, "1", ["1234"]), "an optional name still needs them all")

    def test_takes_is_declared_wherever_a_positional_follows_an_optional_name(self):
        bad = []
        for f in sorted((REPO / "cmd").iterdir()):
            if not (f.is_file() and os.access(f, os.X_OK)):
                continue
            head = "".join(D.leading_block(f))
            if "name=optional" not in head:
                continue
            syn = re.search(r"^# wk \S+ (.*?) -- ", head, re.M)
            if not syn or "[<workspace>]" not in syn.group(1):
                continue
            after = syn.group(1).split("[<workspace>]", 1)[1].strip()
            after = after.lstrip("[")
            if not after or after.startswith("-"):
                continue
            if "takes=" not in head:
                bad.append(f"{f.name}: '{syn.group(1)}' takes an argument after "
                           f"the workspace but declares no takes=")
        self.assertEqual(bad, [], "; ".join(bad))


class TestTheDirectoryNamesTheWorkspaceOnABuildBox(WkTest):

    def _name(self, marker_root, cwd, remote=True):
        marker = self.tmp / "remote-marker"
        fleet = self.tmp / "far-fleet"
        fleet.mkdir(exist_ok=True)
        if remote:
            marker.write_text("inputs=x\n")
            host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip().lower()
            (fleet / "fake.conf").write_text(f"kind=build\nhostname={host}\nroot={marker_root}\n")
        else:
            marker.unlink(missing_ok=True)
        cp = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(REPO / 'lib')!r})\n"
             "from wk import dispatch\n"
             "print(dispatch.cwd_workspace() or 'NONE')"],
            cwd=cwd, env={**os.environ, "WK_REMOTE_MARKER": str(marker), "WK_MACHINES_DIR": str(fleet), "PWD": cwd},
            capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "wk"
        (self.root / "ws" / "image-decoders" / "WebKit" / "Source").mkdir(parents=True)
        (self.root / "cache").mkdir()

    def test_standing_in_or_under_a_workspace_names_it_and_anywhere_else_nothing(self):
        ws = self.root / "ws" / "image-decoders"
        for cwd, remote, want in ((ws, True, "image-decoders"), (ws / "WebKit" / "Source", True, "image-decoders"),
                                  (self.root / "cache", True, "NONE"), (self.root, True, "NONE"),
                                  (ws, False, "NONE")):   # a machine that is not a build box is never asked
            with self.subTest(cwd=cwd, remote=remote):
                self.assertEqual(want, self._name(str(self.root), str(cwd), remote=remote))


class TestHelpNamesEveryWhereOverride(WkTest):

    @staticmethod
    def _overrides(d):
        return [(verbs, spec["where"]) for verbs, spec in d.overrides() if "where" in spec]

    def test_every_override_is_named_with_its_where(self):
        checked = 0
        for d in D.all_commands(REPO):
            overrides = self._overrides(d)
            if not overrides:
                continue
            text = run(d.name, "-h").stdout
            for verbs, where in overrides:
                expected = f"    {verbs.replace(',', ', ')}: {dispatch.where_prose(d, where)}"
                with self.subTest(cmd=d.name, verbs=verbs):
                    self.assertIn(expected, text.splitlines(),
                                  f"'wk {d.name} -h' does not say where '{verbs}' runs:\n{text}")
                checked += 1
        self.assertGreater(checked, 5, "no where= override was checked at all")


if __name__ == "__main__":
    unittest.main()
