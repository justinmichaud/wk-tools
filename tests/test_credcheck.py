"""Every credential's rule (lib/credcheck.py): what it must be able to do, and
what it must not.

Both halves are checked here, because both have cost a working day. A token
that cannot do its job is discovered hours later at the one moment it is needed
-- `git-webkit pr` answered 403 -- and a token that can do far more than its job
turns any escape from the boundary into the blast radius of a whole account.

GitHub is a local HTTP server, the way tests/test_tailnet_retire.py stubs the
tailnet: the real request-making code runs, the headers and the write-shaped
probe included, and nothing leaves the machine. The one constant the module
reads for its base URL is the seam.

Run: python3 -m unittest tests.test_credcheck -v
"""
import json
import os
import sys
import urllib.parse
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CREDCHECK = REPO / "lib" / "credcheck.py"

sys.path.insert(0, str(REPO / "lib"))
import credcheck   # noqa: E402 -- the constants a verdict names are read from it
STORE_SH = (REPO / "lib" / "store.sh").read_text()

FORKS = "wkuser/WebKit wkuser/WPEWebKit"
FINE = "github_pat_11ABCDEFG_notarealtoken"
CLASSIC = "ghp_notarealclassictoken0123456789"


class FakeGitHub(BaseHTTPRequestHandler):
    """`GET /user` answers the token's identity, its classic scope list and its
    expiry; `GET /user/repos` the account's repositories, one page at a time,
    whatever the token was granted; `POST /repos/<r>/pulls` whether the token
    can open a pull request there, 403 unless `pulls` says otherwise. All three
    are what GitHub itself answers."""

    user_status = 200
    scopes = ""
    expiry = ""
    pulls = {}
    repos = []
    repos_status = 200
    repos_answer = None
    seen = []

    def _send(self, code, body, headers=()):
        raw = json.dumps(body).encode()
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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
        self._send(code, {"message": "Resource not accessible by personal "
                                     "access token" if code == 403 else "x"})

    def log_message(self, *a):
        pass


class _Rules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeGitHub.user_status = 200
        FakeGitHub.scopes = ""
        FakeGitHub.expiry = ""
        FakeGitHub.pulls = dict.fromkeys(FORKS.split(), 422)
        FakeGitHub.repos = FORKS.split()
        FakeGitHub.repos_status = 200
        FakeGitHub.repos_answer = None
        FakeGitHub.seen = []
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-credcheck-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))

    def check(self, name, value=None, repos=FORKS, path=None, evidence=(),
              api=None, env=None):
        args = ["python3", str(CREDCHECK), "check", name, "--repos", repos]
        if path is not None:
            args += ["--path", str(path)]
            # What wk_cred_check does: the value always arrives on stdin, read
            # the one way; the path is context for the rules that need it.
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

    def assert_verdict(self, want, got, detail):
        self.assertEqual(want, got, detail)


# --- github-pat ---------------------------------------------------------------
class TestTheTokenCanDoTheJob(_Rules):
    def test_a_fine_grained_token_that_can_open_a_pull_request_on_both_forks(self):
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("fine-grained", detail)
        for repo in FORKS.split():
            self.assertIn("can open a pull request on %s" % repo, detail)

    def test_the_account_repositories_are_listed_once_per_page(self):
        """One GET per page, and the identity call before it."""
        self.check("github-pat", FINE)
        self.assertEqual(["/user", "/user/repos?per_page=100&page=1"],
                         [p[1] for p in FakeGitHub.seen if p[0] == "GET"])

    def test_the_probe_is_a_write_that_creates_nothing(self):
        """An empty body names no head or base branch, so an authorised call is
        422 -- the same probe `wk verify` runs from inside a workspace."""
        self.check("github-pat", FINE)
        posts = [p for p in FakeGitHub.seen if p[0] == "POST"]
        self.assertEqual(["/repos/%s/pulls" % r for r in FORKS.split()],
                         [p[1] for p in posts])
        for _m, _p, auth in posts:
            self.assertTrue(auth.startswith("Bearer "), auth[:12])

    def test_the_identity_and_expiry_github_reports_are_named(self):
        FakeGitHub.expiry = "2026-12-01 00:00:00 UTC"
        _v, detail = self.check("github-pat", FINE)
        self.assertIn("as wkuser", detail)
        self.assertIn("expires 2026-12-01", detail)

    def test_no_pull_request_permission_on_one_fork_is_refused_by_name(self):
        """The failure measured 2026-09-04: the token is spent, GitHub answers
        403, and `git-webkit pr` cannot open a pull request."""
        FakeGitHub.pulls["wkuser/WPEWebKit"] = 403
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("wkuser/WPEWebKit", detail)
        self.assertIn("Pull requests: write", detail)
        self.assertIn("personal-access-tokens/new", detail)
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


