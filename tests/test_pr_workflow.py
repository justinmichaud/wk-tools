"""`wk pr` typed into a world: a PR head checked out into workspace `ws`, its branch rebased onto origin/main, and opened
as a pull request -- pushed by the workspace as a person's own run, or by this machine for a build box."""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_wk_workspace as ws
from tests.fakes import Fake, FlowTest, Result
from tests.support import REPO
from tests.test_wk_workspace import said
from wk import pr, sync  # noqa: E402 -- the bench-head fetches at the bottom only
from wk.clock import Clock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Store  # noqa: E402

SRC, BOX_SRC = "/src/WebKit", ws.FAR_ROOT + "/ws/ws/WebKit"
PERSON = "/opt/wk-tools/container/bin/wk-person"
URLS = {"origin": "https://github.com/WebKit/WebKit.git", "wpe": "https://github.com/WebPlatformForEmbedded/WPEWebKit.git",
        "fork": "https://github.com/me/WebKit.git", "forkwpe": "https://github.com/me/WPEWebKit.git"}
ALICE = "https://github.com/alice/WebKit"
HEAD, MAIN, OLD, NEW = "b" * 40, "c" * 40, "d" * 40, "e" * 40
BOX_REF = "refs/wk/push/fakebox/eng/x"
FORK_SSH = "git@github.com:justinmichaud/WebKit.git"
GH = "#!/bin/sh\nexit 0\n"


