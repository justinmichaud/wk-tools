"""The image store is gone:
a build's output lives wherever its builder left it -- a workspace, a build
host -- and every reader gets there by scanning or by `--from <path>`, never
by looking an id up in a catalogue.

Covers: none of the store's functions survive anywhere in the tree, as a
definition or a call, under cmd/, lib/, boot/, bench/, image/, container/ or
host/; the phrase "image store" survives only where `wk sysimage` says there
is none, and in this test; `wk sysimage rm` is a tombstone naming `wk rm`; `wk boot`'s default system is read off the device
rather than looked up, and a named --system is checked against it;
a profile is derived from both a yocto and a buildroot workspace path; each
builder's outputs (lib/wk/sysimage/ls.py) are what it leaves in a workspace
laid out the way targets/container.sh mounts one.

Run: python3 -m unittest tests.test_image_store_gone -v
"""
import contextlib
import io
import re
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, shell_files, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.sysimage import write  # noqa: E402
from wk.store import Store  # noqa: E402
from wk import act  # noqa: E402
from wk.sysimage import cli, ls  # noqa: E402

# The functions the image store used to be built from (lib/image.sh) --
# every one of them either has no reason to exist without a catalogue to
# read, or (image_dir/image_disk/image_manifest/...) named a path inside one.
_RETIRED_FUNCTIONS = (
    "image_dir",
    "image_manifest",
    "image_complete",
    "image_ids",
    "image_rubble",
    "image_latest",
    "manifest_get",
    "image_verify",
    "image_store_dir",
)


class TestNoStoreFunctionDefinitionRemains(unittest.TestCase):
    """A function *definition* -- 'name() {' or 'name () {' -- not a mention:
    the word can still appear in a refusal or a comment (a tombstone).
    TestNoRetiredFunctionCalled, below, polices the other half: that nothing
    calls one of these names either."""

    def test_no_definition_of_any_retired_function(self):
        pattern = re.compile(
            r"^\s*(?:" + "|".join(_RETIRED_FUNCTIONS) + r")\s*\(\)\s*\{",
            re.MULTILINE,
        )
        bad = []
        for f in shell_files():
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for m in pattern.finditer(text):
                bad.append(f"{f.relative_to(REPO)}: {m.group(0).strip()}")
        self.assertEqual(bad, [], "retired function(s) still defined:\n" + "\n".join(bad))


# Where the store's callers used to live -- the directories `wk selftest`
# treats as shell code, minus tests/ and docs/, which is where the tolerated
# hits below (owed work outside this change's file list) actually live.
_SCAN_DIRS = ("cmd", "lib", "boot", "bench", "image", "container", "host")


def _scoped_shell_files():
    return [f for f in shell_files() if f.relative_to(REPO).parts[0] in _SCAN_DIRS]


def _inside_quotes(line, col):
    """True if column `col` of `line` falls inside a "..." string -- the
    shape a die message or a log line's prose takes, as opposed to a bare
    command-position call."""
    in_str = False
    i = 0
    while i < col:
        c = line[i]
        if c == "\\" and in_str:
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        i += 1
    return in_str


_CALL_PATTERNS = {
    name: re.compile(r"\b" + re.escape(name) + r"\b(?!\s*\(\))")
    for name in _RETIRED_FUNCTIONS
}


class TestNoRetiredFunctionCalled(unittest.TestCase):
    """Not just no definition (above) -- no call either, anywhere the store's
    callers used to live. A retired name may still appear naming what is gone,
    in a comment or inside a die/log message's "..." string; that is prose,
    not a call, so it is not flagged."""

    def test_no_call_to_any_retired_function(self):
        bad = []
        for f in _scoped_shell_files():
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, start=1):
                if line.lstrip().startswith("#"):
                    continue
                for pattern in _CALL_PATTERNS.values():
                    for m in pattern.finditer(line):
                        if _inside_quotes(line, m.start()):
                            continue
                        bad.append(f"{f.relative_to(REPO)}:{lineno}: {line.strip()}")
        self.assertEqual(bad, [], "retired function(s) still called:\n" + "\n".join(bad))


