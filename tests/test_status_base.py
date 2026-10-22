"""The base a workspace tracks: the upstream WebKit line a checkout's HEAD descends from (git.PROBE's `wsbase`,
run inside the workspace by `wk ls` and `wk status`), against real disposable repositories; an image
workspace's base from the preset conf this checkout ships."""
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO
from tests.test_status import machine_rec, render

sys.path.insert(0, str(REPO / "lib"))
from wk import git, places, statusview  # noqa: E402
from wk.machine import Local  # noqa: E402

CONFIGS = REPO / "image" / "presets"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "wk-tools test")
    (repo / "f").write_text("one\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "init")


def upstream_line(repo):
    """The probe `wk ls` and `wk status` run inside a workspace, run in `repo`."""
    return git.probe(Local().run, str(repo))["wsbase"]


class TestUpstreamLine(unittest.TestCase):
    def setUp(self):
        self.repo = self.new_repo()

    def new_repo(self):
        tmp = tempfile.TemporaryDirectory(prefix="wk-base-")
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name) / "r"
        _init_repo(repo)
        return repo

    def test_the_line_a_tracked_branch_names(self):
        for branch, remote, ref, want in (("main", "origin", "main", "main"), ("work", "wpe", "webkitglib/2.52", "2.52"),
                                          ("eng/stringimpl-2.38", "fork", "eng/stringimpl-2.38", "?")):
            with self.subTest(ref=ref):
                repo = self.new_repo()
                _git(repo, "checkout", "-qB", branch)
                _git(repo, "remote", "add", remote, "https://example.invalid/%s.git" % remote)
                _git(repo, "update-ref", "refs/remotes/%s/%s" % (remote, ref), "HEAD")
                _git(repo, "branch", "--set-upstream-to=%s/%s" % (remote, ref), branch)
                self.assertEqual(upstream_line(repo), want)

    def test_detached_with_nothing_reachable_is_unknown(self):
        _git(self.repo, "checkout", "-q", "--detach", "HEAD")
        self.assertEqual(upstream_line(self.repo), "?")

    def test_detached_but_reachable_from_two_releases_picks_the_newer(self):
        repo = self.repo
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        (repo / "f").write_text("two\n")
        _git(repo, "commit", "-qam", "second")
        _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.46", base)
        (repo / "f").write_text("three\n")
        _git(repo, "commit", "-qam", "third")
        _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.52", "HEAD")
        _git(repo, "checkout", "-q", "--detach", base)
        self.assertEqual(upstream_line(repo), "2.52")


class TestImageBase(unittest.TestCase):
    """An image workspace's base is its preset's own CFG_RELEASE, read from the real conf this checkout ships."""

    def test_an_image_workspace_reads_its_presets_release(self):
        for kind, preset in (("buildroot", "webkit-2.52-buildroot-rpi3-32"), ("yocto", "wpewebkit-2.46-yocto-rpi3-32")):
            with self.subTest(kind=kind):
                release = re.search(r"^CFG_RELEASE=(\S+)", (CONFIGS / (preset + ".conf")).read_text(), re.M).group(1)
                self.assertEqual(places.image_base(str(REPO), "%s-%s" % (kind, preset)), release)

    def test_a_plain_checkout_name_is_not_an_image_workspace(self):
        self.assertIsNone(places.image_base(str(REPO), "stringimpl238"))
        self.assertIsNone(places.image_base(str(REPO), "yocto-no-such-preset"))


class TestJsonCarriesTheBase(unittest.TestCase):
    """One merged document feeds every view: a field present in text is present in json."""

    def test_workspace_base_survives_into_json(self):
        recs = [machine_rec("moose"), dict(kind="workspace", machine="moose", method="container", name="stringimpl238", state="running",
                                           ws="present", branch="eng/stringimpl-2.38", base="?")]
        self.assertIn("stringimpl238", render(recs))
        self.assertEqual(statusview.merge(recs)["machines"][0]["methods"][0]["workspaces"][0]["base"], "?")


if __name__ == "__main__":
    unittest.main()