class World(ws.World):
    """`ws` on place fakebox -- a container here, a build box (`remote`) or a peer workstation (`peer`) -- whose
    checkout answers git from `branches`, `head` (None: detached), `dirty`, `config` and `refs`, each change landing
    as a ("git", ...) effect; a git verb named in `fails` fails saying so ("mirror": this machine's mirror fetch).
    GitHub answers ls-remote and a fetch from `github` ({(url, ref): sha}) and takes pushes into `pushed`; this
    machine's mirror takes a box's branch into `mirror_refs`; `gh` is a stub whose `api user` fails once `dead_token()`."""

    def __init__(self, tmp, kind="container"):
        super().__init__(tmp, kinds={"fakebox": kind})
        if kind != "container":
            del self.env["WK_IN_VM"]
        store = self.env["WK_STORE"]
        self.env.update(WK_STORE_DEFAULT=store, WK_HOST_SECRETS=os.path.join(store, "secrets"))
        self.key = os.path.join(store, "push-keys", "build_key_fork")
        self.files[self.key] = "KEY\n"
        self.gh_path = self.tmp / "gh-bin" / "gh"
        self.gh_path.parent.mkdir()
        self.gh_path.write_text(GH)
        self.gh_path.chmod(0o755)
        self.env["PATH"] = "%s:%s" % (self.gh_path.parent, os.environ["PATH"])
        self.answer(["gh", "api", "user"])
        self.src, self.mirror = BOX_SRC if kind == "remote" else SRC, os.path.join(store, "git", "WebKit.git")
        self.branches, self.head, self.dirty, self.refs, self.fails, self.main = {"main": MAIN}, "main", 0, {}, {}, MAIN
        self.config = {"remote.%s.url" % n: u for n, u in URLS.items()}
        self.github, self.pushed, self.mirror_refs, self.mounted, self.said = {}, set(), set(), {self.mirror}, ""
        self.react(["git", "ls-remote"], lambda a, f: Result(0, "%s\t%s\n" % (f.github[a[2], a[3]], a[3]) if (a[2], a[3]) in f.github else ""))
        self.react(["git", "init"], lambda a, f: (f.dirs.add(a[-1]), Result(0))[1])
        self.make()
        self.effects = []

    def _peer(self):
        self._remote()

    def dead_token(self):
        self.answer(["gh", "api", "user"], rc=1)

    def _in_ws(self, argv, f):
        rest = list(argv[2:])
        rest = rest[rest[:1] == [PERSON]:]
        return self.in_checkout(rest) if rest[:1] in (["git"], ["test"]) else super()._in_ws(argv, f)

    def _ssh(self, argv, f):
        words = shlex.split(argv[-1])
        text = words[2] if words[:2] == ["sh", "-c"] else argv[-1]
        if "/wk ls --json" in text:
            return Result(0, json.dumps({"workspaces": [{"name": "ws", "state": "running"}]}))
        if "/wk zed ws --route" in text:
            return Result(0, "user=u\nsrc=%s\nproxy=p\n" % SRC)
        if "test -x " in text:
            return Result(0)
        if text.startswith("cd ") and " && " in text:
            return self.in_checkout(shlex.split(text.split(" && ", 1)[1]))
        return super()._ssh(argv, f)

    def _git(self, argv, f):
        if not argv[2].endswith("/WebKit.git"):
            return super()._git(argv, f)
        if "fetch" in argv:
            if "mirror" in self.fails:
                return Result(1, "", self.fails["mirror"] + "\n")
            self.mirror_refs.add(argv[-1].split(":")[1])
        elif "update-ref" in argv:
            self.mirror_refs.discard(argv[-1])
        elif "push" in argv:
            self.pushed.add((argv[-2], argv[-1].split(":")[1]))
        return Result(0)

    def in_checkout(self, argv):
        if argv[:2] == ["test", "-d"]:
            return Result(0 if argv[2] in self.mounted else 1)
        assert argv[:3] == ["git", "-C", self.src], argv
        sub, c = argv[3:], self.config
        verb = sub[0]
        if verb in self.fails:
            return Result(1, "", self.fails[verb] + "\n")
        if verb == "status":
            return Result(0, " M f\n" * self.dirty)
        if verb == "symbolic-ref":
            return Result(0, self.head + "\n") if self.head else Result(128)
        if verb in ("rev-parse", "show-ref"):
            sha = self.branches.get(sub[-1][len("refs/heads/"):], "")
            return Result(0, sha + "\n") if sha else Result(1)
        if verb == "rev-list":
            return Result(0, "0\n")
        if verb == "--no-pager":
            return Result(0, "abc1234 a message\n")
        if sub[:2] == ["config", "--get-regexp"]:
            return Result(0, "".join("%s %s\n" % kv for kv in sorted(c.items()) if kv[0].startswith("remote.")))
        if sub[:2] == ["config", "--get"]:
            return Result(0, c[sub[2]] + "\n") if sub[2] in c else Result(1)
        if sub[:2] == ["config", "--get-all"]:   # the fetch refspec `git remote add` writes
            name = sub[2][len("remote."):-len(".fetch")]
            return Result(0, "+refs/heads/*:refs/remotes/%s/*\n" % name) if "remote.%s.url" % name in c else Result(1)
        self.effect(("git",) + tuple(sub))
        if verb == "config":
            c[sub[1]] = sub[2]
        elif verb == "remote":
            c["remote.%s.url" % sub[2]] = sub[3]
        elif verb == "fetch":
            src, _, dest = sub[-1].partition(":")
            mirrored = not dest or dest.endswith("/*")
            self.refs["refs/remotes/origin/main" if mirrored else dest] = self.main if mirrored else self.github[sub[-2], src]
        elif verb == "checkout":
            self.head = sub[-2] if "-b" in sub else sub[-1]
            if "-b" in sub:
                self.branches[self.head] = self.refs[sub[-1]]
        elif verb == "reset":
            self.branches[self.head] = self.refs[sub[-1]]
        elif verb == "branch":
            for k in ("remote", "merge"):
                c.pop("branch.%s.%s" % (sub[-1], k), None)
        elif verb == "push":
            self.pushed.add((sub[1], sub[2]))
        elif verb == "rebase":
            self.branches[self.head] = self.refs["refs/remotes/" + sub[1]]
        return Result(0)

    def changed(self):
        return [e[1:] for e in self.effects if e[0] == "git"]

    def checkout_state(self):
        return sorted(self.config.items()), sorted(self.branches.items()), self.head

    def opened(self):
        return sorted(self.pushed), any(e[0] == "exec" for e in self.effects)

    def box_state(self):
        return sorted(self.mirror_refs), sorted(self.pushed)

    def gh(self):
        return [list(e[1]) for e in self.effects if e[0] == "exec"]

    def here_ran(self, word):
        return [list(e[1]) for e in self.effects if e[0] == "act" and word in e[1]]


