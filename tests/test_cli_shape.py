"""Every command has the same shape, declared once in its `# wk:` lines and"""
TIER = "lint"
import itertools
import re
import sys
import unittest

from tests.support import REPO, WkTest, bash, run

sys.path.insert(0, str(REPO / "lib"))
from wk import decl as D  # noqa: E402

GLOBAL_OPTS = {"--force", "--quiet", "--dry-run", "-n", "--yes", "-y",
               "-h", "--help", "--explain"}

DISPATCHER_READS = {"--preset": {"--preset"}}

CMD_FILES = sorted(p for p in (REPO / "cmd").iterdir() if p.is_file())


def declarations():
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
    if decl.split("@")[0] in ("none", "derived"):
        return 0
    return int(decl.split("@")[1]) if "@" in decl else 1


def declared_opts(path):
    opts = set()
    for line in itertools.takewhile(lambda l: l.startswith("#"), path.read_text().splitlines()):
        if not line.startswith("# wk:"):
            continue
        toks = line[5:].split()
        for i, t in enumerate(toks):
            if t == "opts" and i + 1 < len(toks):
                opts |= set(toks[i + 1].split(","))
            elif t.startswith("opts="):
                opts |= set(t[5:].split(","))
    return {o.rstrip("=") for o in opts if o}


ARGS_READ = re.compile(r'\.(?:flag|value|values)\("(-{1,2}[a-z_][a-z_-]*)"\)')


def literal_opts(text):
    return set(re.findall(r'"(-{1,2}[a-z_][a-z_-]*=?)" in ', text) + re.findall(r'== "(-{1,2}[a-z_][a-z_-]*)"', text))


def package_opts(path):
    pkg = REPO / "lib" / "wk" / path.name
    texts = (f.read_text() for f in pkg.glob("*.py")) if pkg.is_dir() else ()
    return {o for t in texts for o in re.findall(r'"(--[a-z][a-z-]*)"', t)}


def imported_reads(path):
    """The options the wk modules a command imports read off its Args (`args.flag("--x")`)."""
    names = {n.strip() for line in re.findall(r"^from wk import ([\w, ]+)", path.read_text(), re.M) for n in line.split(",")}
    mods = [REPO / "lib" / "wk" / (n + ".py") for n in names]
    return {o for m in mods if m.is_file() for o in ARGS_READ.findall(m.read_text())}


def code_opts(path):
    text = path.read_text()
    return literal_opts(text) | set(ARGS_READ.findall(text))


class TestTheDeclarationsParse(WkTest):
    def test_every_command_is_declared(self):
        rows = declarations()
        self.assertEqual(sorted(rows), [p.name for p in CMD_FILES])


class TestArgumentsAreRefusedOnce(WkTest):

    def test_an_unknown_option_is_refused_by_every_command(self):
        for cmd in declarations():
            with self.subTest(cmd=cmd):
                cp = run(cmd, "--no-such-option-zz")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("unknown option: --no-such-option-zz", cp.stdout)
                self.assertIn("usage: wk", cp.stdout)

    def test_an_argument_past_what_it_takes_is_refused(self):
        for cmd, d in declarations().items():
            first = []
            decl = D.Decl(REPO / "cmd" / cmd)
            if decl.verbs:
                first = [v for v in decl.verbs.split(",")
                         if decl.takes_for([v]) != "*" and decl.passthrough_for([v]) not in ("tail", "all")][:1]
                if not first:
                    continue
                d = dict(d, name=decl.name_for(first), takes=decl.takes_for(first), passthrough="")
            if d["takes"] == "*" or d["passthrough"] in ("tail", "all"):
                continue
            n = name_slot(d["name"]) + int(d["takes"]) + 1
            args = first + [f"zz{i}" for i in range(len(first) + 1, n + 1)]
            with self.subTest(cmd=cmd, args=args):
                cp = run(cmd, *args)
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn(f"unexpected argument: zz{n}", cp.stdout)

    def test_the_declared_options_are_the_ones_the_code_reads(self):
        for path in CMD_FILES:
            with self.subTest(cmd=path.name):
                declared = declared_opts(path) - DISPATCHER_READS.get(D.Decl(path).preset, set())
                code = code_opts(path) - GLOBAL_OPTS
                code |= declared & (package_opts(path) | imported_reads(path))
                self.assertEqual(
                    declared, code,
                    f"cmd/{path.name}: declared but not read: "
                    f"{sorted(declared - code)}; read but not declared: "
                    f"{sorted(code - declared)}")


