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
PROJECTS = {"wkuser/WebKit": "WebKit/WebKit",
            "wkuser/WPEWebKit": "WebPlatformForEmbedded/WPEWebKit"}
FINE = "github_pat_11ABCDEFG_notarealtoken"
CLASSIC = "ghp_notarealclassictoken0123456789"

# What WebKit/WebKit answered a never-expiring fine-grained token, 2026-09-15.
POLICY = ("The 'WebKit' organization forbids access via a fine-grained "
          "personal access tokens if the token's lifetime is greater than 366 "
          "days. Please adjust your token's lifetime at the following URL: "
          "https://github.com/settings/personal-access-tokens/19512093")


class FakeGitHub(BaseHTTPRequestHandler):
    """`GET /user` answers the token's identity, its classic scope list and its
    expiry; `GET /user/repos` the account's repositories, one page at a time,
    whatever the token was granted; `GET /repos/<r>` the repository, with the
    project it is a fork of as `parent`, or `repo_status`'s refusal carrying
    `repo_message`; `POST /repos/<r>/pulls` whether the token can open a pull
    request there, 403 unless `pulls` says otherwise. All four are what GitHub
    itself answers."""

    user_status = 200
    scopes = ""
    expiry = ""
    pulls = {}
    repos = []
    repos_status = 200
    repos_answer = None
    parents = {}
    repo_status = {}
    repo_message = ""
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
        FakeGitHub.parents = dict(PROJECTS)
        FakeGitHub.repo_status = {}
        FakeGitHub.repo_message = POLICY
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


# --- bugzilla-api-key ------------------------------------------------------------
BZ_LOGIN = "me@example.test"
BZ_KEY = "notarealbugzillakey0123456789abcdefghijk"


class FakeBugzilla(BaseHTTPRequestHandler):
    """`GET /rest/valid_login?login=..&api_key=..` answers as bugs.webkit.org
    (Bugzilla 5.0.4) does, measured 2026-09-14: `{"result": true}` for the
    key's own login, `{"result": false}` for another login, and HTTP 400 with
    error code 306 for a key it does not know."""

    seen = []

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

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

    def log_message(self, *a):
        pass


