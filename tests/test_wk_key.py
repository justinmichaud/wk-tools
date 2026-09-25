"""lib/wk/key/ against a fake machine: the election across workstations, the deploy keys GitHub holds, `wk key
check` per machine, a run killed after any effect, and a dry run that is the wet run's plan.

The fake is tests/test_wk_secrets.py's machine plus a toy rulebook in lib/credcheck.py's place, a GitHub that keeps
each repository's deploy keys, and peer workstations whose `wk key` answers from what they hold. A private key is
`KEY:<tag>`, its public half `ssh-ed25519 AAAA<tag> wk`, and a stored credential fingerprints as `fp-<value>`.

Run: python3 tests/run.py --unit -k test_wk_key
"""
import contextlib
import io
import os
import shlex
import unittest
from unittest import mock

from tests.killpoints import converges
from tests.test_wk_secrets import ROOT, SECRETFILE, SecretsTest, World

from wk import act, decl, dispatch  # noqa: E402
from wk.key import cli, common  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402

CREDCHECK = os.path.join(ROOT, "lib", "credcheck.py")
NAMES = ("github-pat", "claude-login", "litellm", "ntfy", "deploy-key")
GOOD, OTHER_GOOD, STALE, UNJUDGED = "good-token", "good-token-2", "stale-token", "offline-token"
TOPIC = "topic-minted-here"
LOGIN_OK = "ok\tscopes: user:inference user:profile"
REPOS = {"fork": "justinmichaud/WebKit", "forkwpe": "justinmichaud/WPEWebKit"}


def pub_of(k):
    return "ssh-ed25519 AAAA%s wk" % k.strip()[4:]


def judge(name, value, path, ev):
    v = value.strip()
    if name == "deploy-key":
        if "Hi " in ev.get("ssh", "") and ev.get("read_only") == "false":
            return "ok\tpushes to %s alone" % ev.get("repos", "")
        return "bad\tGitHub does not take it with write access\n    fix: wk key deploy -- then: wk key deploy"
    if not v and path:
        return "absent\tnothing stored -- wk key set " + name
    if name == "github-pat":
        if v.startswith("good"):
            return "ok\tit reaches exactly the forks"
        if v == UNJUDGED:
            return "unverified\tGitHub could not be reached"
        return ("bad\tGitHub refuses it\n    fix: https://github.com/settings/tokens/new -- then: wk key set github-pat%s"
                % (" --replace" if path else ""))
    if name == "litellm":
        return "ok\tai.igalia.com accepts it" if v.startswith("sk-") else "bad\tnot a virtual key"
    if name == "ntfy":
        return "ok\tntfy.sh serves it" if v.startswith("topic") else "bad\tnot one word of letters"
    return LOGIN_OK if v.startswith("{") else "bad\tnot a login"


class Peer:
    def __init__(self, name, creds=None, keys=None, login=LOGIN_OK, answers=True):
        self.name, self.creds, self.keys, self.login, self.answers = name, dict(creds or {}), dict(keys or {}), login, answers
        self.asked = []

    def state(self):
        return sorted(self.creds.items()), sorted(self.keys.items()), self.login

    def verdict(self, name):
        if name == common.LOGIN:
            return self.login
        v = self.creds.get(name, "")
        return judge(name, v, "path", {}) + ("\n    fingerprint: fp-%s" % v if v else "")


class FakeTarget:
    kind = "remote"

    def __init__(self, name, world, peer):
        self.name, self.machine, self.peer = name, world, peer

    def has_wk(self):
        return True

    def wk_cmd(self, args, env):
        return "PEER %s %s" % (self.name, " ".join(shlex.quote(a) for a in args))


class FakeRegistry:
    def __init__(self, world, boxes=()):
        self.world, self.boxes = world, list(boxes)

    def machines(self):
        return sorted(list(self.world.peers) + self.boxes)

    def load(self, name):
        return FakeTarget(name, self.world, name in self.world.peers)


