"""What a command still parses for itself -- the audit of the shape `wk zed
<workspace>` had: an argument a command re-decides for itself and then
mishandles. Six arguments every command could meet, and who decides each:

  workspace name  the dispatcher's, always: it resolves the name, refuses one
                  no workspace answers to, lifts it out of argv and exports
                  WK_NAME (`wk`'s declarations and main)
  --force         the dispatcher's: `GLOBALS` (lib/wk/dispatch.py) maps it to
                  WK_FORCE and `main` *consumes* the flag, so a command's own
                  arm for it could never fire
  --quiet         the same, WK_QUIET
  --target        the declaration's: resolve_target and a `where=workspace`
                  command both read it through wk.decl.Args, never argv by
                  hand. On a host or store command (`wk push --target`, `wk
                  sudo --target`) it names a *machine*, a different argument
                  that shares a spelling
  config          the declaration's: `config=--config` (or `config=arg`,
                  `wk build`'s positional) has the dispatcher refuse a name
                  lib/wk/buildconf.py does not hold, lift it out of argv and
                  export WK_CONFIG. `wk sysimage holds --config` names a
                  cross-build phase (lib/wk/pgo.py), a different argument
                  that shares a spelling
  subverb         the declaration's: `verbs=` (with `default=` for the verb
                  a bare or non-verb first word stands for) has the
                  dispatcher refuse an unknown verb and hand the command a
                  declared one as argv[0]

Run: python3 -m unittest tests.test_dispatch_audit -v
"""
TIER = "lint"
import os
import re
import sys
import unittest
from unittest import mock

from tests.support import REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402
from wk import dispatch  # noqa: E402
from tests.test_cli_shape import arms_file, declared_opts, literal_opts  # noqa: E402


def commands():
    for f in sorted((REPO / "cmd").iterdir()):
        if f.is_file() and os.access(f, os.X_OK):
            yield f


def header(path):
    return path.read_text(errors="replace").splitlines()[:15]


def synopsis(path):
    for line in header(path)[:5]:
        m = re.match(r"^# wk \S+ ?(.*?) -- ", line)
        if m:
            return m.group(1)
    return ""


def declaration(path):
    """The command's own top-level `# wk:` tokens (not its sub/flag lines)."""
    out = []
    for line in header(path):
        if not line.startswith("# wk:"):
            continue
        rest = line[len("# wk:"):]
        if rest.startswith(" sub ") or rest.startswith(" flag "):
            continue
        out += rest.split()
    return out


def decl_value(path, key, default=""):
    for tok in declaration(path):
        if tok.startswith(key + "="):
            return tok[len(key) + 1:]
    return default


def is_python(path):
    return path.read_text(errors="replace").startswith("#!/usr/bin/env python3")


def parses_flag(path, flag):
    """Does the file have a `case` arm of its own for <flag>? The arm, not
    the spelling: `--force` inside a printf string is prose, and an arm
    pattern cannot contain a parenthesis of its own. A python command's is a
    literal matched in argv by hand; a read through wk.decl.Args is not one."""
    text = path.read_text(errors="replace")
    if is_python(path):
        arms = arms_file(path)
        return flag in literal_opts(text) or (arms.is_file() and parses_flag(arms, flag))
    for line in text.splitlines():
        m = re.match(r"^\s*([^()#]*?)\)", line)
        if m and re.search(r"(^|\|)" + re.escape(flag) + r"(=\*)?($|\|)",
                           m.group(1).strip()):
            return True
    return False


def takes_a_subverb(path):
    """Does this command take a subverb at all? From what it declares and
    what its synopsis says -- `wk push on|off|status`, `wk bench <sub>`, or a
    `# wk: sub` line -- rather than from a `case` statement, since a command
    reads its verb wherever it likes and an internal `case "$1"` in a helper
    is not one. `wk boot <machine> [--status|--diag|...]` is not one either:
    its actions are flags."""
    if any(l.startswith("# wk: sub ") for l in header(path)):
        return True
    syn = synopsis(path)
    if re.search(r"<verb>|<sub>", syn):
        return True
    # Outside the placeholders: an alternation inside `<...>` is the shape of
    # one argument (`wk ab <pr-spec|branch|sha>`), not a list of subverbs.
    return bool(re.search(r"(^|[ |])[a-z][a-z0-9-]*\|[a-z0-9-]+",
                          re.sub(r"<[^>]*>", "", syn)))


# `wk sysimage holds --config` is a cross-build phase, not a build config.
SHARED_SPELLING = ("sysimage",)


