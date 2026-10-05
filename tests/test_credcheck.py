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

FORKS = "wkuser/WebKit wkuser/WPEWebKit"
PROJECTS = {"wkuser/WebKit": "WebKit/WebKit",
            "wkuser/WPEWebKit": "WebPlatformForEmbedded/WPEWebKit"}
FINE = "github_pat_11ABCDEFG_notarealtoken"
CLASSIC = "ghp_notarealclassictoken0123456789"

# What WebKit/WebKit answered a never-expiring fine-grained token, 2026-09-15.
POLICY = ("The 'WebKit' organization forbids access via a fine-grained "
          "personal access tokens if the token's lifetime is greater than 366 "
          "days. Please adjust your token's lifetime at the following URL: "
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
    DEFAULTS = dict(user_status=200, scopes="", expiry="", pulls={}, repos=[],
                    repos_status=200, repos_answer=None, parents={},
                    repo_status={}, repo_message="", pulls_message={}, seen=[])

    @classmethod
    def reset(cls, **fields):
        for name, value in dict(cls.DEFAULTS, **fields).items():
            setattr(FakeGitHub, name, copy.deepcopy(value))

    def do_GET(self):
        FakeGitHub.seen.append(("GET", self.path,
                                self.headers.get("Authorization", "")))
        path, _, query = self.path.partition("?")
        if path == "/user/repos":
            if FakeGitHub.repos_status != 200:
                return self._send(FakeGitHub.repos_status,
                                  {"message": "Server Error"})
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
            headers.append(("github-authentication-token-expiration",
                            FakeGitHub.expiry))
        self._send(200, {"login": "wkuser"}, headers)

    def do_POST(self):
        FakeGitHub.seen.append(("POST", self.path,
                                self.headers.get("Authorization", "")))
        repo = self.path[len("/repos/"):-len("/pulls")]
        code = FakeGitHub.pulls.get(repo, 403)
        if code != 403:
            return self._send(code, {"message": "x"})
        self._send(code, {"message": FakeGitHub.pulls_message.get(
            repo, "Resource not accessible by personal access token")})


FakeGitHub.reset()


class _Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = serve(FakeGitHub, cls.addClassCleanup)

    def setUp(self):
        FakeGitHub.reset(
            pulls=dict.fromkeys(FORKS.split() + list(PROJECTS.values()), 422),
            repos=FORKS.split(), parents=dict(PROJECTS), repo_message=POLICY)
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-credcheck-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))

    def check(self, name, value=None, repos=FORKS, path=None, evidence=(),
              api=None, env=None):
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
        cp = subprocess.run(args, input="" if value is None else value,
                            capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        verdict, _, detail = cp.stdout.partition("\t")
        return verdict, detail


BZ_LOGIN = "me@example.test"
BZ_KEY = "notarealbugzillakey0123456789abcdefghijk"


class FakeBugzilla(JsonHandler):
    """bugs.webkit.org's valid_login, measured 2026-09-14: true for the key's own login, false for another, 400/306
    for a key it does not know."""

    seen = []

    def do_GET(self):
        FakeBugzilla.seen.append(self.path)
        path, _, query = self.path.partition("?")
        q = urllib.parse.parse_qs(query)
        if path != "/rest/valid_login":
            return self._send(404, {"error": True, "code": 32614})
        if q.get("api_key", [""])[0] != BZ_KEY:
            return self._send(400, {"error": True, "code": 306,
                                    "message": "The API key you specified is invalid."})
        self._send(200, {"result": q.get("login", [""])[0] == BZ_LOGIN})


class _Bugzilla(_Rules):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bz_base = serve(FakeBugzilla, cls.addClassCleanup)

    def setUp(self):
        super().setUp()
        FakeBugzilla.seen = []

    def bz_check(self, value, login=BZ_LOGIN, api=None):
        return self.check("bugzilla-api-key", value,
                          evidence=("login=%s" % login,) if login else (),
                          env={"WK_BUGZILLA_API": api or self.bz_base})


class TestTheBugzillaKey(_Bugzilla):
    def test_the_keys_own_login_is_ok_and_named(self):
        verdict, detail = self.bz_check(BZ_KEY)
        self.assertEqual("ok", verdict, detail)
        self.assertIn(BZ_LOGIN, detail)
        self.assertIn("while push is on", detail)

    def test_it_is_judged_as_a_pair_by_one_request(self):
        self.bz_check(BZ_KEY)
        self.assertEqual(1, len(FakeBugzilla.seen), FakeBugzilla.seen)
        self.assertTrue(FakeBugzilla.seen[0].startswith("/rest/valid_login?"))
        self.assertIn("api_key=" + BZ_KEY, FakeBugzilla.seen[0])
        self.assertIn("login=me%40example.test", FakeBugzilla.seen[0])

    def test_another_accounts_key_is_refused_by_name(self):
        verdict, detail = self.bz_check(BZ_KEY, login="other@example.test")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("another account", detail)
        self.assertIn("other@example.test", detail)

    def test_a_key_bugzilla_does_not_know_is_refused(self):
        verdict, detail = self.bz_check("notthekey")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("306", detail)
        self.assertIn("wk key set bugzilla-api-key", detail)

    def test_with_no_login_to_judge_against_it_is_unverified_and_names_the_mirror(self):
        verdict, detail = self.bz_check(BZ_KEY, login="")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("contributors.json", detail)
        self.assertIn("wk sync", detail)
        self.assertEqual([], FakeBugzilla.seen, "nothing to ask without a login")

    def test_two_words_are_not_a_key(self):
        verdict, _ = self.bz_check("two words")
        self.assertEqual("bad", verdict)
        self.assertEqual([], FakeBugzilla.seen)

    def test_an_unreachable_bugzilla_is_unverified(self):
        verdict, detail = self.bz_check(BZ_KEY, api="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not reach", detail)

class TestTheTokenCanDoTheJob(_Rules):
    def test_a_fine_grained_token_that_can_open_a_pull_request_on_both_forks(self):
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("fine-grained", detail)
        for repo in FORKS.split() + list(PROJECTS.values()):
            self.assertIn("can open a pull request on %s" % repo, detail)

    def test_the_identity_and_expiry_github_reports_are_named(self):
        FakeGitHub.expiry = "2026-12-01 00:00:00 UTC"
        _v, detail = self.check("github-pat", FINE)
        self.assertIn("as wkuser", detail)
        self.assertIn("expires 2026-12-01", detail)

    def test_no_pull_request_permission_on_one_fork_is_refused_by_name(self):
        FakeGitHub.pulls["wkuser/WPEWebKit"] = 403
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("wkuser/WPEWebKit", detail)
        self.assertIn("Pull requests: write", detail)
        self.assertIn("settings/tokens/new", detail)
        self.assertIn("wk key set github-pat", detail)

    def test_a_fork_the_token_cannot_see_is_refused_by_name(self):
        FakeGitHub.pulls["wkuser/WebKit"] = 404
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("cannot see wkuser/WebKit", detail)

    def test_a_token_github_no_longer_accepts_is_refused(self):
        FakeGitHub.user_status = 401
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("does not accept this token (HTTP 401)", detail)


class TestTheProjectRefusesIt(_Rules):
    """Measured 2026-09-15: a fine-grained token reads WebKit/WebKit and is refused POST /pulls there."""

    def test_a_fine_grained_token_is_refused_with_the_reason_it_cannot_be_fixed(self):
        FakeGitHub.pulls["WebKit/WebKit"] = 403
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("WebKit/WebKit refuses this token a pull request", detail)
        self.assertIn("Resource not accessible by personal access token", detail)
        self.assertIn("reaches only repositories owned by the account", detail)
        self.assertIn("A classic one is what can", detail)
        self.assertIn("settings/tokens/new", detail)
        self.assertIn("scopes=public_repo", detail)

    def test_the_organizations_lifetime_policy_is_refused_in_its_own_words(self):
        FakeGitHub.pulls["WebKit/WebKit"] = 403
        FakeGitHub.pulls_message = {"WebKit/WebKit": POLICY}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("366 days", detail)
        self.assertIn("personal-access-tokens/19512093", detail)
        self.assertIn("wk key set github-pat", detail)

    def test_a_classic_token_is_asked_the_same_question(self):
        FakeGitHub.scopes = "repo"
        FakeGitHub.pulls["WebKit/WebKit"] = 403
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("WebKit/WebKit", detail)
        self.assertNotIn("reaches only repositories owned by the account", detail)

    def test_a_fork_of_nothing_has_no_project_to_ask(self):
        FakeGitHub.parents = {}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertEqual(["/repos/%s/pulls" % r for r in FORKS.split()],
                         [p[1] for p in FakeGitHub.seen if p[0] == "POST"])

    def test_one_project_is_asked_once_however_many_forks_name_it(self):
        FakeGitHub.parents = dict.fromkeys(PROJECTS, "WebKit/WebKit")
        self.check("github-pat", FINE)
        self.assertEqual(1, [p[1] for p in FakeGitHub.seen
                             if p[0] == "POST"].count("/repos/WebKit/WebKit/pulls"))

    def test_a_fork_that_could_not_be_read_is_unverified_not_claimed(self):
        FakeGitHub.repo_status = {"wkuser/WebKit": 500}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("which project each fork belongs to", detail)

    def test_a_project_that_answered_neither_422_nor_403_is_unverified(self):
        FakeGitHub.pulls["WebKit/WebKit"] = 500
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("rather than 422", detail)

    def test_the_project_is_asked_before_the_account_is_enumerated(self):
        FakeGitHub.pulls["WebKit/WebKit"] = 403
        self.check("github-pat", FINE)
        self.assertEqual([], [p for p in FakeGitHub.seen
                              if p[1].startswith("/user/repos")])


class TestTheTokenIsNotWiderThanTheJob(_Rules):
    def test_a_classic_token_is_kept_with_its_reach_named(self):
        FakeGitHub.scopes = "repo, read:org"
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("wide", verdict, detail)
        self.assertIn("every repository this account can write", detail)
        self.assertIn("repo, read:org", detail)

    def test_a_token_that_can_delete_a_repository_or_administer_an_org_is_refused_unprobed(self):
        for scope in ("delete_repo", "admin:org"):
            with self.subTest(scope=scope):
                FakeGitHub.seen = []
                FakeGitHub.scopes = "repo, " + scope
                verdict, detail = self.check("github-pat", CLASSIC)
                self.assertEqual("bad", verdict, detail)
                self.assertIn(scope, detail)
                self.assertEqual([], [p for p in FakeGitHub.seen if p[0] == "POST"])


class TestTheTokenReachesTheForksAndNothingElse(_Rules):
    """No endpoint enumerates what a token was granted, so each listed repository is probed like the forks."""

    OTHERS = ["wkuser/other%d" % i for i in range(44)]

    def test_exactly_the_forks_is_what_ok_means(self):
        FakeGitHub.repos = FORKS.split() + self.OTHERS
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("on the 2 forks and on none of the 44 other", detail)

    def test_a_token_on_every_repository_is_refused_with_the_count(self):
        FakeGitHub.repos = FORKS.split() + self.OTHERS
        FakeGitHub.pulls.update(dict.fromkeys(self.OTHERS, 422))
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("44 repositories beyond the 2 forks", detail)
        self.assertIn("tick nothing but the 'public_repo'", detail)
        for repo in FORKS.split():
            self.assertIn(repo, detail)

    def test_the_list_is_read_to_its_last_page(self):
        others = ["wkuser/r%03d" % i for i in range(150)]
        FakeGitHub.repos = FORKS.split() + others
        FakeGitHub.pulls[others[-1]] = 422
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("1 repositories beyond the 2 forks", detail)

    def test_a_classic_token_is_not_asked_which_repositories_it_reaches(self):
        FakeGitHub.scopes = "repo"
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("wide", verdict, detail)
        self.assertEqual([], [p for p in FakeGitHub.seen
                              if p[1].startswith("/user/repos")])

    def test_a_list_that_could_not_be_read_is_unverified_not_claimed(self):
        for setup in ({"repos_status": 500},
                      {"repos_answer": {"message": "not a list"}}):
            with self.subTest(**setup):
                FakeGitHub.repos_status = 200
                FakeGitHub.repos_answer = None
                for k, v in setup.items():
                    setattr(FakeGitHub, k, v)
                verdict, detail = self.check("github-pat", FINE)
                self.assertEqual("unverified", verdict, detail)
                self.assertIn("which repositories this token reaches", detail)


class TestAMalformedToken(_Rules):
    def test_something_that_is_not_a_github_token(self):
        verdict, detail = self.check("github-pat", "hunter2")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("does not start like a GitHub personal access token", detail)

    def test_an_app_or_oauth_token_is_not_a_personal_access_token(self):
        for token in ("gho_abc", "ghs_abc", "ghu_abc", "ghr_abc"):
            with self.subTest(token=token):
                verdict, detail = self.check("github-pat", token)
                self.assertEqual("bad", verdict, detail)
                self.assertIn("not a personal access token", detail)

    def test_nothing_at_all_asks_nothing_of_github(self):
        verdict, detail = self.check("github-pat", "")
        self.assertEqual("bad", verdict, detail)
        self.assertEqual([], FakeGitHub.seen)


class TestAnUnreachableApi(_Rules):
    def test_offline_is_a_state_and_the_credential_is_still_usable_here(self):
        verdict, detail = self.check("github-pat", FINE, api="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not reach", detail)
        self.assertIn("wk doctor", detail)


class TestWhereTheseApisMayBePointed(_Rules):
    def _run(self, **env):
        e = dict(os.environ)
        e.update(env)
        return subprocess.run(
            ["python3", str(CREDCHECK), "names"],
            capture_output=True, text=True, env=e, timeout=60)

    def test_an_http_host_that_is_not_loopback_is_refused_by_name(self):
        for var in ("WK_GITHUB_API", "WK_ANTHROPIC_API", "WK_BUGZILLA_API"):
            with self.subTest(var=var):
                cp = self._run(**{var: "http://evil.example/"})
                self.assertNotEqual(0, cp.returncode, cp.stdout)
                self.assertIn(var, cp.stderr)
                self.assertIn("Authorization", cp.stderr)

    def test_https_and_loopback_are_both_accepted(self):
        for value in ("https://api.example.com", "http://127.0.0.1:1",
                      "http://localhost:8080"):
            with self.subTest(value=value):
                cp = self._run(WK_GITHUB_API=value, WK_ANTHROPIC_API=value,
                               WK_BUGZILLA_API=value)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)