class KeyWorld(World):
    def __init__(self, base, peers=()):
        super().__init__(base)
        self.peers = {p.name: p for p in peers}
        self.github = {r: {} for r in REPOS.values()}
        self.gen = {}
        self.claude = False
        self.react(["python3", CREDCHECK], self._credcheck)
        self.react(["python3", SECRETFILE, "present"], lambda a, f: Result(0 if f.files.get(a[3], "") else 1))
        self.react(["python3", SECRETFILE, "write"], self._write)
        self.react(["python3", SECRETFILE, "fingerprint"],
                   lambda a, f: Result(0, "fp-%s\n" % f.files[a[3]].strip() if f.files.get(a[3], "").strip() else ""))
        self.react(["ssh-keygen", "-q", "-t"], self._keygen)
        self.react(["ssh-keygen", "-y", "-f"],
                   lambda a, f: Result(0, pub_of(f.files[a[3]]) + "\n") if f.files.get(a[3], "").startswith("KEY:") else Result(1))
        self.react(["ssh-keygen", "-lf", "-"], lambda a, f: Result(0, "256 SHA256:%s wk (ED25519)\n" % f.last_input.split()[1][4:]))
        self.react(["ssh"], lambda a, f: Result(0, "", "Hi justinmichaud/WebKit! You've successfully authenticated\n"))
        self.react(["gh", "api"], self._gh)
        self.react(["hostname", "-s"], lambda a, f: Result(0, "here\n"))
        self.react(["rm"], self._rm)

    @property
    def fake(self):
        return self

    def state(self):
        return (dict(self.files), {n: p.state() for n, p in self.peers.items()},
                {r: sorted(k.values()) for r, k in self.github.items()})

    def holds(self, name, value):
        self._set_file(self.sec().cred_path(name), value + "\n")

    def keys(self, tags=None):
        for fork in REPOS:
            self._set_file(self.held + "/build_key_" + fork, "KEY:%s\n" % (tags or {}).get(fork, fork))
            self._set_file("%s/build_key_%s.pub" % (self.secrets_dir, fork), pub_of("KEY:" + (tags or {}).get(fork, fork)) + "\n")

    def register(self, tags=None):
        for fork, repo in REPOS.items():
            self.github[repo][str(len(self.github[repo]) + 1)] = pub_of("KEY:" + (tags or {}).get(fork, fork))

    def peer_acts(self):
        return [e[1][2] for e in self.effects if e[0] == "act" and e[1][:2] == ("sh", "-c") and e[1][2].startswith("PEER ")]

    def _credcheck(self, argv, f):
        verb = argv[2]
        if verb == "names":
            return Result(0, "\n".join(NAMES) + "\n")
        if verb == "minted":
            return Result(0, "ntfy\n")
        if verb == "mint":
            return Result(0, TOPIC + "\n")
        if verb == "rule":
            return Result(0, "what\tthe %s\nurl\thttps://example.invalid/\nremedy\tsubscribe to it\nneeds\tdo its job\n" % argv[3])
        rest, path, ev = argv[4:], "", {}
        while rest:
            flag, value, rest = rest[0], rest[1], rest[2:]
            if flag == "--path":
                path = value
            elif flag == "--evidence":
                ev[value.partition("=")[0]] = value.partition("=")[2]
            else:
                ev["repos"] = value
        return Result(0, judge(argv[3], f.last_input, path, ev) + "\n")

    def _write(self, argv, f):
        self._set_file(argv[3], f.last_input)
        return Result(0)

    def _keygen(self, argv, f):
        path = argv[argv.index("-f") + 1]
        fork = os.path.basename(path)[len("build_key_"):]
        self.gen[fork] = self.gen.get(fork, 0) + 1
        self._set_file(path, "KEY:%s-g%d\n" % (fork, self.gen[fork]))
        return Result(0)

    def _rm(self, argv, f):
        for p in argv[2:]:
            self._drop(p)
        return Result(0)

    def _gh(self, argv, f):
        a = argv[2:]
        if a[:2] == ["-X", "DELETE"]:
            repo, _, key_id = a[2][len("repos/"):].rpartition("/keys/")
            self.github[repo].pop(key_id, None)
            return Result(0)
        repo = a[0][len("repos/"):-len("/keys")]
        if "-f" in a:
            self.github[repo][str(len(self.github[repo]) + 100)] = a[a.index("-f") + 3][len("key="):]
            return Result(0)
        jq, keys = a[2], self.github[repo]
        if jq == ".[].key":
            return Result(0, "".join(k + "\n" for k in keys.values()))
        if "contains" in jq:
            b64 = jq.split('"')[1]
            return Result(0, "false\n" if any(b64 in k for k in keys.values()) else "")
        return Result(0, "".join(i + "\n" for i, k in keys.items() if common.TITLE in jq))

    def _sh(self, argv, f):
        line = argv[2]
        if line.startswith("command -v claude"):
            return Result(0 if self.claude else 1)
        if not line.startswith("PEER "):
            return super()._sh(argv, f)
        words = shlex.split(line)[1:]
        p, (verb, arg), inp = self.peers[words[0]], (words[2:] + ["", ""])[:2], self.last_input
        p.asked.append(" ".join(words[2:]))
        if not p.answers:
            return Result(255, "", "ssh: connect to host %s: Connection refused" % p.name)
        if verb == "verdict":
            return Result(0, p.verdict(arg) + "\n")
        if verb == "pub":
            return Result(0, pub_of(p.keys[arg]) + "\n") if arg in p.keys else Result(1)
        if verb == "sshtest":
            return Result(0, "Hi justinmichaud/WebKit!\n") if arg in p.keys else Result(1, "no key\n")
        if verb == "give":
            return Result(0, p.keys.get(arg) or p.creds.get(arg, ""))
        if verb == "adopt" and arg == common.LOGIN:
            p.login = LOGIN_OK
            return Result(0, LOGIN_OK + "\n")
        if verb == "adopt":
            p.keys[arg] = inp.rstrip("\n")
            return Result(0)
        if verb == "set":
            p.creds[arg] = inp.strip()
            return Result(0)
        return Result(1)