def takes_a_config(path):
    """A build config the command decides for itself: `<config>` in its
    synopsis or a `--config` it declares, with no `config=` handing it to
    the dispatcher -- or a `--config` it reads again although it declared one."""
    if path.name in SHARED_SPELLING:
        return False
    if decl_value(path, "config"):
        return parses_flag(path, "--config") or '"--config"' in path.read_text(errors="replace")
    return "--config" in declared_opts(path) or "<config>" in synopsis(path)


def audit(path):
    """Every argument this command decides for itself."""
    own = []
    name = decl_value(path, "name", "none").split("@")[0]
    if name != "none" and "WK_NAME" not in path.read_text(errors="replace"):
        own.append("name")
    if takes_a_config(path):
        own.append("config")
    if takes_a_subverb(path) and not decl_value(path, "verbs"):
        own.append("subverb")
    if parses_flag(path, "--target"):
        own.append("--target")
    if parses_flag(path, "--force"):
        own.append("--force")
    if parses_flag(path, "--quiet"):
        own.append("--quiet")
    return own


def offenders(label):
    return sorted(c.name for c in commands() if label in audit(c))


# The audit, as it stands: a command that starts parsing one of these for
# itself appears here and fails the table below.
EXPECTED = {}


class TestWhatTheDispatcherAlreadyDecides(unittest.TestCase):
    """The three arguments no command may re-decide, and the proof that the
    dispatcher decides them."""

    def test_the_dispatcher_sets_force_and_quiet_and_eats_them(self):
        """`--force`/`--quiet` set WK_FORCE/WK_QUIET and leave argv"""
        self.assertEqual(dispatch.GLOBALS["--force"], "WK_FORCE")
        self.assertEqual(dispatch.GLOBALS["--quiet"], "WK_QUIET")
        # `wk version` declares no option at all, so a flag that reached its
        # argv would be refused as unknown; one the dispatcher ate is not.
        plain = run("version")
        self.assertEqual(plain.returncode, 0, plain.stdout)
        cp = run("version", "--force", "--quiet")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout, plain.stdout)
        cp = run("version", "--bogus")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("unknown option: --bogus", cp.stdout)

    def test_no_command_parses_force_or_quiet_again(self):
        """a second parse of a consumed flag is an arm that can never fire"""
        self.assertEqual(offenders("--force"), [])
        self.assertEqual(offenders("--quiet"), [])

    def test_every_workspace_name_comes_from_the_dispatcher(self):
        """a command that takes a name reads WK_NAME, never a positional"""
        self.assertEqual(offenders("name"), [])


class TestTheAuditList(unittest.TestCase):
    def test_the_list_is_what_it_was(self):
        """what each command still parses for itself, command by command"""
        got = {c.name: audit(c) for c in commands() if audit(c)}
        self.assertEqual(got, EXPECTED)


