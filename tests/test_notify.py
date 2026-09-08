"""`wk notify`: telling a person the fleet wants them, and never silently not.

This is how the fleet reaches a person: a plant waiting at the startup manager,
a run handing a machine back, a refusal that arrives in bench mode. A line in a
terminal nobody is watching reaches nobody, so the property under test is the
absence of a no-op -- exit 0 only when ntfy.sh took the message, and a reason on
stderr naming the remedy every other time.

ntfy.sh is a local HTTP server, the way tests/test_credcheck.py stubs GitHub:
the real publishing code runs, the JSON body included, and nothing leaves the
machine. `WK_NTFY_API` is the one constant lib/wknotify.py reads for its base
URL, and it is the seam.

The topic name is the whole credential, so no real one appears here: every
topic below is this test's own placeholder string.

Run: python3 -m unittest tests.test_notify -v
"""
import ast
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tests.support import REPO, WkTest, bash, run

WKNOTIFY = REPO / "lib" / "wknotify.py"
CMD_NOTIFY = REPO / "cmd" / "notify"
CREDCHECK = REPO / "lib" / "credcheck.py"

# The grammar and the guessable bound come from the module that enforces them,
# so a test cannot assert a topic shape the rule does not hold.
sys.path.insert(0, str(REPO / "lib"))
from wknotify import GUESSABLE  # noqa: E402
from wknotify import TOPIC as GRAMMAR  # noqa: E402

# Not a credential, and deliberately nothing like one: long enough that the
# rule calls it unguessable, and recognisable in an assertion.
TOPIC = "placeholder-topic-for-this-test"
SHORT = "short"


class FakeNtfy(BaseHTTPRequestHandler):
    """What ntfy.sh itself answers. `POST /` takes the JSON publishing format;
    `GET /<topic>/json?poll=1` answers 200 with an empty body for a topic
    nobody has published to, 302 for a reserved name (it redirects to the ntfy
    web site) and 404 for a name that is not a topic at all."""

    post_status = 200
    get_status = 200
    published = []

    def _send(self, code, body=b""):
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "https://ntfy.sh/docs/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        FakeNtfy.published.append(json.loads(raw or b"{}"))
        if FakeNtfy.post_status != 200:
            return self._send(FakeNtfy.post_status,
                              json.dumps({"error": "topic %s is not allowed"
                                          % FakeNtfy.published[-1].get("topic")}
                                         ).encode())
        self._send(200, json.dumps({"id": "x", "event": "message"}).encode())

    def do_GET(self):
        self._send(FakeNtfy.get_status)

    def log_message(self, *a):
        pass


class _Ntfy(WkTest):
    def setUp(self):
        super().setUp()
        FakeNtfy.post_status = 200
        FakeNtfy.get_status = 200
        FakeNtfy.published = []
        self.server = HTTPServer(("127.0.0.1", 0), FakeNtfy)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def notify(self, topic, *args, url=None):
        """lib/wknotify.py publish, with the topic on stdin."""
        return subprocess.run(
            ["python3", str(WKNOTIFY), "publish", *args],
            input=topic, capture_output=True, text=True, timeout=60,
            env={"WK_NTFY_API": url if url is not None else self.url,
                 "PATH": "/usr/bin:/bin"})

    def check(self, topic, url=None):
        return subprocess.run(
            ["python3", str(WKNOTIFY), "check"],
            input=topic, capture_output=True, text=True, timeout=60,
            env={"WK_NTFY_API": url if url is not None else self.url,
                 "PATH": "/usr/bin:/bin"})


class TestItPublishesOrSaysWhyNot(_Ntfy):
    def test_a_headline_alone_is_the_message(self):
        cp = self.notify(TOPIC, "the plant is waiting")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual([{"topic": TOPIC, "message": "the plant is waiting"}],
                         FakeNtfy.published)

    def test_a_detail_makes_the_headline_the_title(self):
        cp = self.notify(TOPIC, "arm B stalled", "--detail", "2727s of silence",
                          "--tag", "warning")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual([{"topic": TOPIC, "title": "arm B stalled",
                           "message": "2727s of silence",
                           "tags": ["warning"]}], FakeNtfy.published)

    def test_a_refused_publish_is_not_reported_as_one(self):
        """The property everything else rests on: a notification that did not
        go out must not look like one that did."""
        FakeNtfy.post_status = 400
        cp = self.notify(TOPIC, "the plant is waiting")
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("refused the publish", cp.stderr)
        self.assertIn("HTTP 400", cp.stderr)

    def test_no_network_is_not_reported_as_a_publish(self):
        cp = self.notify(TOPIC, "the plant is waiting",
                         url="http://127.0.0.1:1")
        self.assertEqual(6, cp.returncode, cp.stdout)
        self.assertIn("could not reach", cp.stderr)
        self.assertEqual([], FakeNtfy.published)

    def test_a_publish_that_landed_has_exactly_one_exit_code(self):
        """A caller warns on every non-zero code, so a message ntfy.sh took
        answers 0 and nothing else -- the guessable-name verdict belongs to
        `check`, which is not a publish."""
        for topic in (TOPIC, SHORT):
            with self.subTest(topic=topic):
                self.assertEqual(0, self.notify(topic, "landed").returncode)

    def test_nothing_is_published_without_a_headline(self):
        cp = self.notify(TOPIC)
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertIn("usage:", cp.stderr)
        self.assertEqual([], FakeNtfy.published)

    def test_a_second_bare_word_is_refused_rather_than_dropped(self):
        cp = self.notify(TOPIC, "one", "two")
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertEqual([], FakeNtfy.published)


