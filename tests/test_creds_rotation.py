"""`wk key rotate <server>` on the controller: a new writable deploy key per fork, the server's older ones deleted, the
private halves printed as JSON. GitHub is scripted; the keys are made by a scripted ssh-keygen."""

import json
import unittest

from tests.credsworld import TOKEN, Http
from tests.world import World
from wk.core.fakes import Killed
from wk.core.machine import Result
from wk.core.vars import VARS
from wk.core.errors import Refused
from wk.creds import net, rotation

GH = "https://api.github.com/repos/"
FORKS = VARS["wk_forks"]
TITLE = "wk lin 1970-01-01T00:16:40Z"


def make(token=TOKEN):
    w = World(here="vault")
    if token:
        w.secrets.values["github-token"] = token
    vault, made = w.machines["vault"], []

    def keygen(call):
        made.append(call.argv)
        n = len(made)
        vault.files[call.argv[-1]] = "private %d\n" % n
        vault.files[call.argv[-1] + ".pub"] = "ssh-ed25519 PUB%d %s\n" % (n, call.argv[-3])
    vault.on(("mktemp", "-d"), "/tmp/wk.s\n")
    vault.on(("ssh-keygen",), keygen)
    vault.on(("rm", "-rf"), lambda call: [vault.files.pop(p) for p in list(vault.files) if p.startswith(call.argv[-1] + "/")] and None)
    return w, made


def github(listed=None, posted=(201, {"id": 9})):
    """Each fork answers a POST with key 9 and lists `listed` beside it."""
    answers = {}
    for fork in FORKS:
        answers[("POST", GH + fork + "/keys")] = posted
        answers[("GET", GH + fork + "/keys?per_page=100")] = (200, (listed or []) + [{"id": 9, "title": TITLE}])
        for old in listed or []:
            answers[("DELETE", "%s%s/keys/%d" % (GH, fork, old["id"]))] = (204, b"")
    return Http(answers)


OLD = [{"id": 1, "title": "wk lin 1970-01-01T00:00:00Z"}, {"id": 2, "title": "wk linux 1970-01-01T00:00:00Z"},
       {"id": 3, "title": "laptop"}, {"id": 4, "title": "wk mac 1970-01-01T00:00:00Z"}]


