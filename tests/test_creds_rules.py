import unittest

from tests.credsworld import TOKEN, URL, Http, login, private_key, public_key
from wk.core.errors import Refused
from wk.creds import injector, net, rules
from wk.creds.rules import BAD, OK, UNKNOWN

GH = "https://api.github.com"
USER = ("GET", GH + "/user")
PULLS = ("POST", GH + "/repos/WebKit/WebKit/pulls")
KEYS = ("GET", GH + "/repos/o/r/keys?per_page=100")
BZ = ("GET", "https://bugs.webkit.org/rest/user/me%40example.test?login=me%40example.test&password=k%2B1")
MODELS = ("GET", URL + "/models")
ANTHROPIC = ("GET", "https://api.anthropic.com/v1/models?limit=1")
DOWN = net.NoAnswer("URLError: down")


class Rules(unittest.TestCase):
    def probe(self, name, value, answers, store=None, now=1000.0, litellm=URL):
        self.http = Http(answers)
        with self.http.patch():
            return rules.find(name).probe(value, rules.Context((store or {}).get, now, litellm))

    def verdict(self, *args, **kwargs):
        return self.probe(*args, **kwargs)[0]

    def checks(self, name, good, bad):
        check = rules.find(name).check
        self.assertEqual([check(v) for v in good], [None] * len(good))
        for value in bad:
            self.assertIsInstance(check(value), str, value)


class TestFind(Rules):
    def test_every_rule_says_what_it_is_for(self):
        self.assertEqual([r.name for r in rules.RULES], ["github-user", "github-token", "bugzilla-user", "bugzilla-password", "litellm-key",
                                                         "claude-login", "tailnet-auth-key"])
        self.assertTrue(all(r.what for r in rules.RULES))

    def test_a_deploy_key_is_named_for_its_repository(self):
        self.assertEqual(rules.find("deploy-key/o/r").name, "deploy-key/o/r")

    def test_an_unknown_name_is_refused_naming_the_names(self):
        for name in ("bogus", "deploy-key/norepo", "deploy-key/a/b/c"):
            with self.assertRaisesRegex(Refused, "no credential is named %s: the names are github-user, .* and deploy-key/<owner>/<repo>" % name):
                rules.find(name)

    def test_repos_are_the_deploy_keys_in_the_store(self):
        self.assertEqual(rules.repos(["github-token", "deploy-key/o/r", "deploy-key/odd"]), ["o/r"])


class TestGithub(Rules):
    def test_a_user_is_one_word_and_github_knows_it_or_not(self):
        self.checks("github-user", ["some-one"], ["two words", ""])
        url = ("GET", GH + "/users/some-one")
        self.assertEqual([self.verdict("github-user", "some-one", {url: a}) for a in ((200, {}), (404, {}), (500, {}), DOWN)],
                         [OK, BAD, UNKNOWN, UNKNOWN])

    def test_a_token_is_a_classic_one(self):
        self.checks("github-token", [TOKEN], ["gho_abc", "ghp_a b", ""])
        self.assertIn("fine-grained", rules.find("github-token").check("github_pat_abc"))

    def test_an_authorised_token_answers_422_to_an_empty_pull_request(self):
        got = self.probe("github-token", TOKEN, {USER: (200, {"login": "me"}), PULLS: (422, {})})
        self.assertEqual(got, (OK, "as me, and it can open a pull request on WebKit/WebKit"))
        self.assertEqual(self.http.asked, [("GET", GH + "/user", {"Authorization": "Bearer " + TOKEN}, None),
                                           ("POST", GH + "/repos/WebKit/WebKit/pulls", {"Authorization": "Bearer " + TOKEN}, b"{}")])

    def test_a_token_github_refuses_is_bad(self):
        self.assertEqual(self.verdict("github-token", TOKEN, {USER: (401, {})}), BAD)

    def test_a_token_refused_a_pull_request_is_bad(self):
        self.assertEqual(self.probe("github-token", TOKEN, {USER: (200, {"login": "me"}), PULLS: (403, {})}),
                         (BAD, "WebKit/WebKit refuses this token a pull request (HTTP 403): a classic token needs the public_repo scope"))

    def test_a_token_of_another_account_than_github_user_is_bad(self):
        got = self.probe("github-token", TOKEN, {USER: (200, {"login": "other"})}, {"github-user": "me"})
        self.assertEqual(got, (BAD, "it is other's token, and github-user is me"))

    def test_github_not_answering_either_question_is_unknown(self):
        self.assertEqual(self.probe("github-token", TOKEN, {USER: DOWN}), (UNKNOWN, "GET /user did not answer (URLError: down)"))
        self.assertEqual(self.probe("github-token", TOKEN, {USER: (200, {"login": "me"}), PULLS: (500, b"<html>")}, {"github-user": "me"}),
                         (UNKNOWN, "POST /repos/WebKit/WebKit/pulls answered HTTP 500"))


