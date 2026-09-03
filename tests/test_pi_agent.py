"""The second agent, and the named-secret store both agents draw on.

`wk ai pi <ws>` installs @earendil-works/pi-coding-agent into the workspace's
own ~/.local on first use and hands it the sandbox treatment `wk ai claude`
gets. Its credential arrives the same way Claude's does: `wk key set litellm`
puts one secret per name in the store, container/firstrun.sh links every name
into the workspace, and shell/bashrc exports each into the variable its agent
reads.

wk_agent_secrets (lib/store.sh) is the one table saying what those names are,
and most of what is tested here is that every reader agrees with it: a row with
no link and no exported variable is a secret nothing can use.

Nothing here uses a real key: every value is a placeholder, and what is under
test is the plumbing.

Run: python3 -m unittest tests.test_pi_agent -v
"""
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, run

AI = (REPO / "cmd" / "ai").read_text()
KEY = (REPO / "cmd" / "key").read_text()
STORE = (REPO / "lib" / "store.sh").read_text()
FIRSTRUN = (REPO / "container" / "firstrun.sh").read_text()
RC = REPO / "shell" / "bashrc"

PLACEHOLDER = "placeholder-value-for-this-test"

TOUCHED = ("cmd/ai", "cmd/key", "lib/store.sh", "container/firstrun.sh",
           "shell/bashrc")


def secret_table():
    """wk_agent_secrets, read out of the file that declares it: (name, store
    file, home file, variable, kind) per row. Every test below compares a
    reader against this rather than against a second copy of the list."""
    body = STORE.split("wk_agent_secrets() {", 1)[1]
    body = body.split("<<'EOF'\n", 1)[1].split("EOF\n", 1)[0]
    rows = [tuple(l.split()) for l in body.splitlines() if l.strip()]
    assert rows and all(len(r) == 5 for r in rows), rows
    assert all(r[4] in ("value", "file") for r in rows), rows
    return rows


TABLE = secret_table()
NAMES = [r[0] for r in TABLE]

# The two kinds are delivered and read differently, so most readers below are
# about one of them: a value row is a line an agent reads out of a variable, a
# file row is a document its own tool rewrites in place.
VALUE_ROWS = [r for r in TABLE if r[4] == "value"]
FILE_ROWS = [r for r in TABLE if r[4] == "file"]


def store_path(store, row):
    """Where a row's bytes live under a scratch store: the read-only secrets
    directory for a value, the one writable directory for a file."""
    return store / ("agent-rw" if row[4] == "file" else "secrets") / row[1]


class TestScriptsParse(unittest.TestCase):
    """`bash -n` on everything this touched: the cheapest thing that catches a
    quoting mistake in a heredoc or a here-string, which is most of what these
    files are."""

    def test_bash_n(self):
        for f in TOUCHED:
            with self.subTest(script=f):
                cp = subprocess.run(["bash", "-n", str(REPO / f)],
                                    capture_output=True, text=True, timeout=60)
                self.assertEqual(cp.returncode, 0, cp.stderr)


class TestTheTable(unittest.TestCase):
    def test_it_names_both_agents_credentials(self):
        self.assertEqual(["claude", "litellm", "claude-login"], NAMES)

    def test_claude_keeps_the_file_it_already_has(self):
        """Rotating nothing: a store made before the table existed holds
        secrets/claude-token, and every container already links to that name."""
        self.assertEqual(("claude", "claude-token", ".wk-agent-token",
                          "CLAUDE_CODE_OAUTH_TOKEN", "value"), TABLE[0])

    def test_a_file_row_names_no_variable(self):
        """Nothing exports one: the Claude CLI is pointed at the directory the
        file is in, and shell/bashrc has no line for it."""
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertEqual("-", row[3])