class KeyTest(SecretsTest):
    def world(self, *peers):
        self.w = KeyWorld(self.tmp, peers)
        return self.w

    def key(self, w=None, rotate=False, tty=False, typed="", boxes=()):
        w = w or self.w
        return cli.Key(ROOT, env=w.env, machine=w, reg=FakeRegistry(w, boxes), sec=w.sec(), tty=lambda: tty,
                       prompt=lambda *a: typed or None, out=io.StringIO(), rotate=rotate)

    def run_verb(self, verb, w=None, **kw):
        k = self.key(w, **kw)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                rc = getattr(k, verb)()
            except Refused as e:
                rc = e.status
        return rc, k.out.getvalue(), err.getvalue()

    def stored_on_stdin(self, name, value, typed=False):
        """Every argv the store ran and every stdin it was handed, for `value` set by name."""
        w = self.world()
        k = self.key(w, typed=value if typed else "")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = k.set(name) if typed else k.set(name, paste=True, value=value + "\n")
        self.assertEqual(0, rc)
        self.assertEqual(value + "\n", w.files[w.sec().cred_path(name)])
        return [" ".join(a) for a in w.argvs()], [i for _, i in w.inputs]

    def provisioned(self, *peers, pat=GOOD):
        w = self.world(*peers)
        w.keys()
        w.register()
        w.holds("github-pat", pat)
        w.holds("ntfy", TOPIC)
        w.holds("claude-login", "{login}")
        os.environ["WK_YES"] = "1"
        return w