class TestBugzilla(Rules):
    STORE = {"bugzilla-user": "me@example.test", "bugzilla-password": "k+1"}

    def test_the_login_is_an_email_and_the_key_one_word(self):
        self.checks("bugzilla-user", ["me@example.test"], ["me", "a b@c", ""])
        self.checks("bugzilla-password", ["k+1"], ["two words", ""])

    def test_both_halves_are_checked_with_git_webkits_own_request(self):
        for name in ("bugzilla-user", "bugzilla-password"):
            verdicts = [self.verdict(name, self.STORE[name], {BZ: a}, self.STORE) for a in ((200, {}), (401, {}), (500, {}), DOWN)]
            self.assertEqual(verdicts, [OK, BAD, UNKNOWN, UNKNOWN])

    def test_one_half_alone_is_unknown_and_names_the_other(self):
        self.assertEqual(self.probe("bugzilla-password", "k+1", {}), (UNKNOWN, "it is checked with its other half: `wk key set bugzilla-user`"))
        self.assertEqual(self.probe("bugzilla-user", "me@example.test", {}), (UNKNOWN, "it is checked with its other half: `wk key set bugzilla-password`"))


class TestLitellm(Rules):
    def test_the_key_is_one_word_and_not_an_anthropic_key(self):
        self.checks("litellm-key", ["sk-abc_1"], ["sk-ant-api-1", "a b", "a$b", ""])

    def test_the_endpoint_accepts_refuses_or_does_not_answer(self):
        self.assertEqual(self.probe("litellm-key", "sk-1", {MODELS: (200, {"data": [{"id": "a"}, {"id": "b"}]})}),
                         (OK, URL + " accepts it and serves it 2 model(s)"))
        self.assertEqual(self.http.asked[0][2], {"Authorization": "Bearer sk-1"})
        self.assertEqual([self.verdict("litellm-key", "sk-1", {MODELS: a}) for a in ((401, {}), (502, {}), DOWN)], [BAD, UNKNOWN, UNKNOWN])

    def test_without_an_endpoint_in_the_conf_it_is_unknown(self):
        self.assertEqual(self.probe("litellm-key", "sk-1", {}, litellm=None),
                         (UNKNOWN, "no endpoint to ask: set litellm_url= in the client's machines/<name>.conf"))


class TestClaudeLogin(Rules):
    def test_it_is_a_login_document_and_not_the_placeholder(self):
        self.checks("claude-login", [login()], ["sk-ant-oat-1", "{}", injector.placeholder()])

    def test_anthropic_accepts_refuses_or_does_not_answer_a_live_access_token(self):
        self.assertEqual([self.verdict("claude-login", login(), {ANTHROPIC: a}) for a in ((200, {}), (401, {}), (529, {}), DOWN)],
                         [OK, BAD, UNKNOWN, UNKNOWN])
        self.assertEqual(self.http.asked[0][2], {"anthropic-version": "2023-06-01", "Authorization": "Bearer access-one"})

    def test_an_expired_stored_copy_is_unknown_and_asks_nobody(self):
        got = self.probe("claude-login", login(expires_ms=999000), {}, now=1000.0)
        self.assertEqual((got[0], self.http.asked), (UNKNOWN, []))
        self.assertIn("the injector holds the refreshed one", got[1])


class TestTailnet(Rules):
    def test_an_auth_key_is_checked_by_shape_and_cannot_be_probed(self):
        self.checks("tailnet-auth-key", ["tskey-auth-abc"], ["tskey-api-abc", "tskey-auth-a b", ""])
        self.assertEqual(self.verdict("tailnet-auth-key", "tskey-auth-abc", {}), UNKNOWN)


class TestDeployKey(Rules):
    def test_it_is_an_openssh_private_key_with_no_passphrase(self):
        self.checks("deploy-key/o/r", [private_key(), private_key() + "\n"],
                    ["", "ssh-ed25519 AAAA", private_key().replace("b3Blbn", "AAAAAA"), private_key()[:60] + "\n-----END OPENSSH PRIVATE KEY-----",
                     "-----BEGIN OPENSSH PRIVATE KEY-----\n!!\n-----END OPENSSH PRIVATE KEY-----"])
        self.assertIn("passphrase", rules.find("deploy-key/o/r").check(private_key(cipher=b"aes256-ctr")))

    def test_the_public_half_is_read_out_of_the_private_key(self):
        self.assertEqual(rules.public_key(private_key()), ("none", public_key()))

    def test_it_is_ok_when_registered_on_its_repository_with_write_access(self):
        keys = [{"key": public_key(b"\2"), "read_only": False}, {"key": public_key() + " a comment", "read_only": False}]
        self.assertEqual(self.probe("deploy-key/o/r", private_key(), {KEYS: (200, keys)}, {"github-token": TOKEN}),
                         (OK, "registered on o/r with write access"))
        self.assertEqual(self.http.asked[0][2], {"Authorization": "Bearer " + TOKEN})

    def test_it_is_bad_when_read_only_or_not_registered(self):
        store = {"github-token": TOKEN}
        self.assertEqual(self.probe("deploy-key/o/r", private_key(), {KEYS: (200, [{"key": public_key(), "read_only": True}])}, store)[0], BAD)
        got = self.probe("deploy-key/o/r", private_key(), {KEYS: (200, [])}, store)
        self.assertEqual(got, (BAD, "it is not a deploy key of o/r: add `%s` at https://github.com/o/r/settings/keys/new, allowing write access" % public_key()))

    def test_it_is_unknown_without_a_token_or_an_answer(self):
        self.assertEqual(self.probe("deploy-key/o/r", private_key(), {}), (UNKNOWN, "GitHub is asked with github-token: `wk key set github-token`"))
        self.assertEqual([self.verdict("deploy-key/o/r", private_key(), {KEYS: a}, {"github-token": TOKEN}) for a in ((404, {}), DOWN)],
                         [UNKNOWN, UNKNOWN])
