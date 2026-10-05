"""Every credential's rule (lib/credcheck.py); each issuer is a loopback HTTP server, so the real request code runs."""
import copy
import json
import os
import sys
import urllib.parse
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CREDCHECK = REPO / "lib" / "credcheck.py"

sys.path.insert(0, str(REPO / "lib"))
import credcheck   # noqa: E402
from tests.support import live_selected, owed, requires   # noqa: E402

FORKS = "wkuser/WebKit wkuser/WPEWebKit"
PROJECTS = {"wkuser/WebKit": "WebKit/WebKit", "wkuser/WPEWebKit": "WebPlatformForEmbedded/WPEWebKit"}
FINE = "github_pat_11ABCDEFG_notarealtoken"
CLASSIC = "ghp_notarealclassictoken0123456789"

# What WebKit/WebKit answered a never-expiring fine-grained token, 2026-09-15.
POLICY = ("The 'WebKit' organization forbids access via a fine-grained personal access tokens if the token's lifetime "
          "is greater than 366 days. Please adjust your token's lifetime at the following URL: "
          "https://github.com/settings/personal-access-tokens/19512093")


class JsonHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, headers=()):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def serve(handler, cleanup):
    """A loopback server for `handler`, stopped through `cleanup` (a TestCase's addCleanup); its base URL."""
    server = HTTPServer(("127.0.0.1", 0), handler)
    cleanup(server.server_close)
    cleanup(server.shutdown)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % server.server_port


class FakeGitHub(JsonHandler):
    """GitHub's /user, /user/repos (paged), /repos/<r> (with `parent`) and POST /repos/<r>/pulls (403 by default)."""

    # Class state the suite shares: `reset` gives each fixture its own starting point, on FakeGitHub itself
    # because the handler reads the base class.
    DEFAULTS = dict(user_status=200, scopes="", expiry="", pulls={}, repos=[], repos_status=200, repos_answer=None,
                    parents={}, repo_status={}, repo_message="", pulls_message={}, seen=[])

    @classmethod
    def reset(cls, **fields):
        for name, value in dict(cls.DEFAULTS, **fields).items():
            setattr(FakeGitHub, name, copy.deepcopy(value))

    def do_GET(self):
        FakeGitHub.seen.append(("GET", self.path, self.headers.get("Authorization", "")))
        path, _, query = self.path.partition("?")
        if path == "/user/repos":
            if FakeGitHub.repos_status != 200:
                return self._send(FakeGitHub.repos_status, {"message": "Server Error"})
            if FakeGitHub.repos_answer is not None:
                return self._send(200, FakeGitHub.repos_answer)
            q = urllib.parse.parse_qs(query)
            per = int(q.get("per_page", ["30"])[0])
            page = int(q.get("page", ["1"])[0])
            window = FakeGitHub.repos[(page - 1) * per:page * per]
            return self._send(200, [{"full_name": n} for n in window])
        if path.startswith("/repos/"):
            name = path[len("/repos/"):]
            status = FakeGitHub.repo_status.get(name, 200)
            if status != 200:
                return self._send(status, {"message": FakeGitHub.repo_message})
            body = {"full_name": name}
            if name in FakeGitHub.parents:
                body["parent"] = {"full_name": FakeGitHub.parents[name]}
            return self._send(200, body)
        if path != "/user":
            return self._send(404, {"message": "Not Found"})
        if FakeGitHub.user_status != 200:
            return self._send(FakeGitHub.user_status, {"message": "Bad credentials"})
        headers = [("x-oauth-scopes", FakeGitHub.scopes)]
        if FakeGitHub.expiry:
            headers.append(("github-authentication-token-expiration", FakeGitHub.expiry))
        self._send(200, {"login": "wkuser"}, headers)

    def do_POST(self):
        FakeGitHub.seen.append(("POST", self.path, self.headers.get("Authorization", "")))
        repo = self.path[len("/repos/"):-len("/pulls")]
        code = FakeGitHub.pulls.get(repo, 403)
        if code != 403:
            return self._send(code, {"message": "x"})
        self._send(code, {"message": FakeGitHub.pulls_message.get(repo, "Resource not accessible by personal access token")})