class FakeAnthropic(JsonHandler):
    """api.anthropic.com, measured 2026-09-10: GET /v1/models is 200 for an accepted token, 401 otherwise."""

    status = 200
    seen = []

    def do_GET(self):
        FakeAnthropic.seen.append(
            (self.command, self.path, self.headers.get("Authorization", ""),
             self.headers.get("anthropic-version", "")))
        if FakeAnthropic.status != 200:
            return self._send(FakeAnthropic.status,
                              {"type": "error",
                               "error": {"type": "authentication_error"}})
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
        base = api if api is not None else self.anthropic_base
        return {"WK_ANTHROPIC_API": base}


class FakeLiteLLM(JsonHandler):
    """ai.igalia.com, measured 2026-09-14: /v1/models lists what a key may call (401 for one it does not accept), and
    /key/info is 403 for a key restricted to the LLM API routes."""

    models_status = 200
    info_status = 403

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            status = FakeLiteLLM.models_status
            body = ({"data": [{"id": "m1"}, {"id": "m2"}]} if status == 200
                    else {"error": {"message": "Authentication Error"}})
        elif self.path.startswith("/key/info"):
            status = FakeLiteLLM.info_status
            body = ({"info": {"key_alias": "wk", "expires": None,
                              "max_budget": 10}} if status == 200
                    else {"detail": "Virtual key is not allowed to call this route."})
        else:
            status, body = 404, {"detail": "Not Found"}
        self._send(status, body)