def fork_branch(tmp):
    """alice's eng/x is on her WebKit fork and in no other repository."""
    w = World(tmp)
    w.github[ALICE, "refs/heads/eng/x"] = HEAD
    return w


def on_branch(tmp, kind="container", tracks="origin"):
    """`ws` clean on eng/x, tracking `tracks`: a branch `wk pr open` opens."""
    w = World(tmp, kind)
    w.branches["eng/x"], w.head, w.config["branch.eng/x.remote"] = OLD, "eng/x", tracks
    return w


def behind(tmp):
    """`ws` on eng/y, and origin/main has moved on to NEW."""
    w = World(tmp)
    w.branches["eng/y"], w.head, w.main = OLD, "eng/y", NEW
    return w


def run(case, w, *argv):
    """`wk pr <argv>` once, as a kill-point row runs it: what it said is kept on the world."""
    w.said = case.wk(w, "pr", *argv)[2]


def plan(w):
    """What a run changed in the checkout (a dry run: printed it would) and here, in order; an ssh connection's directory is no change."""
    return ([w.rel(shlex.join(("git", "-C", w.src) + e)) for e in w.changed()]
            + [w.rel(l[len("would run in ws: "):]) for l in w.said.splitlines() if l.startswith("would run in ws: ")]
            + [m for m in w.mutations() if w.mutates(m)])


class BoxKeys:
    """Secrets as tests/test_push_service.py's box push reads them: one fork, and where its private half is."""

    def forks(self):
        return [("fork", "alice/WebKit", "")]

    def push_key_path(self, fork):
        return "/s/push-keys/build_key_%s" % fork


class PrTest(ws.Flow):
    """No tests of its own: the kill-point rows run `wk pr` in its worlds."""
    world_class = World

    def pr(self, *argv, w=None):
        return self.typed("pr", *argv, w=w)


class TestPrCheckout(PrTest):
    def test_a_forks_branch_is_fetched_by_url_into_a_remote_it_adds_and_checked_out_tracking_it(self):
        w = fork_branch(self.tmp)
        rc, err = self.pr("ws", "alice:eng/x", w=w)
        self.assertEqual((rc, w.changed()), (0, [("remote", "add", "alice", ALICE + ".git"), ("remote", "set-url", "alice", ALICE + ".git"),
                                                 ("fetch", "--quiet", "--no-prune", ALICE, "refs/heads/eng/x:refs/remotes/alice/eng/x"),
                                                 ("checkout", "--quiet", "-b", "eng/x", "refs/remotes/alice/eng/x"),
                                                 ("config", "branch.eng/x.remote", "alice"), ("config", "branch.eng/x.merge", "refs/heads/eng/x")]))
        self.assertEqual((w.head, w.branches["eng/x"]), ("eng/x", HEAD))
        self.assertIn("==> 'ws' is on eng/x (WebKit, from alice)", err.splitlines())

    def test_a_pull_request_is_checked_out_as_pr_n_tracking_nothing(self):
        for spec, url, branch in (("1234", URLS["origin"], "pr/1234"), ("wpe:1234", URLS["wpe"], "pr/wpe-1234")):
            w = self.make_world()
            w.github[url[:-4], "refs/pull/1234/head"] = HEAD
            rc, err = self.pr("ws", spec, w=w)
            tracking = "refs/remotes/%s/%s" % (spec.split(":")[0] if ":" in spec else "origin", branch)
            self.assertEqual((rc, w.changed()), (0, [("fetch", "--quiet", "--no-prune", url[:-4], "refs/pull/1234/head:" + tracking),
                                                     ("checkout", "--quiet", "-b", branch, tracking), ("branch", "--quiet", "--unset-upstream", branch)]))

    def test_each_spelling_asks_github_where_it_says(self):
        for spec, asked in (("alice:eng/frame-fix", [(ALICE, "refs/heads/eng/frame-fix"), ("https://github.com/alice/WPEWebKit", "refs/heads/eng/frame-fix")]),
                            ("1234", [("https://github.com/WebKit/WebKit", "refs/pull/1234/head")]),
                            ("wpe:5678", [("https://github.com/WebPlatformForEmbedded/WPEWebKit", "refs/pull/5678/head")]),
                            ("wpe:somebranch", [("https://github.com/wpe/WebKit", "refs/heads/somebranch"),
                                                ("https://github.com/wpe/WPEWebKit", "refs/heads/somebranch")])):
            rc, err = self.pr("ws", spec)
            self.assertEqual((rc, [tuple(e[1][2:]) for e in self.w.effects if e[1][:2] == ("git", "ls-remote")], self.w.changed()), (1, asked, []), spec)

    def test_a_spec_that_is_none_of_them_is_refused_before_github_is_asked(self):
        for spec, line in (("not-a-spec", "error: 'not-a-spec' is not a PR spec: <user>:<branch>, a pull request number, or wpe:<number>"),
                           ("1234x", "error: '1234x' is not a pull request number (digits only)"),
                           ("wpe:12x", "error: 'wpe:12x' is not a pull request number (digits only)"),
                           (":b", "error: expected <user>:<branch>, got ':b'")):
            rc, err = self.pr("ws", spec)
            self.assertEqual((rc, said(err), [e for e in self.w.effects if "ls-remote" in e[1]]), (1, line, []), spec)