class TestImageStorePhraseIsTombstoneOnly(unittest.TestCase):
    """"image store" as a phrase survives only where `wk sysimage` says there
    is none -- its help header, the `ls` footnote and the `rm` tombstone -- plus this test, which is
    the one place allowed to write the retired name in order to look for it."""

    _ALLOWED_SUBSTRINGS = (
        "There is no image store (`wk help`)",  # cmd/sysimage's header
        "There is no image store: the workspace is the name",  # the ls footnote
        "there is no image store to remove from",  # the rm tombstone
    )

    def test_phrase_is_tombstone_only(self):
        bad = []
        files = shell_files() + [REPO / "cmd" / "sysimage"] + sorted((REPO / "lib" / "wk" / "sysimage").glob("*.py"))
        for f in files:
            try:
                lines = f.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, start=1):
                if "image store" not in line.lower():
                    continue
                if any(s in line for s in self._ALLOWED_SUBSTRINGS):
                    continue
                bad.append(f"{f.relative_to(REPO)}:{lineno}: {line.strip()}")
        self.assertEqual(
            bad, [], "'image store' survives outside its tombstones:\n" + "\n".join(bad)
        )


class TestSysimageRmIsATombstone(unittest.TestCase):
    def test_rm_names_wk_rm_instead(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(act.Refused):
            cli.Sysimage.rm()
        self.assertIn("does not exist", err.getvalue())
        self.assertIn("wk rm <workspace>", err.getvalue())


# A stub ssh that never executes the remote script it is handed -- for
# 'wk boot's b_medium_read (boot/machines.sh), which on a workstation asks the
# card helper to read `wk-image.id` off a boot partition of the medium. There
# is no such device here, so letting the real call through would only fail
# differently on whatever this test happens to run on. Instead: recognise the
# one call by the arguments only it carries, and hand back a canned id -- the
# same "answer the identifiable call, succeed silently otherwise" shape
# tests/test_disk_logic.py uses for a stubbed sfdisk.
#
# The helper is addressed by a partition *number*, so the call reads
# `boot-read '/dev/sda' '1'` rather than naming /dev/sda1.
#
# rpi5-usb enumerates two candidate partitions (1 and 3: an A/B pair --
# boot/rpi5-usb.sh), but a board with one system written holds it only on the
# first; the second is bare and answers with nothing, the same as no system at
# all. Answering with the id on *both* would be a medium someone wrote the
# same image to twice, not the fresh single-system board this fixture models,
# so only partition 1 answers.
_SSH_STUB = '''#!/bin/sh
case "$*" in
  *boot-read*"'/dev/sda' '1'"*wk-image.id*) echo "{fake_id}" ;;
  *wk-image.id*) : ;;
  *) : ;;
esac
'''

_FAKE_NODE_CONF = '''NODE_SSH={ssh}
KIND=board
NODE_DRIVER=rpi5-usb
NODE_DEVICE=/dev/sda
NODE_ROOT=/dev/nvme0n1p2
NODE_PROFILE=webkit-2.52-yocto-rpi5-64
NODE_MAC=02:00:00:00:00:01
NODE_BRIDGE=""
NODE_ROLE=workstation
NODE_OS=any
NODE_VOLUME=""
NODE_DTB=bcm2712-rpi-5-b.dtb
NODE_BENCH_SSH=""
NODE_NET=wifi
NODE_NOTE="fake bench board"
'''


class TestBootArmDefaultsToDeviceImage(WkTest):
    """cmd_arm's default system is what the device already holds
    (b_device_image), not a store lookup; a named --system is checked
    against that same evidence rather than trusted."""

    def _env(self, machdir, binp):
        return {
            "WK_MACHINES_DIR": str(machdir),
            "PATH": f"{binp}:/usr/bin:/bin",
        }

    def test_dry_run_arms_the_id_the_device_reports(self):
        fake_id = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = f"fakerpi5{rand_suffix(3)}"
            (machdir / f"{name}.conf").write_text(_FAKE_NODE_CONF.format(ssh=name))
            cp = run("boot", name, "--dry-run", env=self._env(machdir, binp), timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(fake_id, cp.stdout, cp.stdout)
            self.assertNotIn("sysimage build", cp.stdout, cp.stdout)

    def test_a_named_system_that_differs_is_refused_and_names_the_write(self):
        fake_id = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        wanted = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = f"fakerpi5{rand_suffix(3)}"
            (machdir / f"{name}.conf").write_text(_FAKE_NODE_CONF.format(ssh=name))
            cp = run("boot", name, "--system", wanted, "--dry-run",
                      env=self._env(machdir, binp), timeout=30)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(fake_id, cp.stdout, cp.stdout)
            self.assertIn(wanted, cp.stdout, cp.stdout)
            self.assertIn("wk sysimage write --from", cp.stdout, cp.stdout)


class TestProfileFromWorkspacePath(unittest.TestCase):
    """A profile is derived from a lane's name for both builders that leave images inside one, by matching the
    configurations this checkout defines (lib/wk/images.py), and the write derives it the same way from a full path."""

    def test_a_lane_names_its_profile(self):
        self.assertEqual(images.ws_profile("yocto-webkit-2.52-yocto-rpi5-64"), "webkit-2.52-yocto-rpi5-64")
        self.assertEqual(images.ws_profile("buildroot-webkit-2.52-buildroot-rpi5-64"), "webkit-2.52-buildroot-rpi5-64")
        self.assertIsNone(images.ws_profile("jsc-release"))

    def test_a_full_path_names_its_profile(self):
        w = write.Write(REPO, {"WK_ROOT": str(REPO)}, Local(), None)
        for path, want in (
                ("/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/CrossToolChains/rpi5/build/image/"
                 "webkit-2.52-yocto-rpi5-64.wic.xz", "webkit-2.52-yocto-rpi5-64"),
                ("/var/lib/wk/ws/buildroot-webkit-2.52-buildroot-rpi5-64/build/buildroot/rpi5/output/images/sdcard.img",
                 "webkit-2.52-buildroot-rpi5-64")):
            with self.subTest(path=path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(w.profile("", path)[0], want)


class TestScanFindsWhatTheBuildersLeave(unittest.TestCase):
    """Both builders write under /src/WebKit/WebKitBuild, which targets/
    container.sh bind-mounts from ws/<name>/build, so that is where each
    builder's outputs are. A pattern naming any other parent matches nothing
    and `wk sysimage ls` reports no image over one built minutes earlier."""

    def _scan(self, store):
        return ls.scan(Local(), Store({"WK_STORE": str(store)}))

    def _one(self, store):
        found = self._scan(store)
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_finds_a_buildroot_image(self):
        with scratch_dir() as d:
            ws = "buildroot-wpewebkit-2.38-buildroot-rpi4-32"
            img = (d / "ws" / ws / "build" / "buildroot"
                   / "wpewebkit-2.38-buildroot-rpi4-32" / "output" / "images"
                   / "sdcard.img")
            img.parent.mkdir(parents=True)
            img.write_bytes(b"x" * 4096)
            self.assertEqual(self._one(d), ls.Image("buildroot", ws, str(img)))

    def test_finds_a_yocto_image(self):
        with scratch_dir() as d:
            ws = "yocto-webkit-2.52-yocto-rpi4-64"
            img = (d / "ws" / ws / "build" / "CrossToolChains"
                   / "rpi4-64bits-mesa" / "build" / "image"
                   / "webkit-dev-ci-tools.wic.xz")
            img.parent.mkdir(parents=True)
            img.write_bytes(b"x" * 4096)
            self.assertEqual(self._one(d), ls.Image("yocto", ws, str(img)))

    def test_a_workspace_that_built_nothing_is_not_a_row(self):
        with scratch_dir() as d:
            (d / "ws" / "jsc-release" / "build").mkdir(parents=True)
            self.assertEqual(self._scan(d), [])

    def test_an_image_workspace_with_no_image_gets_a_placeholder(self):
        """A yocto rebuild deletes build/image (clear_stale_image_copies,
        image/yocto-build.sh) and repopulates it at the end, so for hours the
        workspace holds nothing -- empty, or with the image directory made
        fresh and not yet written."""
        with scratch_dir() as d:
            for ws, sub in (("yocto-webkit-2.52-yocto-rpi3-32", "build"),
                            ("yocto-webkit-2.52-yocto-rpi4-64", "build/CrossToolChains/rpi4-64bits-mesa/build/image"),
                            ("buildroot-wpewebkit-2.38-buildroot-rpi4-32", "build")):
                (d / "ws" / ws / sub).mkdir(parents=True)
            self.assertEqual(self._scan(d), [
                ls.Image("buildroot", "buildroot-wpewebkit-2.38-buildroot-rpi4-32", None),
                ls.Image("yocto", "yocto-webkit-2.52-yocto-rpi3-32", None),
                ls.Image("yocto", "yocto-webkit-2.52-yocto-rpi4-64", None)])


if __name__ == "__main__":
    unittest.main()
