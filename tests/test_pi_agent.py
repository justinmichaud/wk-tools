"""The named-secret store both agents draw on: AGENT_SECRETS (lib/wk/secrets.py) is the one table, and every
reader (the store, `wk key set`, container/firstrun.sh, shell/bashrc, `wk doctor`) is held to it."""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, bash, run
from tests.test_wk_key import KeyTest
from tests.test_wk_secrets import KEY_SH

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Local  # noqa: E402
from wk.secrets import Secrets  # noqa: E402

FIRSTRUN = (REPO / "container" / "firstrun.sh").read_text()
RC = REPO / "shell" / "bashrc"

PLACEHOLDER = "placeholder-value-for-this-test"

# The claude row's rule (lib/credcheck.py) wants the shape of a setup token.
CLAUDE_SHAPED = "sk-ant-oat01-" + PLACEHOLDER


def secret_table():
    """(name, store file, home file, variable, kind, delivery) per row."""
    from wk import secrets
    rows = [tuple(r) for r in secrets.AGENT_SECRETS]
    assert rows and all(len(r) == 6 for r in rows), rows
    assert all(r[4] in ("value", "file") for r in rows), rows
    return rows


TABLE = secret_table()
NAMES = [r[0] for r in TABLE]

VALUE_ROWS = [r for r in TABLE if r[4] == "value"]
FILE_ROWS = [r for r in TABLE if r[4] == "file"]
VALUE_NAMES = [r[0] for r in VALUE_ROWS]

CONTAINER_ROWS = [r for r in VALUE_ROWS if "container" in r[5].split(",")]
NOT_CONTAINER_ROWS = [r for r in VALUE_ROWS if "container" not in r[5].split(",")]


def store_path(store, row):
    return store / ("agent-rw" if row[4] == "file" else "secrets") / row[1]


class TestTheTable(unittest.TestCase):
    def test_a_file_row_names_no_variable(self):
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertEqual("-", row[3])


class TestTheStoreIsByName(WkTest):

    def _store(self):
        d = self.tmp / "store"
        (d / "secrets").mkdir(parents=True)
        (d / "agent-rw").mkdir(parents=True)
        return d

    def _env(self, store):
        return {"WK_IN_VM": "1", "WK_STORE": str(store), "HOME": str(self.tmp)}

    def _sec(self, store):
        return Secrets(REPO, self._env(store), Local())

    def _sh(self, script, store):
        cp = bash(KEY_SH + script, env=self._env(store))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp

    def test_each_name_has_its_own_file_in_the_store(self):
        store = self._store()
        for row in TABLE:
            with self.subTest(name=row[0]):
                self.assertEqual(str(store_path(store, row)), self._sec(store).cred_path(row[0]))

    def test_an_unknown_name_has_no_path_and_is_not_invented(self):
        self.assertIsNone(self._sec(self._store()).cred_path("nope"))

    def test_stored_then_read_back_per_name(self):
        store = self._store()
        for row in TABLE:
            name = row[0]
            with self.subTest(name=name):
                self._sh(f'printf "%s\\n" {name}-{PLACEHOLDER} | key_store {name}', store)
                self.assertEqual(f"{name}-{PLACEHOLDER}\n", self._sec(store).cred_read(name))
                mode = store_path(store, row).stat().st_mode & 0o777
                self.assertEqual(0o600, mode, oct(mode))

    def test_presence_is_one_question_for_both_kinds(self):
        store = self._store()
        for row in TABLE:
            with self.subTest(name=row[0]):
                self.assertIs(False, self._sec(store).cred_stored(row[0]))
                self._sh(f'printf "%s\\n" x | key_store {row[0]}', store)
                self.assertIs(True, self._sec(store).cred_stored(row[0]))

    def test_a_file_row_is_read_whole_and_not_by_its_first_line(self):
        store = self._store()
        row = FILE_ROWS[0]
        self._sh(f'printf "one\\ntwo\\n" | key_store {row[0]}', store)
        self.assertEqual("one\ntwo\n", self._sec(store).cred_read(row[0]))

    def test_the_writable_directory_is_beside_the_secrets_one_never_inside(self):
        sec = self._sec(self._store())
        secrets, rw = sec.store.keyring_dir(), sec.store.keyring_agent_rw_dir()
        self.assertNotIn(secrets + "/", rw + "/")
        self.assertEqual(str(Path(secrets).parent), str(Path(rw).parent))

    def test_absent_reads_as_nothing_and_is_not_an_error(self):
        self.assertEqual("", self._sec(self._store()).cred_read("litellm"))

    def test_clearing_withdraws_one_and_leaves_the_others(self):
        store = self._store()
        self._sh(f'printf "%s\\n" a-{PLACEHOLDER} | key_store claude\n'
                 f'printf "%s\\n" b-{PLACEHOLDER} | key_store litellm\n'
                 'key_clear litellm\n', store)
        sec = self._sec(store)
        self.assertEqual((f"a-{PLACEHOLDER}\n", ""), (sec.cred_read("claude"), sec.cred_read("litellm")))

    def test_a_driver_moving_wk_store_does_not_move_them(self):
        """One set per machine: WK_HOST_SECRETS on macOS, the pre-override WK_STORE_DEFAULT elsewhere."""
        env = {"WK_HOST_SECRETS": "/this/device/secrets", "WK_STORE_DEFAULT": "/the/machine/store",
               "WK_STORE": "/some/drivers/own/state", "HOME": str(self.tmp)}
        for system, want in (("Linux", "/the/machine/store/secrets/litellm-key"),
                             ("Darwin", "/this/device/secrets/litellm-key")):
            with self.subTest(system=system):
                with mock.patch("wk.store.os.uname", return_value=mock.Mock(sysname=system)):
                    self.assertEqual(want, Secrets(REPO, env, Local(), macos=system == "Darwin").cred_path("litellm"))


