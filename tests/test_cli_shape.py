"""Every command has the same shape, declared once in its `# wk:` lines and
enforced once, by the dispatcher (CLAUDE.md, rules 7-9): an option or
argument it does not declare is refused with its usage line; `--dry-run`
prints what `act` would run or is refused where the command is not on that
path yet; a destructive command asks through `confirm` first. Nothing here
is a list of commands -- each test derives its cases from `wk --declarations`
and from the files themselves, so a new command is covered the day it lands.

Run: python3 -m unittest tests.test_cli_shape -v
"""
import re
import unittest

from tests.support import REPO, WkTest, bash, run

# The dispatcher's own flags: taken out of argv before any command sees them.
GLOBAL_OPTS = {"--force", "--quiet", "--dry-run", "-n", "--yes", "-y",
               "-h", "--help", "--explain"}

CMD_FILES = sorted(p for p in (REPO / "cmd").iterdir() if p.is_file())


def declarations():
    """{cmd: fields} as `wk --declarations` prints them -- what wk reads."""
    rows = {}
    for line in run("--declarations").stdout.splitlines():
        f = line.split("\t")
        if len(f) < 11:
            continue
        rows[f[0]] = dict(where=f[1], name=f[2], group=f[3], syn=f[4],
                          takes=f[5], opts=f[6], destructive=f[7],
                          dryrun=f[8], passthrough=f[9], readonly=f[10])
    return rows


def name_slot(decl):
    if decl.startswith("none"):
        return 0
    return int(decl.split("@")[1]) if "@" in decl else 1


def declared_opts(path):
    """Every option named in the file's declarations: the command's `opts`,
    and each `sub`/`flag` line's `opts=`."""
    opts = set()
    for line in path.read_text().splitlines()[:15]:
        if not line.startswith("# wk:"):
            continue
        toks = line[5:].split()
        for i, t in enumerate(toks):
            if t == "opts" and i + 1 < len(toks):
                opts |= set(toks[i + 1].split(","))
            elif t.startswith("opts="):
                opts |= set(t[5:].split(","))
    return {o.rstrip("=") for o in opts if o}


# A `case` arm label that names an option: `--x)`, `--x=*)`, `-x|--long)`.
ARM = re.compile(r"^\s*((?:--?[A-Za-z_][-A-Za-z0-9_]*(?:=\*)?\|?)+)\)")


def code_opts(path):
    """Every option the command's own code matches on."""
    out = set()
    for line in path.read_text().splitlines():
        m = ARM.match(line)
        if not m:
            continue
        for alt in m.group(1).split("|"):
            alt = alt.replace("=*", "")
            if alt:
                out.add(alt)
    return out


class TestTheDeclarationsParse(WkTest):
    def test_every_command_is_declared(self):
        rows = declarations()
        self.assertEqual(sorted(rows), [p.name for p in CMD_FILES])