class TestTheTokenIsNotWiderThanTheJob(_Rules):
    def test_a_classic_token_is_kept_with_its_reach_named(self):
        """It can do the job, and it is the token a person most likely already
        has working, so the wider reach is reported rather than refused."""
        FakeGitHub.scopes = "repo, read:org"
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("wide", verdict, detail)
        self.assertIn("every repository this account can write", detail)
        self.assertIn("repo, read:org", detail)

    def test_a_token_that_can_delete_a_repository_is_refused(self):
        FakeGitHub.scopes = "repo, delete_repo"
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("delete_repo", detail)

    def test_a_token_that_can_administer_an_organization_is_refused(self):
        FakeGitHub.scopes = "repo, admin:org"
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("admin:org", detail)

    def test_a_refused_scope_costs_no_write_probe(self):
        """The cheap local answer comes first: nothing is spent establishing
        what the scope list already settled."""
        FakeGitHub.scopes = "repo, delete_repo"
        self.check("github-pat", CLASSIC)
        self.assertEqual([], [p for p in FakeGitHub.seen if p[0] == "POST"])


class TestTheTokenReachesTheForksAndNothingElse(_Rules):
    """GitHub's fine-grained token form takes no repository parameter, so a
    token minted from the link arrives on *All repositories* unless the person
    changes that field -- and a token on all of them is the whole account's
    reach behind one workspace boundary. No endpoint enumerates what a token
    was granted, and `GET /user/repos` answers for the account (every public
    repository it has, whatever the token reaches), so each listed repository
    is asked the same write-shaped question as the forks."""

    OTHERS = ["wkuser/other%d" % i for i in range(44)]

    def test_exactly_the_forks_is_what_ok_means(self):
        FakeGitHub.repos = FORKS.split() + self.OTHERS
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("on the 2 forks and on none of the 44 other", detail)

    def test_every_listed_repository_is_probed_not_counted(self):
        """The measurement of 2026-09-11: a token granted exactly the two forks
        is listed 48 repositories with the account's `push: true` on each, and
        answers 422 on the forks alone."""
        FakeGitHub.repos = FORKS.split() + self.OTHERS
        self.check("github-pat", FINE)
        self.assertEqual(["/repos/%s/pulls" % r
                          for r in FORKS.split() + self.OTHERS],
                         [p[1] for p in FakeGitHub.seen if p[0] == "POST"])

    def test_a_token_on_every_repository_is_refused_with_the_count(self):
        FakeGitHub.repos = FORKS.split() + self.OTHERS
        FakeGitHub.pulls.update(dict.fromkeys(self.OTHERS, 422))
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("44 repositories beyond the 2 forks", detail)
        self.assertIn("Only select repositories", detail)
        for repo in FORKS.split():
            self.assertIn(repo, detail)

    def test_the_list_is_read_to_its_last_page(self):
        """100 per page: a token on a busy account whose second page held the
        repositories it reaches would otherwise pass."""
        others = ["wkuser/r%03d" % i for i in range(150)]
        FakeGitHub.repos = FORKS.split() + others
        FakeGitHub.pulls[others[-1]] = 422
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("1 repositories beyond the 2 forks", detail)
        self.assertEqual(["/user/repos?per_page=100&page=1",
                          "/user/repos?per_page=100&page=2"],
                         [p[1] for p in FakeGitHub.seen
                          if p[1].startswith("/user/repos")])

    def test_a_classic_token_is_not_asked_which_repositories_it_reaches(self):
        """Its scope list already answers: `repo` reaches every repository the
        account can write, which is the reach the verdict names."""
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

    def test_nothing_at_all(self):
        verdict, detail = self.check("github-pat", "")
        self.assertEqual("bad", verdict, detail)

    def test_nothing_was_asked_of_github(self):
        self.check("github-pat", "hunter2")
        self.assertEqual([], FakeGitHub.seen)


