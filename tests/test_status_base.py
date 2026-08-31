"""The base a workspace tracks, and the container SDK image's freshness --
regression tests for the maintainer's defect (docs/defects: "wk status
should list the base a workspace is based on ... It should also check if wk
container sdk version is up to date"):

  (a) `ws_upstream_line` (lib/target.sh): the upstream WebKit line a checkout's
      HEAD descends from -- a branch tracking origin/main, one tracking a
      release branch through a fork remote, and a detached HEAD with nothing
      to go on -- against real, disposable git repos. `wk ls`'s `ws_git_base`
      and cmd/status's own probe lift this exact function rather than
      copying it (see the comment on it in cmd/status), so testing it here
      covers both.
  (b) `ws_image_base` (lib/target.sh): an image workspace's
      own base, read from the real `image/configs/*.conf` this checkout
      ships -- so the test tracks the file rather than a copied constant.
  (c) `report_sdk_image` (cmd/status): "unknown -- registry did not answer"
      when the upstream probe is stubbed to hang past `WK_FLEET_TIMEOUT`,
      "current"/"behind (<tag>)" when it answers -- both the shell's raw
      decision and lib/status-view.py's rendering of it.
  (d) `wk status --json` carries a workspace's `base` field and the `sdk`
      record the same way any other field survives from record to document
      (lib/status-view.py) -- the same recipe tests/test_status.py uses for
      fake workspaces (synthetic records fed straight to the renderer, no
      real machine required).

Run: python3 -m unittest tests.test_status_base -v
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash, rand_suffix
from tests.test_status import machine_rec, render

STATUS = REPO / "cmd" / "status"
CONFIGS = REPO / "image" / "configs"


def _lift(name, path=REPO / "lib" / "target.sh"):
    """A function's exact source, out of `path` -- cmd/status and cmd/ls ship
    `ws_upstream_line` into a workspace's shell with `declare -f`, so the body
    under test is the body that runs there."""
    cp = subprocess.run(
        ["sed", "-n", "/^%s()/,/^}/p" % name, str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert cp.stdout.strip(), "%s() not found in %s -- has it moved or been renamed?" % (name, path)
    return cp.stdout


UPSTREAM_LINE = _lift("ws_upstream_line")
IMAGE_BASE = _lift("ws_image_base")
SDK_IMAGE = _lift("report_sdk_image", STATUS)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo):
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@example.invalid")
    _git(repo, "config", "user.name", "wk-tools test")
    (repo / "f").write_text("one\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-qm", "init")


def upstream_line(repo):
    """`ws_upstream_line`, run with $PWD inside `repo` -- the exact function
    cmd/status execs into a real workspace and cmd/ls lifts, against a
    throwaway repo instead of a container."""
    cp = bash(UPSTREAM_LINE + "\nws_upstream_line\n", cwd=str(repo))
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


class TestUpstreamLineFromATrackingBranch(unittest.TestCase):
    """A checkout whose HEAD has a real upstream: the tracked branch names
    the line directly, no walking refs needed."""

    def test_tracking_origin_main_is_main(self):
        with tempfile.TemporaryDirectory(prefix="wk-base-") as tmp:
            repo = Path(tmp) / "r"
            _init_repo(repo)
            _git(repo, "remote", "add", "origin", "https://example.invalid/WebKit.git")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            _git(repo, "branch", "--set-upstream-to=origin/main", "main")
            self.assertEqual(upstream_line(repo), "main")

    def test_tracking_a_release_branch_through_a_fork_remote_is_the_release(self):
        """`wpe/webkitglib/2.52` -> `2.52`: the release number, not the
        remote's own name and not the whole ref."""
        with tempfile.TemporaryDirectory(prefix="wk-base-") as tmp:
            repo = Path(tmp) / "r"
            _init_repo(repo)
            _git(repo, "checkout", "-qb", "work")
            _git(repo, "remote", "add", "wpe", "https://example.invalid/wpe.git")
            _git(repo, "update-ref", "refs/remotes/wpe/webkitglib/2.52", "HEAD")
            _git(repo, "branch", "--set-upstream-to=wpe/webkitglib/2.52", "work")
            self.assertEqual(upstream_line(repo), "2.52")