class TestTheConfigAndTheSubverbAreTheDispatchers(unittest.TestCase):
    def test_the_build_config_is_the_dispatchers(self):
        """every command taking a build config declares `config=` and reads
        WK_CONFIG, never `--config` or its positional"""
        self.assertEqual(offenders("config"), [])

    def test_the_subverb_is_the_dispatchers(self):
        """every command taking a subverb declares `verbs=`"""
        self.assertEqual(offenders("subverb"), [])

    def test_an_unknown_verb_is_refused_by_the_dispatcher(self):
        """with no `default=`, a first word that is no verb is refused before anything runs"""
        for c in commands():
            if not decl_value(c, "verbs") or decl_value(c, "default"):
                continue
            with self.subTest(cmd=c.name):
                cp = run(c.name, "zz-no-such-verb")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown verb: zz-no-such-verb", cp.stdout)
                self.assertIn("usage: wk %s" % c.name, cp.stdout)

    def test_a_missing_verb_is_refused_by_the_dispatcher(self):
        for c in commands():
            if not decl_value(c, "verbs") or decl_value(c, "default"):
                continue
            with self.subTest(cmd=c.name):
                cp = run(c.name)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("'wk %s' needs one of:" % c.name, cp.stdout)

    def test_the_verb_is_handed_over_first(self):
        """a declared verb is argv[0] wherever it was typed; any other word is the default verb's argument"""
        def first(cmd, *args):
            return dispatch.Invocation(cmd, D.Decl(REPO / "cmd" / cmd), list(args)).verb_first()
        self.assertEqual(first("push", "--target", "box", "on"), ["on", "--target", "box"])
        self.assertEqual(first("quiesce"), ["status"])
        self.assertEqual(first("pr", "ws", "1234"), ["checkout", "ws", "1234"])
        self.assertEqual(first("sysimage", "--list"), ["--list"])

    def test_a_mistyped_verb_is_named_where_the_default_takes_no_argument(self):
        for argv in (("push", "onn"), ("key", "chek"), ("quiesce", "of")):
            with self.subTest(argv=argv):
                cp = run(*argv)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown verb: %s (one of" % argv[1], cp.stdout)

    def test_a_retired_flag_or_verb_names_its_replacement(self):
        """a `gone` line: the dispatcher's tombstone, anywhere for a flag, in the verb's place for a verb"""
        for argv, said in ((("gui", "ws", "--wpe"), "'wk gui --wpe' is gone: wk gui --config wpe-release"),
                           (("key", "register"), "'wk key register' is gone: wk key deploy")):
            with self.subTest(argv=argv):
                cp = run(*argv)
                self.assertEqual(cp.returncode, 1, cp.stdout)
                self.assertIn(said, cp.stdout)

    def test_the_far_machine_is_handed_argv_as_typed(self):
        """a delegated `wk pr ws 1234` is not `pr checkout ws 1234` to a wk that may not know the default"""
        inv = dispatch.Invocation("pr", D.Decl(REPO / "cmd" / "pr"), ["ws", "1234"])
        inv.args = inv.verb_first()
        self.assertEqual((inv.args, inv.typed), (["checkout", "ws", "1234"], ["ws", "1234"]))


class TestTheNameInAWorkspaceAfterAVerb(unittest.TestCase):
    def test_it_is_left_for_the_drop_the_name_refusal(self):
        """inside workspace ws, `wk pr ws 1234` reaches main's "Drop the name" refusal, not argv_check's count"""
        inv = dispatch.Invocation("pr", D.Decl(REPO / "cmd" / "pr"), ["checkout", "ws", "1234"])
        with mock.patch.object(dispatch, "in_workspace", return_value=True), \
                mock.patch.object(dispatch, "wk_self", return_value="ws"):
            self.assertEqual(inv.argv_check(), ["checkout", "ws", "1234"])


class TestAnAllPassthroughIsTheOtherPrograms(unittest.TestCase):
    """`passthrough=all` (wk ai): past the workspace name nothing is wk's, `-h` and `--force` included"""

    def tail(self, *argv, inside=False):
        return dispatch.tail_from(D.Decl(REPO / "cmd" / "ai"), list(argv), inside)

    def test_the_tail_starts_after_the_name(self):
        self.assertEqual(self.tail("pi", "ws", "--help"), 2)
        self.assertEqual(self.tail("--force", "claude", "ws", "--force", "-r"), 3)

    def test_in_a_workspace_it_starts_after_the_agent(self):
        self.assertEqual(self.tail("claude", "--help", inside=True), 1)

    def test_help_after_the_name_is_not_wks(self):
        """dispatch.main never explains, and hands --help and --force on to the agent verbatim"""
        with mock.patch.object(dispatch, "explain", side_effect=AssertionError("explained")), \
                mock.patch.object(dispatch, "in_workspace", return_value=False), \
                mock.patch.object(dispatch.Invocation, "where", side_effect=dispatch.Exit(0)), \
                mock.patch.dict(os.environ, {}):
            os.environ.pop("WK_FORCE", None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(["ai", "pi", "ws", "--help", "--force"])
            self.assertNotIn("WK_FORCE", os.environ)

    def test_wks_own_force_goes_before_the_name(self):
        with mock.patch.object(dispatch.Invocation, "where", side_effect=dispatch.Exit(0)), \
                mock.patch.object(dispatch, "in_workspace", return_value=False), \
                mock.patch.dict(os.environ, {}):
            os.environ.pop("WK_FORCE", None)
            with self.assertRaises(dispatch.Exit):
                dispatch.main(["ai", "--force", "claude", "ws"])
            self.assertEqual(os.environ.get("WK_FORCE"), "1")


class TestPythonCommandsReadTheirOptionsThroughArgs(unittest.TestCase):
    def test_every_python_command_reads_its_options_through_args(self):
        """a python command reads a declared option through wk.decl.Args,
        never as a literal matched in argv, so none re-decides what the
        dispatcher already checked"""
        by_hand = [c.name for c in commands()
                   if is_python(c) and literal_opts(c.read_text()) & declared_opts(c)]
        self.assertEqual(by_hand, [])


class TestTheWorkspaceTarget(unittest.TestCase):
    def test_the_workspace_target_is_not_re_parsed(self):
        """a `where=workspace` command reads `--target` through wk.decl.Args,
        the reader resolve_target uses"""
        ws = [c.name for c in commands()
              if "--target" in audit(c) and decl_value(c, "where") == "workspace"]
        self.assertEqual(ws, [])


if __name__ == "__main__":
    unittest.main()