class TestTheElection(KeyTest):
    """`key.election`: the credential its issuer accepts wins from whichever workstation holds it."""

    def test_a_peers_working_credential_is_taken_over_one_refused_here(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": GOOD, "ntfy": TOPIC}, keys={f: "KEY:" + f for f in REPOS}),
                             pat=STALE)
        self.run_verb("setup")
        self.assertEqual(GOOD + "\n", w.files[w.held + "/github-pat"])
        self.assertIn("give github-pat", w.peers["peerbox"].asked)
        self.assertFalse([a for a in w.peer_acts() if "github-pat" in a], "the winner was written back over itself")

    def test_a_tie_goes_to_this_machine(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": OTHER_GOOD, "ntfy": TOPIC}, keys={f: "KEY:" + f for f in REPOS}))
        self.run_verb("setup")
        self.assertEqual(GOOD + "\n", w.files[w.held + "/github-pat"])
        self.assertEqual(GOOD, w.peers["peerbox"].creds["github-pat"])

    def test_a_second_run_moves_nothing(self):
        w = self.provisioned(Peer("peerbox"))
        self.run_verb("setup")
        self.assertEqual(GOOD, w.peers["peerbox"].creds["github-pat"])
        before, told = w.state(), len(w.peer_acts())
        self.run_verb("setup")
        self.assertEqual(before, w.state())
        self.assertEqual(told, len(w.peer_acts()), "a fleet that agrees was written to again")

    def test_the_deploy_key_github_accepts_is_taken_from_the_peer_holding_it(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": GOOD, "ntfy": TOPIC}, keys={f: "KEY:peer-" + f for f in REPOS}))
        w.github = {r: {} for r in REPOS.values()}
        w.register({f: "peer-" + f for f in REPOS})
        self.run_verb("setup")
        self.assertEqual("KEY:peer-fork\n", w.files[w.held + "/build_key_fork"])
        self.assertEqual(pub_of("KEY:peer-fork") + "\n", w.files[w.secrets_dir + "/build_key_fork.pub"])
        self.assertFalse([a for a in w.peer_acts() if " adopt fork" in a])

    def test_one_nobody_could_judge_is_not_written_over_a_peers(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": UNJUDGED, "ntfy": TOPIC}, keys={f: "KEY:" + f for f in REPOS}),
                             pat=UNJUDGED)
        _, _, err = self.run_verb("setup")
        self.assertIn("github-pat: no workstation's could be judged", err)
        self.assertFalse([a for a in w.peer_acts() if "github-pat" in a])

    def test_a_declined_question_writes_to_no_peer(self):
        w = self.provisioned(Peer("peerbox"))
        del os.environ["WK_YES"]
        _, _, err = self.run_verb("setup")
        self.assertEqual([], w.peer_acts())
        self.assertIn("peerbox was left exactly as it is", err)

    def test_a_build_machine_is_asked_nothing(self):
        w = self.provisioned(Peer("peerbox"))
        rc, out, _ = self.run_verb("setup", boxes=("buildbox",))
        self.assertFalse([a for a in w.peer_acts() if "buildbox" in a])
        self.assertIn("buildbox", out.split("build machines:")[1].split("\n")[1])


class TestRegisterPerMachine(KeyTest):
    """`key.register_per_machine`: each fork's key is registered once under the one title, `check` reports each
    workstation in its own section, and a peer that did not answer reads differently from one holding no key."""

    def test_each_fork_is_registered_once_under_the_shared_title(self):
        w = self.world()
        w.keys()
        self.run_verb("deploy")
        adds = [e[1] for e in w.effects if e[0] == "act" and e[1][:2] == ("gh", "api") and "-f" in e[1]]
        self.assertEqual(sorted("repos/%s/keys" % r for r in REPOS.values()), sorted(a[2] for a in adds))
        self.assertTrue(all("title=" + common.TITLE in a for a in adds))
        n = len(w.effects)
        self.run_verb("deploy")
        self.assertFalse([e for e in w.effects[n:] if e[0] == "act" and e[1][:2] == ("gh", "api")])

    def test_a_refused_key_ensure_fails_the_forks_and_registers_none(self):
        k = self.key(self.world())
        registered = []
        k.ensure = lambda: act.die("no key could be made")
        k.fleet_fork = lambda fork, repo: registered.append(fork) or True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(k.converge_forks())
        self.assertEqual([], registered)

    def test_rotate_removes_the_old_key_from_github_and_mints_a_fresh_one(self):
        w = self.world()
        w.keys()
        w.register()
        os.environ["WK_YES"] = "1"
        self.run_verb("deploy", rotate=True)
        self.assertEqual("KEY:fork-g1\n", w.files[w.held + "/build_key_fork"])
        self.assertEqual([pub_of("KEY:fork-g1")], sorted(w.github[REPOS["fork"]].values()))

    def test_check_reports_each_workstation_in_its_own_section(self):
        self.provisioned(Peer("peerbox", keys={f: "KEY:" + f for f in REPOS}))
        _, out, _ = self.run_verb("check")
        here, peer = out.split("  here:\n")[1].split("  peerbox:\n")
        for repo in REPOS.values():
            self.assertRegex(here, r"ok\s+%s\s+pushes to" % repo)
            self.assertRegex(peer, r"ok\s+%s\s+%s" % (repo, common.SAME_AS_HERE))

    def test_a_peer_that_did_not_answer_reads_differently_from_one_holding_no_key(self):
        self.provisioned(Peer("empty"), Peer("gone", answers=False))
        rc, out, _ = self.run_verb("check")
        empty = out.split("  empty:\n")[1].split("  gone:\n")[0]
        gone = out.split("  gone:\n")[1].split("  credentials:\n")[0]
        self.assertRegex(empty, r"none\s+%s\s+no key for this fork" % REPOS["fork"])
        self.assertRegex(gone, r"\?\s+%s\s+gone did not answer" % REPOS["fork"])
        self.assertNotIn("no key", gone)
        self.assertEqual(1, rc)


class TestAValueTravelsOnStdin(KeyTest):
    def test_a_stored_or_fanned_out_credential_is_never_an_argument(self):
        w = self.provisioned(Peer("peerbox"))
        self.run_verb("setup")
        self.assertEqual(GOOD, w.peers["peerbox"].creds["github-pat"])
        for argv in w.argvs():
            self.assertNotIn(GOOD, " ".join(argv))
        self.assertIn(GOOD + "\n", [i for _, i in w.inputs])


class TestTheLoginCrossesAsATar(KeyTest):
    def test_a_login_made_here_is_adopted_there_whole(self):
        w = self.world()
        d = self.tmp + "/made"
        w._set_file(d + "/.credentials.json", "{login}\n")
        w._set_file(d + "/.claude.json", '{"oauthAccount": {}}\n')
        k = self.key()
        rc, line = k.login_adopt(k.login_pack(d).encode())
        rw = w.sec().store.agent_rw_dir()
        self.assertEqual((0, LOGIN_OK), (rc, line))
        self.assertEqual("{login}\n", w.files[rw + "/.credentials.json"])
        self.assertEqual('{"oauthAccount": {}}\n', w.files[rw + "/.claude.json"])

    def test_something_that_is_not_the_two_files_is_refused_and_nothing_moves(self):
        w = self.world()
        w.holds("claude-login", "{mine}")
        rc, line = self.key().login_adopt(b"not a tar")
        self.assertEqual(1, rc)
        self.assertIn("not a login bundle", line)
        self.assertEqual("{mine}\n", w.files[w.sec().cred_path("claude-login")])


class TestCrashOnlyAndDryRun(KeyTest):
    def fleet_world(self):
        w = KeyWorld(self.tmp, [Peer("peerbox", creds={"github-pat": STALE})])
        w.holds("github-pat", GOOD)
        w.holds("claude-login", "{login}")
        return w

    def setup_once(self, w):
        with contextlib.redirect_stderr(io.StringIO()):
            self.key(w).setup()

    def test_setup_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[key]`: the deploy keys minted and registered, a credential minted, and the peer converged."""
        os.environ["WK_YES"] = "1"
        converges(self, self.fleet_world, self.setup_once, KeyWorld.state, max_effects=120)
        w = self.fleet_world()
        self.setup_once(w)
        self.assertEqual({"github-pat": GOOD, "ntfy": TOPIC}, w.peers["peerbox"].creds)
        self.assertEqual({"fork": "KEY:fork-g1", "forkwpe": "KEY:forkwpe-g1"}, w.peers["peerbox"].keys)
        self.assertEqual([pub_of("KEY:fork-g1")], list(w.github[REPOS["fork"]].values()))

    def test_deploy_killed_after_any_effect_and_rerun_converges(self):
        os.environ["WK_YES"] = "1"

        def deploy(w):
            with contextlib.redirect_stderr(io.StringIO()):
                self.key(w).deploy()
        converges(self, self.fleet_world, deploy, KeyWorld.state, max_effects=80)

    def dry_equals_wet(self, verb, make, **kw):
        os.environ["WK_YES"] = "1"
        wet = make()
        self.run_verb(verb, wet, **kw)
        dry = make()
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        self.run_verb(verb, dry, **kw)
        self.assertEqual(wet.acts(), dry.acts())
        self.assertEqual(before, dry.state())
        return wet.acts()

    def keyed_fleet(self):
        w = self.fleet_world()
        w.keys()
        w.holds("ntfy", TOPIC)
        return w

    def test_a_dry_setup_is_the_wet_runs_plan_and_touches_nothing(self):
        plan = self.dry_equals_wet("setup", self.keyed_fleet)
        self.assertTrue([a for a in plan if a[0] == "act" and "PEER peerbox key adopt fork" in " ".join(a[1])])

    def test_a_dry_deploy_is_the_wet_runs_plan_and_touches_nothing(self):
        plan = self.dry_equals_wet("deploy", self.keyed_fleet)
        self.assertTrue([a for a in plan if a[0] == "act" and a[1][:2] == ("gh", "api")])

    def test_a_dry_set_is_the_wet_runs_plan_and_touches_nothing(self):
        def one(w=None):
            k = self.key(w)
            with contextlib.redirect_stderr(io.StringIO()):
                return k.set("ntfy")
        wet = KeyWorld(self.tmp)
        one(wet)
        dry = KeyWorld(self.tmp)
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        one(dry)
        self.assertEqual(wet.acts(), dry.acts())
        self.assertEqual(before, dry.state())
        self.assertIn(TOPIC + "\n", wet.files.values())


class TestTheFleetQuestion(KeyTest):
    """`setup` is declared destructive for what it overwrites on other workstations: asked when there is one to
    overwrite, and acting unasked when there is none or the question was declined."""

    def setUp(self):
        super().setUp()
        os.environ["WK_DESTRUCTIVE"] = "1"

    def test_no_other_workstation_is_nothing_to_ask(self):
        w = self.world()
        rc, _, err = self.run_verb("setup", w)
        self.assertNotIn("BUG", err)
        self.assertNotIn("declining", err)
        self.assertTrue(w.acts(), "setup on a machine of its own made nothing")

    def test_a_declined_question_leaves_the_peer_and_sets_up_here(self):
        w = self.world(Peer("peerbox", creds={"github-pat": STALE}))
        w.holds("github-pat", GOOD)
        with mock.patch("sys.stdin", io.StringIO("")):
            _, _, err = self.run_verb("setup", w)
        self.assertIn("declining (no terminal", err)
        self.assertNotIn("BUG", err)
        self.assertEqual({"github-pat": STALE}, w.peers["peerbox"].creds)
        self.assertIn("peerbox was left exactly as it is", err)

    def test_a_declined_deploy_changes_nothing(self):
        w = self.world(Peer("peerbox"))
        with mock.patch("sys.stdin", io.StringIO("")):
            rc, _, err = self.run_verb("deploy", w)
        self.assertEqual(1, rc)
        self.assertEqual([], [a for a in w.acts() if a[0] == "act"])


class TestTheDeclaration(unittest.TestCase):
    """cmd/key's `# wk:` lines, as the dispatcher reads them, for `sudo` and `backup` beside the credential verbs."""

    d = decl.Decl(os.path.join(ROOT, "cmd", "key"))

    def check(self, *args):
        try:
            return dispatch.Invocation("key", self.d, list(args)).argv_check()
        except dispatch.Exit as e:
            return e.status

    def test_only_the_credential_verbs_are_destructive(self):
        self.assertTrue(self.d.is_destructive(["setup"]) and self.d.is_destructive(["set", "ntfy", "--replace"]))
        self.assertFalse(self.d.is_destructive(["sudo", "setup"]))
        self.assertFalse(self.d.is_destructive(["backup"]))

    def test_sudo_has_a_dry_run_and_backup_only_for_its_read(self):
        self.assertTrue(self.d.honours_dryrun(["sudo", "setup"]) and self.d.honours_dryrun(["sudo"]))
        self.assertTrue(self.d.honours_dryrun(["backup", "--candidates"]))
        self.assertFalse(self.d.honours_dryrun(["backup"]))

    def test_each_subverb_takes_its_own_options(self):
        self.assertEqual(["sudo", "status", "--target=box"], self.check("sudo", "status", "--target", "box"))
        self.assertEqual(["backup", "--candidates"], self.check("backup", "--candidates"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, self.check("backup", "--target", "box"))
            self.assertEqual(2, self.check("sudo", "status", "--candidates"))
            self.assertEqual(2, self.check("sudo", "status", "extra"))

    def test_neither_needs_github(self):
        self.assertEqual("", self.d.needs_for(["sudo"]))
        self.assertEqual("", self.d.needs_for(["backup"]))


if __name__ == "__main__":
    unittest.main()