class TestTheTopicNeverLeaks(_Ntfy):
    def test_a_refusal_ntfy_echoed_the_topic_into_does_not_print_it(self):
        """ntfy's own error body quotes the topic back. Printing that would put
        the credential in whatever log the caller writes."""
        FakeNtfy.post_status = 400
        cp = self.notify(TOPIC, "the plant is waiting")
        self.assertNotIn(TOPIC, cp.stdout + cp.stderr)
        self.assertIn("<the topic>", cp.stderr)

    def test_the_topic_is_not_an_argument_to_anything(self):
        """`ps` shows every argument to every account on the machine, so the
        one way in is stdin -- in cmd/notify as well as here."""
        self.assertIn("wk_cred_read ntfy", CMD_NOTIFY.read_text())
        self.assertNotIn('"$(wk_cred_read ntfy)"', CMD_NOTIFY.read_text())

    def test_nothing_in_the_tree_holds_a_topic_to_leak(self):
        """The repository is public. The topic reaches the code from one
        machine-local file and nowhere else, so no source file, help block or
        rule carries a topic name at all."""
        for f in (WKNOTIFY, CMD_NOTIFY, REPO / "lib" / "credcheck.py"):
            with self.subTest(source=f.name):
                text = f.read_text()
                self.assertNotRegex(text, r"ntfy\.sh/[A-Za-z0-9]")


