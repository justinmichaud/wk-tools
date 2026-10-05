"""The dispatcher's rules, each one body over every declaration (`# wk:` lines in cmd/*): where an invocation runs"""
import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, bash, clean_env, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, dispatch, places, presets, store, workspace  # noqa: E402
from wk import decl as D  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402

DECLS = [d for d in D.all_commands(REPO)]
GLOBAL_WORDS = set(dispatch.GLOBALS) | {"-h", "--help", "--explain"}


class Handed(Exception):
    def __init__(self, how, argv, readonly=None):
        self.how, self.argv, self.env, self.readonly = how, list(argv), dict(os.environ), readonly


HANDED = []


def dispatched(argv, macos=False, place="container", delegates=False, env=None):
    def execv(path, args):
        raise Handed("here", args[1:])

    def forward(inv, cmd, args, env=None):
        raise Handed("forward", args)

    def delegate(t, cmd, args, readonly):
        raise Handed("delegate", args, readonly)

    patches = [mock.patch.dict(os.environ, clean_env(env, wk_root=False), clear=True),
               mock.patch.object(dispatch, "_registry", None),
               mock.patch.object(dispatch, "is_macos", lambda: macos),
               mock.patch.object(dispatch, "in_vm", lambda: False),
               mock.patch.object(store.Store, "is_local", lambda self: not macos),
               mock.patch("os.execv", execv),
               mock.patch.object(dispatch, "forward_to_vm", forward),
               mock.patch.object(dispatch, "forward_status", forward),
               mock.patch.object(dispatch, "bare_report", forward),
               mock.patch.object(dispatch, "delegate_run", delegate),
               mock.patch.object(dispatch, "delegate_driver", lambda t: object() if delegates else None),
               mock.patch.object(dispatch, "resolve_place", lambda *a: place),
               mock.patch.object(dispatch, "ask_place", lambda *a: None),
               mock.patch.object(dispatch.Invocation, "check_needs", lambda self, machine=None: None),
               mock.patch.object(dispatch.Invocation, "derived_name", lambda self: "ws1"),
               mock.patch.object(dispatch.Invocation, "named_place", lambda self: ""),
               mock.patch.object(workspace, "refuse_unsaved_before_forward", lambda *a: None),
               mock.patch.object(dispatch.sshalias, "alias_remove", lambda *a: None)]
    out = io.StringIO()
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stderr(out))
        stack.enter_context(contextlib.redirect_stdout(out))
        try:
            dispatch.main(list(argv))
        except Handed as h:
            HANDED.append(h)
            return h.how, h.argv, h.env
        except (dispatch.Exit, act.Refused) as e:
            return "exit", e.status, out.getvalue()
    return "exit", 0, out.getvalue()


def positionals_for(d, probe):
    slot = D.name_slot(d.name_for(probe))
    takes = d.takes_for(probe)
    total = max(slot + (0 if takes == "*" else int(takes)), 1 if d.verbs else 0)
    words = []
    for k in range(1, total + 1):
        if d.verbs and k == 1:
            words.append(probe[0])
        elif k == slot:
            words.append("ws1")
        elif d.preset == "arg" and not any(w in presets.names() for w in words):
            words.append(presets.names()[0])
        else:
            words.append("a%d" % k)
    return words


def invocations(d):
    if d.verbs:
        out = [(v, positionals_for(d, [v])) for v in d.verbs.split(",")]
    else:
        out = [("", positionals_for(d, []))]
    for flags, spec in d.flag:
        for f in flags.split(","):
            valued = D.in_list(f + "=", d.opts_for([f]))
            out.append((f, positionals_for(d, [f]) + [f] + (["v"] if valued else [])))
    return out


def destructive_invocations(d):
    out = []
    for label, argv in invocations(d):
        if d.is_destructive(argv):
            out.append((label, argv))
        if d.verbs:
            for f in d.opts_for(argv).split(","):
                flag = f.rstrip("=")
                given = argv + [flag] + (["v"] if f.endswith("=") else [])
                if flag and not d.is_destructive(argv) and d.is_destructive(given):
                    out.append(("%s %s" % (label, flag), given))
    return out