class TestArgumentsAreRefusedOnce(WkTest):
    """The dispatcher refuses, before anything runs, with exit 2 and the
    usage line -- for every command, from its declaration alone."""

    def test_an_unknown_option_is_refused_by_every_command(self):
        for cmd in declarations():
            with self.subTest(cmd=cmd):
                cp = run(cmd, "--no-such-option-zz")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown option: --no-such-option-zz", cp.stdout)
                self.assertIn("usage: wk", cp.stdout)

    def test_an_argument_past_what_it_takes_is_refused(self):
        for cmd, d in declarations().items():
            if d["takes"] == "*" or d["passthrough"] == "tail":
                continue
            n = name_slot(d["name"]) + int(d["takes"]) + 1
            args = [f"zz{i}" for i in range(1, n + 1)]
            with self.subTest(cmd=cmd, args=args):
                cp = run(cmd, *args)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn(f"unexpected argument: zz{n}", cp.stdout)

    def test_the_declared_options_are_the_ones_the_code_reads(self):
        """Declared but never matched is drift; matched but never declared
        is an option the dispatcher refuses before the command sees it."""
        for path in CMD_FILES:
            with self.subTest(cmd=path.name):
                declared = declared_opts(path)
                code = code_opts(path) - GLOBAL_OPTS
                self.assertEqual(
                    declared, code,
                    f"cmd/{path.name}: declared but not read: "
                    f"{sorted(declared - code)}; read but not declared: "
                    f"{sorted(code - declared)}")

    def test_no_command_refuses_arguments_for_itself(self):
        """One implementation: the `-*) die "usage..."` arm and the
        `[ $# -eq 0 ] || die "$USAGE"` count check belong to the dispatcher.
        An unknown *subverb* is still the command's to refuse: `takes` counts
        positionals and does not name them."""
        arm = re.compile(r'^\s*(-\*|-\?\*)\)\s*(die|usage_die) ', re.I)
        count = re.compile(r'\[ \$# -(eq|le|ge|gt|ne) \d+ \] \|\| die "\$?USAGE')
        offenders = []
        for path in CMD_FILES:
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if arm.search(line) or count.search(line):
                    offenders.append(f"cmd/{path.name}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestTheGlobalFlagsBelongToTheDispatcher(WkTest):
    def test_dry_run_and_yes_are_stripped_before_the_command(self):
        plain = run("version").stdout
        for flag in ("--dry-run", "-n", "--yes", "-y"):
            with self.subTest(flag=flag):
                cp = run("version", flag)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertEqual(cp.stdout, plain)

    def test_a_dry_run_is_refused_where_it_is_not_honoured_yet(self):
        """Never let through to change something: a command whose changes
        are not all on the `act` path refuses the flag and says why."""
        for cmd, d in declarations().items():
            if d["readonly"] != "-" or d["dryrun"] == "yes":
                continue
            with self.subTest(cmd=cmd):
                cp = run(cmd, "--dry-run")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("has no dry run yet", cp.stdout)

    def test_no_command_parses_the_global_flags_itself(self):
        offenders = []
        for path in CMD_FILES:
            found = code_opts(path) & GLOBAL_OPTS
            if found:
                offenders.append(f"cmd/{path.name}: {sorted(found)}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_help_states_the_class(self):
        for cmd, d in declarations().items():
            with self.subTest(cmd=cmd):
                out = run(cmd, "-h", timeout=60).stdout
                self.assertIn("changes things:", out)
                if d["destructive"] != "-":
                    self.assertIn("destructive:", out)
                if d["readonly"] == "yes":
                    self.assertIn("changes things: no", out)


class TestPromptsAndDestructiveDeclarationsAgree(unittest.TestCase):
    """A prompt is only ever a destructive command asking first: every
    `confirm "` in cmd/* sits in a command declared destructive, and every
    helper file that prompts is reached from one."""

    HELPER_DIRS = ("lib", "bench", "image", "boot")

    def _destructive_cmds(self):
        return {c for c, d in declarations().items() if d["destructive"] != "-"}

    def test_every_prompting_command_is_declared_destructive(self):
        destructive = self._destructive_cmds()
        offenders = [p.name for p in CMD_FILES
                     if 'confirm "' in p.read_text() and p.name not in destructive]
        self.assertEqual(offenders, [], f"prompt in a command not declared destructive: {offenders}")

    def test_every_prompting_helper_is_reached_from_a_destructive_command(self):
        destructive = self._destructive_cmds()
        texts = {c: (REPO / "cmd" / c).read_text() for c in destructive}
        offenders = []
        for d in self.HELPER_DIRS:
            for p in sorted((REPO / d).glob("*.sh")):
                if 'confirm "' not in p.read_text() or p.name == "common.sh":
                    continue
                if not any(p.name in t for t in texts.values()):
                    offenders.append(f"{d}/{p.name}")
        self.assertEqual(offenders, [], f"prompting helper not named by any destructive command: {offenders}")

    def test_the_agent_gateway_does_not_answer_yes_for_everyone(self):
        self.assertNotIn("WK_YES", (REPO / "cmd" / "mcp").read_text())


class TestActAndConfirm(unittest.TestCase):
    """lib/common.sh's two halves of the rule, driven directly."""

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        out = bash(". lib/common.sh; export WK_DRY_RUN=1; f=$(mktemp -u)\n"
                   "act touch \"$f\"; [ -e \"$f\" ] && echo EXISTS || echo ABSENT")
        self.assertIn("would run: touch", out.stderr)
        self.assertIn("ABSENT", out.stdout)
        self.assertNotIn("EXISTS", out.stdout)

    def test_a_destructive_command_cannot_act_before_asking(self):
        out = bash(". lib/common.sh; export WK_DESTRUCTIVE=1\n"
                   "act true; echo REACHED")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("BUG", out.stderr)
        self.assertNotIn("REACHED", out.stdout)

    def test_an_answered_question_lets_it_act(self):
        out = bash(". lib/common.sh; export WK_DESTRUCTIVE=1 WK_YES=1\n"
                   "confirm 'go?'; act echo RAN")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("RAN", out.stdout)

    def test_a_dry_run_shows_the_question_without_asking_it(self):
        out = bash(". lib/common.sh; export WK_DRY_RUN=1\n"
                   "confirm 'erase it?' </dev/null && echo YES")
        self.assertIn("would ask: erase it? [y/N]", out.stderr)
        self.assertIn("YES", out.stdout)