FakeGitHub.reset()


class _Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = serve(FakeGitHub, cls.addClassCleanup)

    def setUp(self):
        FakeGitHub.reset(pulls=dict.fromkeys(FORKS.split() + list(PROJECTS.values()), 422), repos=FORKS.split(),
                         parents=dict(PROJECTS), repo_message=POLICY)
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-credcheck-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))

    def check(self, name, value=None, repos=FORKS, path=None, evidence=(), api=None, env=None):
        args = ["python3", str(CREDCHECK), "check", name, "--repos", repos]
        if path is not None:
            args += ["--path", str(path)]
            if value is None and Path(path).exists():
                value = Path(path).read_text()
        for e in evidence:
            args += ["--evidence", e]
        e = dict(os.environ)
        e["WK_GITHUB_API"] = api if api is not None else self.base
        e.update(env or {})
        cp = subprocess.run(args, input="" if value is None else value, capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        verdict, _, detail = cp.stdout.partition("\t")
        return verdict, detail

    def expect(self, answer, verdict, ins=(), outs=()):
        got, detail = answer
        self.assertEqual(verdict, got, detail)
        for phrase in ins:
            self.assertIn(phrase, detail)
        for phrase in outs:
            self.assertNotIn(phrase, detail)


BZ_LOGIN = "me@example.test"
BZ_KEY = "notarealbugzillakey0123456789abcdefghijk"


class FakeBugzilla(JsonHandler):
    """bugs.webkit.org's valid_login, measured 2026-09-14: true for the key's own login, false for another, 400/306
    for a key it does not know; Bugzilla 5.0's comment-tag search answers `tags` (a status and a body; None hangs up)."""

    seen = []
    tags = (200, [])

    def do_GET(self):
        FakeBugzilla.seen.append(self.path)
        path, _, query = self.path.partition("?")
        q = urllib.parse.parse_qs(query)
        if path.startswith("/rest/bug/comment/tags/"):
            if FakeBugzilla.tags is None:
                self.close_connection = True
                return None
            return self._send(*FakeBugzilla.tags)
        if path != "/rest/valid_login":
            return self._send(404, {"error": True, "code": 32614})
        if q.get("api_key", [""])[0] != BZ_KEY:
            return self._send(400, {"error": True, "code": 306, "message": "The API key you specified is invalid."})
        self._send(200, {"result": q.get("login", [""])[0] == BZ_LOGIN})


class _Bugzilla(_Rules):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bz_base = serve(FakeBugzilla, cls.addClassCleanup)

    def setUp(self):
        super().setUp()
        FakeBugzilla.seen = []
        FakeBugzilla.tags = (200, [])

    def bz_check(self, value, login=BZ_LOGIN, api=None):
        return self.check("bugzilla-api-key", value, evidence=("login=%s" % login,) if login else (),
                          env={"WK_BUGZILLA_API": api or self.bz_base})


class TestTheBugzillaKey(_Bugzilla):
    def test_it_is_judged_as_a_pair_then_probed_read_only_for_comment_tagging(self):
        self.expect(self.bz_check(BZ_KEY), "ok", (BZ_LOGIN, "while push is on", "can tag comments"), ("cannot",))
        self.assertEqual(2, len(FakeBugzilla.seen), FakeBugzilla.seen)
        self.assertTrue(FakeBugzilla.seen[0].startswith("/rest/valid_login?"))
        self.assertIn("api_key=" + BZ_KEY, FakeBugzilla.seen[0])
        self.assertIn("login=me%40example.test", FakeBugzilla.seen[0])
        self.assertTrue(FakeBugzilla.seen[1].startswith("/rest/bug/comment/tags/"), FakeBugzilla.seen)
        self.assertIn("api_key=" + BZ_KEY, FakeBugzilla.seen[1])

    def test_the_comment_tag_answer_names_what_the_account_can_do(self):
        for answer, word in (((401, {"error": True, "code": 304, "message": "not authorized"}), "cannot tag comments"),
                             ((400, {"error": True, "code": 304}), "cannot tag comments"),
                             ((403, {"error": True}), "cannot tag comments"),
                             ((400, {"error": True, "code": 125}), "tagging is off"),
                             ((500, {"error": True, "code": 32000}), "HTTP 500"), (None, "not known")):
            with self.subTest(answer=answer):
                FakeBugzilla.tags = answer
                self.expect(self.bz_check(BZ_KEY), "ok", (word,), ("can tag comments",))

    def test_each_refusal_has_its_verdict(self):
        for value, login, api, verdict, ins, asks in (
                (BZ_KEY, "other@example.test", None, "bad", ("another account", "other@example.test"), True),
                ("notthekey", BZ_LOGIN, None, "bad", ("306", "wk key set bugzilla-api-key"), True),
                (BZ_KEY, "", None, "unverified", ("contributors.json", "wk sync"), False),
                ("two words", BZ_LOGIN, None, "bad", (), False),
                (BZ_KEY, BZ_LOGIN, "http://127.0.0.1:1", "unverified", ("could not reach",), True)):
            with self.subTest(value=value, login=login, api=api):
                FakeBugzilla.seen = []
                self.expect(self.bz_check(value, login=login, api=api), verdict, ins)
                if not asks:
                    self.assertEqual([], FakeBugzilla.seen, "nothing to ask")


class TestBugsWebkitOrgGatesTaggingOnEditbugs(unittest.TestCase):
    @requires(lambda: None if live_selected() else "live tier not selected: needs bugs.webkit.org and two accounts")
    @owed("whether bugs.webkit.org's comment_taggers_group is editbugs: the comment-tag probe answers a key of an account "
          "with editbugs and refuses (304) one of an account without it")
    def test_editbugs_probe(self):
        self.fail("not measured: run the probe with both accounts' keys, then make the doctor row say editbugs")


OTHERS = ["wkuser/other%d" % i for i in range(44)]
PAGED = ["wkuser/r%03d" % i for i in range(150)]
ALL = FORKS.split() + list(PROJECTS.values())


def row(token, verdict, ins=(), outs=(), never=None, api=None, **setup):
    return token, verdict, ins, outs, never, api, setup


# (token, verdict, phrases in the detail, phrases not in it, a (method, path prefix) never requested, api, FakeGitHub state)
GITHUB_ROWS = (
    row(FINE, "ok", ["fine-grained"] + ["can open a pull request on %s" % r for r in ALL]),
    row(FINE, "ok", ("as wkuser", "expires 2026-12-01"), expiry="2026-12-01 00:00:00 UTC"),
    row(FINE, "bad", ("wkuser/WPEWebKit", "Pull requests: write", "settings/tokens/new", "wk key set github-pat"),
        pulls={"wkuser/WPEWebKit": 403}),
    row(FINE, "bad", ("cannot see wkuser/WebKit",), pulls={"wkuser/WebKit": 404}),
    row(FINE, "bad", ("does not accept this token (HTTP 401)",), user_status=401),
    # Measured 2026-09-15: a fine-grained token reads WebKit/WebKit and is refused POST /pulls there; the project is
    # asked before the account is enumerated.
    row(FINE, "bad", ("WebKit/WebKit refuses this token a pull request", "Resource not accessible by personal access token",
                      "reaches only repositories owned by the account", "A classic one is what can",
                      "settings/tokens/new", "scopes=public_repo"),
        never=("GET", "/user/repos"), pulls={"WebKit/WebKit": 403}),
    row(FINE, "bad", ("366 days", "personal-access-tokens/19512093", "wk key set github-pat"),
        pulls={"WebKit/WebKit": 403}, pulls_message={"WebKit/WebKit": POLICY}),
    row(CLASSIC, "bad", ("WebKit/WebKit",), ("reaches only repositories owned by the account",), scopes="repo",
        pulls={"WebKit/WebKit": 403}),
    row(FINE, "unverified", ("which project each fork belongs to",), repo_status={"wkuser/WebKit": 500}),
    row(FINE, "unverified", ("rather than 422",), pulls={"WebKit/WebKit": 500}),
    row(CLASSIC, "wide", ("every repository this account can write", "repo, read:org"), scopes="repo, read:org"),
    row(CLASSIC, "bad", ("delete_repo",), never=("POST", ""), scopes="repo, delete_repo"),
    row(CLASSIC, "bad", ("admin:org",), never=("POST", ""), scopes="repo, admin:org"),
    # No endpoint enumerates what a token was granted, so each listed repository is probed like the forks.
    row(FINE, "ok", ("on the 2 forks and on none of the 44 other",), repos=FORKS.split() + OTHERS),
    row(FINE, "bad", ("44 repositories beyond the 2 forks", "tick nothing but the 'public_repo'") + tuple(FORKS.split()),
        repos=FORKS.split() + OTHERS, pulls=dict.fromkeys(OTHERS, 422)),
    row(FINE, "bad", ("1 repositories beyond the 2 forks",), repos=FORKS.split() + PAGED, pulls={PAGED[-1]: 422}),
    row(CLASSIC, "wide", (), never=("GET", "/user/repos"), scopes="repo"),
    row(FINE, "unverified", ("which repositories this token reaches",), repos_status=500),
    row(FINE, "unverified", ("which repositories this token reaches",), repos_answer={"message": "not a list"}),
    row("hunter2", "bad", ("does not start like a GitHub personal access token",)),
    *(row(t, "bad", ("not a personal access token",)) for t in ("gho_abc", "ghs_abc", "ghu_abc", "ghr_abc")),
    row("", "bad", never=("", "")),
    row(FINE, "unverified", ("could not reach", "wk doctor"), api="http://127.0.0.1:1"),
)


class TestTheGitHubToken(_Rules):
    def test_each_answer_github_gives_has_its_verdict(self):
        for i, (token, verdict, ins, outs, never, api, setup) in enumerate(GITHUB_ROWS):
            with self.subTest(row=i, token=token[:12], setup=sorted(setup)):
                self.setUp()
                FakeGitHub.pulls.update(setup.pop("pulls", {}))
                for k, v in setup.items():
                    setattr(FakeGitHub, k, v)
                self.expect(self.check("github-pat", token, api=api), verdict, ins, outs)
                if never:
                    self.assertEqual([], [p for p in FakeGitHub.seen if p[0].startswith(never[0])
                                          and p[1].startswith(never[1])])

    def test_a_fork_of_nothing_has_no_project_to_ask(self):
        FakeGitHub.parents = {}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertEqual(["/repos/%s/pulls" % r for r in FORKS.split()], [p[1] for p in FakeGitHub.seen if p[0] == "POST"])

    def test_one_project_is_asked_once_however_many_forks_name_it(self):
        FakeGitHub.parents = dict.fromkeys(PROJECTS, "WebKit/WebKit")
        self.check("github-pat", FINE)
        self.assertEqual(1, [p[1] for p in FakeGitHub.seen if p[0] == "POST"].count("/repos/WebKit/WebKit/pulls"))


class TestWhereTheseApisMayBePointed(unittest.TestCase):
    def test_only_https_or_loopback_is_accepted_and_anything_else_refused_by_name(self):
        names = ("WK_GITHUB_API", "WK_ANTHROPIC_API", "WK_BUGZILLA_API")
        for env in [{var: "http://evil.example/"} for var in names] + [dict.fromkeys(names, value) for value in
                                                                         ("https://api.example.com", "http://127.0.0.1:1",
                                                                          "http://localhost:8080")]:
            with self.subTest(env=env):
                cp = subprocess.run(["python3", str(CREDCHECK), "names"], capture_output=True, text=True,
                                    env=dict(os.environ, **env), timeout=60)
                if "http://evil.example/" in env.values():
                    self.assertNotEqual(0, cp.returncode, cp.stdout)
                    self.assertIn(next(iter(env)), cp.stderr)
                    self.assertIn("Authorization", cp.stderr)
                else:
                    self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)