class TestWkKeySet(WkTest):

    def _store(self, **secrets):
        d = self.tmp / "-".join(["store", *secrets])
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
        for name in VALUE_NAMES:
            self.assertIn(name, cp.stdout)

    def test_an_unknown_name_is_refused_and_the_valid_ones_named(self):
        cp = self._key("set", "not-an-agent")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("not-an-agent", cp.stdout)
        for name in VALUE_NAMES:
            self.assertIn(name, cp.stdout)

    def test_replacing_nothing_is_refused_and_names_the_remedy(self):
        cp = self._key("set", "litellm", "--replace")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("wk key set litellm", cp.stdout)

    def test_a_stored_secret_is_reported_by_name_and_never_printed(self):
        for name, value, var in (("litellm", PLACEHOLDER, "LITELLM_API_KEY"),
                                 ("claude", CLAUDE_SHAPED, "CLAUDE_CODE_OAUTH_TOKEN")):
            with self.subTest(name=name):
                cp = self._key("set", name, store=self._store(**{name: value}))
                self.assertEqual(0, cp.returncode, cp.stdout)
                self.assertRegex(cp.stdout, r"%s\s+stored\s+\S.*\$%s" % (name, var))
                self.assertNotIn(PLACEHOLDER, cp.stdout)


    def test_the_login_the_cli_makes_is_not_set_here(self):
        cp = self._key("set", FILE_ROWS[0][0])
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("there is no credential called '%s'" % FILE_ROWS[0][0], cp.stdout)

    def test_a_stored_credential_that_breaks_its_rule_is_reported_non_zero(self):
        store = self._store(claude="sk-ant-api03-" + PLACEHOLDER)
        cp = self._key("set", "claude", store=store)
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("Console API key", cp.stdout)
        self.assertNotIn(PLACEHOLDER, cp.stdout)

    def test_a_bad_flag_is_refused(self):
        cp = self._key("set", "litellm", "--wat")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("usage: wk key", cp.stdout)

class TestAContainerLinksEveryName(WkTest):
    """container/firstrun.sh links each value row a container gets, dangling until stored; a file row is not linked,
    since the Claude CLI's rename on refresh would replace the link with a private copy."""

    def test_the_loop_links_every_container_row_and_no_file_row(self):
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
        for name, file_, home_file, var, _kind, _delivery in CONTAINER_ROWS:
            with self.subTest(name=name):
                link = home / home_file
                self.assertTrue(link.is_symlink(), f"{home_file} is not a link")
                self.assertEqual(f"/secrets/{file_}", str(link.readlink()))
                self.assertIn(f"wk key set {name}", cp.stdout)
        for row in FILE_ROWS:
            with self.subTest(name=row[0]):
                self.assertFalse((home / row[2]).exists() or (home / row[2]).is_symlink(), row[2])


