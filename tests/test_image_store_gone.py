"""The image store is gone (docs/HANDOFF-fleet.md, "Images without a store"):
a build's output lives wherever its builder left it -- a workspace, a build
host -- and every reader gets there by scanning or by `--from <path>`, never
by looking an id up in a catalogue.

Covers: none of the store's functions survive anywhere in the tree, as a
definition or a call, under cmd/, lib/, boot/, bench/, image/, container/ or
host/; the phrase "image store" survives only as cmd/sysimage's own
tombstones and in this test; `wk sysimage rm` is a tombstone naming `wk rm`;
`wk boot`'s default system is read off the device rather than looked up, and
a named --system is checked against it; `_ws_profile`/`_profile_from_path`
(cmd/sysimage) derive a profile from both a yocto and a buildroot workspace
path.

Run: python3 -m unittest tests.test_image_store_gone -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, shell_files, stub_path

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
    """"image store" as a phrase survives only where it names the thing that
    is gone: cmd/sysimage's header comment and its two tombstone messages
    (the 'ls' empty-listing note and the 'rm' refusal), plus this test, which
    is the one place allowed to write the retired name in order to look for
    it. A hit anywhere else is a regression: this test is meant to fail
    loudly the next time the phrase leaks somewhere new."""

    _ALLOWED_SUBSTRINGS = (
        "There is no image store (`wk help`)",  # cmd/sysimage:13, the header
        "There is no image store: the workspace is the name",  # cmd_ls
        "there is no image store to remove from",  # cmd_rm
    )

    def test_phrase_is_tombstone_only(self):
        bad = []
        for f in shell_files():
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


class TestSysimageRmIsATombstone(WkTest):
    def test_rm_names_wk_rm_instead(self):
        cp = run("sysimage", "rm", "some-workspace", env={"WK_ROOT": str(REPO)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk rm", cp.stdout, cp.stdout)
        self.assertIn("does not exist", cp.stdout, cp.stdout)


# A stub ssh that never executes the remote script it is handed -- for
# 'wk boot's b_device_image (boot/machines.sh), which mounts the boot
# partition for real on a machine that has one; here there is no such device,
# so any attempt to actually run that script (mount, findmnt) would just fail
# differently on whatever this test happens to run on. Instead: recognise the
# one script by a string only it contains, and hand back a canned id: this is
# the same "answer the identifiable call, succeed silently otherwise" shape
# tests/test_disk_logic.py uses for a stubbed sfdisk.
_SSH_STUB = '''#!/bin/sh
case "$*" in
  *wk-image.id*) echo "{fake_id}" ;;
  *) : ;;
esac
'''

_FAKE_MACH_CONF = '''MACH_SSH={ssh}
MACH_DRIVER=rpi5-usb
MACH_DEVICE=/dev/sda
MACH_ROOT=/dev/nvme0n1p2
MACH_PROFILE=webkit-2.52-yocto-rpi5-64
MACH_MAC=02:00:00:00:00:01
MACH_BRIDGE=""
MACH_ROLE=workstation
MACH_OS=any
MACH_LOCAL=""
MACH_VOLUME=""
MACH_DTB=bcm2712-rpi-5-b.dtb
MACH_BENCH_SSH=""
MACH_NET=wifi
MACH_NOTE="fake bench board"
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
            (machdir / f"{name}.conf").write_text(_FAKE_MACH_CONF.format(ssh=name))
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
            (machdir / f"{name}.conf").write_text(_FAKE_MACH_CONF.format(ssh=name))
            cp = run("boot", name, "--system", wanted, "--dry-run",
                      env=self._env(machdir, binp), timeout=30)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(fake_id, cp.stdout, cp.stdout)
            self.assertIn(wanted, cp.stdout, cp.stdout)
            self.assertIn("wk sysimage write --from", cp.stdout, cp.stdout)


class TestProfileFromWorkspacePath(unittest.TestCase):
    """_ws_profile (cmd/sysimage) derives a profile from a workspace name for
    both builders that leave images inside one (image_workspace_scan,
    lib/image.sh); _profile_from_path is the same derivation from a full
    path and calls through it -- lifted together, sed's the idiom
    tests/test_quick.py uses for 'bump' (cmd/status)."""

    def _lift(self):
        body = subprocess.run(
            ["sed", "-n", "/^_ws_profile()/,/^}/p", str(REPO / "cmd" / "sysimage")],
            capture_output=True, text=True,
        ).stdout
        self.assertTrue(body.strip(), "could not lift _ws_profile from cmd/sysimage")
        body2 = subprocess.run(
            ["sed", "-n", "/^_profile_from_path()/,/^}/p", str(REPO / "cmd" / "sysimage")],
            capture_output=True, text=True,
        ).stdout
        self.assertTrue(body2.strip(), "could not lift _profile_from_path from cmd/sysimage")
        return body + "\n" + body2

    def setUp(self):
        self.prelude = self._lift()

    def _run(self, *args):
        script = self.prelude + "\n" + " ".join(args)
        return bash(script)

    def test_yocto_workspace_name(self):
        cp = self._run("_ws_profile", "yocto-webkit-2.52-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-yocto-rpi5-64")

    def test_buildroot_workspace_name(self):
        cp = self._run("_ws_profile", "buildroot-webkit-2.52-buildroot-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-buildroot-rpi5-64")

    def test_unrelated_workspace_name_is_not_a_profile(self):
        cp = self._run("_ws_profile", "jsc-release")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_profile_from_a_full_yocto_path(self):
        cp = self._run(
            "_profile_from_path",
            "/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/changes/WebKitBuild/"
            "CrossToolChains/rpi5/build/image/webkit-2.52-yocto-rpi5-64.wic.xz",
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-yocto-rpi5-64")

    def test_profile_from_a_full_buildroot_path(self):
        cp = self._run(
            "_profile_from_path",
            "/var/lib/wk/ws/buildroot-webkit-2.52-buildroot-rpi5-64/changes/WebKitBuild/"
            "buildroot/rpi5/output/images/sdcard.img",
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-buildroot-rpi5-64")


if __name__ == "__main__":
    unittest.main()