class TestPrRebase(PrTest):
    def setUp(self):
        super().setUp()
        self.w = behind(self.tmp)

    def test_it_fetches_from_the_mirror_when_the_workspace_has_it_and_from_origin_when_not(self):
        for there in (True, False):
            w = behind(self.tmp)
            source = [w.mirror, "+refs/heads/*:refs/remotes/origin/*"] if there else ["origin"]
            w.mounted = w.mounted if there else set()
            rc, err = self.pr("rebase", "ws", w=w)
            self.assertEqual((rc, said(err), w.changed()), (0, "  abc1234 a message", [("fetch", "--prune", "--quiet", *source), ("rebase", "origin/main")]))
            self.assertEqual(w.branches["eng/y"], NEW)

    def test_a_failed_fetch_never_rebases(self):
        self.w.fails["fetch"] = "network unreachable"
        rc, err = self.pr("rebase", "ws")
        self.assertEqual((rc, said(err), self.w.changed(), self.w.branches["eng/y"]),
                         (1, "  in the workspace:  git status; git rebase --continue   (or --abort)", [], OLD))
        self.assertIn("warning: the rebase stopped -- almost always a conflict, and the sequencer holds where.", err.splitlines())

    def test_a_conflict_is_shown_before_the_remedy(self):
        self.w.fails["rebase"] = "CONFLICT (content): Merge conflict in a.cpp"
        rc, err = self.pr("rebase", "ws")
        lines = err.splitlines()
        self.assertEqual((rc, self.w.branches["eng/y"]), (1, OLD))
        self.assertLess(lines.index("CONFLICT (content): Merge conflict in a.cpp"), lines.index("  in the workspace:  git status; git rebase --continue   (or --abort)"))


