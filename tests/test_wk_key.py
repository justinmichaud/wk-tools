"""lib/wk/key/ over tests/test_wk_secrets.py's fake machine, a toy rulebook in lib/credcheck.py's place, a GitHub
holding deploy keys, and peers answering from what they hold. A private key is `KEY:<tag>`, its public half
`ssh-ed25519 AAAA<tag> wk`, and a stored credential fingerprints as `fp-<value>`."""
import contextlib
import io
import os
import pty
import select
import shlex
import subprocess
import sys
import termios
import threading
import time
import unittest
from unittest import mock

from tests.fakes import FakeRegistry, FakeDriver
from tests.test_wk_secrets import ROOT, SECRETFILE, SecretsTest, World

import credcheck  # noqa: E402
from wk import act, secrets  # noqa: E402
from wk.key import cli, common  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402

NAMES = ("github-pat", "litellm", "claude-login", "deploy-key")
GOOD, OTHER_GOOD, STALE, UNJUDGED = "good-token", "good-token-2", "stale-token", "offline-token"
REPOS = {"fork": "justinmichaud/WebKit", "forkwpe": "justinmichaud/WPEWebKit", "wk-tools": "someone/wk-tools"}
ALL_KEYS = {f: "KEY:" + f for f in REPOS}


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
    if name == "claude-login":
        return "ok\tthe injector holds it" if v.startswith("login") else "bad\tnot a login"
    if name == "litellm":
        return "ok\tai.igalia.com accepts it" if v.startswith("sk-") else "bad\tnot a virtual key"
    return "ok\tit serves"


class Peer:
    def __init__(self, name, creds=None, keys=None, answers=True):
        self.name, self.creds, self.keys, self.answers = name, dict(creds or {}), dict(keys or {}), answers
        self.asked = []

    def state(self):
        return sorted(self.creds.items()), sorted(self.keys.items())

    def verdict(self, name):
        v = self.creds.get(name, "")
        return judge(name, v, "path", {}) + ("\n    fingerprint: fp-%s" % v if v else "")


class KeyWorld(World):
    def __init__(self, base, peers=()):
        super().__init__(base)
        self.peers = {p.name: p for p in peers}
        self.github = {r: {} for r in REPOS.values()}
        self.gen = {}
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
        self.answer(["git", "-c", "safe.directory=*", "-C", ROOT, "remote", "get-url", "origin"], out="git@github.com:someone/wk-tools.git\n")
        self.react(["hostname", "-s"], lambda a, f: Result(0, "here\n"))
        self.react(["rm"], self._rm)

    def state(self):
        return (dict(self.files), {n: p.state() for n, p in self.peers.items()},
                {r: sorted(k.values()) for r, k in self.github.items()})

    def holds(self, name, value):
        self._set_file(self.sec().cred_path(name), value + "\n")

    def keys(self, tags=None):
        for fork in REPOS:
            self._set_file(self.held + "/build_key_" + fork, "KEY:%s\n" % (tags or {}).get(fork, fork))
            self._set_file("%s/build_key_%s.pub" % (self.keyring_dir, fork), pub_of("KEY:" + (tags or {}).get(fork, fork)) + "\n")

    def register(self, tags=None):
        for fork, repo in REPOS.items():
            self.github[repo][str(len(self.github[repo]) + 1)] = pub_of("KEY:" + (tags or {}).get(fork, fork))

    def peer_acts(self):
        return [e[1][2] for e in self.effects if e[0] == "act" and e[1][:2] == ("sh", "-c") and e[1][2].startswith("PEER ")]

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
        if verb == "adopt":
            p.keys[arg] = inp.rstrip("\n")
            return Result(0)
        if verb == "set":
            p.creds[arg] = inp.strip()
            return Result(0)
        return Result(1)


def toy_rules():
    """credcheck's table in miniature: claude-login is minted, the rest asked for."""
    return {n: credcheck.Rule(needs="do its job", forbids="travel", what="the " + n, url="https://example.invalid/",
                              remedy="subscribe to it", store_with="wk key set " + n, check=None, alone=n == "claude-login",
                              mint=(lambda: "login-minted\n") if n == "claude-login" else None) for n in NAMES}


def toy_verdict(name, value, repos=(), path="", evidence=None):
    return judge(name, value, path, dict(evidence or {}, repos=" ".join(repos)))