class TestRotate(unittest.TestCase):
    def rotate(self, http, *words, w=None):
        self.w, self.made = w or make()
        with http.patch():
            return self.w.wk("key", "rotate", "lin", *(words or ("--yes",)))

    def test_each_fork_gets_a_new_writable_key_titled_for_the_server_and_the_private_halves_are_printed(self):
        http = github()
        r = self.rotate(http)
        self.assertEqual((r.code, json.loads(r.out)), (0, {fork: "private %d\n" % (n + 1) for n, fork in enumerate(FORKS)}))
        posts = [(url, json.loads(body)) for method, url, _, body in http.asked if method == "POST"]
        self.assertEqual(posts, [(GH + fork + "/keys", {"title": TITLE, "key": "ssh-ed25519 PUB%d %s" % (n + 1, TITLE), "read_only": False})
                                 for n, fork in enumerate(FORKS)])
        self.assertEqual(http.asked[0][2]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(self.made[0], ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", TITLE, "-f", "/tmp/wk.s/key"))

    def test_every_older_key_of_this_server_goes_and_no_other(self):
        http = github(OLD)
        r = self.rotate(http)
        self.assertEqual(r.code, 0)
        self.assertEqual([url for method, url, _, _ in http.asked if method == "DELETE"], [GH + fork + "/keys/1" for fork in FORKS])

    def test_no_key_lingers_on_the_controller_and_none_is_in_an_argv_or_a_log(self):
        r = self.rotate(github())
        self.assertEqual(self.w.machines["vault"].files, {})
        self.assertNotIn("private", r.err + "".join(r.calls) + repr([c.argv for c in self.w.machines["vault"].calls]))
        self.assertNotIn(TOKEN, r.out + r.err)

    def test_a_run_killed_after_its_post_leaves_a_key_the_next_run_deletes(self):
        http = github()
        for fork in FORKS:
            http.answers[("GET", GH + fork + "/keys?per_page=100")] = Killed()
        with self.assertRaises(Killed):
            self.rotate(http)
        again = github([{"id": 7, "title": TITLE}])
        self.assertEqual(self.rotate(again).code, 0)
        self.assertEqual([url for method, url, _, _ in again.asked if method == "DELETE"], [GH + fork + "/keys/7" for fork in FORKS])

    def test_a_dry_run_makes_keys_but_registers_and_deletes_nothing(self):
        http = github(OLD)
        r = self.rotate(http, "--dry-run", "--yes")
        self.assertEqual((r.code, [method for method, _, _, _ in http.asked]), (0, ["GET"] * len(FORKS)))
        self.assertEqual(r.plan[:2], ["[github] POST /repos/%s/keys %s" % (FORKS[0], TITLE),
                                      "[github] DELETE /repos/%s/keys/1 (wk lin 1970-01-01T00:00:00Z)" % FORKS[0]])

    def test_a_refusal_from_github_names_the_request_and_the_token(self):
        for posted, said in (((403, {"message": "Resource not accessible"}), "answered HTTP 403: Resource not accessible"),
                             (net.NoAnswer("timed out"), "did not answer (timed out)")):
            r = self.rotate(github(posted=posted))
            self.assertEqual((r.code, r.out), (4, ""))
            self.assertIn("error: POST /repos/%s/keys %s\n    github-token must be a classic token with public_repo of the fork's owner"
                          % (FORKS[0], said), r.err)

    def test_without_github_token_nothing_is_made(self):
        r = self.rotate(Http({}), w=make(token=None))
        self.assertEqual((r.code, self.made), (1, []))
        self.assertIn("error: the forks' deploy keys are managed with github-token, and there is none: `wk key set github-token`", r.err)

    def test_a_failed_ssh_keygen_is_refused_and_its_scratch_directory_removed(self):
        w = make()
        w[0].machines["vault"].on(("ssh-keygen",), Result(1, "", "no entropy\n"))
        r = self.rotate(Http({}), w=w)
        self.assertEqual(r.code, 1)
        self.assertIn("error: ssh-keygen failed on vault (exit 1): no entropy", r.err)
        self.assertEqual(w[0].machines["vault"].calls[-1].argv, ("rm", "-rf", "/tmp/wk.s"))

    def test_it_revokes_keys_so_it_asks_and_without_a_terminal_or_yes_makes_and_asks_github_nothing(self):
        http = github(OLD)
        r = self.rotate(http, "--dry-run")
        self.assertIn("would ask: replace lin's push keys, revoking its older deploy keys on %s? [y/N]" % ", ".join(FORKS), r.err)
        r = self.rotate(http, "--quiet")
        self.assertEqual((r.code, r.out, self.made), (4, "", []))
        self.assertIn("not asked: there is no terminal\n    re-run in a terminal, or pass --yes", r.err)
        self.assertEqual([method for method, _, _, _ in http.asked], ["GET"] * len(FORKS))

    def test_only_a_server_is_rotated_and_only_on_the_controller(self):
        self.assertIn("error: vault holds no workspace: it is the controller", make()[0].wk("key", "rotate", "vault").err)
        self.assertIn("error: wk key rotate runs on the controller (vault), and this is lin", make()[0].wk("key", "rotate", "lin", on="lin").err)


class TestRevoke(unittest.TestCase):
    def test_every_key_of_the_server_goes_from_every_fork_and_no_other_is_touched(self):
        w, _ = make()
        listed = OLD + [{"id": 9, "title": TITLE}]
        http = Http(dict([(("GET", GH + fork + "/keys?per_page=100"), (200, listed)) for fork in FORKS]
                         + [(("DELETE", "%s%s/keys/%d" % (GH, fork, n)), (204, b"")) for fork in FORKS for n in (1, 9)]))
        with http.patch():
            rotation.revoke(w.inv("vault"), "lin")
        self.assertEqual([(method, url) for method, url, _, _ in http.asked if method != "GET"],
                         [("DELETE", GH + fork + "/keys/%d" % n) for fork in FORKS for n in (1, 9)])

    def test_without_github_token_nothing_is_asked(self):
        w, _ = make(token=None)
        http = github(OLD)
        with http.patch(), self.assertRaisesRegex(Refused, "managed with github-token, and there is none"):
            rotation.revoke(w.inv("vault"), "lin")
        self.assertEqual(http.asked, [])