class TestUpstreamLineWithoutATrackingBranch(unittest.TestCase):
    """Detached, or tracking a branch that names neither main nor a release
    (a personal fork branch): the nearest of those two kinds of ref that
    contains HEAD, never a guess from the untrusted branch name."""

    def test_detached_with_nothing_reachable_is_unknown(self):
        with tempfile.TemporaryDirectory(prefix="wk-base-") as tmp:
            repo = Path(tmp) / "r"
            _init_repo(repo)
            _git(repo, "checkout", "-q", "--detach", "HEAD")
            self.assertEqual(upstream_line(repo), "?")

    def test_detached_but_reachable_from_two_releases_picks_the_newer(self):
        """HEAD is an ancestor of both webkitglib/2.46 and webkitglib/2.52:
        the more specific (higher) release wins, not whichever ref sorts
        first."""
        with tempfile.TemporaryDirectory(prefix="wk-base-") as tmp:
            repo = Path(tmp) / "r"
            _init_repo(repo)
            base = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            (repo / "f").write_text("two\n")
            _git(repo, "commit", "-qam", "second")
            _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.46", base)
            (repo / "f").write_text("three\n")
            _git(repo, "commit", "-qam", "third")
            _git(repo, "update-ref", "refs/remotes/origin/webkitglib/2.52", "HEAD")
            _git(repo, "checkout", "-q", "--detach", base)
            self.assertEqual(upstream_line(repo), "2.52")

    def test_a_personal_fork_branch_with_nothing_reachable_is_unknown(self):
        """`@{u}` resolves, but to neither `main` nor `webkitglib/*` --
        a real-world shape (moose:stringimpl238 tracks
        `forkwpe/eng/stringimpl-2.38`) that must not be misread as a release
        just because the branch name contains one."""
        with tempfile.TemporaryDirectory(prefix="wk-base-") as tmp:
            repo = Path(tmp) / "r"
            _init_repo(repo)
            _git(repo, "checkout", "-qb", "eng/stringimpl-2.38")
            _git(repo, "remote", "add", "fork", "https://example.invalid/fork.git")
            _git(repo, "update-ref", "refs/remotes/fork/eng/stringimpl-2.38", "HEAD")
            _git(repo, "branch", "--set-upstream-to=fork/eng/stringimpl-2.38", "eng/stringimpl-2.38")
            self.assertEqual(upstream_line(repo), "?")


class TestImageBase(unittest.TestCase):
    """An image workspace's base is its profile's own CFG_RELEASE, whatever
    the checkout inside says -- read from the real conf this checkout ships,
    so a renamed or repointed profile fails this test rather than going
    unnoticed."""

    def _release_of(self, conf_name):
        conf = CONFIGS / conf_name
        self.assertTrue(conf.exists(), "fixture profile missing: %s" % conf)
        m = re.search(r"^CFG_RELEASE=(\S+)", conf.read_text(), re.M)
        self.assertIsNotNone(m, "%s has no CFG_RELEASE any more" % conf)
        return m.group(1)

    def _ws_image_base(self, ws):
        cp = bash(IMAGE_BASE + "\nws_image_base %s\n" % ws)
        return cp.returncode, cp.stdout.strip()

    def test_a_buildroot_workspace_reads_its_profiles_release(self):
        profile = "webkit-2.52-buildroot-rpi3-32"
        expected = self._release_of(profile + ".conf")
        rc, out = self._ws_image_base("buildroot-" + profile)
        self.assertEqual(rc, 0)
        self.assertEqual(out, expected)

    def test_a_yocto_workspace_reads_its_profiles_release(self):
        profile = "wpewebkit-2.46-yocto-rpi3-32"
        expected = self._release_of(profile + ".conf")
        rc, out = self._ws_image_base("yocto-" + profile)
        self.assertEqual(rc, 0)
        self.assertEqual(out, expected)

    def test_a_plain_checkout_name_is_not_an_image_workspace(self):
        """Neither `yocto-`- nor `buildroot-`-prefixed: nothing to read, and
        the function says so by failing rather than guessing at a profile."""
        rc, out = self._ws_image_base("stringimpl238")
        self.assertNotEqual(rc, 0)
        self.assertEqual(out, "")


