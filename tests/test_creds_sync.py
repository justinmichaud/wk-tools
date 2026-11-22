import json
import unittest

from tests.credsworld import CA_PEM, MODELS, TOKEN, URL, Http, private_key, world
from tests.killpoints import converges, dry_run_is_the_plan
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result
from wk.creds import deliver, hooks, injector, net

S = "/home/u/wk/secrets"
ANSWERS = {MODELS: (200, {"data": [{"id": "big"}, {"id": "small"}]})}
PATHS = [S + "/deploy/Me--WebKit", S + "/ssh_config", S + "/env.sh", "/home/u/wk/claude-login/.credentials.json",
         "/home/u/wk/injector-ca.pem", "/home/u/.pi/agent/models.json", "/home/u/.ssh/config"]


def full():
    return world(github_user="me", github_token=TOKEN, bugzilla_user="me@example.test", bugzilla_password="it's",
                 litellm_key="sk-1", claude_login="never delivered", tailnet_auth_key="tskey-auth-never", **{"deploy-key/Me/WebKit": private_key()})


class Synced(unittest.TestCase):
    def sync(self, w, *words, answers=ANSWERS):
        with Http(answers).patch():
            return w.wk("key", "sync", *words)


class TestKeySync(Synced):
    def test_each_item_is_written_in_order_after_the_digests_are_asked_for(self):
        w = full()
        r = self.sync(w, "lin")
        self.assertEqual(r.code, 0, r.err)
        self.assertTrue(r.calls[0].startswith("[lin] python3 -I -c "), r.calls[0])
        self.assertEqual(r.effects, ["[lin] install -d -m 700 %s %s/deploy" % (S, S)] + ["[lin] write " + p for p in PATHS + ["/home/u/.bashrc"]])
        self.assertEqual(r.out.splitlines(), ["lin          changed    " + p for p in PATHS + ["/home/u/.bashrc"]])

    def test_every_secret_is_under_secrets_with_owner_only_modes(self):
        w = full()
        self.sync(w, "lin")
        lin = w.machines["lin"]
        self.assertEqual({p: lin.modes[p] for p in lin.files},
                         dict({p: "600" for p in PATHS}, **{"/home/u/wk/injector-ca.pem": "644", "/home/u/.bashrc": "644"}))
        self.assertEqual(lin.dirs, {S: "700", S + "/deploy": "700"})
        for path, text in lin.files.items():
            if not path.startswith(S + "/"):
                for secret in (TOKEN, "it's", private_key()):
                    self.assertNotIn(secret, text, path)

    def test_env_sh_exports_what_git_webkit_and_gh_read_and_the_push_alias(self):
        w = full()
        self.sync(w, "lin")
        self.assertEqual(w.machines["lin"].files[S + "/env.sh"].splitlines()[1:],
                         ["export GITHUB_COM_USERNAME=me", "export GITHUB_COM_TOKEN=" + TOKEN, "export GH_TOKEN=" + TOKEN,
                          "export BUGS_WEBKIT_ORG_USERNAME=me@example.test", "export BUGS_WEBKIT_ORG_PASSWORD='it'\"'\"'s'",
                          "export GIT_CONFIG_KEY_0=url.git@github-me-webkit:Me/WebKit.git.pushInsteadOf",
                          "export GIT_CONFIG_VALUE_0=https://github.com/Me/WebKit.git", "export GIT_CONFIG_COUNT=1"])

    def test_each_repository_has_an_ssh_alias_offering_its_key_alone(self):
        w = full()
        self.sync(w, "lin")
        lin = w.machines["lin"]
        self.assertEqual(lin.files[S + "/ssh_config"].splitlines()[1:],
                         ["Host github-me-webkit", "    HostName github.com", "    User git", "    IdentityFile %s/deploy/Me--WebKit" % S,
                          "    IdentitiesOnly yes"])
        self.assertEqual(lin.files[S + "/deploy/Me--WebKit"], private_key() + "\n")

    def test_ssh_and_the_login_shell_are_pointed_at_secrets_by_one_line_each_keeping_what_was_there(self):
        w = full()
        w.machines["mac"].files.update({"/home/u/.ssh/config": "Host x\n", "/home/u/.zshrc": "alias a=b"})
        self.sync(w, "mac")
        mac = w.machines["mac"].files
        self.assertEqual(mac["/home/u/.ssh/config"], "Include %s/ssh_config\nHost x\n" % S)
        self.assertEqual(mac["/home/u/.zshrc"], "alias a=b\n[ -r %s/env.sh ] && . %s/env.sh\n" % (S, S))
        self.assertNotIn("/home/u/.bashrc", mac)

    def test_an_agent_session_is_given_the_ca_the_placeholder_and_pis_models(self):
        w = full()
        self.sync(w, "lin")
        lin = w.machines["lin"].files
        self.assertEqual((lin["/home/u/wk/injector-ca.pem"], lin["/home/u/wk/claude-login/.credentials.json"]), (CA_PEM, injector.placeholder()))
        self.assertEqual(json.loads(lin["/home/u/.pi/agent/models.json"]),
                         {"providers": {"litellm": {"baseUrl": URL, "api": "openai-completions", "apiKey": "sk-1", "models": [{"id": "big"}, {"id": "small"}]}}})

    def test_the_real_login_and_the_tailnet_key_are_delivered_nowhere(self):
        w = full()
        self.sync(w)
        held = repr([m.files for m in w.machines.values()])
        self.assertNotIn("never delivered", held)
        self.assertNotIn("tskey-auth-never", held)

    def test_a_second_run_changes_nothing(self):
        w = full()
        self.sync(w, "lin")
        r = self.sync(w, "lin")
        self.assertEqual((r.code, r.effects), (0, []))
        self.assertEqual(r.out.splitlines(), ["lin          unchanged  " + p for p in PATHS + ["/home/u/.bashrc"]])

    def test_only_what_differs_by_digest_or_mode_is_written_again(self):
        w = full()
        self.sync(w, "lin")
        lin = w.machines["lin"]
        lin.files[S + "/env.sh"] += "tampered\n"
        lin.modes[S + "/ssh_config"] = "644"
        lin.dirs[S] = "755"
        r = self.sync(w, "lin")
        self.assertEqual(r.effects, ["[lin] install -d -m 700 %s %s/deploy" % (S, S), "[lin] write %s/ssh_config" % S, "[lin] write %s/env.sh" % S])
        self.assertEqual((lin.modes[S + "/ssh_config"], lin.dirs[S]), ("600", "700"))

    def test_no_secret_is_read_back_or_put_in_an_argv(self):
        w = full()
        self.sync(w)
        r = self.sync(w)
        argvs = repr([c.argv for m in w.machines.values() for c in m.calls]) + "\n".join(r.calls) + r.out + r.err
        for secret in (TOKEN, "it's", private_key(), "sk-1"):
            self.assertNotIn(secret, argvs)
        self.assertEqual([c.argv[0] for c in w.machines["lin"].calls], ["python3", "install", "python3"])

    def test_every_server_of_yours_is_synced_and_a_borrowed_one_is_skipped_saying_so(self):
        w = full()
        r = self.sync(w)
        self.assertEqual(r.code, 0)
        self.assertIn("box          skipped    a borrowed machine holds no credential: ssh agent forwarding makes `git push` work there\n", r.out)
        self.assertEqual(({e.split()[0] for e in r.effects}, w.machines["box"].files, w.machines["box"].calls), ({"[lin]", "[mac]"}, {}, []))
        self.assertEqual(self.sync(w, "box").out.split()[:2], ["box", "skipped"])

    def test_the_credential_server_and_the_client_are_not_servers_to_sync(self):
        self.assertIn("error: vault holds no workspace", self.sync(full(), "vault").err)

    def test_a_server_that_does_not_answer_is_unknown_and_the_rest_are_still_synced(self):
        w = full()
        w.machines["lin"].down = True
        r = self.sync(w)
        self.assertEqual(r.code, 4)
        self.assertIn("lin          unknown    it did not answer\n", r.out)
        self.assertIn("error: not everything is delivered: lin did not answer\n    it is left as it was; run `wk key sync` again\n", r.err)
        self.assertIn(S + "/env.sh", w.machines["mac"].files)

    def test_a_server_whose_digests_cannot_be_read_is_refused_and_nothing_is_written(self):
        for answer in (Result(1, "", "no python3"), "not json"):
            w = full()
            w.machines["lin"].on(("python3", "-I", "-c"), answer)
            r = self.sync(w, "lin")
            self.assertEqual((r.code, r.effects), (4, []))
            self.assertIn("error: lin could not say what it holds (python3 -I failed there), so nothing is delivered to it", r.err)
        with Http(ANSWERS).patch():
            r = w.wk("key", "sync", "lin", "--force")
        self.assertEqual((r.code, len(r.effects)), (0, 1 + len(PATHS) + 1))

    def test_what_the_store_lacks_is_not_delivered_and_is_said(self):
        w = world()
        r = self.sync(w, "lin", answers={})
        self.assertEqual(r.code, 0)
        self.assertIn("warning: github-token is not in the store, so it is not delivered: `wk key set github-token`\n", r.err)
        lin = w.machines["lin"].files
        self.assertEqual((len(lin[S + "/env.sh"].splitlines()), len(lin[S + "/ssh_config"].splitlines())), (1, 1))
        self.assertNotIn("/home/u/.pi/agent/models.json", lin)

    def test_without_an_injector_the_ca_is_not_delivered_and_is_said(self):
        w = full()
        del w.machines["vault"].files["/home/u/wk/injector/ca.pem"]
        r = self.sync(w, "lin")
        self.assertEqual(r.code, 0)
        self.assertIn("warning: vault has no injector yet, so its CA is not delivered: `wk machine setup vault`\n", r.err)
        self.assertNotIn("/home/u/wk/injector-ca.pem", w.machines["lin"].files)

    def test_a_model_endpoint_that_does_not_answer_leaves_pis_models_as_they_were_and_exits_4(self):
        for answer, why in ((net.NoAnswer("down"), "did not answer (down)"), ((502, b"<html>"), "answered HTTP 502")):
            w = full()
            w.machines["lin"].files["/home/u/.pi/agent/models.json"] = "as it was"
            r = self.sync(w, "lin", answers={MODELS: answer})
            self.assertEqual((r.code, w.machines["lin"].files["/home/u/.pi/agent/models.json"]), (4, "as it was"))
            self.assertIn("error: not everything is delivered: pi's models.json (GET %s/models %s)" % (URL, why), r.err)
            self.assertIn(S + "/env.sh", w.machines["lin"].files)

    def test_without_a_litellm_url_pis_models_are_not_delivered_and_is_said(self):
        w = full()
        w.confs["desk"] = type(w.confs["desk"])("desk", "client")
        r = self.sync(w, "lin", answers={})
        self.assertEqual(r.code, 0)
        self.assertIn("warning: no litellm_url= in machines/desk.conf, so pi's models.json is not delivered\n", r.err)

    def test_killed_after_any_effect_and_run_again_it_converges(self):
        with Http(ANSWERS).patch():
            converges(self, full, "key", "sync")

    def test_a_dry_run_is_the_plan(self):
        with Http(ANSWERS).patch():
            dry_run_is_the_plan(self, full, "key", "sync")