class FakeAnthropic(JsonHandler):
    """api.anthropic.com, measured 2026-09-10: GET /v1/models is 200 for an accepted token, 401 otherwise."""

    status = 200
    seen = []

    def do_GET(self):
        FakeAnthropic.seen.append((self.command, self.path, self.headers.get("Authorization", ""),
                                   self.headers.get("anthropic-version", "")))
        if FakeAnthropic.status != 200:
            return self._send(FakeAnthropic.status, {"type": "error", "error": {"type": "authentication_error"}})
        return self._send(200, {"data": [{"id": "claude-x"}]})


class _Anthropic(_Rules):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.anthropic_base = serve(FakeAnthropic, cls.addClassCleanup)

    def setUp(self):
        super().setUp()
        FakeAnthropic.status = 200
        FakeAnthropic.seen = []

    def anthropic_env(self, api=None):
        return {"WK_ANTHROPIC_API": api if api is not None else self.anthropic_base}


class FakeLiteLLM(JsonHandler):
    """ai.igalia.com, measured 2026-09-14: /v1/models lists what a key may call (401 for one it does not accept), and
    /key/info is 403 for a key restricted to the LLM API routes."""

    models_status = 200
    info_status = 403

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            status = FakeLiteLLM.models_status
            body = {"data": [{"id": "m1"}, {"id": "m2"}]} if status == 200 else {"error": {"message": "Authentication Error"}}
        elif self.path.startswith("/key/info"):
            status = FakeLiteLLM.info_status
            body = ({"info": {"key_alias": "wk", "expires": None, "max_budget": 10}} if status == 200
                    else {"detail": "Virtual key is not allowed to call this route."})
        else:
            status, body = 404, {"detail": "Not Found"}
        self._send(status, body)


