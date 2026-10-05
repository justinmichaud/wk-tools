"""The base a workspace tracks: the upstream WebKit line a checkout's HEAD descends from (targets.UPSTREAM_LINE,
run inside the workspace by `wk ls` and `wk status`), against real disposable repositories; an image
workspace's base from the profile conf this checkout ships; and the SDK image's freshness as the renderer
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
from wk import targets  # noqa: E402

CONFIGS = REPO / "image" / "configs"


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
    cp = bash(targets.UPSTREAM_LINE, cwd=str(repo))
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


class TestUpstreamLine(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="wk-base-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "r"
        _init_repo(self.repo)

    def track(self, branch, remote, ref):
        _git(self.repo, "checkout", "-qB", branch)
        _git(self.repo, "remote", "add", remote, "https://example.invalid/%s.git" % remote)
        _git(self.repo, "update-ref", "refs/remotes/%s/%s" % (remote, ref), "HEAD")
        _git(self.repo, "branch", "--set-upstream-to=%s/%s" % (remote, ref), branch)

    def test_tracking_origin_main_is_main(self):
        self.track("main", "origin", "main")
        self.assertEqual(upstream_line(self.repo), "main")

    def test_tracking_a_release_branch_through_a_fork_remote_is_the_release(self):
        self.track("work", "wpe", "webkitglib/2.52")
        self.assertEqual(upstream_line(self.repo), "2.52")

    def test_a_personal_fork_branch_with_nothing_reachable_is_unknown(self):
        self.track("eng/stringimpl-2.38", "fork", "eng/stringimpl-2.38")
        self.assertEqual(upstream_line(self.repo), "?")

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
    """An image workspace's base is its profile's own CFG_RELEASE, read from the real conf this checkout ships."""

    def test_an_image_workspace_reads_its_profiles_release(self):
        for kind, profile in (("buildroot", "webkit-2.52-buildroot-rpi3-32"), ("yocto", "wpewebkit-2.46-yocto-rpi3-32")):
            with self.subTest(kind=kind):
                release = re.search(r"^CFG_RELEASE=(\S+)", (CONFIGS / (profile + ".conf")).read_text(), re.M).group(1)
                self.assertEqual(targets.image_base(str(REPO), "%s-%s" % (kind, profile)), release)

    def test_a_plain_checkout_name_is_not_an_image_workspace(self):
        self.assertIsNone(targets.image_base(str(REPO), "stringimpl238"))
        self.assertIsNone(targets.image_base(str(REPO), "yocto-no-such-profile"))


def sdk_rec(machine, **extra):
    r = {"kind": "sdk", "machine": machine}
    r.update(extra)
    return r


class TestSdkImageRendering(unittest.TestCase):
    """The renderer's wording for the three verdicts the collector's sdk_record decides (tests.test_status.TestSdkDecision)."""

    def test_no_upstream_answer_renders_unknown_with_the_reason(self):
        recs = [machine_rec("moose"), sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01", unknown="registry did not answer within 1s")]
        self.assertIn("unknown -- registry did not answer within 1s", render(recs).stdout)

    def test_a_matching_upstream_tag_renders_current(self):
        out = render([machine_rec("moose"), sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01", upstream="2.53-v9-abc0000")]).stdout
        self.assertIn("current", out)
        self.assertIn("2.53-v9-abc0000", out)

    def test_a_newer_upstream_tag_renders_behind_and_names_it(self):
        out = render([machine_rec("moose"), sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01", upstream="2.53-v11-def0000")]).stdout
        self.assertIn("behind (2.53-v11-def0000)", out)


def workspace_rec(machine, name, **extra):
    r = {"kind": "workspace", "machine": machine, "method": "container", "name": name}
    r.update(extra)
    return r


class TestJsonCarriesBaseAndSdk(unittest.TestCase):
    """One merged document feeds every view: a field present in text is present in json."""

    def test_workspace_base_survives_into_json(self):
        recs = [machine_rec("moose"), workspace_rec("moose", "stringimpl238", state="running", ws="present", branch="eng/stringimpl-2.38", base="?"),
                {"kind": "exit", "code": 0}]
        self.assertIn("stringimpl238", render(recs).stdout)
        moose = next(m for m in json.loads(render(recs, "json").stdout)["machines"] if m["name"] == "moose")
        self.assertEqual(moose["methods"][0]["workspaces"][0]["base"], "?")

    def test_sdk_record_survives_into_json(self):
        recs = [machine_rec("moose"), sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01", upstream="2.53-v11-def0000"), {"kind": "exit", "code": 0}]
        moose = next(m for m in json.loads(render(recs, "json").stdout)["machines"] if m["name"] == "moose")
        self.assertEqual((moose["sdk"][0]["tag"], moose["sdk"][0]["upstream"]), ("2.53-v9-abc0000", "2.53-v11-def0000"))


if __name__ == "__main__":
    unittest.main()
