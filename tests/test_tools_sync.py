"""Putting wk-tools on a machine across ssh (lib/tools.sh, and the two
drivers that use it).

The rule under test: a machine is given a *commit*, never a file copy. The
copy over there is a real checkout whose HEAD is this tree's HEAD, converged
from whatever was there before -- nothing, a directory an older tooling
copied file by file, another commit, a dirty tree -- and an uncommitted tree
here is refused by name instead of being copied.

Nothing here reaches a machine. The far side is a directory in a scratch
tree, reached through a fake ssh that runs the command string it is handed
with `bash -c`: exactly the shape both drivers hand `tools_push` (their own
ssh wrapper, one command string, the bundle on stdin).

Run: python3 -m unittest tests.test_tools_sync -v
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import func_body as support_func_body
from tests.support import REPO, bash, stub_path

VM = REPO / "targets" / "vm.sh"
REMOTE = REPO / "targets" / "remote.sh"

TOUCHED = [
    "lib/tools.sh",
    "targets/remote.sh",
    "targets/vm.sh",
    "cmd/sync",
    "cmd/status",
    "cmd/remote",
]

# The helper, sourced from this tree, then pointed at a scratch tree: WK_ROOT
# is what tools_push pushes, so the test owns a whole repository's worth of
# state without touching this one.
PRELUDE = f"""
WK_ROOT={REPO}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/tools.sh"
"""

# The far side. `bash -c "$1"` is what an ssh wrapper does with its one
# command string, and stdin reaches it the same way -- which is what carries
# the bundle.
FAR = 'far() { bash -c "$1"; }\n'


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def func_body(path, name):
    """A shell function's body, read from a file. One implementation of the
    lift lives in tests/support.py; this only says which file."""
    return support_func_body(Path(path).read_text(), name)


class ToolsPushCase(unittest.TestCase):
    """A scratch source tree (committed), and a far directory to converge."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-tools-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.far = self.tmp / "far" / "tools"
        self.src.mkdir()
        git(self.src, "init", "-q", ".")
        (self.src / "wk").write_text("#!/bin/sh\necho one\n")
        (self.src / ".gitignore").write_text("ignored-here\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.sha = git(self.src, "rev-parse", "HEAD").stdout.strip()

    def push(self, extra=""):
        return bash(
            PRELUDE + FAR + f'WK_ROOT={self.src}\n' + extra
            + f'tools_push {self.far} far\n'
        )

    def far_head(self):
        cp = git(self.far, "rev-parse", "HEAD", check=False)
        return cp.stdout.strip() if cp.returncode == 0 else ""


class TestConverge(ToolsPushCase):
    def test_first_push_makes_a_checkout_at_this_trees_head(self):
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertTrue((self.far / ".git").is_dir(), "not a checkout")
        self.assertEqual((self.far / "wk").read_text(), "#!/bin/sh\necho one\n")

    def test_a_second_push_changes_nothing_and_still_reports_ok(self):
        self.assertEqual(self.push().returncode, 0)
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)

    def test_a_far_checkout_at_another_commit_is_reset_to_the_pushed_one(self):
        self.assertEqual(self.push().returncode, 0)
        (self.far / "theirs").write_text("a commit only they have\n")
        git(self.far, "add", "-A")
        git(self.far, "commit", "-qm", "theirs")
        diverged = self.far_head()
        self.assertNotEqual(diverged, self.sha)

        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "theirs").exists(),
                         "a commit of their own survived the reset")

    def test_a_directory_that_is_not_a_checkout_is_replaced(self):
        self.far.mkdir(parents=True)
        (self.far / "wk").write_text("#!/bin/sh\necho months old\n")
        (self.far / "gone.sh").write_text("a file this tree no longer has\n")

        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "gone.sh").exists(),
                         "the file copy was merged into, not replaced")

    def test_a_dirty_far_tree_is_overwritten_and_its_ignored_files_kept(self):
        self.assertEqual(self.push().returncode, 0)
        (self.far / "wk").write_text("edited over there\n")
        (self.far / "untracked").write_text("dropped in over there\n")
        (self.far / "ignored-here").write_text("this machine's own\n")

        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertEqual((self.far / "wk").read_text(), "#!/bin/sh\necho one\n")
        self.assertFalse((self.far / "untracked").exists())
        self.assertTrue((self.far / "ignored-here").exists(),
                        "an ignored file that machine keeps for itself was deleted")

    def test_a_half_made_far_repository_converges_on_a_re_run(self):
        """Crash-only: killed between `git init` and the fetch, the far side
        is an empty repository with no HEAD. The re-run converges it."""
        self.far.mkdir(parents=True)
        git(self.far, "init", "-q", ".")
        self.assertEqual(self.far_head(), "")

        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)