class TestSetupStage(Synced):
    def converge(self, w, name, answers=ANSWERS):
        with Http(answers).patch():
            return w.wk("converge", name)

    def test_a_server_of_yours_has_one_stage_at_60_that_delivers_and_reports_change(self):
        w = full()
        self.assertEqual(self.converge(w, "lin").out.splitlines()[len(PATHS) + 1], "60 credentials changed")
        self.assertIn(S + "/env.sh", w.machines["lin"].files)
        self.assertIn("60 credentials unchanged\n", self.converge(w, "lin").out)

    def test_the_stage_fails_while_something_is_unknown(self):
        r = self.converge(full(), "lin", {MODELS: net.NoAnswer("down")})
        self.assertEqual(r.code, 4)
        self.assertIn("error: not everything is delivered: pi's models.json", r.err)

    def test_a_borrowed_machine_and_the_client_have_no_stage_and_no_row(self):
        self.assertEqual([self.converge(full(), name).out for name in ("box", "desk")], ["", ""])


class TestDoctor(Synced):
    def rows(self, w):
        return [line.split("|") for line in w.wk("converge", "lin").out.splitlines() if "|" in line]

    def test_each_delivered_item_is_ok_when_present_with_its_mode(self):
        w = full()
        with Http(ANSWERS).patch():
            rows = self.rows(w)
        self.assertEqual([(r[0], r[1]) for r in rows],
                         [(p, "ok") for p in (S, S + "/deploy", S + "/env.sh", S + "/ssh_config", "/home/u/wk/claude-login/.credentials.json",
                                              "/home/u/wk/injector-ca.pem", "/home/u/.pi/agent/models.json")])

    def doctor(self, w):
        inv = Inv("/", {}, Fleet(lambda: w.confs, "desk", w.machines.get, w.machines["desk"], workers=1), w.clock, None, {}, w.seams)
        return hooks.doctor(inv, w.confs["lin"], None)

    def test_an_absent_item_or_a_wrong_mode_is_missing_with_the_remedy(self):
        w = full()
        self.sync(w, "lin")
        del w.machines["lin"].files[S + "/env.sh"]
        w.machines["lin"].dirs[S] = "755"
        rows = {r.name: (r.status, r.detail, r.remedy) for r in self.doctor(w)}
        self.assertEqual((rows[S + "/env.sh"], rows[S], rows[S + "/ssh_config"]),
                         (("missing", "absent", "wk key sync lin"), ("missing", "mode 755, not 700", "wk key sync lin"), ("ok", "", "")))

    def test_a_server_that_cannot_say_what_it_holds_is_unknown_never_ok(self):
        w = full()
        w.machines["lin"].on(("python3", "-I", "-c"), Result(1))
        self.assertEqual([(r.name, r.status, r.remedy) for r in self.doctor(w)], [("credentials", "unknown", "wk key sync lin")])

    def test_a_workspace_has_no_credential_row(self):
        w = full()
        self.assertEqual(hooks.doctor(None, w.confs["lin"], w.workspace("x")), [])
        self.assertEqual(deliver.RC, {"linux": ".bashrc", "mac": ".zshrc"})