class TestPrOpen(PrTest):
    def test_the_branch_is_pushed_as_a_persons_own_run_then_this_process_becomes_gh_pr_create(self):
        w = on_branch(self.tmp)
        rc, err = self.pr("open", "ws", "--draft", w=w)
        self.assertEqual((rc, w.changed()), (0, [("push", "fork", "eng/x"), ("config", "branch.eng/x.remote", "fork"),
                                                 ("config", "branch.eng/x.merge", "refs/heads/eng/x")]))
        self.assertEqual(w.gh(), [["gh", "pr", "create", "--repo", "WebKit/WebKit", "--head", "me:eng/x", "--fill", "--draft"]])
        self.assertIn([PERSON, "git", "-C", SRC, "push", "fork", "eng/x"],
                      [list(e[1][e[1].index(PERSON):]) for e in w.effects if e[0] == "run" and PERSON in e[1]])

    def test_the_project_is_the_one_the_branch_tracks_whether_upstream_or_its_fork(self):
        for tracks, fork, base in (("origin", "fork", "WebKit/WebKit"), ("fork", "fork", "WebKit/WebKit"),
                                   ("wpe", "forkwpe", "WebPlatformForEmbedded/WPEWebKit"), ("forkwpe", "forkwpe", "WebPlatformForEmbedded/WPEWebKit")):
            w = on_branch(self.tmp, tracks=tracks)
            rc, err = self.pr("open", "ws", w=w)
            self.assertEqual((rc, w.changed()[0], w.gh()[0][4:7]), (0, ("push", fork, "eng/x"), [base, "--head", "me:eng/x"]), tracks)

    def test_what_cannot_be_opened_is_refused_by_name_before_any_push(self):
        main, detached, dirty, loose = (on_branch(self.tmp) for _ in range(4))
        main.head, detached.head, dirty.dirty = "main", None, 1
        del loose.config["branch.eng/x.remote"]
        for w, line in ((main, "error: 'wk pr open' cannot open 'main' as a pull request -- create a branch first"),
                        (detached, "error: 'wk pr open' needs a branch checked out, not a detached HEAD"),
                        (dirty, "error: the tree has uncommitted changes -- commit or stash them: git -C /src/WebKit status"),
                        (loose, "error: 'eng/x' has no upstream, so 'wk pr open' cannot tell which project it belongs to -- "
                                "track one first: git branch -u origin/main (WebKit) or wpe/main (WPEWebKit)")):
            rc, err = self.pr("open", "ws", w=w)
            self.assertEqual((rc, said(err), w.changed(), w.gh()), (1, line, [], []))

    def test_a_peer_workstation_pushes_its_own_branch(self):
        w = on_branch(self.tmp, "peer")
        rc, err = self.pr("open", "ws", w=w)
        self.assertEqual((rc, w.changed()[0], w.here_ran("push"), len(w.gh())), (0, ("push", "fork", "eng/x"), [], 1))

    def test_a_dry_run_of_each_verb_prints_its_plan_and_changes_nothing(self):
        w = on_branch(self.tmp)
        rc, err = self.pr("open", "ws", "--dry-run", w=w)
        self.assertEqual((rc, w.changed(), w.gh(), sorted(w.pushed)), (0, [], [], []))
        self.assertEqual(err.splitlines()[-1], "would run: gh pr create --repo WebKit/WebKit --head me:eng/x --fill")
        self.assertIn("would run in ws: git -C /src/WebKit push fork eng/x", err.splitlines())
        w.github[URLS["origin"][:-4], "refs/pull/42/head"] = HEAD
        for argv in (("ws", "42"), ("rebase", "ws")):
            rc, err = self.pr(*argv, "--dry-run", w=w)
            self.assertEqual((rc, w.changed(), w.branches), (0, [], {"main": MAIN, "eng/x": OLD}), argv)

    def test_a_gh_whose_token_the_api_refuses_is_refused_naming_gh_auth_login(self):
        w = on_branch(self.tmp)
        w.dead_token()
        rc, err = self.pr("open", "ws", w=w)
        self.assertEqual((rc, w.changed(), w.gh()), (1, [], []))
        self.assertIn("    gh-auth    gh cannot reach the GitHub API (not logged in, or the token expired): gh auth login", err.splitlines())


