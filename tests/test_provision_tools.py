import os
import tempfile
import unittest

from tests import ROOT
from tests.killpoints import converges, dry_run_is_the_plan
from tests.provision import HOME, SHA, TOOLS, checkout, lanes, server
from tests.world import World
from wk.core import act, conf
from wk.core.clock import Clock
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Local, Result
from wk.provision import tools

LINE = 'export PATH=%s/bin:"$PATH"' % HOME
DELIVERY = ["[lin] git init -q %s" % TOOLS,
            "[lin] write %s/.git/wk.bundle" % TOOLS,
            "[lin] git -C %s fetch -q %s/.git/wk.bundle HEAD" % (TOOLS, TOOLS),
            "[lin] remove %s/.git/wk.bundle" % TOOLS,
            "[lin] git -C %s reset -q --hard %s" % (TOOLS, SHA),
            "[lin] git -C %s clean -qfd" % TOOLS]


def make(shell="/bin/bash"):
    w = World()
    checkout(w.machines["desk"])
    server(w.machines["lin"], shell)
    return w


class TestStage(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.own")
        self.w = make()
        self.lin = self.w.machines["lin"]

    def setup(self):
        return self.w.wk("machine", "setup", "lin")

    def test_the_server_gets_a_bundle_of_head_reset_to_its_sha_then_the_layout_the_link_and_the_profile_line(self):
        r = self.setup()
        self.assertEqual((r.code, r.out), (0, "wk  changed\nlin: 1 changed\n"))
        self.assertEqual(r.effects, DELIVERY + ["[lin] install -d -m 755 %s/%s" % (HOME, name) for name in ("bin", "cache", "git", "ws")]
                         + ["[lin] install -d -m 700 %s/secrets" % HOME, "[lin] ln -sfn %s/wk %s/bin/wk" % (TOOLS, HOME),
                            """[lin] sh -c 'printf "\\n%%s\\n" "$1" >>"$2"' sh '%s' /home/u/.profile""" % LINE])
        self.assertEqual((self.lin.files[TOOLS + "/.git/HEAD"], self.lin.modes, self.lin.files["/home/u/.profile"]), (SHA, {TOOLS + "/.git/wk.bundle": "600"}, "\n%s\n" % LINE))

    def test_the_bundle_is_read_from_this_checkout_and_travels_as_a_file_write(self):
        r = self.setup()
        self.assertIn("[desk] git -C %s bundle create - HEAD" % ROOT, r.calls)
        self.assertEqual(self.lin.files[TOOLS + "/.git/FETCH_HEAD"], SHA)

    def test_a_second_run_changes_nothing(self):
        self.setup()
        r = self.setup()
        self.assertEqual((r.out, r.effects), ("wk  unchanged\nlin: no changes\n", []))

    def test_uncommitted_changes_are_refused_before_the_server_is_touched(self):
        checkout(self.w.machines["desk"], status="?? scratch\n")
        r = self.setup()
        self.assertEqual((r.code, self.lin.calls), (1, []))
        self.assertIn("error: stage wk failed on lin: this checkout has uncommitted changes, and a machine is given a commit and nothing else\n"
                      "    commit or remove them: git -C %s status --short\n    --force crosses this\n" % ROOT, r.err)

    def test_force_gives_the_server_the_commit_while_the_checkout_has_changes_and_says_so_again_at_exit(self):
        checkout(self.w.machines["desk"], status="?? scratch\n")
        r = self.w.wk("machine", "setup", "lin", "--force")
        self.assertEqual((r.code, self.lin.files[TOOLS + "/.git/HEAD"]), (0, SHA))
        self.assertIn("warning: this command was forced past: this checkout has uncommitted changes", r.err)

    def test_a_tree_with_no_commit_is_refused(self):
        checkout(self.w.machines["desk"], sha="")
        r = self.setup()
        self.assertEqual((r.code, self.lin.calls), (1, []))
        self.assertIn("%s has no commit to give a machine: run wk from a clone of its repository" % ROOT, r.err)

    def test_a_bundle_that_cannot_be_made_is_refused(self):
        self.w.machines["desk"].on(("git", "-C", ROOT, "bundle", "create", "-", "HEAD"), Result(128, "", "fatal: no space\n"))
        r = self.setup()
        self.assertEqual((r.effects, r.err.splitlines()[0]), ([], "error: stage wk failed on lin: cannot bundle %s at %s: fatal: no space" % (ROOT, SHA)))

    def test_a_server_at_another_commit_or_with_an_edited_tree_is_given_the_commit_again(self):
        self.setup()
        self.lin.files[TOOLS + "/.git/HEAD"] = "0ld" * 13
        self.assertEqual(self.setup().effects, DELIVERY)
        self.lin.files[TOOLS + "/edited"] = "x"
        self.assertEqual((self.setup().effects, TOOLS + "/edited" in self.lin.files), (DELIVERY, False))

    def test_a_command_that_fails_on_the_server_stops_the_stage_naming_it(self):
        self.lin.on(("git", "-C", TOOLS, "fetch"), Result(128, "", "fatal: bad bundle\n"))
        r = self.setup()
        self.assertEqual((r.code, r.effects), (1, DELIVERY[:3]))
        self.assertIn("stage wk failed on lin: `git -C %s fetch -q %s/.git/wk.bundle HEAD` failed on lin (exit 128): fatal: bad bundle\n"
                      "    `wk doctor` names what lin lacks\n" % (TOOLS, TOOLS), r.err)

    def test_an_old_python_is_refused_and_nothing_is_installed(self):
        self.lin.on(("python3",), "3.8\n")
        r = self.setup()
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("stage wk failed on lin: python3 on lin: 3.8 is older than 3.9\n    install it on lin with the system's package manager\n", r.err)

    def test_a_server_without_git_is_refused(self):
        self.lin.on(("git", "--version"), Result(127, "", "sh: git: command not found\n"))
        r = self.setup()
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("git on lin: sh: git: command not found\n    install it on lin", r.err)

    def test_a_wrongly_moded_secrets_directory_is_made_private(self):
        self.setup()
        self.lin.files[HOME + "/secrets"] = "drwxr-xr-x"
        self.assertEqual(self.setup().effects, ["[lin] install -d -m 700 %s/secrets" % HOME])

    def test_a_link_to_another_checkout_is_replaced(self):
        self.setup()
        self.lin.files[HOME + "/bin/wk"] = "/elsewhere/wk"
        self.assertEqual(self.setup().effects, ["[lin] ln -sfn %s/wk %s/bin/wk" % (TOOLS, HOME)])

    def test_zsh_reads_zprofile(self):
        w = make("/bin/zsh")
        w.wk("machine", "setup", "lin")
        self.assertEqual(w.machines["lin"].files["/home/u/.zprofile"], "\n%s\n" % LINE)

    def test_bash_reads_bash_profile_when_there_is_one_and_the_line_is_appended(self):
        self.lin.files["/home/u/.bash_profile"] = "umask 022"
        self.setup()
        self.assertEqual((self.lin.files["/home/u/.bash_profile"], "/home/u/.profile" in self.lin.files), ("umask 022\n%s\n" % LINE, False))

    def test_killed_after_any_effect_and_typed_again_it_converges(self):
        converges(self, make, "machine", "setup", "lin")

    def test_a_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, make, "machine", "setup", "lin")


