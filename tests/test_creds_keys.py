import unittest
from unittest import mock

from tests.credsworld import MODELS, TOKEN, Http, private_key, world
from wk.creds import net

GH = "https://api.github.com"
GOOD = {("GET", GH + "/user"): (200, {"login": "me"}), ("POST", GH + "/repos/WebKit/WebKit/pulls"): (422, {})}


class TestKeySet(unittest.TestCase):
    def set(self, name, value, answers, w=None, **how):
        self.w = w or world()
        self.http = Http(answers)
        with self.http.patch():
            return self.w.wk("key", "set", name, stdin=value, **how)

    def test_a_value_its_issuer_accepts_is_stored_from_stdin(self):
        r = self.set("github-token", TOKEN + "\n", GOOD)
        self.assertEqual((r.code, r.effects, self.w.secrets.values), (0, ["[store] secret put github-token"], {"github-token": TOKEN}))
        self.assertIn("==> stored github-token: as me, and it can open a pull request on WebKit/WebKit; `wk key sync` delivers it\n", r.err)

    def test_the_value_is_never_in_an_argv_a_log_line_or_the_output(self):
        r = self.set("github-token", TOKEN, GOOD)
        seen = r.out + r.err + "\n".join(r.calls) + repr([c for m in self.w.machines.values() for c in m.calls])
        self.assertNotIn(TOKEN, seen)

    def test_at_a_terminal_it_is_asked_for_without_echo(self):
        with mock.patch("getpass.getpass", return_value=TOKEN) as asked:
            r = self.set("github-token", "", GOOD, tty=True)
        self.assertEqual((r.code, self.w.secrets.values["github-token"]), (0, TOKEN))
        self.assertRegex(asked.call_args[0][0], r"^github-token \(a classic GitHub personal access token")

    def test_a_value_of_the_wrong_shape_is_refused_unstored_and_unasked(self):
        r = self.set("github-token", "hunter2-not-a-token", {})
        self.assertEqual((r.code, r.effects, self.http.asked), (1, [], []))
        self.assertIn("error: that is not a classic GitHub personal access token (scope public_repo), for git-webkit and gh: "
                      "a classic GitHub personal access token starts `ghp_`\n    nothing is stored\n", r.err)
        self.assertNotIn("hunter2", r.err)

    def test_a_value_its_issuer_refuses_is_not_stored(self):
        r = self.set("github-token", TOKEN, {("GET", GH + "/user"): (401, {})})
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("error: github-token: GitHub does not accept this token (HTTP 401)", r.err)

    def test_force_stores_a_value_its_issuer_refuses_and_says_so_again_at_exit(self):
        w = world()
        with Http({("GET", GH + "/user"): (401, {})}).patch():
            r = w.wk("key", "set", "github-token", "--force", stdin=TOKEN)
        self.assertEqual((r.code, w.secrets.values), (0, {"github-token": TOKEN}))
        self.assertIn("warning: this command was forced past: github-token: GitHub does not accept this token (HTTP 401)", r.err)

    def test_a_value_nobody_could_verify_is_stored_and_said_to_be_unverified(self):
        r = self.set("github-token", TOKEN, {("GET", GH + "/user"): net.NoAnswer("down")})
        self.assertEqual((r.code, self.w.secrets.values), (0, {"github-token": TOKEN}))
        self.assertIn("warning: github-token is stored unverified: GET /user did not answer (down)\n", r.err)
        self.assertIn("==> stored github-token; `wk key sync` delivers it\n", r.err)

    def test_a_multi_line_value_is_piped_in(self):
        r = self.set("deploy-key/o/r", private_key() + "\n", {})
        self.assertEqual((r.code, self.w.secrets.values), (0, {"deploy-key/o/r": private_key()}))

    def test_the_probe_sees_the_store_the_clock_and_the_clients_endpoint(self):
        r = self.set("litellm-key", "sk-1", {MODELS: (200, {"data": []})})
        self.assertEqual((r.code, self.http.asked[0][:2]), (0, MODELS))

    def test_an_unknown_name_is_refused_before_anything_is_read(self):
        r = self.set("bogus", TOKEN, {})
        self.assertEqual(r.code, 1)
        self.assertIn("error: no credential is named bogus", r.err)

    def test_it_runs_on_the_client_only(self):
        r = self.set("github-token", TOKEN, {}, on="lin")
        self.assertIn("error: wk key set runs on the client", r.err)

    def test_a_dry_run_checks_the_value_and_stores_nothing(self):
        wet = self.set("github-token", TOKEN, GOOD)
        w = world()
        with Http(GOOD).patch():
            dry = w.wk("--dry-run", "key", "set", "github-token", stdin=TOKEN)
        self.assertEqual((dry.code, dry.plan, dry.effects, w.secrets.values), (0, wet.effects, [], {}))


class TestKeyCheck(unittest.TestCase):
    def test_one_row_per_credential_present_or_absent_with_the_probes_verdict(self):
        w = world(github_user="me", github_token=TOKEN, tailnet_auth_key="tskey-auth-a", **{"deploy-key/o/r": private_key(), "bmc/lin/user": "admin"})
        answers = dict(GOOD)
        answers[("GET", GH + "/users/me")] = (200, {})
        answers[("GET", GH + "/repos/o/r/keys?per_page=100")] = net.NoAnswer("down")
        with Http(answers).patch():
            r = w.wk("key", "check")
        rows = [line.split(None, 3) for line in r.out.splitlines()]
        self.assertEqual((r.code, r.effects, [row[:3] for row in rows]),
                         (0, [], [["github-user", "present", "ok"], ["github-token", "present", "ok"], ["bugzilla-user", "absent", "-"],
                                  ["bugzilla-password", "absent", "-"], ["litellm-key", "absent", "-"], ["claude-login", "absent", "-"],
                                  ["tailnet-auth-key", "present", "unknown"], ["tailnet-api-key", "absent", "-"], ["wifi-ssid", "absent", "-"],
                                  ["wifi-psk", "absent", "-"], ["sudo-password", "absent", "-"], ["bmc/lin/user", "present", "unknown"], ["deploy-key/o/r", "present", "unknown"]]))
        self.assertEqual(rows[2][3], "your https://bugs.webkit.org login, an email address: `wk key set bugzilla-user`")
        self.assertEqual(rows[12][3], "GET /repos/o/r/keys did not answer (down)")
        self.assertNotIn(TOKEN, r.out + r.err)

    def test_a_bad_credential_exits_1(self):
        w = world(github_token=TOKEN)
        with Http({("GET", GH + "/user"): (401, {})}).patch():
            r = w.wk("key", "check")
        self.assertEqual(r.code, 1)
        self.assertIn("github-token                 present  bad      GitHub does not accept this token", r.out)

    def test_key_alone_is_key_check(self):
        self.assertEqual(world().wk("key").out, world().wk("key", "check").out)