class TestPrOpenFromABox(PrTest):
    """A build box's branch is fetched over ssh into this machine's mirror and pushed from here: no key reaches a box."""

    def setUp(self):
        super().setUp()
        self.w = on_branch(self.tmp, "remote")

    def test_the_branch_lands_on_the_fork_and_no_temporary_ref_is_left(self):
        rc, err = self.pr("open", "ws")
        self.assertEqual((rc, self.w.box_state(), self.w.changed()[0][0], len(self.w.gh())),
                         (0, ([], [(FORK_SSH, "refs/heads/eng/x")]), "config", 1))
        self.assertEqual(self.w.here_ran("fetch")[0][-2:], ["box.example:" + BOX_SRC, "+refs/heads/eng/x:" + BOX_REF])

    def test_the_first_push_makes_the_mirror_and_the_next_fetches_into_it(self):
        self.pr("open", "ws")
        made = [a[:4] for a in self.w.here_ran("init")] + [a[3:] for a in self.w.here_ran("gc.auto")]
        self.assertEqual(made, [["git", "init", "--bare", "-q"], ["config", "gc.auto", "0"]])
        self.pr("open", "ws")
        self.assertEqual((self.w.here_ran("init"), len(self.w.here_ran("fetch"))), ([], 1))

    def test_the_temporary_ref_is_locked_from_its_fetch_until_its_delete(self):
        """`wk gc` keeps a ref whose lock is held, so it cannot delete one between this fetch and this push."""
        self.pr("open", "ws")
        lock = [i for i, e in enumerate(self.w.effects) if e[0] in ("symlink", "remove") and "push-ref-fakebox%eng%x@" in e[1]]
        work = [i for i, e in enumerate(self.w.effects) if e[0] == "act" and ("fetch" in e[1] or "update-ref" in e[1])]
        self.assertEqual((len(lock), lock[0] < work[0], work[-1] < lock[-1]), (2, True, True), self.w.effects)

    def test_the_push_uses_the_forks_deploy_key_and_no_agent(self):
        self.pr("open", "ws")
        (argv,) = self.w.here_ran("push")
        ssh = shlex.split(next(a for a in argv if a.startswith("core.sshCommand=")).split("=", 1)[1])
        self.assertEqual((ssh[ssh.index("-i") + 1], "IdentityAgent=none" in ssh, argv[-2:]),
                         (self.w.key, True, [FORK_SSH, BOX_REF + ":refs/heads/eng/x"]))

    def test_a_machine_with_no_key_refuses_naming_wk_key_deploy_before_any_effect(self):
        del self.w.files[self.w.key]
        rc, err = self.pr("open", "ws")
        self.assertEqual((rc, said(err), self.w.here_ran("git"), self.w.box_state()),
                         (1, "error: 'wk pr open' pushes 'eng/x' from here with the deploy key 'fork', and this machine holds none.", [], ([], [])))
        self.assertEqual(err.splitlines()[-1], "    wk key deploy")

    def test_a_fetch_that_fails_is_refused_naming_the_branch_and_nothing_is_pushed(self):
        self.w.fails["mirror"] = "box.example: connection refused"
        rc, err = self.pr("open", "ws")
        self.assertEqual((rc, said(err), self.w.box_state()),
                         (1, "error: could not fetch refs/heads/eng/x from box.example:%s into the mirror" % BOX_SRC, ([], [])))

    def test_a_dry_run_fetches_and_pushes_nothing(self):
        rc, err = self.pr("open", "ws", "--dry-run")
        self.assertEqual((rc, self.w.box_state(), [e for e in self.w.effects if e[0] == "run" and "git" in e[1]]), (0, ([], []), []))
        self.assertIn("would run: git -C %s fetch --quiet box.example:%s +refs/heads/eng/x:%s" % (self.w.here_ran("fetch")[0][2], BOX_SRC, BOX_REF),
                      err.splitlines())