class TestTheRuleJudgesWhatTheTopicCanDo(_Ntfy):
    def test_a_topic_ntfy_serves_is_acceptable(self):
        cp = self.check(TOPIC)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("serves it", cp.stdout)

    def test_a_topic_nobody_has_published_to_is_not_reported_bad(self):
        """The state a freshly stored credential is in: ntfy.sh answers 200
        with an empty body, which is what a valid unused topic looks like."""
        FakeNtfy.get_status = 200
        cp = self.check(TOPIC)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_a_guessable_name_is_wide_rather_than_acceptable(self):
        cp = self.check(SHORT)
        self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("guessing", cp.stderr)

    def test_a_name_ntfy_carries_no_topic_by_is_bad(self):
        """A reserved name redirects to the ntfy web site; a followed redirect
        would read as 200 for a name that carries no topic."""
        for status in (302, 404):
            with self.subTest(status=status):
                FakeNtfy.get_status = status
                cp = self.check(TOPIC)
                self.assertEqual(5, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("carries no topic", cp.stderr)

    def test_a_rate_limit_or_an_outage_is_not_a_bad_credential(self):
        """429 and 5xx say nothing about the topic, so a good credential must
        not be condemned by one -- the same distinction the GitHub rule makes
        between 401 and every other non-200."""
        for status in (429, 503):
            with self.subTest(status=status):
                FakeNtfy.get_status = status
                cp = self.check(TOPIC)
                self.assertEqual(6, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("nothing about the topic was established",
                              cp.stderr)

    def test_a_name_that_is_not_a_topic_at_all_is_refused_before_the_network(self):
        cp = self.check("not one word", url="http://127.0.0.1:1")
        self.assertEqual(4, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("one word of letters", cp.stderr)

    def test_nothing_stored_is_refused_and_not_published(self):
        cp = self.check("")
        self.assertEqual(4, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("nothing there", cp.stderr)

    def _verdict(self, topic, url=None):
        cp = subprocess.run(
            ["python3", str(CREDCHECK), "check", "ntfy"],
            input=topic, capture_output=True, text=True, timeout=60,
            env={"WK_NTFY_API": url if url is not None else self.url,
                 "PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return cp.stdout.split("\t", 1)

    def test_the_credential_rule_reaches_the_same_verdicts(self):
        """One implementation of "ask ntfy.sh": the rule runs lib/wknotify.py
        rather than making a request of its own."""
        for topic, want in ((TOPIC, "ok"), (SHORT, "wide")):
            with self.subTest(topic=topic):
                self.assertEqual(want, self._verdict(topic)[0])
        FakeNtfy.get_status = 302
        self.assertEqual("bad", self._verdict(TOPIC)[0])
        FakeNtfy.get_status = 503
        self.assertEqual("unverified", self._verdict(TOPIC)[0])

    def test_a_network_that_did_not_answer_is_unverified_not_bad(self):
        verdict, detail = self._verdict(TOPIC, url="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not ask ntfy.sh", detail)


class TestTheTopicIsMintedNotInvented(_Ntfy):
    """The topic is a secret wk mints, the way `wk key deploy` generates a
    deploy key rather than asking for one: a name a person invents is short and
    guessable, which the rule can report (`wide`) and never prevent. `mint` is
    also the one verb here whose output carries a topic -- it is the only one
    holding a topic nothing has stored yet."""

    def mint(self, url=None):
        cp = subprocess.run(
            ["python3", str(WKNOTIFY), "mint"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=60,
            env={"WK_NTFY_API": url if url is not None else self.url,
                 "PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("", cp.stderr)
        self.assertEqual(1, len(cp.stdout.splitlines()), cp.stdout)
        return cp.stdout.strip()

    def test_a_minted_topic_is_one_word_of_the_grammar_the_rule_enforces(self):
        self.assertRegex(self.mint(), GRAMMAR.pattern)

    def test_a_minted_topic_can_never_be_the_guessable_verdict(self):
        """The stub serves it, which is the one answer `wide` comes out of; a
        minted name is long enough that the verdict is `ok` instead."""
        topic = self.mint()
        self.assertGreaterEqual(len(topic), GUESSABLE)
        self.assertEqual(0, self.check(topic).returncode)
        cp = self._credcheck("check", "ntfy", stdin=topic)
        self.assertEqual("ok", cp.stdout.split("\t", 1)[0], cp.stdout)

    def test_two_mints_are_never_the_same_topic(self):
        self.assertEqual(4, len({self.mint() for _ in range(4)}))

    def test_a_mint_needs_neither_a_topic_on_stdin_nor_a_network(self):
        """`secrets`, not a request: a machine with no network still ends up
        with a topic, and nothing was published to get one."""
        self.assertRegex(self.mint(url="http://127.0.0.1:1"), GRAMMAR.pattern)
        self.assertEqual([], FakeNtfy.published)

    def test_an_argument_after_mint_is_refused_rather_than_ignored(self):
        cp = subprocess.run(
            ["python3", str(WKNOTIFY), "mint", "extra"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=60, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertIn("usage:", cp.stderr)

    def test_no_other_verb_puts_a_topic_on_a_stream(self):
        topic = self.mint()
        FakeNtfy.post_status = 400
        for cp in (self.notify(topic, "landed"), self.check(topic)):
            self.assertNotIn(topic, cp.stdout + cp.stderr)

    def _credcheck(self, *args, stdin=""):
        return subprocess.run(
            ["python3", str(CREDCHECK), *args],
            input=stdin, capture_output=True, text=True, timeout=60,
            env={"WK_NTFY_API": self.url, "PATH": "/usr/bin:/bin"})

    def test_the_rule_table_names_ntfy_as_the_one_wk_mints(self):
        cp = self._credcheck("minted")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(["ntfy"], cp.stdout.split())

    def test_the_rule_mints_through_this_module(self):
        """One implementation of what a topic is: the rule does not roll
        randomness of its own beside lib/wknotify.py's."""
        cp = self._credcheck("mint", "ntfy")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        topic = cp.stdout.strip()
        self.assertRegex(topic, GRAMMAR.pattern)
        self.assertEqual(0, self.check(topic).returncode)

    def test_a_credential_nothing_mints_is_refused_by_name(self):
        cp = self._credcheck("mint", "github-pat")
        self.assertEqual(2, cp.returncode, cp.stdout)
        self.assertIn("ntfy", cp.stderr)
        self.assertEqual("", cp.stdout)


class TestTheCommand(_Ntfy):
    """`wk notify` end to end: the credential this machine holds, read the one
    way, published to the stub."""

    def env(self, extra=None):
        e = {"WK_STORE": str(self.tmp / "store"), "WK_NTFY_API": self.url}
        if extra:
            e.update(extra)
        return e

    def store_topic(self, topic=TOPIC):
        d = self.tmp / "store" / "notify"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "ntfy-topic"
        p.write_text(topic + "\n")
        p.chmod(0o600)
        return p

    def test_no_topic_here_names_the_remedy_and_publishes_nothing(self):
        cp = run("notify", "the plant is waiting", env=self.env())
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no ntfy topic on this machine: wk key set ntfy", cp.stdout)
        self.assertEqual([], FakeNtfy.published)

    def test_a_stored_topic_publishes(self):
        self.store_topic()
        cp = run("notify", "the plant is waiting", "--detail", "round 17",
                 env=self.env())
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertEqual([{"topic": TOPIC, "title": "the plant is waiting",
                           "message": "round 17"}], FakeNtfy.published)
        self.assertNotIn(TOPIC, cp.stdout)

    def test_a_refused_publish_exits_one_with_a_reason(self):
        self.store_topic()
        FakeNtfy.post_status = 400
        cp = run("notify", "the plant is waiting", env=self.env())
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("did not go out", cp.stdout)
        self.assertNotIn(TOPIC, cp.stdout)

    def test_it_is_refused_inside_a_workspace(self):
        """A notification a person acts on must not be forgeable from inside a
        workspace, so the credential is not in one and the command says so."""
        from tests.support import fake_workspace
        with fake_workspace() as ws:
            cp = ws.run("notify", "the plant is waiting")
            self.assertNotEqual(0, cp.returncode, cp.stdout)
            self.assertIn("acts on a host", cp.stdout)

    def test_an_unknown_flag_is_refused(self):
        cp = run("notify", "headline", "--bogus", env=self.env())
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("usage:", cp.stdout)

    def test_a_flag_with_no_value_is_refused(self):
        cp = run("notify", "headline", "--detail", env=self.env())
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("--detail needs a value", cp.stdout)


class TestTheCredentialIsDeclaredWhereARebuildLooks(unittest.TestCase):
    """New machine-local state is a line in `wk doctor`'s machine-local
    section, or a reinstall loses it silently (CLAUDE.md)."""

    DOCTOR = (REPO / "cmd" / "doctor").read_text()

    def test_the_machine_local_section_names_the_topic(self):
        for line in self.DOCTOR.splitlines():
            if line.startswith('local_state "$(wk_ntfy_topic_path)"'):
                self.assertIn("re-authable", line)
                self.assertIn("wk key set ntfy", line)
                return
        raise AssertionError("the machine-local section does not name the topic")

    def test_it_is_not_in_a_directory_a_workspace_can_read(self):
        """The secrets directory is mounted read-only into every container, so
        a topic in there would let any workspace forge a notification."""
        cp = bash('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n'
                  'printf "%s\\n%s\\n%s\\n" "$(wk_ntfy_topic_path)" '
                  '"$(wk_secrets_dir)" "$(wk_agent_rw_dir)"',
                  env={"WK_STORE": "/scratch/store",
                       "WK_HOST_SECRETS": "/scratch/store/secrets"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        topic, secrets, agent_rw = cp.stdout.split()
        self.assertEqual("/scratch/store/notify/ntfy-topic", topic)
        for mounted in (secrets, agent_rw):
            self.assertFalse(topic.startswith(mounted + "/"), topic)

    def test_the_help_block_says_a_failed_notify_is_a_warning(self):
        """What `wk notify -h` prints, so no caller decides for itself whether
        a notification that did not go out should end a run."""
        text = CMD_NOTIFY.read_text()
        head = text[:text.index("\nset -euo pipefail")]
        self.assertIn("warning", head)
        self.assertIn("cannot cost a measurement", head)


class TestSdNotifyIsUntouched(unittest.TestCase):
    """The three Type=notify host services read this module too; the publish
    is beside sd_notify because both are "how a program here tells someone
    it is up", and tests/test_host_units.py holds the rest of that rule."""

    def test_it_is_still_a_no_op_without_the_socket(self):
        cp = subprocess.run(
            ["python3", "-c", "import sys; sys.path.insert(0, %r);"
             "from wknotify import sd_notify; sd_notify('READY=1');"
             "print('returned')" % str(REPO / "lib")],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("returned", cp.stdout)

    def test_it_sends_the_state_to_the_socket_it_is_given(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "notify.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            srv.bind(path)
            srv.settimeout(30)
            cp = subprocess.run(
                ["python3", "-c", "import sys; sys.path.insert(0, %r);"
                 "from wknotify import sd_notify; sd_notify('READY=1')"
                 % str(REPO / "lib")],
                capture_output=True, text=True, timeout=60,
                env={"PATH": "/usr/bin:/bin", "NOTIFY_SOCKET": path})
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            self.assertEqual(b"READY=1", srv.recv(64))
            srv.close()

    def test_the_publish_and_sd_notify_share_nothing_but_the_file(self):
        """Two ways a program here says something, not one behaviour with two
        spellings: the publish is HTTP and sd_notify is a datagram."""
        tree = ast.parse(WKNOTIFY.read_text())
        bodies = dict((n.name, ast.dump(n)) for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef))
        self.assertNotIn("socket", bodies["publish"])
        self.assertNotIn("urllib", bodies["sd_notify"])


if __name__ == "__main__":
    unittest.main()