class TestTheAgentKeys(_Anthropic):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.litellm_base = serve(FakeLiteLLM, cls.addClassCleanup)

    def claude(self, value, api=None):
        return self.check("claude", value, env=self.anthropic_env(api))

    def litellm_key(self, value, api=None):
        return self.check("litellm", value, env={"WK_LITELLM_API": api if api is not None else self.litellm_base})

    def test_a_setup_token_anthropic_accepts_is_asked_about_and_its_narrowness_named(self):
        self.expect(self.claude("sk-ant-oat01-abc"), "ok", ("inference-only", "Anthropic accepts it"))
        self.assertEqual([("GET", "Bearer sk-ant-oat01-abc", "2023-06-01")], [s[::2] + s[3:] for s in FakeAnthropic.seen])
        self.assertTrue(FakeAnthropic.seen[0][1].startswith("/v1/models"), FakeAnthropic.seen)

    def test_a_token_anthropic_refuses_is_bad_and_one_it_cannot_ask_unverified(self):
        FakeAnthropic.status = 401
        self.expect(self.claude("sk-ant-oat01-abc"), "bad", ("spent, revoked or expired", "wk key set claude --replace"))
        self.expect(self.claude("sk-ant-oat01-abc", api="http://127.0.0.1:1"), "unverified", ("could not reach",))

    def test_a_token_of_another_shape_is_refused_without_a_request(self):
        for value, why in (("sk-ant-api03-abc", "bills the organization"), ("hunter2", "sk-ant-oat"),
                           (json.dumps({"claudeAiOauth": {}}), "")):
            with self.subTest(value=value[:12]):
                self.expect(self.claude(value), "bad", (why,))
        self.assertEqual([], FakeAnthropic.seen)

    def test_each_answer_litellm_gives_has_its_verdict(self):
        for key, models, info, api, verdict, ins in (
                ("sk-abc123", 200, 403, None, "ok", ("serves it 2 model(s)", "restricted to the LLM API routes",
                                                    "models.json", self.litellm_base + "/v1")),
                ("sk-abc123", 401, 403, None, "bad", ("does not accept this key", "wk key set litellm")),
                ("sk-abc123", 200, 200, None, "wide", ("key-management routes", "alias wk")),
                ("sk-abc123", 200, 403, "http://127.0.0.1:1", "unverified", ("could not reach",)),
                ("sk-ant-api03-abc", 200, 403, None, "bad", ("upstream account",)),
                ("", 200, 403, None, "bad", ())):
            with self.subTest(key=key, models=models, info=info, api=api):
                FakeLiteLLM.models_status, FakeLiteLLM.info_status = models, info
                self.expect(self.litellm_key(key, api), verdict, ins)