class TestTheGlobalFlagsBelongToTheDispatcher(WkTest):
    def test_dry_run_and_yes_are_stripped_before_the_command(self):
        plain = run("doctor", "--probe-tools").stdout
        for flag in ("--dry-run", "-n", "--yes", "-y", "--force", "--quiet"):
            with self.subTest(flag=flag):
                cp = run("doctor", "--probe-tools", flag)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertEqual(cp.stdout, plain)

    def test_a_dry_run_is_refused_where_it_is_not_honoured_yet(self):
        for cmd, d in declarations().items():
            if d["readonly"] != "-" or d["dryrun"] in ("yes", "exempt"):
                continue
            decl = D.Decl(REPO / "cmd" / cmd)
            verb = [v for v in decl.verbs.split(",") if not decl.honours_dryrun([v]) and not decl.is_readonly(v)][:1] \
                if decl.verbs else []
            if decl.verbs and not verb:
                continue
            with self.subTest(cmd=cmd):
                cp = run(cmd, *verb, "--dry-run")
                self.assertEqual(cp.returncode, 2, cp.stdout)
                self.assertIn("has no dry run yet", cp.stdout)

    def test_a_command_declared_exempt_refuses_dry_run_by_name(self):
        exempt = [c for c, d in declarations().items() if d["dryrun"] == "exempt"]
        self.assertEqual(exempt, ["selftest"])
        cp = run("selftest", "--dry-run")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("exempt from --dry-run", cp.stdout)

    def test_every_mutating_command_and_verb_has_a_dry_run(self):
        missing = []
        for d in D.all_commands(REPO):
            if d.nodryrun:
                continue
            for v in (d.verbs.split(",") if d.verbs else [""]):
                if not d.is_readonly(v) and not d.honours_dryrun([v] if v else []):
                    missing.append(("%s %s" % (d.name, v)).strip())
        self.assertEqual(missing, [])

    def test_no_command_parses_the_global_flags_itself(self):
        offenders = []
        for path in CMD_FILES:
            found = code_opts(path) & GLOBAL_OPTS
            if found:
                offenders.append(f"cmd/{path.name}: {sorted(found)}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestTheHelpTextOffersWhatTheDispatcherAccepts(unittest.TestCase):

    PLACEHOLDER = re.compile(r"<[^>]*>")
    STANDS_FOR_MORE = {"options", "flags", "args", "..."}

    def examples(self, path):
        head, out = [], []
        for i, line in enumerate(path.read_text().splitlines()):
            if i == 0:
                continue
            if not line.startswith("#"):
                break
            head.append(line[1:])
        for line in head[2:]:            # past the blank line and the synopsis
            m = re.match(r"^\s+wk %s(\s.*)?$" % re.escape(path.name), line)
            if not m:
                continue
            args = []
            for tok in (m.group(1) or "").split("#")[0].split():
                tok = tok.strip("[]").split("|")[0]
                tok = self.PLACEHOLDER.sub("X", tok).replace('"', "").replace("'", "")
                if tok and tok not in self.STANDS_FOR_MORE and tok not in GLOBAL_OPTS:
                    args.append(tok)
            if any(a.startswith("--") for a in args):
                out.append(args)
        return out

    def test_every_option_in_every_help_text_is_accepted(self):
        offenders = []
        for path in CMD_FILES:
            for args in self.examples(path):
                out = run(path.name, *args, "-h").stdout
                if "unknown option" in out:
                    offenders.append(f"  wk {path.name} {' '.join(args)}")
        self.assertEqual(offenders, [], "the help text names options the "
                         "dispatcher refuses before the command runs:\n"
                         + "\n".join(offenders))


class TestEveryRemovalAsks(unittest.TestCase):
    def _destructive_cmds(self):
        return {c for c, d in declarations().items() if d["destructive"] != "-"}
    REMOVING = re.compile(r"delete_vm|\"podman\", *\"(rm|rmi)\"|\"image\", \"prune\"|\"volume\", \"prune\"|unshare\", \"rm\"|"
                          r"tart, \"delete\"")
    REMOVERS = {"lib/wk/gc.py": ("gc",), "lib/wk/sysimage/guestbase.py": ("sysimage",), "lib/wk/places.py": ("rm", "gc", "stop")}

    def test_every_removing_effect_is_reached_only_from_commands_that_ask(self):
        found = {str(p.relative_to(REPO)) for p in (REPO / "lib" / "wk").rglob("*.py") if self.REMOVING.search(p.read_text())}
        self.assertEqual(found, set(self.REMOVERS), "a file with a removing effect is not named in REMOVERS (or no longer has one)")
        self.assertEqual(sorted({c for cs in self.REMOVERS.values() for c in cs} - self._destructive_cmds()), [])


class TestActAndConfirm(unittest.TestCase):

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        out = bash(". lib/common.sh; export WK_DRY_RUN=1; f=$(mktemp -u)\n"
                   "act touch \"$f\"; [ -e \"$f\" ] && echo EXISTS || echo ABSENT")
        self.assertIn("would run: touch", out.stderr)
        self.assertIn("ABSENT", out.stdout)
        self.assertNotIn("EXISTS", out.stdout)


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