class _Bugzilla(_Rules):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bz = HTTPServer(("127.0.0.1", 0), FakeBugzilla)
        cls.bz_base = "http://127.0.0.1:%d" % cls.bz.server_port
        threading.Thread(target=cls.bz.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.bz.shutdown()
        cls.bz.server_close()
        super().tearDownClass()

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
        """`wk_cred_check` (lib/store.sh) supplies the login as evidence, from
        the mirror; the rule sends the pair to `valid_login` and nothing else."""
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

    def test_the_rule_names_the_injector_and_the_key_page(self):
        cp = subprocess.run(["python3", str(CREDCHECK), "rule", "bugzilla-api-key",
                             "--repos", FORKS], capture_output=True, text=True)
        fields = dict(l.split("\t", 1) for l in cp.stdout.splitlines())
        self.assertIn("github-inject.py", fields["spent_by"])
        self.assertIn("api_key", fields["spent_by"])
        self.assertEqual("https://bugs.webkit.org/userprefs.cgi?tab=apikey", fields["url"])
        self.assertIn("whole account", fields["forbids"])


# --- github-pat ---------------------------------------------------------------
class TestTheTokenCanDoTheJob(_Rules):
    def test_a_fine_grained_token_that_can_open_a_pull_request_on_both_forks(self):
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertIn("fine-grained", detail)
        for repo in FORKS.split():
            self.assertIn("can open a pull request on %s" % repo, detail)

    def test_the_account_repositories_are_listed_once_per_page(self):
        """One GET per page, after the identity call, each fork and each
        project a fork belongs to."""
        self.check("github-pat", FINE)
        self.assertEqual(["/user",
                          "/repos/wkuser/WebKit", "/repos/wkuser/WPEWebKit",
                          "/repos/WebKit/WebKit",
                          "/repos/WebPlatformForEmbedded/WPEWebKit",
                          "/user/repos?per_page=100&page=1"],
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


class TestTheProjectRefusesIt(_Rules):
    """A pull request is opened on the project, not on the fork, and an
    organization's personal-access-token policy refuses a token wholesale:
    measured 2026-09-15, a fine-grained token that never expires opens nothing
    on WebKit/WebKit and answers 403 to a plain read there, while every fork
    probe above it passes. So the forks are not the whole question -- each
    fork's parent is asked whether it accepts the token at all."""

    def test_a_project_that_refuses_the_token_is_refused_in_githubs_words(self):
        FakeGitHub.repo_status = {"WebKit/WebKit": 403}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("WebKit/WebKit refuses this token outright", detail)
        self.assertIn("366 days", detail)
        self.assertIn("personal-access-tokens/19512093", detail)
        self.assertIn("wk key set github-pat", detail)

    def test_a_refusal_with_no_message_still_names_the_project(self):
        FakeGitHub.repo_status = {"WebKit/WebKit": 403}
        FakeGitHub.repo_message = ""
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("WebKit/WebKit", detail)

    def test_a_classic_token_is_asked_the_same_question(self):
        """The policy is the organization's, not the token format's: a classic
        token it disallows is refused before its reach is reported."""
        FakeGitHub.scopes = "repo"
        FakeGitHub.repo_status = {"WebKit/WebKit": 403}
        verdict, detail = self.check("github-pat", CLASSIC)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("WebKit/WebKit", detail)

    def test_every_project_accepting_it_is_named(self):
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        for project in PROJECTS.values():
            self.assertIn("%s accepts it" % project, detail)

    def test_a_fork_of_nothing_has_no_project_to_ask(self):
        """The parent is what GitHub answers, not a list kept here: a
        repository that is nobody's fork is its own base, already probed."""
        FakeGitHub.parents = {}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("ok", verdict, detail)
        self.assertEqual(["/user", "/repos/wkuser/WebKit",
                          "/repos/wkuser/WPEWebKit",
                          "/user/repos?per_page=100&page=1"],
                         [p[1] for p in FakeGitHub.seen if p[0] == "GET"])

    def test_one_project_is_asked_once_however_many_forks_name_it(self):
        FakeGitHub.parents = dict.fromkeys(PROJECTS, "WebKit/WebKit")
        self.check("github-pat", FINE)
        self.assertEqual(1, [p[1] for p in FakeGitHub.seen
                             if p[0] == "GET"].count("/repos/WebKit/WebKit"))

    def test_a_fork_that_could_not_be_read_is_unverified_not_claimed(self):
        FakeGitHub.repo_status = {"wkuser/WebKit": 500}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("which project each fork belongs to", detail)

    def test_a_project_that_answered_neither_200_nor_403_is_unverified(self):
        FakeGitHub.repo_status = {"WebKit/WebKit": 500}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("rather than 200 or 403", detail)

    def test_the_project_is_asked_before_the_account_is_enumerated(self):
        """The wholesale answer costs two requests; enumerating the account
        costs one per repository it owns."""
        FakeGitHub.repo_status = {"WebKit/WebKit": 403}
        self.check("github-pat", FINE)
        self.assertEqual([], [p for p in FakeGitHub.seen
                              if p[1].startswith("/user/repos")])


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


class FakeAnthropic(BaseHTTPRequestHandler):
    """api.anthropic.com and platform.claude.com as the rules see them
    (measured 2026-09-10 and 2026-09-14): `GET /v1/models` answers 200 for a
    token Anthropic accepts and 401 for one it does not, with no model
    inferred; `GET /api/oauth/profile` names the account and organization
    behind a login; `GET /api/claude_code/policy_limits` is the organization's
    restrictions, what the CLI reads before it starts remote control; `POST
    /v1/oauth/token` renews a login, or answers 400 invalid_grant for a refresh
    token it no longer knows."""

    status = 200             # /v1/models and /api/oauth/profile
    policy_status = 200
    policy = {"restrictions": {}, "compliance_taints": []}
    refresh_status = 200
    refresh_answer = None
    seen = []

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeAnthropic.seen.append(
            (self.command, self.path, self.headers.get("Authorization", ""),
             self.headers.get("anthropic-version", "")))
        if self.path.startswith("/api/claude_code/policy_limits"):
            return self._send(FakeAnthropic.policy_status, FakeAnthropic.policy)
        if FakeAnthropic.status != 200:
            return self._send(FakeAnthropic.status,
                              {"type": "error",
                               "error": {"type": "authentication_error"}})
        if self.path.startswith("/api/oauth/profile"):
            return self._send(200, {
                "account": {"email": "someone@example.invalid"},
                "organization": {"uuid": ORG, "name": "Example Org",
                                 "subscription_status": "active"}})
        return self._send(200, {"data": [{"id": "claude-x"}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        FakeAnthropic.seen.append(
            (self.command, self.path, body.get("grant_type", ""),
             body.get("refresh_token", "")))
        if FakeAnthropic.refresh_status != 200:
            return self._send(FakeAnthropic.refresh_status,
                              {"error": "invalid_grant"})
        answer = FakeAnthropic.refresh_answer or {
            "access_token": RENEWED, "refresh_token": ROTATED,
            "expires_in": 28800, "refresh_token_expires_in": 30 * 86400,
            "scope": body.get("scope", "")}
        return self._send(200, answer)

    def log_message(self, *a):
        pass


ORG = "org-1111"
RENEWED = "renewed-" + "a" * 20
ROTATED = "rotated-" + "r" * 20
RECORD = {"oauthAccount": {"organizationUuid": ORG,
                           "organizationName": "Example"}}


class _Anthropic(_Rules):
    """A FakeAnthropic of the class's own, reset before every test."""

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
        FakeAnthropic.policy_status = 200
        FakeAnthropic.policy = {"restrictions": {}, "compliance_taints": []}
        FakeAnthropic.refresh_status = 200
        FakeAnthropic.refresh_answer = None
        FakeAnthropic.seen = []

    def anthropic_env(self, api=None):
        base = api if api is not None else self.anthropic_base
        return {"WK_ANTHROPIC_API": base, "WK_CLAUDE_OAUTH": base}


class FakeLiteLLM(BaseHTTPRequestHandler):
    """ai.igalia.com as measured 2026-09-14: `GET /v1/models` lists what a key
    may call (401 for one it does not accept), and `GET /key/info` answers 403
    for a key restricted to the LLM API routes, 200 with the key's own record
    for one that is not."""

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
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


# --- the claude.ai login -------------------------------------------------------
def login(scopes=("user:profile", "user:inference"), refresh="r" * 20,
          refresh_expires_days=29, subscription="team", access_expires_s=3600):
    doc = {"accessToken": "a" * 20, "scopes": list(scopes),
           "expiresAt": int((time.time() + access_expires_s) * 1000),
           "subscriptionType": subscription}
    if refresh:
        doc["refreshToken"] = refresh
    if refresh_expires_days is not None:
        doc["refreshTokenExpiresAt"] = int(
            (time.time() + refresh_expires_days * 86400) * 1000)
    return json.dumps({"claudeAiOauth": doc})


class TestTheClaudeLogin(_Anthropic):
    def test_a_login_that_carries_what_an_agent_spends(self):
        verdict, detail = self.check("claude-login", login())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("user:profile", detail)
        self.assertIn("subscription: team", detail)
        self.assertIn("renewable until", detail)

    def _stored(self, record=RECORD, doc=None):
        """A login as the store holds it: the credential file, and beside it
        the CLI's config file with (or without) the account record."""
        d = self.tmp / "agent-rw"
        d.mkdir(exist_ok=True)
        (d / ".credentials.json").write_text(doc or login())
        if record is not None:
            (d / ".claude.json").write_text(json.dumps(record))
        return d / ".credentials.json"

    def stored(self, path, api=None):
        return self.check("claude-login", path=path, env=self.anthropic_env(api))

    def test_a_stored_login_is_judged_with_the_record_beside_it(self):
        """Measured 2026-09-11: remote control reads organizationUuid from the
        CLI's config file and refuses without it, so a stored login is judged
        by the record `claude auth login` left beside the credential."""
        verdict, detail = self.stored(self._stored())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("organization: Example", detail)

    def test_a_stored_login_with_no_record_is_refused_and_names_the_rotation(self):
        for record in (None, {}, {"oauthAccount": {"emailAddress": "x"}}):
            with self.subTest(record=record):
                verdict, detail = self.stored(self._stored(record))
                self.assertEqual("bad", verdict, detail)
                self.assertIn("no account record", detail)
                self.assertIn("organizationUuid", detail)
                self.assertIn("wk key set claude-login --replace", detail)
        self.assertEqual([], FakeAnthropic.seen, "asked before the record was read")

    def test_a_stored_login_is_asked_about_and_the_answer_is_the_report(self):
        """What Anthropic says now, not what the file says: the organization
        and subscription from the profile, and the organization's policy on
        remote control, published as a line a command can decide on."""
        verdict, detail = self.stored(self._stored())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("organization: Example Org", detail)
        self.assertIn("subscription active", detail)
        self.assertIn("remote control allowed", detail)
        self.assertIn("\n    remote-control: allowed", detail)
        paths = [seen[1] for seen in FakeAnthropic.seen]
        self.assertTrue(any(p.startswith("/api/oauth/profile") for p in paths), paths)
        self.assertTrue(any(p.startswith("/api/claude_code/policy_limits") for p in paths), paths)
        self.assertNotIn("POST", [seen[0] for seen in FakeAnthropic.seen],
                         "a current access token was renewed for nothing")

    def test_an_expired_access_token_is_renewed_and_written_back(self):
        """The one way to ask Anthropic anything about a login whose access
        token has run out: the refresh token is posted the way the CLI posts
        it, and the rotated pair written over the one file every holder reads,
        the rest of the document kept."""
        path = self._stored(doc=login(access_expires_s=-60))
        verdict, detail = self.stored(path)
        self.assertEqual("ok", verdict, detail)
        posts = [seen for seen in FakeAnthropic.seen if seen[0] == "POST"]
        self.assertEqual(1, len(posts), FakeAnthropic.seen)
        self.assertEqual(("/v1/oauth/token", "refresh_token", "r" * 20), posts[0][1:])
        after = json.loads(path.read_text())["claudeAiOauth"]
        self.assertEqual(RENEWED, after["accessToken"])
        self.assertEqual(ROTATED, after["refreshToken"])
        self.assertGreater(after["expiresAt"], time.time() * 1000)
        self.assertEqual("team", after["subscriptionType"])
        self.assertEqual(["user:profile", "user:inference"], after["scopes"])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertFalse((path.parent / ".oauth_refresh.lock").exists())
        self.assertNotIn("r" * 20, detail)
        self.assertNotIn(RENEWED, detail)

    def test_a_refresh_anthropic_refuses_is_a_dead_login(self):
        FakeAnthropic.refresh_status = 400
        path = self._stored(doc=login(access_expires_s=-60))
        before = path.read_text()
        verdict, detail = self.stored(path)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("refuses to renew it", detail)
        self.assertIn("invalid_grant", detail)
        self.assertIn("wk key set claude-login --replace", detail)
        self.assertEqual(before, path.read_text(), "a refused refresh rewrote the file")

    def test_a_login_anthropic_no_longer_accepts_is_refused(self):
        FakeAnthropic.status = 401
        verdict, detail = self.stored(self._stored())
        self.assertEqual("bad", verdict, detail)
        self.assertIn("does not accept this login", detail)

    def test_a_refresh_lock_another_process_holds_is_respected(self):
        """The CLI's own lock, beside the credential: a session mid-refresh is
        left to finish, and the login is reported unverified rather than
        refreshed twice -- which would rotate the token out from under it."""
        path = self._stored(doc=login(access_expires_s=-60))
        lock = path.parent / ".oauth_refresh.lock"
        lock.mkdir()
        before = path.read_text()
        verdict, detail = self.stored(path)
        self.assertEqual("unverified", verdict, detail)
        self.assertIn("refresh lock", detail)
        self.assertIn("\n    remote-control: unverified", detail)
        self.assertEqual([], [seen for seen in FakeAnthropic.seen if seen[0] == "POST"])
        self.assertEqual(before, path.read_text())
        self.assertTrue(lock.is_dir(), "another process's lock was removed")

    def test_a_lock_abandoned_over_a_minute_ago_is_taken(self):
        path = self._stored(doc=login(access_expires_s=-60))
        lock = path.parent / ".oauth_refresh.lock"
        lock.mkdir()
        stale = time.time() - 120
        os.utime(lock, (stale, stale))
        verdict, detail = self.stored(path)
        self.assertEqual("ok", verdict, detail)
        self.assertEqual(RENEWED, json.loads(path.read_text())["claudeAiOauth"]["accessToken"])
        self.assertFalse(lock.exists())

    def test_a_policy_that_denies_remote_control_is_said_with_the_remedy(self):
        """The login is still a login -- every plain session works -- so the
        verdict stays ok and the denial rides as the fact the two commands that
        start remote control refuse on."""
        FakeAnthropic.policy = {"restrictions": {"allow_remote_control": {"allowed": False}},
                                "compliance_taints": []}
        verdict, detail = self.stored(self._stored())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("remote control DENIED", detail)
        self.assertIn("\n    remote-control: denied", detail)
        self.assertIn("fix: an owner of the Example Org organization", detail)
        self.assertIn("WK_NO_CLAUDE_RC=1", detail)

    def test_a_hipaa_organization_is_denied_the_same_way(self):
        FakeAnthropic.policy = {"restrictions": {}, "compliance_taints": ["hipaa"]}
        verdict, detail = self.stored(self._stored())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("\n    remote-control: denied", detail)
        self.assertIn("HIPAA", detail)

    def test_a_policy_the_api_does_not_serve_is_unverified_and_names_the_path(self):
        """The CLI refuses remote control on a 404 for this path and says a
        proxy is the usual cause; the verdict says the same, ahead of time."""
        FakeAnthropic.policy_status = 404
        verdict, detail = self.stored(self._stored())
        self.assertEqual("ok", verdict, detail)
        self.assertIn("\n    remote-control: unverified", detail)
        self.assertIn("policy_limits", detail)
        self.assertIn("404", detail)

    def test_no_network_is_unverified_not_refused(self):
        for doc, want in ((login(), "could not reach"),
                          (login(access_expires_s=-60), "neither renewed nor asked about")):
            with self.subTest(want=want):
                path = self._stored(doc=doc)
                verdict, detail = self.stored(path, api="http://127.0.0.1:1")
                self.assertEqual("unverified", verdict, detail)
                self.assertIn(want, detail)
                self.assertIn("\n    remote-control: unverified", detail)
                self.assertEqual(doc, path.read_text())

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
class TestTheAgentKeys(_Anthropic):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.litellm = HTTPServer(("127.0.0.1", 0), FakeLiteLLM)
        cls.litellm_base = "http://127.0.0.1:%d" % cls.litellm.server_port
        cls.litellm_thread = threading.Thread(
            target=cls.litellm.serve_forever, daemon=True)
        cls.litellm_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.litellm.shutdown()
        cls.litellm.server_close()
        super().tearDownClass()

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

    def test_a_litellm_key_the_endpoint_accepts_and_restricts_is_ok(self):
        """Measured, not assumed: the endpoint is asked what the key may call,
        and a key shut out of the management routes (403 on /key/info) is what
        a key every workspace holds should be."""
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
        for name in ("github-pat", "bugzilla-api-key", "tailnet", "tailnet-api",
                     "deploy-key"):
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
        which a link cannot carry, and which the remedy therefore names. The
        expiry is one an organization's token policy allows: a token minted to
        never expire is refused by every project (TestTheProjectRefusesIt)."""
        fields = self.rule("github-pat")
        url = fields["url"]
        self.assertTrue(url.startswith(
            "https://github.com/settings/personal-access-tokens/new?"), url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(["write"], query["contents"])
        self.assertEqual(["write"], query["pull_requests"])
        self.assertEqual([str(credcheck.MAX_PAT_DAYS)], query["expires_in"])
        # The ceiling WebKit's policy states, not a preference: a link that
        # minted a longer-lived token would mint one every project refuses.
        self.assertLessEqual(credcheck.MAX_PAT_DAYS, 366)
        self.assertGreater(credcheck.MAX_PAT_DAYS, 0)
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