class TestTheStoreIsByName(WkTest):
    """One pair of functions for every name, so no command has an idea of its
    own about where a secret lives or how it is written."""

    def _store(self):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True)
        (d / "agent-rw").mkdir(parents=True)
        return d

    def _sh(self, script, store):
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
WK_IN_VM=1
WK_STORE={store}
. "$WK_ROOT/lib/store.sh"
{script}
''')

    def test_each_name_has_its_own_file_in_the_store(self):
        """And which of the two directories is the row's kind: the read-only
        secrets mount for a value, the writable one for a file the agent's own
        tool rewrites."""
        store = self._store()
        for row in TABLE:
            with self.subTest(name=row[0]):
                cp = self._sh(f'wk_agent_secret_path {row[0]}', store)
                self.assertEqual(str(store_path(store, row)), cp.stdout.strip(),
                                 cp.stderr)

    def test_an_unknown_name_has_no_path_and_is_not_invented(self):
        cp = self._sh('if wk_agent_secret_path nope; then echo "invented"; else echo "rc=1"; fi',
                      self._store())
        self.assertIn("rc=1", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("secrets/nope", cp.stdout)

    def test_stored_then_read_back_per_name(self):
        store = self._store()
        for row in TABLE:
            name = row[0]
            reader = "wk_agent_secret_bytes" if row[4] == "file" else "wk_agent_secret"
            with self.subTest(name=name):
                cp = self._sh(
                    f'printf "%s\\n" {name}-{PLACEHOLDER} | wk_agent_secret_store {name}\n'
                    f'printf "[%s]\\n" "$({reader} {name})"', store)
                self.assertIn(f"[{name}-{PLACEHOLDER}]", cp.stdout,
                              cp.stdout + cp.stderr)
                mode = store_path(store, row).stat().st_mode & 0o777
                self.assertEqual(0o600, mode, oct(mode))

    def test_presence_is_one_question_for_both_kinds(self):
        """Every gate asks "can this workspace authenticate?", and asks it the
        same way whichever kind the row is."""
        store = self._store()
        for row in TABLE:
            with self.subTest(name=row[0]):
                cp = self._sh(
                    f'if wk_agent_secret_present {row[0]}; then echo yes; else echo no; fi\n'
                    f'printf "%s\\n" x | wk_agent_secret_store {row[0]}\n'
                    f'if wk_agent_secret_present {row[0]}; then echo yes; else echo no; fi',
                    store)
                self.assertEqual(["no", "yes"], cp.stdout.split(), cp.stderr)

    def test_a_file_row_is_read_whole_and_not_by_its_first_line(self):
        """A credentials file is a document its tool parses. Truncating it to
        the first line would hand a workspace half a JSON object."""
        store = self._store()
        row = FILE_ROWS[0]
        cp = self._sh(
            f'printf "one\\ntwo\\n" | wk_agent_secret_store {row[0]}\n'
            f'printf "bytes=[%s]\\n" "$(wk_agent_secret_bytes {row[0]})"', store)
        self.assertIn("bytes=[one\ntwo]", cp.stdout, cp.stdout + cp.stderr)

    def test_the_writable_directory_is_beside_the_secrets_one_never_inside(self):
        """The secrets directory is mounted read-only into every workspace and
        has to stay unwritable; this one is the single thing a workspace may
        write, so it is a sibling -- the same shape wk_push_held_dir has."""
        cp = self._sh('printf "%s %s\\n" "$(wk_secrets_dir)" "$(wk_agent_rw_dir)"',
                      self._store())
        secrets, rw = cp.stdout.split()
        self.assertNotIn(secrets + "/", rw + "/")
        self.assertEqual(str(Path(secrets).parent), str(Path(rw).parent))

    def test_absent_reads_as_nothing_and_is_not_an_error(self):
        cp = self._sh('printf "[%s]\\n" "$(wk_agent_secret litellm)"; echo "rc=$?"',
                      self._store())
        self.assertIn("[]", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_clearing_withdraws_one_and_leaves_the_others(self):
        store = self._store()
        cp = self._sh(
            f'printf "%s\\n" a-{PLACEHOLDER} | wk_agent_secret_store claude\n'
            f'printf "%s\\n" b-{PLACEHOLDER} | wk_agent_secret_store litellm\n'
            'wk_agent_secret_clear litellm\n'
            'printf "claude=[%s] litellm=[%s]\\n" "$(wk_agent_secret claude)" "$(wk_agent_secret litellm)"',

            store)
        self.assertIn(f"claude=[a-{PLACEHOLDER}] litellm=[]", cp.stdout,
                      cp.stdout + cp.stderr)

    def test_a_driver_moving_wk_store_does_not_move_them(self):
        """There is one set per *machine*. targets/vm.sh points $WK_STORE at
        its own state directory, so resolving a secret against $WK_STORE would
        send `wk vm start` looking somewhere `wk key set` never writes.

        Two spellings of one directory (wk_secrets_dir, lib/store.sh): on a
        macOS host it is this device's own path (WK_HOST_SECRETS), never
        $WK_STORE; where the store is this machine's own it is the store
        recorded before the override."""
        cp = bash('''
