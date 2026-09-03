"""What a command still parses for itself -- owed by docs/defects: "Finish
the audit of the shape `wk zed <workspace>` had -- an argument a command
re-decides for itself and then mishandles ... configs, flags, paths and
subverbs are still parsed command by command".

The audit, not the refactor. Six arguments every command could meet, and
who decides each:

  workspace name  the dispatcher's, always: it resolves the name, refuses one
                  no workspace answers to, lifts it out of argv and exports
                  WK_NAME (`wk`'s declarations and main)
  --force         the dispatcher's: `--force)` sets WK_FORCE and *consumes*
                  the flag, so a command's own arm for it could never fire
  --quiet         the same, WK_QUIET
  --target        the dispatcher reads it (resolve_target) and leaves it in
                  argv. For a `where=workspace` command that is the same fact
                  the dispatcher already resolved into WK_TARGET; for a host
                  or store command (`wk push --target`, `wk sudo --target`)
                  it names a *machine*, which is a different argument that
                  happens to share a spelling
  --config        nobody's: WK_CONFIG is only what a forwarded command
                  inherits (`wk`'s environment protocol), and every command
                  that takes a build config parses the flag itself
  subverb         nobody's: the dispatcher reads `${1:-}` to apply a `sub`
                  override and leaves it in argv, so each command re-reads it

The three tests below hold what is already true; the three
`expectedFailure`s name what is not, file by file, and are the audit's
answer to the defects line.

Run: python3 -m unittest tests.test_owed_dispatch_audit -v
"""
import os
import re
import unittest

from tests.support import REPO


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


def parses_flag(path, flag):
    """Does the file have a `case` arm of its own for <flag>? The arm, not
    the spelling: `--force` inside a printf string is prose, and an arm
    pattern cannot contain a parenthesis of its own."""
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"^\s*([^()#]*?)\)", line)
        if m and re.search(r"(^|\|)" + re.escape(flag) + r"(=\*)?($|\|)",
                           m.group(1).strip()):
            return True
    return False


def takes_a_subverb(path):
    """Does this command take a subverb at all? From what it declares and
    what its synopsis says -- `wk push on|off|status`, `wk vm <sub>`, or a
    `# wk: sub` line -- rather than from a `case` statement, since a command
    reads its verb wherever it likes and an internal `case "$1"` in a helper
    is not one. `wk boot <machine> [--status|--diag|...]` is not one either:
    its actions are flags."""
    if any(l.startswith("# wk: sub ") for l in header(path)):
        return True
    syn = synopsis(path)
    if re.search(r"<verb>|<sub>", syn):
        return True
    return bool(re.search(r"(^|[ |])[a-z][a-z0-9-]*\|[a-z0-9-]+", syn))


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
    "bench":      ["config", "subverb"],
    "bridge":     ["subverb"],
    "build":      ["config"],
    "completion": ["subverb"],
    "gui":        ["config"],
    "key":        ["subverb"],
    "new":        ["--target"],
    "pi":         ["subverb"],
    "pr":         ["subverb"],
    "profile":    ["config"],
    "push":       ["subverb", "--target"],
    "quiesce":    ["subverb"],
    "remote":     ["subverb"],
    "run":        ["config"],
    "session":    ["subverb"],
    "skills":     ["subverb"],
    "sudo":       ["subverb", "--target"],
    "sync":       ["--target"],
    "sysimage":   ["subverb"],
    "test":       ["config"],
    "vm":         ["subverb"],
}


class TestWhatTheDispatcherAlreadyDecides(unittest.TestCase):
    """The three arguments no command may re-decide, and the proof that the
    dispatcher decides them."""

    def test_the_dispatcher_sets_force_and_quiet_and_eats_them(self):
        """`--force`/`--quiet` set WK_FORCE/WK_QUIET and leave argv"""
        text = (REPO / "wk").read_text()
        for flag, var in (("--force", "WK_FORCE"), ("--quiet", "WK_QUIET")):
            arm = [l.strip() for l in text.splitlines()
                   if l.strip().startswith(f":{flag})")]
            self.assertEqual(len(arm), 1, f"{flag} is not the dispatcher's: {arm}")
            self.assertIn(f"{var}=1", arm[0], arm[0])
            self.assertIn("export", arm[0], arm[0])
            # `continue` is what keeps it out of the argv the command sees.
            self.assertIn("continue", arm[0], arm[0])

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
    """Three arguments the dispatcher hands nobody. Each `expectedFailure`
    names the files, and is the audit's entry for docs/defects."""

    @unittest.expectedFailure
    def test_the_build_config_is_not_the_dispatchers(self):
        """defect: cmd/build takes <config> as a positional and cmd/bench,
        cmd/gui, cmd/profile, cmd/run, cmd/test each parse `--config` and
        default it from WK_CONFIG themselves -- there is no declaration for a
        config and no WK_CONFIG the dispatcher sets, so the six agree by
        being written the same way rather than by construction"""
        self.assertEqual(offenders("config"), [])

    @unittest.expectedFailure
    def test_the_subverb_is_not_the_dispatchers(self):
        """defect: the dispatcher reads ${1:-} to apply a `sub` override and
        leaves it in argv, so cmd/ai, cmd/bench, cmd/boot, cmd/bridge,
        cmd/completion, cmd/key, cmd/pi, cmd/pr, cmd/push, cmd/quiesce,
        cmd/remote, cmd/session, cmd/skills, cmd/sudo, cmd/sysimage and
        cmd/vm each re-read it and each write their own refusal for an
        unknown one"""
        self.assertEqual(offenders("subverb"), [])

    @unittest.expectedFailure
    def test_the_workspace_target_is_not_re_parsed(self):
        """defect: cmd/new and cmd/sync parse `--target` for the same fact
        the dispatcher resolved (resolve_target reads the flag and hands the
        answer on in WK_TARGET) -- `wk push --target` and `wk sudo --target`
        are not this: on a host or store command the flag names a machine"""
        ws = [c.name for c in commands()
              if "--target" in audit(c) and decl_value(c, "where") == "workspace"]
        self.assertEqual(ws, [])


if __name__ == "__main__":
    unittest.main()
