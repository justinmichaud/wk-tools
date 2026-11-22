import os
import tempfile
import unittest

from tests.world import World
from tests.ws import HOME, MIRROR, git
from wk.core import act, seams
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Local
from wk.workspace import lifecycle, mirror
from wk.workspace.drivers import Plain

INIT = "[lin] git init --bare --quiet %s" % MIRROR
CONFIG = "[lin] write %s/config" % MIRROR


class TestMirror(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")
        self.m = self.w.machines["lin"]
        git(self.m, fetched=False)

    def test_it_is_a_bare_repository_that_never_repacks_fetches_no_tags_and_only_the_named_branches(self):
        self.assertEqual(mirror.CONFIG, '[core]\n\trepositoryformatversion = 0\n\tbare = true\n[gc]\n\tauto = 0\n\twriteCommitGraph = false\n'
                         '[fetch]\n\twriteCommitGraph = false\n[remote "origin"]\n\turl = https://github.com/WebKit/WebKit.git\n'
                         '\ttagOpt = --no-tags\n\tfetch = +refs/heads/main:refs/heads/main\n')
        self.assertEqual(mirror.where("/srv/wk"), "/srv/wk/git/WebKit.git")

    def test_configure_makes_it_and_writes_the_whole_config_once(self):
        mirror.configure(self.m, HOME)
        self.assertEqual(self.w.journal.effects(), [INIT, CONFIG])
        mirror.configure(self.m, HOME)
        self.assertEqual(self.w.journal.effects(), [INIT, CONFIG])

    def test_a_config_that_drifted_is_written_again(self):
        mirror.configure(self.m, HOME)
        self.m.files[MIRROR + "/config"] += "[gc]\n\tauto = 6700\n"
        mirror.configure(self.m, HOME)
        self.assertEqual(self.w.journal.effects()[2:], [CONFIG])
        self.assertEqual(self.m.files[MIRROR + "/config"], mirror.CONFIG)


class TestRealGit(unittest.TestCase):
    """The commands as git itself takes them: a mirror fetched from a local upstream, and a checkout made from it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(act.begin, act.flags)
        act.begin(act.Flags(quiet=True))
        self.home, self.m = os.path.realpath(tmp.name), Local()
        self.up, self.store = self.home + "/upstream", mirror.where(self.home)
        who = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
        for argv in (["init", "--quiet", "--initial-branch", "main", self.up], ["-C", self.up, "commit", "--quiet", "--allow-empty", "-m", "one"],
                     ["-C", self.up, "tag", "v1"], ["-C", self.up, "branch", "side"]):
            self.assertEqual(self.m.run(["git"] + argv, env=who).err, "")
        self.who = who

    def git(self, *argv):
        r = self.m.run(["git"] + list(argv))
        self.assertEqual(r.rc, 0, r.err)
        return r.out.strip()

    def test_the_mirror_holds_the_named_branch_alone_and_a_new_workspace_fetches_through_it(self):
        mirror.configure(self.m, self.home)
        self.m.write(self.store + "/config", mirror.CONFIG.replace("https://github.com/WebKit/WebKit.git", self.up))
        self.assertFalse(mirror.fetched(self.m, self.home))
        mirror.fetch(self.m, self.home)
        self.assertTrue(mirror.fetched(self.m, self.home))
        self.assertEqual(self.git("-C", self.store, "for-each-ref", "--format=%(refname)"), "refs/heads/main")
        self.assertEqual(self.git("-C", self.store, "symbolic-ref", "HEAD"), "refs/heads/main")

        w = World(here="mac")
        fleet = Fleet(lambda: w.confs, "mac", None, self.m)
        lifecycle.make(Inv("/", {"WK_HOME": self.home}, fleet, w.clock, None, {}, {"driver": lambda inv, kind, machine: Plain()}), "x")
        src = self.home + "/ws/x/src"
        self.assertEqual(self.m.listdir(self.home + "/ws/x"), [".ready", "build", "src"])
        self.assertEqual(self.m.read(src + "/.git/objects/info/alternates"), self.store + "/objects\n")
        self.assertEqual(self.git("-C", src, "config", "--get", "remote.origin.url"), "https://github.com/WebKit/WebKit.git")
        self.assertEqual(self.git("-C", src, "remote", "get-url", "origin"), self.store)
        self.assertEqual(self.git("-C", src, "remote", "get-url", "--push", "origin"), "no-push://push-to-the-fork-remote")
        self.assertEqual(self.git("-C", src, "remote", "get-url", "--push", "fork"), "https://github.com/justinmichaud/WebKit.git")
        self.assertEqual([self.git("-C", src, "config", "--get", key) for key in
                          ("remote.origin.tagOpt", "remote.fork.tagOpt", "fetch.writeCommitGraph", "gc.writeCommitGraph")],
                         ["--no-tags", "--no-tags", "false", "false"])
        self.assertNotEqual(self.m.run(["git", "-C", src, "push", "origin", "main"]).rc, 0)

        self.assertEqual(self.m.run(["git", "-C", self.up, "commit", "--quiet", "--allow-empty", "-m", "two"], env=self.who).err, "")
        mirror.fetch(self.m, self.home)
        lifecycle.fetch(self.m, seams.Workspace("x", "mac", self.home + "/ws/x", "plain", seams.READY))
        self.assertEqual(self.git("-C", src, "log", "--format=%s", "-1", "origin/main"), "two")
        self.assertEqual(self.git("-C", src, "tag"), "")