. "$WK_ROOT/lib/common.sh"
WK_STORE=/the/machine/store
. "$WK_ROOT/lib/store.sh"
WK_STORE_DEFAULT=/the/machine/store
WK_STORE=/some/drivers/own/state
printf "path=%s\\n" "$(wk_agent_secret_path litellm)"
''', env={"WK_HOST_SECRETS": "/this/device/secrets"})
        self.assertIn("path=/this/device/secrets/litellm-key",
                      cp.stdout, cp.stdout + cp.stderr)


class TestWkKeySet(WkTest):
    """`wk key set <name>` is the one way in, and `wk key claude` is the same
    arm with the name filled in."""

    def _store(self, **secrets):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True)
        (d / "agent-rw").mkdir(parents=True)
        rows = dict((r[0], r) for r in TABLE)
        for name, value in secrets.items():
            f = store_path(d, rows[name])
            f.write_text(value + "\n")
            f.chmod(0o600)
        return d

    def _key(self, *args, store=None):
        return run("key", *args,
                   env={"WK_IN_VM": "1", "WK_STORE": str(store or self._store())})

    def test_no_name_lists_the_names(self):
        cp = self._key("set")
        self.assertNotEqual(0, cp.returncode)
        for name in NAMES:
            self.assertIn(name, cp.stdout)

    def test_an_unknown_name_is_refused_and_the_valid_ones_named(self):
        cp = self._key("set", "not-an-agent")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("not-an-agent", cp.stdout)
        for name in NAMES:
            self.assertIn(name, cp.stdout)

    def test_replacing_nothing_is_refused_and_names_the_remedy(self):
        cp = self._key("set", "litellm", "--replace")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("wk key set litellm", cp.stdout)

    def test_a_stored_secret_is_reported_and_never_printed(self):
        store = self._store(litellm=PLACEHOLDER)
        cp = self._key("set", "litellm", store=store)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("LITELLM_API_KEY", cp.stdout)
        self.assertNotIn(PLACEHOLDER, cp.stdout)

    def test_wk_key_claude_is_the_same_arm(self):
        store = self._store(claude=PLACEHOLDER)
        cp = self._key("claude", store=store)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", cp.stdout)
        self.assertNotIn(PLACEHOLDER, cp.stdout)
        self.assertIn("set|claude)", KEY)

    def test_a_bad_flag_is_refused(self):
        cp = self._key("set", "litellm", "--wat")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("usage: wk key", cp.stdout)

    def test_set_needs_no_github_login(self):
        """The dispatcher clears `needs` per subverb; storing a key is
        ssh-keygen-free and gh-free, and a machine whose gh login has expired
        must still be able to do it."""
        decl = [l for l in KEY.splitlines() if l.startswith("# wk: sub ")][0]
        self.assertIn("set", decl.split("needs=")[0].split()[3].split(","))

    def test_the_value_is_never_an_argument(self):
        """An argument is in `ps` for everyone on the machine. The one writer
        takes it on stdin, and nothing hands it on as a parameter."""
        self.assertIn('printf \'%s\\n\' "$_val" | wk_agent_secret_store "$_name"', KEY)
        self.assertNotIn("--token", KEY)
        self.assertNotIn('wk_agent_secret_store "$_name" "$_val"', KEY)
        # The writer is lib/secretfile.py, which takes the value on stdin and
        # is handed only the path (it refuses a path that is not a plain file
        # of this user's; see the file).
        self.assertIn('secretfile.py" write "$p"', STORE)


class TestAContainerLinksEveryName(WkTest):
    """container/firstrun.sh makes one link per row, whether or not the secret
    exists yet: a dangling link is "not set", and storing one later needs no
    rebuild."""

    def test_it_loops_over_the_table(self):
        self.assertIn('. "$1/lib/store.sh"; wk_agent_secrets', FIRSTRUN)
        self.assertIn('ln -sfn "/secrets/$_sfile" "$HOME/$_shome"', FIRSTRUN)

    def test_the_loop_links_every_row(self):
        """The loop lifted out and run against a scratch HOME -- the container
        it belongs to is not reachable from a test, but the linking is."""
        home = self.tmp / "home"
        home.mkdir()
        block = FIRSTRUN.split("_agent_secrets() {", 1)[1]
        block = "_agent_secrets() {" + block.split("\nEOF\n", 1)[0] + "\nEOF\n"
        cp = bash(f'''