class KeyTest(SecretsTest):
    def setUp(self):
        super().setUp()
        for name, fn in (("rules", toy_rules), ("verdict", toy_verdict)):
            p = mock.patch.object(credcheck, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def world(self, *peers):
        self.w = KeyWorld(self.tmp, peers)
        return self.w

    def key(self, w=None, rotate=False, tty=False, typed="", boxes=()):
        w = w or self.w
        reg = FakeRegistry(w.env, w, lambda n, e: FakeDriver(n, peer=n in w.peers, machine=w), names=sorted(list(w.peers) + list(boxes)))
        return cli.Key(ROOT, env=w.env, machine=w, reg=reg, sec=w.sec(), tty=lambda: tty,
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

    def fleet_world(self):
        w = KeyWorld(self.tmp, [Peer("peerbox", creds={"github-pat": STALE})])
        w.holds("github-pat", GOOD)
        return w

    def keyed_fleet(self):
        w = self.fleet_world()
        w.keys()
        w.holds("claude-login", "login-held")
        return w

    def provisioned(self, *peers, pat=GOOD):
        w = self.world(*peers)
        w.keys()
        w.register()
        w.holds("github-pat", pat)
        os.environ["WK_YES"] = "1"
        return w


class TestTheElection(KeyTest):
    def test_a_peers_working_credential_is_taken_over_one_refused_here(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": GOOD}, keys=ALL_KEYS), pat=STALE)
        self.run_verb("setup")
        self.assertEqual(GOOD + "\n", w.files[w.held + "/github-pat"])
        self.assertIn("give github-pat", w.peers["peerbox"].asked)
        self.assertFalse([a for a in w.peer_acts() if "github-pat" in a], "the winner was written back over itself")

    def test_a_tie_goes_to_this_machine(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": OTHER_GOOD}, keys=ALL_KEYS))
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
        w = self.provisioned(Peer("peerbox", creds={"github-pat": GOOD}, keys={f: "KEY:peer-" + f for f in REPOS}))
        w.github = {r: {} for r in REPOS.values()}
        w.register({f: "peer-" + f for f in REPOS})
        self.run_verb("setup")
        self.assertEqual("KEY:peer-fork\n", w.files[w.held + "/build_key_fork"])
        self.assertEqual(pub_of("KEY:peer-fork") + "\n", w.files[w.keyring_dir + "/build_key_fork.pub"])
        self.assertFalse([a for a in w.peer_acts() if " adopt fork" in a])

    def test_one_nobody_could_judge_is_not_written_over_a_peers(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": UNJUDGED}, keys=ALL_KEYS),
                             pat=UNJUDGED)
        _, _, err = self.run_verb("setup")
        self.assertIn("github-pat: no workstation's could be judged", err)
        self.assertFalse([a for a in w.peer_acts() if "github-pat" in a])

    def test_a_build_machine_is_asked_nothing(self):
        w = self.provisioned(Peer("peerbox"))
        rc, out, _ = self.run_verb("setup", boxes=("buildbox",))
        self.assertFalse([a for a in w.peer_acts() if "buildbox" in a])
        self.assertIn("buildbox", out.split("build machines:")[1].split("\n")[1])


class TestRegisterPerMachine(KeyTest):
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
        k.fleet_key = lambda fork, repo: registered.append(fork) or True
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(k.converge_keys())
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
        self.provisioned(Peer("peerbox", keys=ALL_KEYS))
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
        self.assertRegex(empty, r"none\s+%s\s+no key here" % REPOS["fork"])
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


class TestAReplaceWithNothingStored(KeyTest):
    def test_a_replace_with_nothing_stored_sets_one(self):
        k = self.key(KeyWorld(self.tmp))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(0, k.set("claude-login", replace=True))
        self.assertIn("nothing to replace: setting one", err.getvalue())


class TestADrySet(KeyTest):
    def test_a_dry_set_stops_at_the_mint_and_the_wet_one_stores_it(self):
        """A minted value exists only once minted, so a dry `set` plans nothing past the mint."""
        os.environ["WK_YES"] = "1"
        wet, dry = KeyWorld(self.tmp), KeyWorld(self.tmp)
        with contextlib.redirect_stderr(io.StringIO()), mock.patch("os.urandom", return_value=b"\0" * 8):
            self.key(wet).set("claude-login")
            before = dry.state()
            os.environ["WK_DRY_RUN"] = "1"
            self.key(dry).set("claude-login")
        self.assertIn("login-minted\n", wet.files.values())
        self.assertEqual(([], before), (dry.acts(), dry.state()))


class TestTheFleetQuestion(KeyTest):
    """`setup` asks before overwriting another workstation, and sets up here either way."""

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


class TestTheVerbs(KeyTest):
    """Each verb of `wk key` on one machine."""

    def test_ensure_makes_each_key_once_and_keeps_its_public_half_apart(self):
        w = self.world()
        self.run_verb("ensure")
        for fork in REPOS:
            self.assertTrue(w.files[w.held + "/build_key_" + fork].startswith("KEY:"))
            self.assertIn(w.keyring_dir + "/build_key_%s.pub" % fork, w.files)
            self.assertNotIn(w.held + "/build_key_%s.pub" % fork, w.files)
            self.assertIn(("act", ("chmod", "0600", w.held + "/build_key_" + fork)), w.effects)
        for d in (w.keyring_dir, w.held):
            self.assertIn(("act", ("chmod", "0700", d)), w.effects)
        self.run_verb("ensure")
        self.assertEqual({f: 1 for f in REPOS}, w.gen, "a second run generated a new key")

    def test_pub_and_fingerprints_say_what_is_here(self):
        w = self.world()
        w.keys()
        w._drop(w.held + "/build_key_forkwpe")
        k = self.key(w)
        self.assertEqual(0, k.pub("fork"))
        self.assertEqual(pub_of("KEY:fork") + "\n", k.out.getvalue())
        _, out, _ = self.run_verb("fingerprints", w)
        self.assertRegex(out, r"fork\s+SHA256:\S+\s+private half here")
        self.assertRegex(out, r"forkwpe\s+SHA256:\S+\s+no private half")

    def test_check_on_an_empty_machine_names_every_row_one_deploy_per_key_and_changes_nothing(self):
        w = self.world()
        rc, out, _ = self.run_verb("check", w)
        self.assertEqual(1, rc)
        self.assertEqual(len(REPOS), out.partition("needs you:")[2].count("wk key deploy"))
        for name in NAMES[:-1]:
            self.assertRegex(out, r"\n    \S+\s+%s\s" % name)
        self.assertIn("nothing stored", out)
        self.assertEqual([], w.acts())

    def test_a_refused_credentials_fix_is_under_needs_you_and_not_in_the_table(self):
        w = self.provisioned(pat=STALE)
        rc, out, _ = self.run_verb("check", w)
        table, _, needs = out.partition("needs you:")
        self.assertEqual(1, rc)
        self.assertNotIn("fix:", table)
        self.assertRegex(needs, r"\d+\. github-pat\s+wk key set github-pat --replace")
        self.assertIn("https://github.com/settings/tokens/new", needs)

    def test_a_row_no_issuer_answered_is_not_something_to_do(self):
        for ok, says in ((True, "nothing needs you."), (False, "nothing to do by hand")):
            k = self.key(self.world())
            k.needs_you([], ok)
            self.assertIn(says, k.out.getvalue())

    def test_the_key_rows_are_asked_at_once(self):
        """Proved by overlap: one gh call waits for the second, which never comes if they run one after the other."""
        w = self.provisioned()
        met = threading.Barrier(len(REPOS), timeout=5)
        gh = w._gh

        def overlapping(argv, f):
            if "contains" in argv[-1]:
                try:
                    met.wait()
                except threading.BrokenBarrierError:
                    return Result(1)
            return gh(argv, f)
        w.react(["gh", "api"], overlapping)
        rc, out, _ = self.run_verb("check", w)
        self.assertFalse(met.broken, out)

    def test_setup_names_what_it_could_not_settle_mints_what_it_mints_and_runs_the_rest(self):
        w = self.world()
        rc, out, err = self.run_verb("setup", w)
        (left,) = [l for l in err.splitlines() if "not settled:" in l]
        self.assertEqual(["github-pat", "litellm"], left.split("not settled:")[1].split())
        self.assertEqual("login-minted\n", w.files[w.sec().cred_path("claude-login")])
        self.assertIn(w.held + "/build_key_fork", w.files)
        self.assertRegex(err, r"github-pat\s+skipped\s+\S")

    def test_rotate_turns_over_every_stored_credential_and_one_kept_is_left_exactly(self):
        w = self.world()
        w.keys()
        w.holds("github-pat", GOOD)
        before = w.files[w.held + "/github-pat"]
        _, _, err = self.run_verb("setup", w)
        self.assertEqual(before, w.files[w.held + "/github-pat"])
        self.assertRegex(err, r"github-pat\s+stored\s+/")
        os.environ["WK_YES"] = "1"
        self.run_verb("setup", w, rotate=True)
        self.assertNotIn(w.held + "/github-pat", w.files, "the old token outlived its replace")
        self.assertEqual("KEY:fork-g1\n", w.files[w.held + "/build_key_fork"])

    def quiet_set(self, w, name, **kw):
        k = self.key(w, **kw.pop("key", {}))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                rc = k.set(name, **kw)
            except Refused as e:
                rc = e.status
        return rc, k.out.getvalue(), err.getvalue()

    def test_a_pasted_value_is_put_to_the_rule_and_never_printed(self):
        w = self.world()
        rc, out, err = self.quiet_set(w, "litellm", paste=True, value="sk-from-elsewhere\n")
        self.assertEqual((0, "sk-from-elsewhere\n"), (rc, w.files[w.sec().cred_path("litellm")]))
        self.assertNotIn("sk-from-elsewhere", out + err)
        for given, says in (("not a key\n", "refused"), ("", "wk key set litellm")):
            with self.subTest(given=given):
                w = self.world()
                rc, out, err = self.quiet_set(w, "litellm", paste=True, value=given)
                self.assertNotEqual(0, rc)
                self.assertNotIn(w.sec().cred_path("litellm"), w.files)
                self.assertIn(says, err)

    def test_a_name_set_does_not_take_is_refused_naming_the_ones_it_does(self):
        for name, says in (("", "names: github-pat"), ("nope", "This checkout knows: github-pat"),
                           ("deploy-key", "generated here and never pasted")):
            with self.subTest(name=name):
                rc, _, err = self.quiet_set(self.world(), name)
                self.assertEqual(1, rc)
                self.assertIn(says, err)

    def test_a_stored_one_is_reported_by_name_never_printed_and_non_zero_once_refused(self):
        for value, ok in (("sk-" + GOOD, 0), ("broken-" + GOOD, 1)):
            with self.subTest(value=value):
                w = self.world()
                w.holds("litellm", value)
                rc, out, err = self.quiet_set(w, "litellm")
                self.assertEqual(ok, rc)
                self.assertRegex(err, r"litellm\s+stored\s+\S+")
                self.assertNotIn(GOOD, out + err)

    def test_replace_asks_first_says_to_revoke_the_old_one_and_an_empty_answer_leaves_none(self):
        w = self.world()
        w.holds("github-pat", "good-old")
        with mock.patch("sys.stdin", io.StringIO("")):
            rc, _, err = self.quiet_set(w, "github-pat", replace=True, key={"typed": GOOD, "tty": True})
        self.assertEqual((1, "good-old\n"), (rc, w.files[w.held + "/github-pat"]), err)
        os.environ["WK_YES"] = "1"
        rc, _, err = self.quiet_set(w, "github-pat", replace=True, key={"typed": GOOD, "tty": True})
        self.assertEqual((0, GOOD + "\n"), (rc, w.files[w.held + "/github-pat"]), err)
        self.assertIn("revoke it too if it is still live", err)
        rc, _, _ = self.quiet_set(w, "github-pat", replace=True, key={"tty": True})
        self.assertNotEqual(0, rc)
        self.assertNotIn(w.held + "/github-pat", w.files)
        rc, _, err = self.quiet_set(self.world(), "github-pat", replace=True)
        self.assertIn("nothing to replace: setting one", err)

    def test_every_store_rotate_and_withdraw_reaches_both_injector_copies(self):
        w = self.world()
        sec = w.sec()
        copies = lambda: (w.files.get(sec.machine_read_pat()), w.files.get(sec.machine_pat()))
        self.assertEqual(0, self.quiet_set(w, "github-pat", paste=True, value="good-old")[0])
        self.assertEqual(("good-old\n", "good-old\n"), copies())
        os.environ["WK_YES"] = "1"
        self.quiet_set(w, "github-pat", replace=True, key={"typed": GOOD, "tty": True})
        self.assertEqual((GOOD + "\n", GOOD + "\n"), copies())
        self.quiet_set(w, "github-pat", replace=True)
        self.assertEqual((None, None), copies())

    def test_a_stored_value_reaches_the_container_view_and_an_injector_that_did_not_take_it_is_named(self):
        w = self.world()
        self.quiet_set(w, "litellm", paste=True, value="sk-" + GOOD)
        self.assertEqual("sk-%s\n" % GOOD, w.files[w.keyring_dir + "/view/container/litellm-key"])
        with mock.patch.object(secrets.Secrets, "push_deliver", return_value=False):
            _, _, err = self.quiet_set(self.world(), "github-pat", paste=True, value=GOOD)
        self.assertIn("./setup", err)

    def test_a_prompt_reads_the_paste_with_echo_off_and_no_terminal_asks_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(common.prompt_secret("a token", "", "", lambda: False))
        self.assertIn("Re-run interactively", err.getvalue())
        master, slave = pty.openpty()
        code = "from wk.key.common import prompt_secret as p; print('GOT=%s' % p('a token', 'u', 'h', lambda: True))"
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=slave, stdout=slave, stderr=slave,
                                env=dict(os.environ, PYTHONPATH=ROOT + "/lib"), close_fds=True)
        os.close(slave)
        out, sent, end = b"", False, time.monotonic() + 30
        while time.monotonic() < end and select.select([master], [], [], 1)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if not sent and b"paste it" in out:
                while termios.tcgetattr(master)[3] & termios.ECHO:
                    time.sleep(0.005)
                os.write(master, b"ghp_typed\n")
                sent = True
        proc.wait(timeout=30)
        os.close(master)
        self.assertIn(b"GOT=ghp_typed", out)
        self.assertEqual(1, out.count(b"ghp_typed"), "the paste was echoed")


class TestTheFleetsCredentials(KeyTest):
    """What the other workstations hold, as `check` reports it and `setup` settles it; an `alone` one never travels."""

    def test_every_credential_but_an_alone_one_is_put_to_the_election_and_sent(self):
        w = self.provisioned(Peer("peerbox", keys=ALL_KEYS))
        self.run_verb("setup")
        asked = w.peers["peerbox"].asked
        for name in ("github-pat", "litellm"):
            self.assertIn("verdict " + name, asked)
        self.assertEqual([], [a for a in asked if "claude-login" in a])
        self.assertEqual([], [a for a in w.peer_acts() if "claude-login" in a])

    def test_one_no_workstation_accepts_is_not_written_over_a_peers(self):
        w = self.provisioned(Peer("peerbox", creds={"github-pat": STALE}, keys=ALL_KEYS), pat=STALE)
        _, _, err = self.run_verb("setup")
        self.assertFalse([a for a in w.peer_acts() if "set github-pat" in a])
        self.assertIn("wk key set github-pat --replace", err)
        self.assertEqual(STALE + "\n", w.files[w.held + "/github-pat"])

    def check(self, peer_creds, pat=GOOD):
        w = self.provisioned(Peer("peerbox", creds=peer_creds, keys=ALL_KEYS), pat=pat)
        rc, out, _ = self.run_verb("check", w)
        return rc, out.partition("needs you:")

    def test_each_peer_row_is_its_own_answer_and_the_alone_one_is_never_asked(self):
        rc, (table, _, needs) = self.check({"github-pat": OTHER_GOOD, "litellm": "sk-x"})
        self.assertRegex(table, r"peerbox github-pat\s+it reaches exactly the forks")
        self.assertNotIn("peerbox claude-login", table)
        rc, (table, _, needs) = self.check({})
        self.assertRegex(table, r"peerbox github-pat\s+nothing stored")
        self.assertRegex(needs, r"peerbox github-pat\s+wk key setup\s+\(it puts the fleet's github-pat there\)")
        self.assertEqual(1, rc)
        _, (table, _, _) = self.check({"github-pat": GOOD})
        self.assertRegex(table, r"peerbox github-pat\s+%s" % common.SAME_AS_HERE)

    def test_one_missing_or_refused_here_is_taken_from_the_fleet_in_one_line(self):
        for pat in ("", STALE):
            with self.subTest(pat=pat):
                _, (_, _, needs) = self.check({"github-pat": GOOD}, pat=pat)
                self.assertRegex(needs, r"github-pat\s+wk key setup\s+\(peerbox holds one its issuer accepts\)")
                self.assertNotRegex(needs, r"\d+\. github-pat\s+wk key set github-pat")
        _, (_, _, needs) = self.check({"github-pat": STALE}, pat=STALE)
        self.assertRegex(needs, r"github-pat\s+wk key set github-pat")
        self.assertNotRegex(needs, r"github-pat\s+wk key setup")

    def test_give_prints_a_deploy_key_and_a_credential_and_never_an_alone_one(self):
        w = self.provisioned()
        w.holds("claude-login", "login-held")
        k = self.key(w)
        self.assertEqual(0, k.give("fork"))
        self.assertEqual(0, k.give("github-pat"))
        self.assertEqual("KEY:fork\n" + GOOD + "\n", k.out.getvalue())
        for verb, arg, says in (("give", "claude-login", "never given to another"), ("give", "nope", "nope"),
                                ("adopt", "nope", "nope")):
            with self.subTest(verb=verb, arg=arg), contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(Refused):
                    k.give(arg) if verb == "give" else k.adopt_verb(arg, lambda: b"KEY:x\n")
                self.assertIn(says, err.getvalue())
        self.assertNotIn("login-held", k.out.getvalue())


if __name__ == "__main__":
    unittest.main()