class TestTheClaudeLogin(_Anthropic):
    """A login document: its access token is asked about while it lasts, and its refresh token is never spent here."""

    def login(self, expires_at, access="sk-ant-oat01-abc", refresh="sk-ant-ort01-def"):
        return self.check("claude-login", json.dumps({"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                                                                        "expiresAt": expires_at}}),
                          env=self.anthropic_env())

    def test_it_is_judged_by_its_access_token_while_it_lasts(self):
        self.expect(self.login(4102444800000), "ok")
        self.assertEqual("Bearer sk-ant-oat01-abc", FakeAnthropic.seen[0][2])
        FakeAnthropic.status = 401
        self.expect(self.login(4102444800000), "bad", ("another holder",))
        FakeAnthropic.seen = []
        self.expect(self.login(1), "unverified")
        self.assertEqual([], FakeAnthropic.seen)

    def test_what_is_not_a_login_is_refused_without_a_request(self):
        for value, why in (("sk-ant-oat01-abc", "not JSON"), ("{}", "no claudeAiOauth"),
                           (json.dumps({"claudeAiOauth": {"accessToken": "a", "expiresAt": 1}}), "no refreshToken"),
                           (json.dumps({"claudeAiOauth": {"accessToken": "wk-injects-this", "refreshToken": "wk-injects-this",
                                                          "expiresAt": 1}}), "placeholder")):
            with self.subTest(why=why):
                self.expect(self.check("claude-login", value, env=self.anthropic_env()), "bad", (why,))
        self.assertEqual([], FakeAnthropic.seen)


class TestTheTailnetKeys(_Rules):
    def test_each_key_is_judged_by_what_it_is(self):
        for name, value, verdict, ins in (
                ("tailnet", "tskey-auth-k1-abc", "ok", ("enroll a node and nothing else", "NOT ephemeral")),
                ("tailnet", "tskey-api-k1-abc", "bad", ("administers the whole tailnet",)),
                ("tailnet", "tskey-client-k1-abc", "bad", ("OAuth client secret",)),
                ("tailnet-api", "tskey-client-k1-abc", "bad", ("OAuth client secret",)),
                ("tailnet-api", "tskey-auth-k1-abc", "bad", ("enrolls a node",))):
            with self.subTest(name=name, value=value):
                self.expect(self.check(name, value), verdict, ins)

    def test_a_stored_api_token_is_put_to_the_tailnet(self):
        path = self.tmp / "api-key"
        path.write_text("tskey-api-k1-abc\n")
        self.expect(self.check("tailnet-api", path=path, env={"WK_TAILNET_API": "http://127.0.0.1:1"}), "unverified",
                    ("could not ask the tailnet",))


class TestADeployKey(_Rules):
    REPO_NAME = "wkuser/WebKit"

    def test_each_answer_github_gives_has_its_verdict(self):
        mine = "Hi %s! You've successfully authenticated" % self.REPO_NAME
        account = "Hi wkuser! You've successfully authenticated, but GitHub does not provide shell access."
        for ssh, read_only, verdict, why in (
                (mine, "false", "ok", "write access, and on no other"),
                (mine, "true", "bad", "READ-ONLY"), (mine, "true", "bad", "wk key deploy"),
                (mine, "", "unverified", "unconfirmed"),
                ("Permission denied (publickey).", "", "bad", "not registered on"),
                (account, "", "bad", "account key"),
                ("no key", "", "bad", "no key for")):
            with self.subTest(ssh=ssh[:20], read_only=read_only):
                self.expect(self.check("deploy-key", "", repos=self.REPO_NAME,
                                       evidence=["ssh=" + ssh, "read_only=" + read_only]), verdict, (why,))


class TestOneTableForEveryCredential(_Rules):
    def names(self):
        return subprocess.run(["python3", str(CREDCHECK), "names"], capture_output=True, text=True).stdout.split()

    def test_every_credential_wk_stores_has_a_rule(self):
        from wk import secrets
        held = ["github-pat", "bugzilla-api-key", "claude-login", "tailnet", "tailnet-api", "deploy-key"]
        for row in [r[0] for r in secrets.AGENT_SECRETS] + held:
            self.assertIn(row, self.names(), row)

    def rule(self, name):
        cp = subprocess.run(["python3", str(CREDCHECK), "rule", name], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return dict(l.split("\t", 1) for l in cp.stdout.splitlines())

    def test_every_rule_names_where_it_is_spent_what_to_ask_for_and_where_to_get_it(self):
        for name in self.names():
            fields = self.rule(name)
            with self.subTest(name=name):
                self.assertEqual({"needs", "forbids", "what", "url", "remedy", "store_with", "alone", "fix"}, set(fields))
                self.assertTrue(fields["what"].strip(), "%s: no `what`" % name)
                self.assertTrue(fields["remedy"].strip(), name)
                if fields["url"]:
                    self.assertTrue(fields["url"].startswith("https://"), fields["url"])
                    self.assertIn(fields["url"], fields["fix"])
                self.assertIn(fields["remedy"], fields["fix"])

    def test_the_token_page_is_the_one_that_mints_a_token_that_works(self):
        fields = self.rule("github-pat")
        url = fields["url"]
        self.assertTrue(url.startswith("https://github.com/settings/tokens/new?"), url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(["public_repo"], query["scopes"])
        self.assertIn("wk", query["description"][0])
        self.assertIn(str(credcheck.MAX_PAT_DAYS), fields["remedy"])
        self.assertIn("public_repo", fields["remedy"])

    def test_this_machine_knows_where_each_one_is_kept(self):
        from wk.key.cli import Key
        k = Key(REPO)
        for name in k.settable():
            with self.subTest(name=name):
                self.assertTrue((k.path(name) or "").startswith("/"), name)

    def test_nothing_stored_is_a_state_and_not_a_fault(self):
        self.expect(self.check("litellm", path=self.tmp / "absent"), "absent", ("wk key set litellm",))

    def test_an_unknown_name_is_refused_rather_than_admitted(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "check", "nosuchthing"], input="x", capture_output=True, text=True)
        self.assertEqual(2, cp.returncode)
        self.assertIn("no rule for", cp.stderr)


class TestTheModelsPiIsPointedAt(unittest.TestCase):
    def test_chat_models_are_kept_in_order_and_an_embedding_model_is_not(self):
        doc = {"data": [{"model_name": "gpt-oss-120b", "model_info": {"mode": None}},
                        {"model_name": "qwen3-embedding-8b", "model_info": {"mode": "embedding"}},
                        {"model_name": "glm-5p3-flash", "model_info": {"mode": "chat"}},
                        {"model_info": {"mode": "chat"}}]}
        self.assertEqual(["gpt-oss-120b", "glm-5p3-flash"], credcheck.chat_model_ids(doc))
        self.assertEqual([], credcheck.chat_model_ids({}))

    def test_the_first_model_a_completion_reaches_is_the_one_named(self):
        from unittest import mock
        asked = []

        def http(method, url, token, body=None, headers=()):
            model = json.loads(body)["model"]
            asked.append(model)
            return (404 if model == "listed-not-deployed" else 200), {}, b"{}"
        with mock.patch.object(credcheck, "_http", side_effect=http):
            self.assertEqual("m2", credcheck.litellm_callable("k", ["listed-not-deployed", "m2", "m3"]))
            self.assertIsNone(credcheck.litellm_callable("k", ["listed-not-deployed"]))
        self.assertEqual(["listed-not-deployed", "m2", "listed-not-deployed"], asked)


if __name__ == "__main__":
    unittest.main()