log() {{ printf '%s\\n' "$*"; }}
WK_TOOLS="$WK_ROOT"
HOME={home}
{block}
''')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for name, file_, home_file, var, _kind in VALUE_ROWS:
            with self.subTest(name=name):
                link = home / home_file
                self.assertTrue(link.is_symlink(), f"{home_file} is not a link")
                self.assertEqual(f"/secrets/{file_}", str(link.readlink()))
                # And it says so, since the store has none of them yet.
                self.assertIn(f"wk key set {name}", cp.stdout)
                self.assertIn(var, cp.stdout)

    def test_a_file_row_is_not_linked_at_all(self):
        """The Claude CLI writes its credentials file through a temp file and a
        rename, which replaces a symlink with a regular file the first time it
        refreshes -- so the container would stop sharing the one file every
        other container is refreshing. It reads the mounted directory instead
        (shell/bashrc)."""
        home = self.tmp / "home-file-row"
        home.mkdir()
        block = FIRSTRUN.split("_agent_secrets() {", 1)[1]
        block = "_agent_secrets() {" + block.split("\nEOF\n", 1)[0] + "\nEOF\n"
        cp = bash(f'''
log() {{ printf '%s\\n' "$*"; }}
WK_TOOLS="$WK_ROOT"
HOME={home}
{block}
''')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertFalse((home / row[2]).exists(), row[2])
                self.assertFalse((home / row[2]).is_symlink(), row[2])


class TestTheShellExportsEveryName(WkTest):
    """shell/bashrc is the only reader, in every shell a person or `wk`
    starts -- an editor's terminal pane above all, which is not a login
    shell."""

    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh": ("zsh", ["-l", "-c"]),
        "bash -lc (every t_exec)": ("bash", ["-lc"]),
        "non-interactive bash": ("bash", ["-c"]),
    }

    def _home(self, values=None):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        for name, _file, home_file, _var, _kind in VALUE_ROWS:
            if values and name in values:
                (h / home_file).write_text(values[name] + "\n")
        return h

    def _values(self, shell, args, home):
        script = "; ".join(f'echo "{r[3]}=${r[3]}"' for r in VALUE_ROWS)
        cp = subprocess.run(
            [shell, *args, f'. "{RC}"; {script}'],
            cwd=str(REPO),
            env={"HOME": str(home), "TERM": "dumb",
                 "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
        )
        out = {}
        for line in cp.stdout.splitlines():
            k, _, v = line.partition("=")
            if k in [r[3] for r in VALUE_ROWS]:
                out[k] = v
        return out

    def test_every_shell_exports_every_one(self):
        want = {r[0]: f"{r[0]}-{PLACEHOLDER}" for r in VALUE_ROWS}
        home = self._home(want)
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            got = self._values(shell, args, home)
            for name, _file, _home_file, var, _kind in VALUE_ROWS:
                with self.subTest(shell=what, name=name):
                    self.assertEqual(want[name], got.get(var))

    def test_an_absent_one_sets_nothing(self):
        """The rc is shared by every machine in the fleet, so absence has to
        mean absence -- a workstation must not acquire either variable."""
        home = self._home({"claude": PLACEHOLDER})
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = self._values(shell, args, home)
                self.assertEqual(PLACEHOLDER, got.get("CLAUDE_CODE_OAUTH_TOKEN"))
                self.assertEqual("", got.get("LITELLM_API_KEY"))

    def test_a_dangling_link_means_not_set(self):
        """What a container has before `wk key set litellm` is ever run."""
        home = self._home()
        (home / ".wk-litellm-key").symlink_to(home / "nothing-here")
        got = self._values("bash", ["-c"], home)
        self.assertEqual("", got.get("LITELLM_API_KEY"))

    def test_the_rc_reads_the_same_pairs_the_table_declares(self):
        """The rc cannot source lib/store.sh -- a workspace has no store, and
        this runs in every shell without forking -- so the pairs are literal
        here and held to the table by this test."""
        text = RC.read_text()
        line = [l for l in text.splitlines() if l.startswith("for _wk_sec in ")][0]
        pairs = line.split(" in ", 1)[1].rstrip("; do").split()
        self.assertEqual([f"{r[2]}:{r[3]}" for r in VALUE_ROWS], pairs)

    def test_reading_them_does_not_fork(self):
        text = RC.read_text()
        block = text[text.index("# --- 6b. The agents' credentials"):
                     text.index("# --- 7. Completion")]
        code = "\n".join(l for l in block.splitlines()
                         if not l.lstrip().startswith("#"))
        for forker in ("cat ", "sed ", "awk ", "$(", "`"):
            self.assertNotIn(forker, code, f"{forker!r} in the credential block")


class TestPiIsAnAgentThisCommandKnows(unittest.TestCase):
    def test_it_no_longer_refuses_by_name(self):
        self.assertIn("claude|pi) shift ;;", AI)
        self.assertNotIn("the pi agent is not built yet", AI)

    def test_an_unknown_agent_still_is_refused(self):
        """cmd/ai directly, not through `wk`: the dispatcher resolves the
        workspace name first (name=required@2), so a test going that way is
        refused for the name and never reaches the agent word."""
        cp = bash('"$WK_ROOT/cmd/ai" not-an-agent ws')
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("unknown agent", cp.stderr)
        self.assertIn("claude, pi", cp.stderr)

    def test_the_research_is_written_down_where_the_branch_is(self):
        """What pi needs is not re-derivable from the code that uses it: the
        package name, the node floor and the reason LiteLLM is a models.json
        provider rather than an environment variable."""
        for fact in ("@earendil-works/pi-coding-agent",
                     "https://www.npmjs.com/package/@earendil-works/pi-coding-agent",
                     "https://github.com/earendil-works/pi",
                     "PI_NODE_MIN=22.19.0",
                     "models.json"):
            self.assertIn(fact, AI, fact)

    def test_remote_control_is_claude_code_s_alone(self):
        self.assertIn('[ -z "$RC" ] || [ "$AGENT" = claude ] || die', AI)

    def test_both_agents_get_the_same_sandbox_treatment(self):
        """One implementation, not two: the push switch, the commit wall and
        `wk verify` are above the launch and know nothing about which
        agent it is."""
        self.assertIn('exec ${WALL}$(sh_quote "$AGENT_BIN")', AI)
        self.assertIn('env WK_AGENT="$AGENT"', AI)
        self.assertEqual(1, AI.count('WALL="$(commit_wall_prefix'))
        self.assertEqual(1, AI.count('WK_NAME="$NAME" "$WK_ROOT/cmd/verify"'))
        # cmd/verify reads the name from WK_NAME and refuses a positional as
        # usage, so the name must never be passed as one (measured live: it
        # was, and remote control never started in a fresh workspace).
        self.assertNotIn('cmd/verify" "$NAME"', AI)

    def test_the_install_line_is_the_documented_one(self):
        self.assertIn("npm install -g --ignore-scripts --prefix", AI)

    def test_the_registry_is_reachable_from_a_workspace(self):
        proxy = (REPO / "container" / "proxy" / "wk-proxy.py").read_text()
        self.assertIn('"registry.npmjs.org"', proxy)


class TestPiNodeCompare(unittest.TestCase):
    """The refusal is about what pi actually requires (>= 22.19.0), so the
    compare is dotted rather than a major number."""

    CASES = {
        "v24.3.0": True, "v22.19.0": True, "v22.20.1": True, "v23.0.0": True,
        "v22.18.9": False, "v22.11.0": False, "v20.19.0": False,
        "v18.0.0": False, "": False, "vwhat": False,
    }

    def test_versions(self):
        script = "\n".join(
            f'if pi_node_ok {v!r}; then echo "{v}=yes"; else echo "{v}=no"; fi'
            for v in self.CASES)
        cp = bash(f'''
