"""The base a workspace tracks: the upstream WebKit line a checkout's HEAD descends from (places.upstream_line(),
run inside the workspace by `wk ls` and `wk status`), against real disposable repositories; an image
workspace's base from the preset conf this checkout ships; and the SDK image's freshness as the renderer
words it."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash
from tests.test_status import machine_rec, render

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402

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
    """The script `wk status` runs inside a workspace, run with $PWD inside `repo`."""
    cp = bash(places.upstream_line(), cwd=str(repo))
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


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


def sdk_rec(machine, **extra):
    return dict(kind="sdk", machine=machine, tag="2.53-v9-abc0000", pulled="2026-08-01", **extra)


class TestSdkImageRendering(unittest.TestCase):
    """The renderer's wording for the three verdicts the collector's sdk_record decides (tests.test_status.TestSdkDecision)."""

    def test_each_verdict_s_words(self):
        for extra, words in (({"unknown": "registry did not answer within 1s"}, ["unknown -- registry did not answer within 1s"]),
                             ({"upstream": "2.53-v9-abc0000"}, ["current", "2.53-v9-abc0000"]),
                             ({"upstream": "2.53-v11-def0000"}, ["behind (2.53-v11-def0000)"])):
            with self.subTest(extra=extra):
                out = render([machine_rec("moose"), sdk_rec("moose", **extra)]).stdout
                for w in words:
                    self.assertIn(w, out)


def workspace_rec(machine, name, **extra):
    return dict(kind="workspace", machine=machine, method="container", name=name, **extra)


class TestJsonCarriesBaseAndSdk(unittest.TestCase):
    """One merged document feeds every view: a field present in text is present in json."""

    def test_workspace_base_survives_into_json(self):
        recs = [machine_rec("moose"), workspace_rec("moose", "stringimpl238", state="running", ws="present", branch="eng/stringimpl-2.38", base="?"),
                {"kind": "exit", "code": 0}]
        self.assertIn("stringimpl238", render(recs).stdout)
        moose = next(m for m in json.loads(render(recs, "json").stdout)["machines"] if m["name"] == "moose")
        self.assertEqual(moose["methods"][0]["workspaces"][0]["base"], "?")

    def test_sdk_record_survives_into_json(self):
        recs = [machine_rec("moose"), sdk_rec("moose", upstream="2.53-v11-def0000"), {"kind": "exit", "code": 0}]
        moose = next(m for m in json.loads(render(recs, "json").stdout)["machines"] if m["name"] == "moose")
        self.assertEqual((moose["sdk"][0]["tag"], moose["sdk"][0]["upstream"]), ("2.53-v9-abc0000", "2.53-v11-def0000"))


if __name__ == "__main__":
    unittest.main()