class TestAnUnreachableApi(_Rules):
    def test_offline_is_a_state_and_the_credential_is_still_usable_here(self):
        """Refusing to store a credential because the machine is offline would
        leave the person with nothing; the verdict says it was not established
        and every reader asks again."""
        verdict, detail = self.check("github-pat", FINE, api="http://127.0.0.1:1")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not reach", detail)
        self.assertIn("wk doctor", detail)


class TestWhereTheseApisMayBePointed(_Rules):
    """Both bases come from the environment so a test can stand a stub up on
    loopback, and a credential goes to whatever they name in an Authorization
    header. So: https, or loopback, or a refusal that names the variable."""

    def _run(self, **env):
        e = dict(os.environ)
        e.update(env)
        return subprocess.run(
            ["python3", str(CREDCHECK), "names"],
            capture_output=True, text=True, env=e, timeout=60)

    def test_an_http_host_that_is_not_loopback_is_refused_by_name(self):
        for var in ("WK_GITHUB_API", "WK_ANTHROPIC_API"):
            with self.subTest(var=var):
                cp = self._run(**{var: "http://evil.example/"})
                self.assertNotEqual(0, cp.returncode, cp.stdout)
                self.assertIn(var, cp.stderr)
                self.assertIn("Authorization", cp.stderr)

    def test_https_and_loopback_are_both_accepted(self):
        for value in ("https://api.example.com", "http://127.0.0.1:1",
                      "http://localhost:8080"):
            with self.subTest(value=value):
                cp = self._run(WK_GITHUB_API=value, WK_ANTHROPIC_API=value)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)


# --- the claude.ai login -------------------------------------------------------
def login(scopes=("user:profile", "user:inference"), refresh="r" * 20,
          refresh_expires_days=29, subscription="team"):
    doc = {"accessToken": "a" * 20, "scopes": list(scopes),
           "expiresAt": int((time.time() + 3600) * 1000),
           "subscriptionType": subscription}
    if refresh:
        doc["refreshToken"] = refresh
    if refresh_expires_days is not None:
        doc["refreshTokenExpiresAt"] = int(
            (time.time() + refresh_expires_days * 86400) * 1000)
    return json.dumps({"claudeAiOauth": doc})


class TestTheClaudeLogin(_Rules):
    def test_a_login_that_carries_what_an_agent_spends(self):
        verdict, detail = self.check("claude-login", login())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("user:profile", detail)
        self.assertIn("subscription: team", detail)
        self.assertIn("renewable until", detail)

    def _stored(self, record):
        """A login as the store holds it: the credential file, and beside it
        the CLI's config file with (or without) the account record."""
        d = self.tmp / "agent-rw"
        d.mkdir(exist_ok=True)
        (d / ".credentials.json").write_text(login())
        if record is not None:
            (d / ".claude.json").write_text(json.dumps(record))
        return d / ".credentials.json"

    def test_a_stored_login_is_judged_with_the_record_beside_it(self):
        """Measured 2026-09-11: remote control reads organizationUuid from the
        CLI's config file and refuses without it, so a stored login is judged
        by the record `claude auth login` left beside the credential."""
        path = self._stored({"oauthAccount": {"organizationUuid": "org-1",
                                              "organizationName": "Example"}})
        verdict, detail = self.check("claude-login", login(), path=path)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("organization: Example", detail)

    def test_a_stored_login_with_no_record_is_refused_and_names_the_rotation(self):
        for record in (None, {}, {"oauthAccount": {"emailAddress": "x"}}):
            with self.subTest(record=record):
                path = self._stored(record)
                verdict, detail = self.check("claude-login", login(), path=path)
                self.assertEqual("bad", verdict, detail)
                self.assertIn("no account record", detail)
                self.assertIn("organizationUuid", detail)
                self.assertIn("wk key set claude-login --replace", detail)

    def test_a_login_on_stdin_alone_is_not_asked_for_a_record(self):
        """Before anything is stored there is no directory to look beside."""
        verdict, detail = self.check("claude-login", login())
        self.assertEqual("ok", verdict, detail)
        self.assertNotIn("organization:", detail)

    def test_a_login_without_the_profile_scope_is_refused(self):
        verdict, detail = self.check("claude-login",
                                     login(scopes=("user:inference",)))
        self.assertEqual("bad", verdict, detail)
        self.assertIn("user:profile", detail)
        self.assertIn("claude auth login", detail)

    def test_a_login_that_cannot_run_inference_is_refused(self):
        verdict, detail = self.check("claude-login",
                                     login(scopes=("user:profile",)))
        self.assertEqual("bad", verdict, detail)
        self.assertIn("user:inference", detail)

    def test_a_setup_token_document_is_not_a_login(self):
        verdict, detail = self.check("claude-login", login(refresh=""))
        self.assertEqual("bad", verdict, detail)
        self.assertIn("setup-token", detail)

    def test_a_login_whose_refresh_token_has_expired_is_refused(self):
        verdict, detail = self.check("claude-login",
                                     login(refresh_expires_days=-1))
        self.assertEqual("bad", verdict, detail)
        self.assertIn("cannot be renewed", detail)

    def test_a_malformed_document(self):
        for value, want in (("not json at all", "not JSON"),
                            ('{"nope": 1}', "no claudeAiOauth")):
            with self.subTest(value=value):
                verdict, detail = self.check("claude-login", value)
                self.assertEqual("bad", verdict, detail)
                self.assertIn(want, detail)


