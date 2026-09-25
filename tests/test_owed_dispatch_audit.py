"""What a command still parses for itself -- owed by docs/defects: "Finish
the audit of the shape `wk zed <workspace>` had -- an argument a command
re-decides for itself and then mishandles ... configs, flags, paths and
subverbs are still parsed command by command".

The audit, not the refactor. Six arguments every command could meet, and
who decides each:

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
  --config        nobody's: WK_CONFIG is only what a forwarded command
                  inherits (`wk`'s environment protocol), and every command
                  that takes a build config parses the flag itself
  subverb         nobody's: the dispatcher reads `${1:-}` to apply a `sub`
                  override and leaves it in argv, so each command re-reads it

The tests below hold what is already true; the `owed` marks name what is
not, file by file, and are the audit's answer to the defects line.

Run: python3 -m unittest tests.test_owed_dispatch_audit -v
"""
TIER = "lint"
import os
import re
import sys
import unittest

from tests.support import REPO, owed, run

sys.path.insert(0, str(REPO / "lib"))
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


def takes_a_config(path):
    """A build config: `wk build <workspace> <config>` takes it as a
    positional, everything else as `--config`."""
    return parses_flag(path, "--config") or "<config>" in synopsis(path)


def audit(path):
    """Every argument this command decides for itself."""
    own = []
    name = decl_value(path, "name", "none").split("@")[0]
    if name != "none" and "WK_NAME" not in path.read_text(errors="replace"):
        own.append("name")
    if takes_a_config(path):
        own.append("config")
    if takes_a_subverb(path):
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


# The audit, as it stands. A command that starts parsing one of these for
# itself appears here and fails the table below; one that stops parsing one
# disappears from it and fails the same way, which is what makes shrinking
# the list a change to this file rather than a silent drift.
EXPECTED = {
    "ai":         ["subverb"],
    "bench":      ["subverb"],
    "build":      ["config"],
    "key":        ["subverb"],
    "machine":    ["subverb"],
    "pr":         ["subverb"],
    "push":       ["subverb"],
    "quiesce":    ["subverb"],
    "session":    ["subverb"],
    "sysimage":   ["subverb"],
}


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


class TestWhatIsStillParsedCommandByCommand(unittest.TestCase):
    """What the dispatcher still hands nobody. Each `owed` mark names the
    files, and is the audit's entry for docs/defects."""

    @owed("the build config is parsed by seven commands, declared to the dispatcher by none")
    def test_the_build_config_is_not_the_dispatchers(self):
        """defect: cmd/build takes <config> as a positional and cmd/bench,
        cmd/gui, cmd/profile, cmd/run, cmd/sysimage, cmd/test each parse
        `--config` and default it from WK_CONFIG themselves -- there is no
        declaration for a config and no WK_CONFIG the dispatcher sets, so the
        seven agree by being written the same way rather than by
        construction"""
        self.assertEqual(offenders("config"), [])

    @owed("the subverb is re-read and refused by each command, not by the dispatcher")
    def test_the_subverb_is_not_the_dispatchers(self):
        """defect: the dispatcher reads ${1:-} to apply a `sub` override and
        leaves it in argv, so cmd/ai, cmd/bench, cmd/boot, cmd/key,
        cmd/machine, cmd/pr, cmd/push, cmd/quiesce,
        cmd/session and cmd/sysimage each re-read it and each write their
        own refusal for an unknown one"""
        self.assertEqual(offenders("subverb"), [])


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