def dynamic_answer(d, argv):
    cp = subprocess.run([str(d.path), "--where", *argv], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        env=clean_env(wk_root=False), timeout=30)
    return cp.stdout.strip()


def declared_option(d, argv, word):
    key = word.split("=")[0]
    return D.in_list(key, d.opts_for(argv)) or D.in_list(key + "=", d.opts_for(argv))


class TestWhere(unittest.TestCase):

    def expected(self, d, argv, macos, delegates):
        where = d.where_for(argv)
        if where == "dynamic":
            where = dynamic_answer(d, argv)
        if where in ("host", "local"):
            return "here"
        if where == "store":
            return "forward" if macos else "here"
        if delegates and d.name_for(argv).split("@")[0] != "none" and not d.here_for(argv) and not d.lifecycle:
            return "delegate"
        return "forward" if macos and d.forward_for(argv) else "here"

    def test_every_invocation_runs_where_it_says(self):
        checked = 0
        for macos, delegates in ((True, False), (False, False), (False, True)):
            for d in DECLS:
                for label, argv in invocations(d):
                    with self.subTest(cmd=d.name, verb=label, macos=macos, delegates=delegates):
                        want = self.expected(d, argv, macos, delegates)
                        how, handed, env = dispatched([d.name, "--quiet", *argv], macos=macos, delegates=delegates)
                        self.assertEqual(how, want, "wk %s %s -> %s %s %s" % (d.name, " ".join(argv), how, handed, env))
                        if how == "delegate":
                            self.assertEqual(HANDED[-1].readonly, d.is_readonly(argv[0] if argv else ""))
                        self.assertFalse(GLOBAL_WORDS & set(handed), handed)
                        self.assertEqual(env.get("WK_QUIET"), "1")
                        for w in handed[:handed.index("--") if "--" in handed else len(handed)]:
                            if w.startswith("-") and len(w) > 1:
                                self.assertTrue(declared_option(d, argv, w), "%s handed %s, which it does not declare" % (label, w))
                        checked += 1
        self.assertGreater(checked, 3 * len(DECLS))


class TestParsesEveryArgument(unittest.TestCase):

    PATH = "some dir/a=b:c.txt"

    def test_every_valued_option_hands_its_path_on_as_typed(self):
        checked = 0
        for d in DECLS:
            for label, argv in invocations(d):
                opts = d.opts_for(argv).split(",")
                for opt in sorted(o[:-1] for o in opts if o.endswith("=") and o != "--preset="):
                    optional = opt in opts   # declared bare too: a value only as `--x=v`, handed on so
                    for typed in ([] if optional else [[opt, self.PATH]]) + [["%s=%s" % (opt, self.PATH)]]:
                        with self.subTest(cmd=d.name, verb=label, typed=typed):
                            how, handed, _ = dispatched([d.name, *argv, *typed])
                            self.assertEqual(how, "here", handed)
                            if optional:
                                self.assertIn("%s=%s" % (opt, self.PATH), handed)
                            else:
                                self.assertEqual(handed[handed.index(opt) + 1], self.PATH, handed)
                            checked += 1
        self.assertGreater(checked, 100)

    def test_a_positional_path_is_one_word(self):
        how, handed, env = dispatched(["scp", "ws1", ":" + self.PATH, self.PATH])
        self.assertEqual((how, handed, env["WK_NAME"]), ("here", [":" + self.PATH, self.PATH], "ws1"))