class TestSdkImageDecision(unittest.TestCase):
    """`report_sdk_image`'s own decision -- current/behind/unknown -- from a
    stubbed local pull and upstream tag list, `capped` under a real
    (short) `WK_FLEET_TIMEOUT` exactly as `wk status` runs it. The record
    writer (`rec_*`, cmd/status) is stubbed to plain shell variables here:
    what is under test is the decision, not the JSON encoder every other
    `report_*` function already exercises."""

    STUBS = (
        ". lib/common.sh\n"
        "rec_start() { :; }\n"
        "rec_set()   { eval \"SDK_$1=\\$2\"; }\n"
        "rec_opt()   { [ -n \"${2:-}\" ] && rec_set \"$1\" \"$2\" || true; }\n"
        "rec_json()  { rec_set \"$1\" \"$2\"; }\n"
        "rec_emit()  { :; }\n"
    ) + SDK_IMAGE

    def _run(self, script, timeout=10):
        cp = bash(self.STUBS + script, timeout=timeout)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        fields = {}
        for line in cp.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                fields[k] = v
        return fields

    def test_a_registry_that_never_answers_is_unknown(self):
        """`t_sdk_upstream` stubbed to hang: `capped` kills it at
        `WK_FLEET_TIMEOUT`, and the record says so rather than hanging
        `wk status` or guessing an answer."""
        fields = self._run(
            """
t_sdk_local() { printf 'image=ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\\ncreated=2026-08-01\\n'; }
t_sdk_upstream() { sleep 5; echo should-not-appear; }
WK_FLEET_TIMEOUT=1 report_sdk_image testmachine
printf 'tag=%s\\n' "$SDK_tag"
printf 'upstream=%s\\n' "${SDK_upstream:-}"
printf 'unknown=%s\\n' "${SDK_unknown:-}"
""",
            timeout=15,
        )
        self.assertEqual(fields.get("tag"), "2.53-v9-abc0000")
        self.assertEqual(fields.get("upstream"), "")
        self.assertIn("registry did not answer within 1s", fields.get("unknown", ""))

    def test_the_registry_echoing_the_local_tag_is_current(self):
        fields = self._run(
            """
t_sdk_local() { printf 'image=ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\\ncreated=2026-08-01\\n'; }
t_sdk_upstream() { echo '2.53-v9-abc0000'; }
WK_FLEET_TIMEOUT=4 report_sdk_image testmachine
printf 'tag=%s\\n' "$SDK_tag"
printf 'upstream=%s\\n' "${SDK_upstream:-}"
printf 'unknown=%s\\n' "${SDK_unknown:-}"
"""
        )
        self.assertEqual(fields.get("tag"), fields.get("upstream"))
        self.assertEqual(fields.get("unknown", ""), "")

    def test_a_newer_tag_upstream_is_behind(self):
        """The real ghcr.io/igalia/wkdev-sdk scheme (`<ver>-v<N>-<sha>`):
        the highest v-number sharing the local tag's own version wins, not
        whichever line the registry lists first."""
        fields = self._run(
            """
t_sdk_local() { printf 'image=ghcr.io/igalia/wkdev-sdk:2.53-v9-abc0000\\ncreated=2026-08-01\\n'; }
t_sdk_upstream() { printf '2.53-v9-abc0000\\n2.53-v11-def0000\\n24.04_arm32\\n'; }
WK_FLEET_TIMEOUT=4 report_sdk_image testmachine
printf 'upstream=%s\\n' "${SDK_upstream:-}"
"""
        )
        self.assertEqual(fields.get("upstream"), "2.53-v11-def0000")


def sdk_rec(machine, **extra):
    r = {"kind": "sdk", "machine": machine}
    r.update(extra)
    return r


class TestSdkImageRendering(unittest.TestCase):
    """lib/status-view.py's own wording for the three verdicts -- exercised
    the way lib/status-view.py's contract with the shell side is:
    `report_sdk_image` decides, this renders exactly what it decided."""

    def test_no_upstream_answer_renders_unknown_with_the_reason(self):
        recs = [
            machine_rec("moose"),
            sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01",
                    unknown="registry did not answer within 1s"),
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("unknown -- registry did not answer within 1s", out)

    def test_a_matching_upstream_tag_renders_current(self):
        recs = [
            machine_rec("moose"),
            sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01",
                    upstream="2.53-v9-abc0000"),
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("current", out)
        self.assertIn("2.53-v9-abc0000", out)

    def test_a_newer_upstream_tag_renders_behind_and_names_it(self):
        recs = [
            machine_rec("moose"),
            sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01",
                    upstream="2.53-v11-def0000"),
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("behind (2.53-v11-def0000)", out)


def workspace_rec(machine, name, **extra):
    r = {"kind": "workspace", "machine": machine, "method": "container", "name": name}
    r.update(extra)
    return r


class TestJsonCarriesBaseAndSdk(unittest.TestCase):
    """One merged document feeds every view (tests.test_status's own defect
    6): a workspace's `base` and the machine's `sdk` record are present in
    `--json` exactly as fed in, the same recipe tests/test_status.py uses
    for every other field."""

    def test_workspace_base_survives_into_json(self):
        recs = [
            machine_rec("moose"),
            workspace_rec("moose", "stringimpl238", state="running", ws="present",
                           branch="eng/stringimpl-2.38", base="?"),
            {"kind": "exit", "code": 0},
        ]
        text_out = render(recs, "text").stdout
        json_out = json.loads(render(recs, "json").stdout)
        self.assertIn("stringimpl238", text_out)
        moose = next(m for m in json_out["machines"] if m["name"] == "moose")
        ws = moose["methods"][0]["workspaces"][0]
        self.assertEqual(ws["base"], "?")

    def test_sdk_record_survives_into_json(self):
        recs = [
            machine_rec("moose"),
            sdk_rec("moose", tag="2.53-v9-abc0000", pulled="2026-08-01",
                    upstream="2.53-v11-def0000"),
            {"kind": "exit", "code": 0},
        ]
        json_out = json.loads(render(recs, "json").stdout)
        moose = next(m for m in json_out["machines"] if m["name"] == "moose")
        sdk = moose["sdk"][0]
        self.assertEqual(sdk["tag"], "2.53-v9-abc0000")
        self.assertEqual(sdk["upstream"], "2.53-v11-def0000")


if __name__ == "__main__":
    unittest.main()
