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
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CREDCHECK = REPO / "lib" / "credcheck.py"
STORE_SH = (REPO / "lib" / "store.sh").read_text()

FORKS = "wkuser/WebKit wkuser/WPEWebKit"
FINE = "github_pat_11ABCDEFG_notarealtoken"
CLASSIC = "ghp_notarealclassictoken0123456789"


class FakeGitHub(BaseHTTPRequestHandler):
    """`GET /user` answers the token's identity, its classic scope list and its
    expiry; `POST /repos/<r>/pulls` answers whether a pull request could be
    opened. Both are what GitHub itself answers."""

    user_status = 200
    scopes = ""
    expiry = ""
    pulls = {}
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
        if self.path != "/user":
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
        code = FakeGitHub.pulls.get(repo, 422)
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
        FakeGitHub.pulls = {}
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
        FakeGitHub.pulls = {"wkuser/WPEWebKit": 403}
        verdict, detail = self.check("github-pat", FINE)
        self.assertEqual("bad", verdict, detail)
        self.assertIn("wkuser/WPEWebKit", detail)
        self.assertIn("Pull requests: write", detail)
        self.assertIn("personal-access-tokens/new", detail)
        self.assertIn("wk key set github-pat", detail)

    def test_a_fork_the_token_cannot_see_is_refused_by_name(self):
        FakeGitHub.pulls = {"wkuser/WebKit": 404}
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

    def test_the_eligibility_it_cannot_answer_is_said_rather_than_claimed(self):
        """The document carries no organization -- measured against a real one
        -- so nothing here can say remote control will accept it."""
        _v, detail = self.check("claude-login", login())
        self.assertIn("no organization", detail)
        self.assertIn("--rc", detail)

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
class TestTheAgentKeys(_Rules):
    def test_a_setup_token_is_accepted_and_its_narrowness_named(self):
        verdict, detail = self.check("claude", "sk-ant-oat01-abc")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("inference-only", detail)

    def test_a_console_api_key_is_refused_as_wider_than_the_job(self):
        verdict, detail = self.check("claude", "sk-ant-api03-abc")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("bills the organization", detail)

    def test_a_login_document_pasted_here_names_the_row_that_takes_one(self):
        verdict, detail = self.check("claude", login())
        self.assertEqual("bad", verdict, detail)
        self.assertIn("wk key set claude-login", detail)

    def test_anything_else_is_refused_by_shape(self):
        verdict, detail = self.check("claude", "hunter2")
        self.assertEqual("bad", verdict, detail)
        self.assertIn("sk-ant-oat", detail)

    def test_a_litellm_virtual_key_is_accepted(self):
        verdict, detail = self.check("litellm", "sk-abc123")
        self.assertEqual("ok", verdict, detail)
        self.assertIn("models.json", detail)

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
        self.assertIn("wk key register", detail)

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

    def test_every_rule_names_where_the_credential_is_spent(self):
        """The evidence for a rule is the code that spends it, so each row
        carries that file rather than leaving it to a reader to find."""
        for name in self.names():
            cp = subprocess.run(["python3", str(CREDCHECK), "rule", name],
                                capture_output=True, text=True)
            fields = dict(l.split("\t", 1) for l in cp.stdout.splitlines())
            self.assertEqual({"spent_by", "needs", "forbids", "remedy",
                              "store_with"}, set(fields), name)
            self.assertRegex(fields["spent_by"], r"[\w.-]+/[\w.-]+",
                             "%s: spent_by names no file" % name)

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