class TestDestructiveAsksOnce(unittest.TestCase):

    def test_every_destructive_invocation_is_armed_and_nothing_else_is(self):
        armed = 0
        for d in DECLS:
            spots = destructive_invocations(d)
            for label, argv in spots:
                with self.subTest(cmd=d.name, spot=label):
                    how, handed, env = dispatched([d.name, *argv])
                    self.assertEqual(how, "here", handed)
                    self.assertEqual(env.get("WK_DESTRUCTIVE"), "1")
                    self.assertNotIn("WK_CONFIRMED", env)
                    self.assertNotIn("WK_YES", env)
                    how, handed, env = dispatched([d.name, *argv, "--yes"])
                    self.assertEqual((env.get("WK_YES"), "--yes" in handed), ("1", False))
                    armed += 1
            for label, argv in invocations(d):
                if spots and not d.is_destructive(argv) and not any(l.startswith(label + " ") for l, _ in spots):
                    with self.subTest(cmd=d.name, calm=label):
                        how, handed, env = dispatched([d.name, *argv])
                        self.assertNotIn("WK_DESTRUCTIVE", env if how != "exit" else {}, (how, handed))
        self.assertGreater(armed, 10)

    def test_an_effect_outside_act_is_armed_by_the_command_that_reaches_it(self):
        for effect, cmd, argv in (("retire a tailnet node", "sysimage", ["write"]),
                                  ("replace a far destination", "scp", ["-r"])):
            with self.subTest(effect=effect):
                d = next(d for d in DECLS if d.name == cmd)
                how, handed, env = dispatched([cmd, *positionals_for(d, argv), *(argv if argv[0].startswith('-') else [])])
                self.assertEqual((how, env.get("WK_DESTRUCTIVE")), ("here", "1"), handed)

    def test_an_armed_command_cannot_act_before_it_asks(self):
        with mock.patch.dict(os.environ, {"WK_DESTRUCTIVE": "1"}), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("WK_CONFIRMED", None)
            with self.assertRaises(act.Refused):
                Fake().act_run(["true"])
        cp = bash(". lib/common.sh; export WK_DESTRUCTIVE=1; act true; echo REACHED")
        self.assertNotIn("REACHED", cp.stdout)

    def test_one_answer_covers_every_effect_after_it(self):
        with mock.patch.dict(os.environ, {"WK_DESTRUCTIVE": "1", "WK_YES": "1"}):
            self.assertTrue(act.confirm("remove ws1?"))
            self.assertTrue(Local().act_run(["true"]).ok)
            self.assertTrue(Local().act_run(["true"]).ok)

    def test_the_default_is_no_and_no_terminal_declines(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        with mock.patch.dict(os.environ), contextlib.redirect_stderr(io.StringIO()) as err:
            for v in ("WK_YES", "WK_DRY_RUN", "WK_CONFIRMED"):
                os.environ.pop(v, None)
            self.assertFalse(act.confirm("remove ws1?", stdin=Tty("\n")))
            self.assertFalse(act.confirm("remove ws1?", stdin=Tty("n\n")))
            self.assertFalse(act.confirm("remove ws1?", stdin=io.StringIO("y\n")))
            self.assertNotIn("WK_CONFIRMED", os.environ)
            self.assertTrue(act.confirm("remove ws1?", stdin=Tty("y\n")))
        self.assertIn("remove ws1? -- declining (no terminal", err.getvalue())
        cp = bash(". lib/common.sh; confirm 'remove ws1?' </dev/null && echo YES")
        self.assertNotIn("YES", cp.stdout)
        self.assertIn("declining", cp.stderr)

    def test_a_forwarded_command_carries_the_answer_and_asks_again(self):
        line = places.Container("container", str(REPO), {}, Fake()).wk_cmd(
            ["rm", "ws1"], {"WK_YES": "1", "WK_DESTRUCTIVE": "1", "WK_CONFIRMED": "1"})
        self.assertIn("WK_YES=1 ", line)
        self.assertNotIn("WK_CONFIRMED", line)
        self.assertNotIn("WK_DESTRUCTIVE", line)


class TestForceNamesWhatItCrosses(unittest.TestCase):

    def test_force_is_carried_as_environment(self):
        how, handed, env = dispatched(["gc", "--force"])
        self.assertEqual((how, "--force" in handed, env.get("WK_FORCE")), ("here", False, "1"))
        line = places.Container("container", str(REPO), {}, Fake()).wk_cmd(["gc"], {"WK_FORCE": "1"})
        self.assertIn("WK_FORCE=1 ", line)

    def test_a_barrier_refuses_naming_itself_and_the_flag(self):
        with mock.patch.dict(os.environ), contextlib.redirect_stderr(io.StringIO()) as err:
            os.environ.pop("WK_FORCE", None)
            with self.assertRaises(act.Refused):
                act.barrier("the disk is the one this machine runs from")
        self.assertIn("the disk is the one this machine runs from\n    --force proceeds anyway", err.getvalue())

    def test_a_forced_barrier_is_named_as_crossed_and_again_at_the_end(self):
        code = ("import sys; sys.path.insert(0, 'lib'); from wk import act\n"
                "act.barrier('first gate\\nits detail'); act.barrier('second gate'); print('WENT ON')")
        cp = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True,
                            env=clean_env({"WK_FORCE": "1"}), timeout=30)
        self.assertIn("WENT ON", cp.stdout)
        self.assertIn("FORCED past a barrier: first gate", cp.stderr)
        self.assertTrue(cp.stderr.rstrip().endswith("forced past 2 barrier(s):\n- first gate\n- second gate"), cp.stderr)
        sh = bash(". lib/common.sh; barrier 'first gate'; echo WENT ON", env={"WK_FORCE": "1"})
        self.assertIn("WENT ON", sh.stdout)
        self.assertIn("FORCED past a barrier: first gate", sh.stderr)
        self.assertIn("forced past 1 barrier(s):", sh.stderr)

    def test_a_refusal_that_is_no_barrier_is_not_crossed(self):
        with mock.patch.dict(os.environ, {"WK_FORCE": "1"}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(act.Refused):
                act.die("no such workspace: ws1")

    FORCED = re.compile(r"\bforced\(")

    def test_force_is_read_only_to_cross_a_barrier_or_to_record_a_result_as_forced(self):
        offenders = []
        for p in sorted((REPO / "lib" / "wk").rglob("*.py")):
            if p.name == "act.py":
                continue
            lines = p.read_text().splitlines()
            for i, line in enumerate(lines):
                if not self.FORCED.search(line) or '"forced=" + ' in line:
                    continue
                if not any("barrier(" in l for l in lines[i:i + 3]):
                    offenders.append("%s:%d: %s" % (p.relative_to(REPO), i + 1, line.strip()))
        self.assertEqual(offenders, [], "--force crossing something no barrier names:\n" + "\n".join(offenders))


class TestAFarEndNoConfNames(unittest.TestCase):

    def test_it_is_refused_naming_the_conf_to_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "wk-remote")
            with open(marker, "w") as f:
                f.write("place=box\n")
            for argv in (["gc"], ["build", "jsc-release"]):
                with self.subTest(argv=argv):
                    how, status, out = dispatched(argv, env={"WK_REMOTE_MARKER": marker})
                    self.assertEqual((how, status), ("exit", 1), out)
                    self.assertIn("no machines/<name>.conf\n    names it", out)