class TestTheAgentKeys(_Anthropic):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.litellm_base = serve(FakeLiteLLM, cls.addClassCleanup)

    def setUp(self):
        super().setUp()
        FakeLiteLLM.models_status = 200
        FakeLiteLLM.info_status = 403

    def claude(self, value, api=None):
        return self.check("claude", value, env=self.anthropic_env(api))

    def litellm_key(self, value, api=None):
        return self.check("litellm", value, env={
            "WK_LITELLM_API": api if api is not None else self.litellm_base})

    def test_a_setup_token_is_accepted_and_its_narrowness_named(self):
        verdict, detail = self.claude("sk-ant-oat01-abc")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("inference-only", detail)

    def test_whether_anthropic_still_accepts_it_is_asked_and_not_assumed(self):
        _v, detail = self.claude("sk-ant-oat01-abc")
        self.assertIn("Anthropic accepts it", detail)
        self.assertEqual(1, len(FakeAnthropic.seen), FakeAnthropic.seen)
        method, path, auth, version = FakeAnthropic.seen[0]
        self.assertEqual("GET", method)
        self.assertTrue(path.startswith("/v1/models"), path)
        self.assertEqual("Bearer sk-ant-oat01-abc", auth)
        self.assertEqual("2023-06-01", version)

    def test_a_token_anthropic_no_longer_accepts_is_refused(self):
        FakeAnthropic.status = 401
        verdict, detail = self.claude("sk-ant-oat01-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("spent, revoked or expired", detail)
        self.assertIn("wk key set claude --replace", detail)

    def test_offline_leaves_the_token_usable_and_says_it_was_not_established(self):
        verdict, detail = self.claude("sk-ant-oat01-abc",
                                      api="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not reach", detail)

    def test_a_token_of_another_shape_is_refused_without_a_request(self):
        for value, why in (("sk-ant-api03-abc", "bills the organization"), ("hunter2", "sk-ant-oat"),
                           (json.dumps({"claudeAiOauth": {}}), "")):
            with self.subTest(value=value[:12]):
                verdict, detail = self.claude(value)
                self.assertEqual("bad", verdict, detail)
                self.assertIn(why, detail)
        self.assertEqual([], FakeAnthropic.seen)

    def test_a_litellm_key_the_endpoint_accepts_and_restricts_is_ok(self):
        verdict, detail = self.litellm_key("sk-abc123")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("serves it 2 model(s)", detail)
        self.assertIn("restricted to the LLM API routes", detail)
        self.assertIn("models.json", detail)
        self.assertIn(self.litellm_base + "/v1", detail)

    def test_a_litellm_key_the_endpoint_refuses_is_refused_here(self):
        FakeLiteLLM.models_status = 401
        verdict, detail = self.litellm_key("sk-abc123")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("does not accept this key", detail)
        self.assertIn("wk key set litellm", detail)

    def test_a_litellm_key_that_reaches_the_management_routes_is_wide(self):
        FakeLiteLLM.info_status = 200
        verdict, detail = self.litellm_key("sk-abc123")
        self.assertEqual("wide", verdict, detail)
        self.assertIn("key-management routes", detail)
        self.assertIn("alias wk", detail)

    def test_a_litellm_endpoint_out_of_reach_leaves_the_key_unverified(self):
        verdict, detail = self.litellm_key("sk-abc123", api="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not reach", detail)

    def test_the_upstream_anthropic_key_is_refused_where_a_virtual_one_belongs(self):
        verdict, detail = self.check("litellm", "sk-ant-api03-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("upstream account", detail)

    def test_nothing_is_refused(self):
        verdict, _d = self.check("litellm", "")
        self.assertEqual("bad", verdict)


class TestTheTailnetKeys(_Rules):
    def test_an_auth_key_is_accepted_and_what_it_cannot_prove_is_said(self):
        verdict, detail = self.check("tailnet", "tskey-auth-k1-abc")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("enroll a node and nothing else", detail)
        self.assertIn("NOT ephemeral", detail)

    def test_a_key_of_the_wrong_kind_is_refused_by_what_it_is(self):
        for name, value, why in (("tailnet", "tskey-api-k1-abc", "administers the whole tailnet"),
                                 ("tailnet", "tskey-client-k1-abc", "OAuth client secret"),
                                 ("tailnet-api", "tskey-client-k1-abc", "OAuth client secret"),
                                 ("tailnet-api", "tskey-auth-k1-abc", "enrolls a node")):
            with self.subTest(name=name, value=value):
                verdict, detail = self.check(name, value)
                self.assertEqual("bad", verdict, detail)
                self.assertIn(why, detail)

    def test_a_stored_api_token_is_put_to_the_tailnet(self):
        path = self.tmp / "api-key"
        path.write_text("tskey-api-k1-abc\n")
        verdict, detail = self.check("tailnet-api", path=path,
                                     env={"WK_TAILNET_API": "http://127.0.0.1:1"})
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not ask the tailnet", detail)


class TestADeployKey(_Rules):
    REPO_NAME = "wkuser/WebKit"

    def key(self, ssh, read_only):
        return self.check("deploy-key", "", repos=self.REPO_NAME,
                          evidence=["ssh=" + ssh, "read_only=" + read_only])

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
                got, detail = self.check("deploy-key", "", repos=self.REPO_NAME,
                                         evidence=["ssh=" + ssh, "read_only=" + read_only])
                self.assertEqual(verdict, got, detail)
                self.assertIn(why, detail)


class TestOneTableForEveryCredential(_Rules):
    def names(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "names"],
                            capture_output=True, text=True)
        return cp.stdout.split()

    def test_every_credential_wk_stores_has_a_rule(self):
        from wk import secrets
        held = ["github-pat", "bugzilla-api-key", "tailnet", "tailnet-api", "deploy-key"]
        for row in [r[0] for r in secrets.AGENT_SECRETS if r[4] == "value"] + held:
            self.assertIn(row, self.names(), row)

    def rule(self, name):
        cp = subprocess.run(["python3", str(CREDCHECK), "rule", name], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return dict(l.split("\t", 1) for l in cp.stdout.splitlines())

    def test_every_rule_names_where_it_is_spent_what_to_ask_for_and_where_to_get_it(self):
        for name in self.names():
            fields = self.rule(name)
            with self.subTest(name=name):
                self.assertEqual({"needs", "forbids", "what", "url",
                                  "remedy", "store_with", "fix"}, set(fields), name)
                self.assertTrue(fields["what"].strip(), "%s: no `what`" % name)
                self.assertTrue(fields["remedy"].strip(), name)
                if fields["url"]:
                    self.assertTrue(fields["url"].startswith("https://"),
                                    fields["url"])
                    self.assertIn(fields["url"], fields["fix"])
                self.assertIn(fields["remedy"], fields["fix"])

    def test_the_token_page_is_the_one_that_mints_a_token_that_works(self):
        fields = self.rule("github-pat")
        url = fields["url"]
        self.assertTrue(url.startswith(
            "https://github.com/settings/tokens/new?"), url)
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
        verdict, detail = self.check("litellm", path=self.tmp / "absent")
        self.assertEqual("absent", verdict, detail)
        self.assertIn("wk key set litellm", detail)

    def test_an_unknown_name_is_refused_rather_than_admitted(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "check", "nosuchthing"],
                            input="x", capture_output=True, text=True)
        self.assertEqual(2, cp.returncode)
        self.assertIn("no rule for", cp.stderr)


class TestTheModelsPiIsPointedAt(unittest.TestCase):
    def test_chat_models_are_kept_in_order_and_an_embedding_model_is_not(self):
        doc = {"data": [{"model_name": "gpt-oss-120b", "model_info": {"mode": None}},
                        {"model_name": "qwen3-embedding-8b", "model_info": {"mode": "embedding"}},
                        {"model_name": "glm-5p3-flash", "model_info": {"mode": "chat"}},
                        {"model_info": {"mode": "chat"}}]}
        self.assertEqual(["gpt-oss-120b", "glm-5p3-flash"], credcheck.chat_model_ids(doc))

    def test_an_answer_with_no_rows_names_no_model(self):
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