class TestTheShellExportsEveryName(WkTest):
    """shell/bashrc is the only reader, in every shell a person or `wk` starts."""

    SHELLS = {
        "editor terminal pane": ("zsh", ["-i", "-c"]),
        "login zsh": ("zsh", ["-l", "-c"]),
        "bash -lc (every Driver.exec)": ("bash", ["-lc"]),
        "non-interactive bash": ("bash", ["-c"]),
    }

    def _home(self, values=None):
        h = self.tmp / "home"
        h.mkdir(exist_ok=True)
        for name, _file, home_file, _var, _kind, _delivery in VALUE_ROWS:
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
            for name, _file, _home_file, var, _kind, _delivery in VALUE_ROWS:
                with self.subTest(shell=what, name=name):
                    self.assertEqual(want[name], got.get(var))

    def test_an_absent_one_sets_nothing(self):
        home = self._home({"claude": PLACEHOLDER})
        for what, (shell, args) in self.SHELLS.items():
            if not shutil.which(shell):
                continue
            with self.subTest(shell=what):
                got = self._values(shell, args, home)
                self.assertEqual(PLACEHOLDER, got.get("CLAUDE_CODE_OAUTH_TOKEN"))
                self.assertEqual("", got.get("LITELLM_API_KEY"))

    def test_a_dangling_link_means_not_set(self):
        home = self._home()
        (home / ".wk-litellm-key").symlink_to(home / "nothing-here")
        got = self._values("bash", ["-c"], home)
        self.assertEqual("", got.get("LITELLM_API_KEY"))

class TestPiIsAnAgentThisCommandKnows(unittest.TestCase):
    def test_an_unknown_agent_still_is_refused(self):
        cp = run("ai", "not-an-agent", "ws")
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertIn("claude, pi", cp.stdout)


class TestDoctorReportsEveryName(WkTest):
    """Every row is one `re-authable` line; an absent one is `??`, not missing, since the agent can still log in."""

    def test_it_reads_the_table(self):
        from tests.support import clean_env
        sys.path.insert(0, str(REPO / "lib"))
        from wk import doctor
        paths = doctor.Doctor(str(REPO), env=clean_env({"WK_STORE": "/scratch", "WK_IN_VM": "1"})).paths()
        self.assertEqual(NAMES, [k[7:] for k in paths if k.startswith("secret.")])

    def test_it_prints_one_line_per_name_and_none_of_them_as_missing(self):
        from tests.support import clean_env
        sys.path.insert(0, str(REPO / "lib"))
        from wk import doctor
        store = self.tmp / "store"
        (store / "secrets").mkdir(parents=True)
        (store / "agent-rw").mkdir(parents=True)
        first = TABLE[0]
        store_path(store, first).write_text(PLACEHOLDER + "\n")
        doc = doctor.Doctor(str(REPO), env=clean_env({"WK_STORE": str(store), "WK_IN_VM": "1"}))
        rows = list(doc.machine_local())
        self.assertEqual([], [r for r in rows if r[0] == doctor.MISS], rows)
        for name, sfile in ((r[0], r[1]) for r in TABLE):
            with self.subTest(name=name):
                line = [r for r in rows if f"/{sfile} " in r[1] + " "]
                self.assertEqual(1, len(line), rows)
                self.assertIn("re-authable", line[0][1] + line[0][2])
                self.assertIn(f"wk key set {name}", line[0][1] + line[0][2])
                self.assertEqual(doctor.OK if name == first[0] else doctor.UNK, line[0][0], line[0])
        self.assertNotIn(PLACEHOLDER, "".join(w + r for _, w, r in rows))


class TestAKeyTypedAtThePromptTravelsOnStdin(KeyTest):
    def test_the_litellm_key_is_never_an_argument(self):
        argvs, inputs = self.stored_on_stdin("litellm", "sk-notarealvirtualkey", typed=True)
        self.assertFalse([a for a in argvs if "sk-notarealvirtualkey" in a], argvs)
        self.assertIn("sk-notarealvirtualkey\n", inputs)


if __name__ == "__main__":
    unittest.main()