export WK_CLAUDE_LIB=1
. "$WK_ROOT/cmd/ai"
{script}
''')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        for v, ok in self.CASES.items():
            with self.subTest(version=v or "(nothing)"):
                self.assertIn(f"{v}={'yes' if ok else 'no'}", cp.stdout)


class TestPiEnsure(WkTest):
    """pi_ensure against a fake workspace: cmd/ai in library mode
    (WK_CLAUDE_LIB=1, the guard it defines for exactly this) with t_exec
    replaced, so nothing here reaches a container."""

    def _script(self, node="v22.19.0", npm="/usr/bin/npm", body="pi_ensure ws"):
        store = self.tmp / "store"
        (store / "secrets").mkdir(parents=True)
        self.store = store
        return f'''
export WK_CLAUDE_LIB=1
export WK_IN_VM=1
export WK_STORE={store}
. "$WK_ROOT/cmd/ai"

CALLS={self.tmp}/calls
INSTALLED={self.tmp}/installed

# The workspace, faked: the probe answers from these variables, and an install
# is recorded rather than run. Defined after the source so it wins over
# lib/target.sh's own, the same way tests/test_claude_rc.py does it.
t_exec() {{
    case "$*" in
        *"npm install"*) printf 'install: %s\\n' "$*" >> "$CALLS"; : > "$INSTALLED"; return 0 ;;
    esac
    printf 'node={node}\\n'
    printf 'npm={npm}\\n'
    if [ -e "$INSTALLED" ]; then printf 'pi=%s\\n' "$HOME/.local/bin/pi"; else printf 'pi=\\n'; fi
    printf 'models=no\\n'
}}
{body}
'''

    def test_no_npm_is_refused_with_the_remedy(self):
        cp = bash(self._script(npm=""))
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("no node runtime pi can use", cp.stderr)
        self.assertIn("container/firstrun.sh", cp.stderr)
        self.assertIn("22.19.0", cp.stderr)

    def test_too_old_a_node_is_refused_with_what_it_found(self):
        cp = bash(self._script(node="v20.11.1"))
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("v20.11.1", cp.stderr)
        self.assertIn("container/firstrun.sh", cp.stderr)

    def test_nothing_is_installed_when_the_check_refuses(self):
        bash(self._script(node="v18.0.0"))
        self.assertFalse((self.tmp / "calls").exists())

    def test_it_installs_once_and_then_finds_it(self):
        cp = bash(self._script(body='pi_ensure ws; pi_ensure ws; echo "bin=$PI_BIN"'))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        calls = (self.tmp / "calls").read_text().splitlines()
        self.assertEqual(1, len(calls), calls)
        self.assertIn("--ignore-scripts", calls[0])
        self.assertIn("@earendil-works/pi-coding-agent", calls[0])
        self.assertIn("bin=", cp.stdout)
        self.assertIn("/.local/bin/pi", cp.stdout)

    def test_it_prints_the_endpoint_file_when_a_key_is_stored(self):
        script = self._script()
        (self.store / "secrets" / "litellm-key").write_text(PLACEHOLDER + "\n")
        cp = bash(script)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("models.json", cp.stderr)
        self.assertIn("openai-completions", cp.stderr)
        self.assertIn("$LITELLM_API_KEY", cp.stderr)
        # The key itself is a value in a file, not something to print.
        self.assertNotIn(PLACEHOLDER, cp.stderr + cp.stdout)

    def test_with_no_key_it_says_which_command_stores_one(self):
        cp = bash(self._script())
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("wk key set litellm", cp.stderr)


class TestDoctorReportsEveryName(WkTest):
    """`wk doctor`'s machine-local section is the checklist a reinstall works
    from, so every named credential is one line in it -- looped from the table
    rather than the claude row alone, which is what "one implementation per
    behaviour" means here.

    Each is `re-authable`: a fresh value from the provider is as good as the
    old one, so it is neither backed up nor regenerable by this repo. And an
    absent one is `??`, never `--`: an agent with no credential asks the person
    to log in, which still works, so counting it as missing would report a
    healthy machine as broken."""

    DOCTOR = (REPO / "cmd" / "doctor").read_text()

    def test_it_loops_the_table(self):
        self.assertIn("wk_agent_secret_names", self.DOCTOR)
        self.assertIn('local_state "$(wk_agent_secret_path "$_sname")" re-authable',
                      self.DOCTOR)
        for row in TABLE:
            with self.subTest(name=row[0]):
                self.assertNotIn(row[1], self.DOCTOR,
                                 f"cmd/doctor names the {row[0]} row itself")

    def test_it_prints_one_line_per_name_and_none_of_them_as_missing(self):
        """Driven: the real `local_state` and the real loop, against a scratch
        store with one of the two stored. WK_IN_VM=1 because on a macOS host
        local_state forwards a store path into the podman machine, and doctor
        never starts one -- inside the VM (where its probe runs) the read is
        the plain one."""
        store = self.tmp / "store"
        (store / "secrets").mkdir(parents=True)
        (store / "agent-rw").mkdir(parents=True)
        first = TABLE[0]
        store_path(store, first).write_text(PLACEHOLDER + "\n")

        text = self.DOCTOR
        fn = text[text.index("local_state() { # <path> <kind> <how to get it back>"):]
        fn = fn[:fn.index("\n}\n") + 3]
        loop = text[text.index("for _sname in $(wk_agent_secret_names); do"):]
        loop = loop[:loop.index("done\n") + 5]

        cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
WK_STORE={store}
. "$WK_ROOT/lib/store.sh"
missing=0
ok()   {{ printf 'ok %s\\n' "$*"; }}
miss() {{ printf 'miss %s -> %s\\n' "$1" "$2"; missing=$((missing + 1)); }}
unk()  {{ printf 'unk %s -> %s\\n' "$1" "$2"; }}
{fn}
{loop}
printf 'missing=%s\\n' "$missing"
''', env={"WK_IN_VM": "1"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("missing=0", out, out)
        for row in TABLE:
            name, sfile = row[0], row[1]
            with self.subTest(name=name):
                line = [l for l in out.splitlines() if f"/{sfile} " in l + " "]
                self.assertEqual(1, len(line), out)
                self.assertIn("re-authable", line[0])
                self.assertIn(f"wk key set {name}", line[0])
                self.assertTrue(line[0].startswith("ok " if name == first[0]
                                                   else "unk "), line[0])
        self.assertNotIn(PLACEHOLDER, out)


if __name__ == "__main__":
    unittest.main()