# --- the two pasted keys -------------------------------------------------------
class FakeAnthropic(BaseHTTPRequestHandler):
    """`GET /v1/models` is the read-only request that answers whether Anthropic
    still accepts a token: 200 when it does, 401 when it does not, and no model
    is inferred either way (measured against api.anthropic.com, 2026-09-10)."""

    status = 200
    seen = []

    def do_GET(self):
        FakeAnthropic.seen.append(
            (self.command, self.path, self.headers.get("Authorization", ""),
             self.headers.get("anthropic-version", "")))
        body = ({"data": [{"id": "claude-x"}]} if FakeAnthropic.status == 200
                else {"type": "error",
                      "error": {"type": "authentication_error"}})
        raw = json.dumps(body).encode()
        self.send_response(FakeAnthropic.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_POST = do_GET

    def log_message(self, *a):
        pass


class TestTheAgentKeys(_Rules):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.anthropic = HTTPServer(("127.0.0.1", 0), FakeAnthropic)
        cls.anthropic_base = "http://127.0.0.1:%d" % cls.anthropic.server_port
        cls.anthropic_thread = threading.Thread(
            target=cls.anthropic.serve_forever, daemon=True)
        cls.anthropic_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.anthropic.shutdown()
        cls.anthropic.server_close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        FakeAnthropic.status = 200
        FakeAnthropic.seen = []

    def claude(self, value, api=None):
        return self.check("claude", value, env={
            "WK_ANTHROPIC_API": api if api is not None else self.anthropic_base})

    def test_a_setup_token_is_accepted_and_its_narrowness_named(self):
        verdict, detail = self.claude("sk-ant-oat01-abc")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("inference-only", detail)

    def test_whether_anthropic_still_accepts_it_is_asked_and_not_assumed(self):
        """The one request a workspace cannot make for itself before it is
        handed the token: a stale token is otherwise discovered as a /login
        prompt inside a workspace, hours later."""
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

    def test_a_token_refused_by_shape_costs_no_request(self):
        for value in ("hunter2", "sk-ant-api03-abc", login()):
            with self.subTest(value=value[:12]):
                self.claude(value)
        self.assertEqual([], FakeAnthropic.seen)

    def test_a_console_api_key_is_refused_as_wider_than_the_job(self):
        verdict, detail = self.claude("sk-ant-api03-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("bills the organization", detail)

    def test_a_login_document_pasted_here_names_the_row_that_takes_one(self):
        verdict, detail = self.claude(login())
        self.assertEqual("bad", verdict, detail)
        self.assertIn("wk key set claude-login", detail)

    def test_anything_else_is_refused_by_shape(self):
        verdict, detail = self.claude("hunter2")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("sk-ant-oat", detail)

    def test_a_litellm_virtual_key_names_the_endpoint_it_belongs_to(self):
        """The endpoint is one constant beside the key page (LITELLM_ENDPOINT),
        so the verdict names it rather than leaving a reader to find it in a
        workspace's config."""
        verdict, detail = self.check("litellm", "sk-abc123")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("models.json", detail)
        self.assertIn(credcheck.LITELLM_ENDPOINT, detail)
        self.assertIn("unmeasured", detail)

    def test_the_upstream_anthropic_key_is_refused_where_a_virtual_one_belongs(self):
        verdict, detail = self.check("litellm", "sk-ant-api03-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("upstream account", detail)

    def test_nothing_is_refused(self):
        verdict, _d = self.check("litellm", "")
        self.assertEqual("bad", verdict)


# --- the two tailscale credentials ---------------------------------------------
class TestTheTailnetKeys(_Rules):
    def test_an_auth_key_is_accepted_and_what_it_cannot_prove_is_said(self):
        verdict, detail = self.check("tailnet", "tskey-auth-k1-abc")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("enroll a node and nothing else", detail)
        self.assertIn("NOT ephemeral", detail)

    def test_the_api_token_is_refused_where_an_auth_key_belongs(self):
        verdict, detail = self.check("tailnet", "tskey-api-k1-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("administers the whole tailnet", detail)

    def test_an_oauth_client_secret_is_refused_in_both_directions(self):
        for name in ("tailnet", "tailnet-api"):
            with self.subTest(name=name):
                verdict, detail = self.check(name, "tskey-client-k1-abc")
                self.assertEqual("bad", verdict, detail)
                self.assertIn("OAuth client secret", detail)

    def test_an_auth_key_is_refused_where_the_api_token_belongs(self):
        verdict, detail = self.check("tailnet-api", "tskey-auth-k1-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("enrolls a node", detail)

    def test_a_stored_api_token_is_put_to_the_tailnet(self):
        path = self.tmp / "api-key"
        path.write_text("tskey-api-k1-abc\n")
        verdict, detail = self.check("tailnet-api", path=path,
                                     env={"WK_TAILNET_API": "http://127.0.0.1:1"})
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("could not ask the tailnet", detail)


# --- a deploy key --------------------------------------------------------------
class TestADeployKey(_Rules):
    REPO_NAME = "wkuser/WebKit"

    def key(self, ssh, read_only):
        return self.check("deploy-key", "", repos=self.REPO_NAME,
                          evidence=["ssh=" + ssh, "read_only=" + read_only])

    def test_registered_on_its_fork_with_write_access(self):
        verdict, detail = self.key("Hi %s! You've successfully authenticated"
                                   % self.REPO_NAME, "false")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("write access, and on no other", detail)

    def test_a_read_only_registration_is_refused(self):
        verdict, detail = self.key("Hi %s!" % self.REPO_NAME, "true")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("READ-ONLY", detail)
        self.assertIn("wk key deploy", detail)

    def test_a_key_github_does_not_know_is_refused(self):
        verdict, detail = self.key("Permission denied (publickey).", "")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("not registered on", detail)

    def test_an_account_key_reaches_too_far_and_is_refused(self):
        """A user key authenticates without naming a repository: it reaches
        every repository that account can, which is the reach a deploy key
        exists to avoid."""
        verdict, detail = self.key("Hi wkuser! You've successfully "
                                   "authenticated, but GitHub does not provide "
                                   "shell access.", "")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("account key", detail)

    def test_no_key_at_all(self):
        verdict, detail = self.key("no key", "")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("no key for", detail)

    def test_write_access_that_could_not_be_read_is_unverified_not_claimed(self):
        verdict, detail = self.key("Hi %s!" % self.REPO_NAME, "")
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("unconfirmed", detail)


# --- the table itself ----------------------------------------------------------
class TestOneTableForEveryCredential(_Rules):
    def names(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "names"],
                            capture_output=True, text=True)
        return cp.stdout.split()

    def test_every_delivered_credential_has_a_rule(self):
        """wk_agent_secrets (lib/store.sh) is what delivers a credential into a
        workspace; a row added there without a rule would be stored unchecked."""
        body = STORE_SH.split("wk_agent_secrets() {", 1)[1]
        body = body.split("EOF", 1)[0]
        rows = [l.split()[0] for l in body.splitlines()
                if l.strip() and not l.strip().startswith(("cat", "<<"))]
        for row in rows:
            self.assertIn(row, self.names(), row)

    def test_the_credentials_held_beside_the_deploy_keys_have_rules_too(self):
        for name in ("github-pat", "tailnet", "tailnet-api", "deploy-key"):
            self.assertIn(name, self.names())

    def rule(self, name, repos=FORKS):
        cp = subprocess.run(["python3", str(CREDCHECK), "rule", name,
                             "--repos", repos], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return dict(l.split("\t", 1) for l in cp.stdout.splitlines())

    def test_every_rule_names_where_the_credential_is_spent(self):
        """The evidence for a rule is the code that spends it, so each row
        carries that file rather than leaving it to a reader to find."""
        for name in self.names():
            fields = self.rule(name)
            self.assertEqual({"spent_by", "needs", "forbids", "what", "url",
                              "remedy", "store_with", "fix"}, set(fields), name)
            self.assertRegex(fields["spent_by"], r"[\w.-]+/[\w.-]+",
                             "%s: spent_by names no file" % name)

    def test_every_rule_says_what_to_ask_for_and_where_to_get_it(self):
        """`wk key` carries no prose of its own: the prompt, the page and the
        choices left to make all come from here, so a credential nobody
        described is a credential nobody can be asked for."""
        for name in self.names():
            fields = self.rule(name)
            with self.subTest(name=name):
                self.assertTrue(fields["what"].strip(), "%s: no `what`" % name)
                self.assertTrue(fields["remedy"].strip(), name)
                if fields["url"]:
                    self.assertTrue(fields["url"].startswith("https://"),
                                    fields["url"])
                    self.assertIn(fields["url"], fields["fix"])
                self.assertIn(fields["remedy"], fields["fix"])

    def test_the_token_page_arrives_with_the_permissions_filled_in(self):
        """GitHub takes the name, the expiry and each permission as query
        parameters, so the only thing left to choose is the repository list --
        which a link cannot carry, and which the remedy therefore names."""
        fields = self.rule("github-pat")
        url = fields["url"]
        self.assertTrue(url.startswith(
            "https://github.com/settings/personal-access-tokens/new?"), url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(["write"], query["contents"])
        self.assertEqual(["write"], query["pull_requests"])
        self.assertEqual(["none"], query["expires_in"])
        self.assertEqual(["wkuser"], query["target_name"])
        self.assertNotIn("repositories", query)
        for repo in FORKS.split():
            self.assertIn(repo, fields["remedy"])

    def test_this_machine_knows_where_each_one_is_kept(self):
        """One path table (wk_cred_path), so `wk key set`, `wk key check` and
        `wk doctor` read the same bytes."""
        script = ('. "%s/lib/common.sh"\n. "%s/lib/store.sh"\n'
                  'for n in $(wk_cred_names); do\n'
                  '  [ "$n" = deploy-key ] && continue\n'
                  '  printf "%%s %%s\\n" "$n" "$(wk_cred_path "$n")"\n'
                  'done\n' % (REPO, REPO))
        cp = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, cwd=str(REPO))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for line in cp.stdout.splitlines():
            name, _sp, path = line.partition(" ")
            self.assertTrue(path.startswith("/"), line)

    def test_nothing_stored_is_a_state_and_not_a_fault(self):
        verdict, detail = self.check("litellm", path=self.tmp / "absent")
        self.assertEqual("absent", verdict, detail)
        self.assertIn("wk key set litellm", detail)

    def test_a_stored_credential_is_read_the_one_way(self):
        """A workspace can write in the agent-rw directory, so a link left
        there pointing at the token beside the deploy keys would turn every
        read of the login into a read of the token. wk_cred_check reads through
        lib/secretfile.py, which refuses one."""
        real = self.tmp / "push-keys" / "github-pat"
        real.parent.mkdir()
        real.write_text("github_pat_11ABC_hidden\n")
        secrets = self.tmp / "secrets"
        secrets.mkdir()
        (secrets / "litellm-key").symlink_to(real)
        script = ('. "%s/lib/common.sh"\n. "%s/lib/store.sh"\n'
                  'wk_cred_check litellm --stored\n' % (REPO, REPO))
        env = dict(os.environ, WK_HOST_SECRETS=str(secrets),
                   WK_STORE=str(self.tmp))
        cp = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, cwd=str(REPO), env=env)
        self.assertNotIn("hidden", cp.stdout + cp.stderr)
        self.assertIn("refusing to read", cp.stderr)
        self.assertTrue(cp.stdout.startswith("bad\t"), cp.stdout)
        self.assertIn("could not be read", cp.stdout)

    def test_an_unknown_name_is_refused_rather_than_admitted(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "check", "nosuchthing"],
                            input="x", capture_output=True, text=True)
        self.assertEqual(2, cp.returncode)
        self.assertIn("no rule for", cp.stderr)


if __name__ == "__main__":
    unittest.main()