class TestRefusal(ToolsPushCase):
    def test_an_uncommitted_change_here_is_refused_with_the_remedy(self):
        (self.src / "wk").write_text("#!/bin/sh\necho edited\n")
        cp = self.push()
        self.assertNotEqual(cp.returncode, 0, "a dirty tree is not a success")
        self.assertIn("uncommitted changes", cp.stderr)
        self.assertIn("compare", cp.stderr)          # why
        self.assertIn("commit", cp.stderr)            # the remedy
        self.assertEqual(self.far_head(), "")
        self.assertFalse(self.far.exists(), "something was sent anyway")

    def test_an_untracked_non_ignored_file_here_is_not_a_dirty_tree(self):
        # Dirtiness is tracked-only: a new file not yet `git add`-ed is not
        # a change to this repository any more than an ignored one is --
        # neither has a commit for a machine to be given.
        (self.src / "new.sh").write_text("not added yet\n")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "new.sh").exists(),
                         "an untracked file was sent as if it were part of the tree")

    def test_an_ignored_file_here_is_not_a_dirty_tree(self):
        (self.src / "ignored-here").write_text("machine-local\n")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)
        self.assertFalse((self.far / "ignored-here").exists(),
                         "an ignored file was sent as if it were part of the tree")

    def test_a_tree_that_is_not_a_checkout_at_all_is_refused(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        cp = bash(PRELUDE + FAR + f'WK_ROOT={plain}\ntools_push {self.far} far\n')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not a git checkout", cp.stderr)
        self.assertIn("git clone", cp.stderr)
        self.assertFalse(self.far.exists())

    def test_a_far_side_that_answers_with_another_sha_is_not_reported_ok(self):
        """The verdict is the far side's own `git rev-parse HEAD`, not the
        exit status of the copy: a transport that swallows the work is
        caught."""
        cp = bash(
            PRELUDE + 'far() { cat >/dev/null; echo 0000000000000000000000000000000000000000; }\n'
            + f'WK_ROOT={self.src}\ntools_push {self.far} far\n'
        )
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("did not end at", cp.stderr)


class TestTheDestinationHasAFloorUnderIt(ToolsPushCase):
    """The far side's convergence is an `rm -rf "$d"` whenever $d is not
    already a checkout, so a destination that is the root or an account's home
    would erase it. Refused here, before a bundle or a script exists -- the
    script is never even generated, which is the property these check."""

    REFUSED = ("", "/", "/opt", "opt/wk-tools", "/opt/wk-tools/", "$HOME")

    def _push_to(self, dest):
        return bash(PRELUDE + FAR + f'WK_ROOT={self.src}\ntools_push "{dest}" far\n',
                    env={"HOME": str(self.tmp / "home")})

    def test_each_dangerous_destination_is_refused(self):
        for dest in self.REFUSED:
            with self.subTest(dest=dest):
                cp = self._push_to(dest)
                self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn("refusing to push wk-tools", cp.stderr)

    def test_a_refusal_generates_no_script_at_all(self):
        """`tools_converge_script / <sha>` really does emit `rm -rf '/'`; the
        refusal has to come before anything can hand that to a machine."""
        cp = bash(PRELUDE + 'far() { cat > "$WK_TEST_SCRIPT"; echo x; }\n'
                  + f'WK_ROOT={self.src}\ntools_push / far\n',
                  env={"WK_TEST_SCRIPT": str(self.tmp / "script")})
        self.assertNotEqual(cp.returncode, 0)
        self.assertFalse((self.tmp / "script").exists(),
                         "the far side was handed a command anyway")

    def test_the_home_directory_of_the_account_is_refused_by_name(self):
        home = self.tmp / "home"
        home.mkdir()
        cp = bash(PRELUDE + FAR + f'WK_ROOT={self.src}\ntools_push "{home}" far\n',
                  env={"HOME": str(home)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(home.exists())

    def test_a_two_component_absolute_path_is_allowed(self):
        """The floor refuses the dangerous shapes and nothing else: every real
        destination (t_tools) is an absolute path well below two components."""
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestTheFarSideRefusesBeforeItDeletes(ToolsPushCase):
    """The converge script's own guards. Each is checked by making the far
    side the shape it guards against and looking at what survives."""

    def test_a_machine_with_no_git_keeps_its_checkout(self):
        """The `is this a checkout` test needs git; without it the guard reads
        false, and the `rm -rf` would take the real tree before `git init`
        failed."""
        self.assertEqual(self.push().returncode, 0)
        self.assertEqual(self.far_head(), self.sha)

        nogit = self.tmp / "nogit-bin"
        nogit.mkdir()
        for tool in ("sh", "cat", "rm", "mkdir", "head", "tr", "tail", "sed", "printf"):
            src = shutil.which(tool)
            if src:
                os.symlink(src, nogit / tool)
        far = 'far() { PATH="%s" /bin/sh -c "$1"; }\n' % nogit
        cp = bash(PRELUDE + far + f'WK_ROOT={self.src}\ntools_push {self.far} far\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no git on this machine", cp.stdout + cp.stderr)
        self.assertTrue((self.far / ".git").is_dir(),
                        "the checkout was deleted by a machine that has no git")

    def test_a_symlinked_destination_is_refused_by_name(self):
        real = self.tmp / "real-tools"
        real.mkdir()
        (real / "keep").write_text("a real directory somebody linked to\n")
        self.far.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(real, self.far)

        cp = self.push()
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("symlink", cp.stdout + cp.stderr)
        self.assertTrue(self.far.is_symlink(), "the link was replaced by a directory")
        self.assertTrue((real / "keep").exists())

    def test_replacing_a_loose_directory_says_so(self):
        """That branch takes the ignored files with it -- a build directory, a
        machine-local conf -- so it is not allowed to be silent."""
        self.far.mkdir(parents=True)
        (self.far / "wk").write_text("loose\n")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("replacing", cp.stdout + cp.stderr)
        self.assertIn("not a git checkout", cp.stdout + cp.stderr)

    def test_converging_a_checkout_says_nothing_about_replacing(self):
        self.assertEqual(self.push().returncode, 0)
        cp = self.push()
        self.assertNotIn("replacing", cp.stdout + cp.stderr)


class TestRemoteDriver(ToolsPushCase):
    """targets/remote.sh's t_sync_tools, through its own `_rsh` -- with a
    fake `ssh` on PATH that runs the command string it is handed. Proves the
    driver hands tools_push a transport that carries the bundle on stdin."""

    SOURCE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/targets/remote.sh"
'''

    FAKE_SSH = """#!/bin/sh
# The command is ssh's last argument; everything before it is options and
# the destination, which a fake has no use for.
for a in "$@"; do cmd="$a"; done
exec bash -c "$cmd"
"""

    def _run(self, extra=""):
        with stub_path({"ssh": self.FAKE_SSH}) as binp:
            env = {"PATH": f"{binp}:{os.environ['PATH']}",
                   "WK_REMOTE_MARKER": str(self.tmp / "no-such-marker"),
                   "XDG_STATE_HOME": str(self.tmp / "state")}
            return bash(
                self.SOURCE
                + f'WK_TARGET=fakebox\nWK_REMOTE_HOST=fakebox\n'
                + '_remote_probe() { :; }\n'
                + f't_tools() {{ printf %s {self.far}; }}\n'
                + f'WK_ROOT={self.src}\n' + extra
                + 't_sync_tools ""\n',
                env=env,
            )

    def test_t_sync_tools_puts_a_checkout_on_the_box(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)

    def test_a_peer_is_not_pushed_to_at_all(self):
        cp = self._run(extra="WK_REMOTE_PEER=1\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse(self.far.exists(),
                         "a peer's own checkout was written over")


class TestVmDriver(ToolsPushCase):
    """targets/vm.sh's _push_tools, through its own `_ssh` -- the transport
    it binds the guest's address into. A fake `ssh` on PATH runs the command
    string it is handed, so the guest is a directory in a scratch tree."""

    SOURCE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/targets/vm.sh"
'''

    def test_push_tools_puts_a_checkout_in_the_guest(self):
        with stub_path({"ssh": TestRemoteDriver.FAKE_SSH}) as binp:
            env = {"PATH": f"{binp}:{os.environ['PATH']}",
                   "XDG_STATE_HOME": str(self.tmp / "state"),
                   "WK_VM_STORE": str(self.tmp / "vmstore")}
            cp = bash(
                self.SOURCE
                + f't_tools() {{ printf %s {self.far}; }}\n'
                + f'WK_ROOT={self.src}\n'
                + '_push_tools guest 10.0.0.1\n',
                env=env,
            )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.far_head(), self.sha)


class TestNoFileCopyLeft(unittest.TestCase):
    """The two ssh-reached drivers copy no tree: `rsync` survives in them
    only for pulling a *build* out (t_pull_dir) and for the WebKit seed."""

    def test_no_driver_rsyncs_the_wk_root(self):
        hits = []
        for f in (REMOTE, VM):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if "rsync" in line and "WK_ROOT" in line:
                    hits.append(f"{f}:{i}:{line.strip()}")
        self.assertEqual(hits, [], "wk-tools is still being file-copied")

    def test_both_drivers_push_through_the_one_helper(self):
        self.assertIn("tools_push", func_body(REMOTE, "t_sync_tools"))
        self.assertIn("tools_push", func_body(VM, "_push_tools"))

    def test_every_touched_file_parses(self):
        for rel in TOUCHED:
            cp = subprocess.run(["bash", "-n", str(REPO / rel)],
                                capture_output=True, text=True, timeout=30)
            self.assertEqual(cp.returncode, 0, f"{rel}: {cp.stderr}")


class TestGuestStartConverges(unittest.TestCase):
    """targets/vm.sh converges everything a guest gets through one
    `_converge_guest` -- marker, shell rc, clock, egress, agent token -- and
    the tooling checkout is on that list. Both t_start arms call it: the guest
    that was already running and the one this start booted."""

    def test_both_arms_converge_through_the_one_function(self):
        arms = func_body(VM, "t_start")
        self.assertEqual(2, arms.count('_converge_guest "$name" "$ip"'),
                         "t_start no longer calls _converge_guest from both arms")
        # No arm may still run a step of its own beside the call.
        for step in ("_push_tools", "_write_deploy_keys", "_settle_desktop"):
            self.assertNotIn(step, arms, f"t_start still runs {step} itself")

    def test_the_one_function_pushes_the_tools_once_and_only_warns(self):
        calls = [l for l in func_body(VM, "_converge_guest").splitlines()
                 if "_push_tools" in l]
        self.assertEqual(1, len(calls), calls)
        self.assertIn("|| warn", calls[0], "a failed tools push fails the start")
        self.assertIn("wk sync --tools", calls[0], "the warning names no remedy")

    def test_every_step_is_run_exactly_once(self):
        body = func_body(VM, "_converge_guest")
        for step in ("_push_tools", "_write_marker", "_write_shell_rc",
                     "_write_lldbinit", "_set_guest_clock", "_set_guest_egress",
                     "_write_claude_config", "_write_agent_secrets",
                     "_write_deploy_keys", "_settle_desktop", "_report_desktop"):
            with self.subTest(step=step):
                self.assertEqual(1, body.count(step + ' "$name"'), body)


class TestStatusToolsRow(unittest.TestCase):
    """cmd/status's wk-tools row for a machine across ssh: every copy is
    compared by commit -- a checkout's own, or, in the podman VM, this very
    checkout mounted in. A `-` sha is neither, and is never in sync.

    report_machine is lifted with its one helper and driven against a stubbed
    `t_wk version`, so no machine is reached and no record writer is faked
    beyond printing what it was handed."""

    LIFT = subprocess.run(
        ["sed", "-n", "/^sha_matches()/,/^}/p;/^report_machine()/,/^}/p",
         str(REPO / "cmd" / "status")],
        capture_output=True, text=True).stdout

    STUBS = """
rec_start() { printf 'kind=%s\\n' "$1"; }
rec_set()   { printf '%s=%s\\n' "$1" "${2:-}"; }
rec_opt()   { [ -n "${2:-}" ] && rec_set "$1" "$2"; return 0; }
rec_json()  { printf '%s=%s\\n' "$1" "$2"; }
rec_emit()  { printf 'emit\\n'; }
this_machine() { printf fakebox; }
default_target() { printf container; }
t_has_wk() { return 0; }
WK_TARGET_KIND=remote
_group_machine=fakebox
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-toolsrow-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src = self.tmp / "src"
        self.src.mkdir()
        git(self.src, "init", "-q", ".")
        (self.src / "f").write_text("one\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-qm", "one")
        self.short = git(self.src, "rev-parse", "--short", "HEAD").stdout.strip()
        self.full = git(self.src, "rev-parse", "HEAD").stdout.strip()

    def row(self, ver, extra=""):
        cp = bash(
            f'. "{REPO}/lib/common.sh"\n'
            + self.STUBS
            + 't_wk() { case "$1" in version) printf %s '
            + shlex.quote(ver) + ' ;; esac; }\n'
            + self.LIFT
            + f'WK_ROOT={self.src}\n' + extra
            + 'report_machine fakebox\n'
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = {}
        for line in cp.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    def test_a_checkout_at_this_trees_commit_reads_in_sync(self):
        row = self.row(f"sha={self.full}\ndirty=no\n")
        self.assertEqual(row["sha"], self.full)
        self.assertEqual(row["expect"], self.short)
        self.assertEqual(row["insync"], "true")
        self.assertNotIn("fix", row)

    def test_a_checkout_at_another_commit_differs_and_names_the_push(self):
        row = self.row("sha=0000000\ndirty=no\n")
        self.assertEqual(row["sha"], "0000000")
        self.assertEqual(row["expect"], self.short)
        self.assertEqual(row["insync"], "false")
        self.assertEqual(row["fix"], "wk sync --tools fakebox")

    def test_a_dirty_checkout_over_there_is_reported_as_dirty(self):
        row = self.row(f"sha={self.short}\ndirty=yes\n")
        self.assertEqual(row["dirty"], "true")
        self.assertEqual(row["insync"], "true")

    def test_a_copy_with_no_commit_is_never_in_sync(self):
        # A `-` sha is neither a checkout nor the podman VM's mount of this
        # one: it is always DIFFERS, whatever files happen to be there.
        row = self.row("sha=-\ndirty=unknown\n", extra="WK_IN_VM=1\n")
        self.assertEqual(row["insync"], "false")
        self.assertEqual(row["fix"],
                         "./setup   (recreates the machine with this checkout mounted at /opt/wk-tools)")

    def test_reporting_reaches_no_machine_and_syncs_nothing(self):
        """Read-only: the row is built from a question (`wk version` over
        there), and no statement in the path can write anything."""
        code = "\n".join(l for l in self.LIFT.splitlines()
                         if not l.lstrip().startswith("#"))
        for writer in ("tools_push", "t_sync", "rsync", "rev-parse HEAD --"):
            self.assertNotIn(writer, code, f"the status path runs {writer}")
        self.assertIn("t_wk version", code)


if __name__ == "__main__":
    unittest.main()


class TestAToolsSweepThatLostATargetFails(unittest.TestCase):
    """`wk sync --tools` prints "N target(s) did not take the tooling" when a
    driver's push failed; the command's exit status says the same, whatever
    the mirror and snapshot half did afterwards (measured live: it exited 0
    over three refused targets)."""

    def test_the_furniture_verdict_is_the_exit_status(self):
        text = (REPO / "cmd" / "sync").read_text()
        self.assertNotIn('sync_furniture "$TARGET" || true', text)
        self.assertIn('sync_furniture "$TARGET" || FURNITURE_RC=1', text)
        # Every exit after it carries the verdict: the no-local-store return,
        # the podman-VM pointer, and the end of the publish.
        self.assertEqual(3, text.count('exit "$FURNITURE_RC"'))
        self.assertTrue(text.rstrip().endswith('exit "$FURNITURE_RC"'))