class TestRealGit(unittest.TestCase):
    def git(self, *argv):
        r = Local().run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"] + list(argv))
        self.assertEqual((r.rc, r.err), (0, ""))
        return r.out.strip()

    def test_real_git_and_coreutils_converge_a_directory_and_a_second_run_changes_nothing(self):
        self.addCleanup(act.begin, act.flags)
        act.begin(act.Flags())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            src, far = tmp + "/src", Local()
            far.home = tmp + "/far"
            self.git("init", "-q", src)
            with open(src + "/wk", "w") as f:
                f.write("#!/bin/sh\n")
            self.git("-C", src, "add", "wk")
            self.git("-C", src, "commit", "-q", "-m", "one")
            confs = {"desk": conf.parse("desk", "kind=client"), "lin": conf.parse("lin", "kind=linux")}
            inv = Inv(src, {}, Fleet(lambda: confs, "desk", lambda name: far, Local()), Clock(), None, {})
            home = far.home + "/wk"
            self.assertEqual((tools.converge(inv, confs["lin"]), tools.converge(inv, confs["lin"])), (True, False))
            self.assertEqual((self.git("-C", home + "/tools", "rev-parse", "HEAD"), os.readlink(home + "/bin/wk"), sorted(os.listdir(home))),
                             (self.git("-C", src, "rev-parse", "HEAD"), home + "/tools/wk", ["bin", "cache", "git", "secrets", "tools", "ws"]))
            self.assertEqual((os.stat(home + "/secrets").st_mode & 0o777, os.path.exists(home + "/tools/.git/wk.bundle")), (0o700, False))
            written = [name for name in (".zprofile", ".profile") if os.path.exists(far.home + "/" + name)]
            with open(far.home + "/" + written[0]) as f:
                self.assertEqual((len(written), f.read()), (1, '\nexport PATH=%s/bin:"$PATH"\n' % home))