class TestGitWebkitPrThroughTheInjector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("needs the openssl CLI")
        path = REPO / "container" / "proxy" / "github-inject.py"
        spec = importlib.util.spec_from_file_location("wkinject_e2e", str(path))
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def _run(self, token):
        m = self.m
        d = Path(tempfile.mkdtemp(prefix="wk-test-inject-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        chain = m.ensure_certs(str(d / "certs"), str(d / "ca.pem"))
        pat = d / "pat"
        pat.write_text(token + "\n")

        seen = {}

        async def upstream(reader, writer):
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                head += chunk
            seen["head"] = head
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                         b"Connection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        async def main():
            up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            up_ctx.load_cert_chain(chain, str(d / "certs" / "leaf.key"))
            up = await asyncio.start_server(upstream, host="127.0.0.1", port=0,
                                            ssl=up_ctx)
            port = up.sockets[0].getsockname()[1]

            client_ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            m.INJECT_PORT = port
            injector = m.Injector(str(pat), str(d / "read-pat"), str(d / "bz-key"),
                                  m.Holder(str(d / "claude-login")), client_ctx)

            srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            srv_ctx.load_cert_chain(chain, str(d / "certs" / "leaf.key"))
            sock = str(d / "inject.sock")
            server = await asyncio.start_unix_server(injector.handle, path=sock,
                                                     ssl=srv_ctx)

            ws_ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            reader, writer = await asyncio.open_unix_connection(
                sock, ssl=ws_ctx, server_hostname="api.github.com")
            writer.write(
                b"GET /user HTTP/1.1\r\n"
                b"Host: api.github.com\r\n"
                b"User-Agent: python-requests/2.31.0\r\n"
                b"Accept: application/vnd.github.v3+json\r\n"
                b"Authorization: Basic d2s6d2staW5qZWN0cy10aGlz\r\n"
                b"Connection: keep-alive\r\n\r\n")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(4096), 10)
            writer.close()
            server.close()
            up.close()
            return body

        # The injector dials api.github.com by name; the fake upstream is on loopback.
        real_open = asyncio.open_connection

        async def to_loopback(host, port, **kw):
            return await real_open("127.0.0.1", port, **kw)

        with mock.patch.object(asyncio, "open_connection", to_loopback):
            got = asyncio.run(asyncio.wait_for(main(), 30))
        return seen.get("head", b""), got

    def test_the_real_token_arrives_and_the_placeholder_never_does(self):
        head, got = self._run("ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", head)
        self.assertNotIn(b"Basic", head)
        self.assertNotIn(b"wk-injects-this", head)
        self.assertIn(b"200 OK", got)
        self.assertIn(b"GET /user HTTP/1.1", head)
        self.assertIn(b"Accept: application/vnd.github.v3+json", head)


class TestTheMirrorFetchesOfABenchHead(FlowTest):
    """Not reachable from `wk pr`: `wk bench ab <spec>` fetches a pull request's head into the mirror
    (lib/wk/bench/change.py), and its dry run plans that fetch; these move to that command's conversion."""

    def setUp(self):
        super().setUp()
        self.store = Store({"WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                            "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "", "HOME": str(self.tmp)})

    def test_a_pull_request_lands_as_refs_remotes_pr_remote_n(self):
        origin = self.tmp / "origin"
        origin.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(origin)] + list(a), capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
        git("init", "-q", "-b", "main")
        git("-c", "user.email=t@example.com", "-c", "user.name=Test", "commit", "-q", "--allow-empty", "-m", "init")
        sha = git("rev-parse", "HEAD")
        git("update-ref", "refs/pull/7/head", sha)
        with contextlib.redirect_stderr(io.StringIO()):
            sync.fetch_pull_into_mirror(Local(), self.store, Lock(self.store, Local(), Clock()), "origin", "7", (("origin", str(origin)),))
        got = subprocess.run(["git", "-C", self.store.mirror_dir(), "rev-parse", "refs/remotes/pr/origin/7"], capture_output=True, text=True)
        self.assertEqual(got.stdout.strip(), sha)

    def test_an_unknown_remote_is_refused_by_name(self):
        """No spec parses to a remote outside origin and wpe, so no command line reaches this refusal."""
        err = self.refused(lambda: sync.fetch_pull_into_mirror(Fake(), self.store, None, "nosuchremote", "1"))
        self.assertIn("no such upstream remote", err)

    def test_a_dry_run_reads_a_head_the_mirror_has_and_plans_the_fetch_of_one_it_lacks(self):
        here, mirror, fetched = Fake("here"), self.store.mirror_dir(), []
        here.dirs.add(mirror)
        here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/x^{commit}"], out="c" * 40 + "\n")
        here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet"], rc=1)
        self.dry_run()
        got, err = self.stderr(lambda: [pr.resolved_or_planned(here, mirror, ref, "x from y", lambda: fetched.append(1)) for ref in ("refs/x", "refs/y")])
        self.assertEqual((got, fetched, err), (["c" * 40, pr.PLANNED_COMMIT], [], "would fetch x from y into the mirror\n"))
