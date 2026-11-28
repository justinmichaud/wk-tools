import json
import unittest

from tests.credsworld import TOKEN, URL, Http, login
from wk.core.errors import Refused
from wk.creds import injector, net, rules
from wk.creds.rules import BAD, OK, UNKNOWN

GH = "https://api.github.com"
USER = ("GET", GH + "/user")
PULLS = ("POST", GH + "/repos/WebKit/WebKit/pulls")
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
                                                         "claude-login", "tailnet-auth-key", "tailnet-api-key", "wifi-ssid", "wifi-psk", "sudo-password"])
        self.assertTrue(all(r.what for r in rules.RULES))

    def test_an_unknown_name_is_refused_naming_the_names(self):
        for name in ("bogus", "deploy-key/o/r", "rotate-key"):
            with self.assertRaisesRegex(Refused, "no credential is named %s: the names are github-user, .*, sudo-password, "
                                                 r"bmc/<machine>/user\|password and sudo/<machine>" % name):
                rules.find(name)

    def test_the_named_credentials_in_the_store_are_those_of_a_machine(self):
        self.assertEqual(rules.named(["github-token", "deploy-key/o/r", "bmc/lin/user", "bmc/lin/pin", "bmc/Lin/user", "sudo/desk", "sudo/a/b"]),
                         ["bmc/lin/user", "sudo/desk"])


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
                         (UNKNOWN, "no endpoint to ask: set litellm_url= in the controller's machines/<name>.conf"))


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

    def test_a_copy_whose_refresh_token_has_expired_is_bad_and_asks_nobody(self):
        dead = json.loads(login(expires_ms=999000))
        dead["claudeAiOauth"]["refreshTokenExpiresAt"] = (1000 - 3 * 86400) * 1000
        got = self.probe("claude-login", json.dumps(dead), {}, now=1000.0)
        self.assertEqual((got, self.http.asked), ((BAD, "its refresh token expired 3 day(s) ago, so nothing can refresh it: log in to Claude "
                                                        "again, then `wk key set claude-login`"), []))
        dead["claudeAiOauth"]["refreshTokenExpiresAt"] = 1001000
        self.assertEqual(self.verdict("claude-login", json.dumps(dead), {}, now=1000.0), UNKNOWN)


class TestTailnet(Rules):
    def test_an_auth_key_is_checked_by_shape_and_cannot_be_probed(self):
        self.checks("tailnet-auth-key", ["tskey-auth-abc"], ["tskey-api-abc", "tskey-auth-a b", ""])
        self.assertEqual(self.verdict("tailnet-auth-key", "tskey-auth-abc", {}), UNKNOWN)


    def test_an_api_token_is_checked_by_shape_and_by_listing_the_devices(self):
        self.checks("tailnet-api-key", ["tskey-api-abc"], ["tskey-auth-abc", "tskey-api-a b", ""])
        devices = ("GET", "https://api.tailscale.com/api/v2/tailnet/-/devices")
        self.assertEqual(self.probe("tailnet-api-key", "tskey-api-abc", {devices: (200, {"devices": [{}, {}]})}),
                         (OK, "Tailscale accepts it, and it lists 2 device(s)"))
        self.assertEqual(self.http.asked[0][2], {"Authorization": "Bearer tskey-api-abc"})
        self.assertEqual([self.verdict("tailnet-api-key", "tskey-api-abc", {devices: a}) for a in ((401, {}), (403, {}), (500, {}), DOWN)],
                         [BAD, BAD, UNKNOWN, UNKNOWN])


class TestInstall(Rules):
    def test_what_only_an_install_can_try_is_checked_by_shape_and_never_probed(self):
        self.checks("wifi-ssid", ["Some Network"], ["", "two\nlines"])
        self.checks("wifi-psk", ["8 chars!", "f" * 64], ["short", "f" * 65, "eight or\nmore"])
        self.checks("bmc/lin/user", ["admin"], ["", "a\nb"])
        self.checks("sudo-password", ["1"], ["", "a\nb"])
        for name in ("wifi-ssid", "wifi-psk", "sudo-password", "bmc/lin/user", "bmc/lin/password"):
            self.assertEqual(self.probe(name, "any value", {}),
                             (UNKNOWN, "it is first used by `wk machine install`, whose --dry-run asks the store and the BMC"))
        self.assertEqual(self.http.asked, [])

    def test_a_bmc_login_is_named_for_its_machine(self):
        self.assertEqual((rules.find("bmc/lin/password").name, rules.find("bmc/lin/user").what),
                         ("bmc/lin/password", "one half of the login of a server's BMC, which `wk machine install` installs it through"))
        for name in ("bmc/lin", "bmc/lin/pin", "bmc/Lin/user", "bmc//user"):
            with self.assertRaises(Refused):
                rules.find(name)