class TestHelpPreviewsTheCommandLine(unittest.TestCase):

    def test_the_preview_is_the_argv_and_where(self):
        out = run("bench", "stage", "ws1", "--to=mbp", "--preset", "mac-release", "-h", timeout=30).stdout
        self.assertIn("  this command line runs: WK_PRESET=mac-release %s stage ws1 --to mbp\n"
                      % shlex.quote(str(REPO / "cmd" / "bench")), out)
        self.assertIn("    on: %s\n" % dispatch.where_prose(D.Decl(REPO / "cmd" / "bench"), "host"), out)

    def test_a_refused_command_line_previews_its_refusal(self):
        out = run("gc", "--nonesuch", "-h", timeout=30).stdout
        self.assertIn("unknown option: --nonesuch", out)
        self.assertIn("  this command line is refused, as above", out)

    def test_the_workspace_name_is_not_the_config(self):
        out = run("build", "ws1", "jsc-release", "-h", timeout=30).stdout
        self.assertIn("  this command line runs: WK_PRESET=jsc-release %s\n" % shlex.quote(str(REPO / "cmd" / "build")), out)
        out = run("run", "ws1", "--preset", "jsc-release", "-h", timeout=30).stdout
        self.assertIn("  this command line runs: WK_PRESET=jsc-release %s\n" % shlex.quote(str(REPO / "cmd" / "run")), out)

    def test_no_arguments_no_preview(self):
        self.assertNotIn("this command line", run("gc", "-h", timeout=30).stdout)


if __name__ == "__main__":
    unittest.main()
