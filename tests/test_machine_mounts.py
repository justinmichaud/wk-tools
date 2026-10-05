"""host/macos/machine.sh: the podman machine's four mounts (tools, secrets, mirror read-only; agent-rw writable),
driven against a stub podman whose `machine init` writes the config the verify reads back."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, container_side, requires_container_place, stub_path

MACHINE_SH = REPO / "host" / "macos" / "machine.sh"

MACOS_HOST = r"""
is_macos() { return 0; }
wk_py() {
    [ "$1" = wk.store ] || { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m "$@"; return; }
    shift
    PYTHONPATH="$WK_ROOT/lib" python3 -c 'import os, sys
from unittest import mock
from wk import store
with mock.patch.object(os, "uname", return_value=mock.Mock(sysname="Darwin")):
    sys.exit(store.main(sys.argv[1:]))' "$@"
}
"""

FAKE_PODMAN = r'''#!/bin/sh
printf '%s\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$1 $2" in
"machine list")
    echo "${WK_TEST_MACHINE_LIST:-[]}" ;;
"machine inspect")
    [ -f "$WK_TEST_VM/exists" ] || exit 1
    _cpus=$(cat "$WK_TEST_VM/cpus" 2>/dev/null || echo "${WK_TEST_CPUS:-999}")
    _mem=$(cat "$WK_TEST_VM/mem" 2>/dev/null || echo "${WK_TEST_MEM:-999}")
    printf '[{"State":"%s","Resources":{"CPUs":%s,"Memory":%s,"DiskSize":%s}}]\n' \
        "$(cat "$WK_TEST_VM/state")" "$_cpus" "$_mem" "${WK_TEST_DISK:-500}" ;;
"machine init")
    : > "$WK_TEST_VM/exists"
    echo stopped > "$WK_TEST_VM/state"
    ( while [ $# -gt 0 ]; do
          case "$1" in
          --cpus)   echo "$2" > "$WK_TEST_VM/cpus" ;;
          --memory) echo "$2" > "$WK_TEST_VM/mem" ;;
          esac
          shift
      done )
    mkdir -p "$(dirname "$WK_TEST_CFG")"
    python3 - "$WK_TEST_CFG" "$@" <<'PY'
import json, sys
cfg, argv = sys.argv[1], sys.argv[2:]
mounts = []
for i, a in enumerate(argv):
    if a in ("--volume", "-v"):
        spec = argv[i + 1].split(":")
        mounts.append({"Type": "virtiofs", "Source": spec[0], "Target": spec[1],
                       "ReadOnly": len(spec) > 2 and spec[2] == "ro"})
json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))
PY
    ;;
"machine rm")
    rm -f "$WK_TEST_VM/exists" "$WK_TEST_VM/cpus" "$WK_TEST_VM/mem" "$WK_TEST_CFG" ;;
"machine stop")
    echo stopped > "$WK_TEST_VM/state" ;;
"machine start")
    echo running > "$WK_TEST_VM/state" ;;
"machine ssh")
    case "$*" in
    *findmnt*)
        for t in ${WK_TEST_ABSENT:-}; do
            case "$*" in *"$t"*) exit 1 ;; esac
        done
        exit 0 ;;
    esac
    echo "workspaces  wk-demo" ;;
esac
exit 0
'''


def write_cfg(path, mounts):
    """A machine config file with the mounts given as (source, target, ro)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "Name": "wk",
        "Mounts": [{"Type": "virtiofs", "Source": s, "Target": t, "ReadOnly": ro}
                   for s, t, ro in mounts],
    }))


class _Stage(WkTest):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.vm = self.tmp / "vm"
        self.secrets = self.tmp / "secrets"
        self.agent_rw = self.tmp / "agent-rw"
        self.mirror_dir = self.home / ".local" / "state" / "wk" / "git"
        self.log = self.tmp / "podman.log"
        for d in (self.home, self.vm):
            d.mkdir(parents=True)
        self.log.write_text("")
        self.cfg = (self.home / ".config" / "containers" / "podman"
                    / "machine" / "applehv" / "wk.json")

    def want(self):
        """(source, target, read-only) for each mount."""
        return ((str(self.secrets), "/var/lib/wk/secrets", True),
                (str(REPO), "/var/opt/wk-tools", True),
                (str(self.agent_rw), "/var/lib/wk/agent-rw", False),
                (str(self.mirror_dir), "/var/lib/wk/git", True))

    def run_stage(self, env=None, podman=None, wk_root=REPO):
        script = f'''
set -euo pipefail
WK_ROOT={wk_root}
. "$WK_ROOT/lib/common.sh"
{MACOS_HOST}
. "{REPO}/host/macos/machine.sh"
'''
        e = dict(os.environ)
        for var in ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_MARKER",
                    "WK_YES", "WK_STORE", "WK_IN_VM", "WK_DRY_RUN"):
            e.pop(var, None)
        e.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".local" / "state"),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_STORE": "/var/lib/wk",
            "WK_TEST_VM": str(self.vm),
            "WK_TEST_CFG": str(self.cfg),
            "WK_TEST_PODMAN_LOG": str(self.log),
            "WK_DEBUG": "1",
        })
        if env:
            e.update(env)
        with stub_path({"podman": podman or FAKE_PODMAN}) as binp:
            e["PATH"] = f"{binp}:{os.environ['PATH']}"
            cp = subprocess.run(["bash", "-c", script], cwd=str(REPO), env=e,
                                capture_output=True, text=True, timeout=120)
        self.podman = self.log.read_text()
        return cp

    def exists(self):
        (self.vm / "exists").write_text("")
        (self.vm / "state").write_text("stopped\n")

    def init_argv(self):
        for line in self.podman.splitlines():
            if line.startswith("machine init"):
                return line
        return ""


class TestInitAsksForExactlyFourMountsOnlyOneWritable(_Stage):
    def test_the_init_argv(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        argv = self.init_argv()
        self.assertTrue(argv, self.podman)
        for src, target, ro in self.want():
            with self.subTest(target=target):
                self.assertIn(f"--volume {src}:{target}:{'ro' if ro else 'rw'}", argv)
        self.assertEqual(4, argv.count("--volume"), argv)
        self.assertIn("--rootful", argv)
        self.assertNotIn("--playbook", argv)

    def test_the_source_directories_are_made_first_and_the_credential_ones_are_private(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for d in (self.secrets, self.agent_rw):
            with self.subTest(dir=d.name):
                self.assertTrue(d.is_dir())
                self.assertEqual(0o700, d.stat().st_mode & 0o777)
        self.assertTrue(self.mirror_dir.is_dir(), "the mirror directory is not made before the init")

    def test_what_init_wrote_is_what_the_verify_accepts(self):
        cp = self.run_stage()
        self.assertIn("read-write (verified)", cp.stdout + cp.stderr)


class TestAMachineWithTheWantedMountsIsLeftAlone(_Stage):
    def test_no_change_and_nothing_destroyed(self):
        self.exists()
        write_cfg(self.cfg, list(self.want()))
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("machine rm", self.podman, self.podman)
        self.assertNotIn("machine init", self.podman, self.podman)
        self.assertIn("read-write (verified)", cp.stdout + cp.stderr)


class TestAMachineWithAnyOtherMountSetIsRecreated(_Stage):
    CASES = {
        "none": [],
        "users": [("/Users", "/Users", False)],
        "one of four": [(str(REPO), "/var/opt/wk-tools", True)],
    }

    def setUp(self):
        super().setUp()
        self.CASES = dict(self.CASES)
        self.CASES["no agent-rw"] = [self.want()[0], self.want()[1], self.want()[3]]
        self.CASES["no mirror"] = list(self.want()[:3])

    def _run(self, case, env=None):
        self.exists()
        write_cfg(self.cfg, self.CASES[case])
        return self.run_stage(env)

    def test_it_refuses_without_an_answer_and_destroys_nothing(self):
        for case in self.CASES:
            with self.subTest(case=case):
                self.setUp()
                cp = self._run(case)
                out = cp.stdout + cp.stderr
                self.assertNotEqual(cp.returncode, 0, out)
                self.assertIn("does not have this design's mounts", out)
                self.assertNotIn("machine rm", self.podman, self.podman)

    def test_a_machine_with_no_mounts_lists_none_rather_than_a_blank_row(self):
        cp = self._run("none")
        self.assertNotIn("has \n", cp.stdout + cp.stderr)
        self.assertNotIn("    has  ", cp.stdout + cp.stderr)

    def test_the_mounts_it_does_have_are_named(self):
        cp = self._run("users")
        self.assertIn("has /Users:/Users rw", cp.stdout + cp.stderr)

    def test_the_prompt_says_what_the_recreate_loses(self):
        cp = self._run("none")
        out = cp.stdout + cp.stderr
        self.assertIn("workspaces  wk-demo", out)
        self.assertIn("machine ssh", self.podman)
        self.assertIn("/var/lib/wk/bench", out)
        self.assertIn("tar -C /var/lib/wk -cf - bench", out)
        self.assertIn("wk key deploy", out)

    def test_it_reads_the_losses_off_a_stopped_machine_by_starting_it(self):
        cp = self._run("none")
        self.assertIn("machine start", self.podman, cp.stdout + cp.stderr)

    def test_a_headless_yes_destroys_and_recreates_it(self):
        cp = self._run("none", env={"WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("machine rm", self.podman, self.podman)
        self.assertIn("machine init", self.podman, self.podman)
        self.assertIn("--volume", self.init_argv())
        self.assertIn("read-write (verified)", cp.stdout + cp.stderr)

    def test_a_dry_run_reports_and_touches_nothing(self):
        cp = self._run("none", env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("would be destroyed and recreated", out)
        for verb in ("machine rm", "machine init", "machine start"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.podman, self.podman)
        self.assertIn("a dry run does not start it", out)

    def test_a_dry_run_against_a_running_machine_still_lists_the_losses(self):
        self.exists()
        (self.vm / "state").write_text("running\n")
        write_cfg(self.cfg, self.CASES["none"])
        cp = self.run_stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("workspaces  wk-demo", out)
        self.assertNotIn("machine rm", self.podman, self.podman)

    def test_the_order_is_stop_then_remove_then_create(self):
        self._run("none", env={"WK_YES": "1"})
        verbs = [l.split()[1] for l in self.podman.splitlines()
                 if l.startswith("machine ") and l.split()[1] in ("stop", "rm", "init")]
        self.assertEqual(["stop", "rm", "init"], verbs, self.podman)


class TestAMountTheMachineAsksForAndHasNotGot(_Stage):
    """The config asks for a mount the running machine has not got."""

    ABSENT = "/var/opt/wk-tools"

    def _running_machine_missing_the_tools_mount(self, env=None):
        self.exists()
        (self.vm / "state").write_text("running\n")
        write_cfg(self.cfg, list(self.want()))
        return self.run_stage({"WK_TEST_ABSENT": self.ABSENT, **(env or {})})

    def test_it_is_not_reported_as_verified(self):
        cp = self._running_machine_missing_the_tools_mount()
        out = cp.stdout + cp.stderr
        self.assertNotIn("read-write (verified)", out)
        self.assertIn("has not got them", out)
        self.assertIn(f"absent {self.ABSENT}", out)

    def test_it_names_the_command_that_says_why_the_unit_failed(self):
        cp = self._running_machine_missing_the_tools_mount()
        self.assertIn("systemctl --failed", cp.stdout + cp.stderr)

    def test_it_destroys_nothing_without_an_answer(self):
        cp = self._running_machine_missing_the_tools_mount()
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("machine rm", self.podman, self.podman)

    def test_a_headless_yes_recreates_it(self):
        cp = self._running_machine_missing_the_tools_mount(env={"WK_YES": "1"})
        self.assertIn("machine rm", self.podman, self.podman)
        self.assertIn("machine init", self.podman, self.podman)
        self.assertIn("internal error", cp.stdout + cp.stderr)

    def test_a_stopped_machine_is_not_started_to_ask(self):
        self.exists()
        write_cfg(self.cfg, list(self.want()))
        cp = self.run_stage({"WK_TEST_ABSENT": self.ABSENT})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("machine start", self.podman, self.podman)
        self.assertIn("read-write (verified)", cp.stdout + cp.stderr)

    def test_a_dry_run_reads_it_and_still_touches_nothing(self):
        cp = self._running_machine_missing_the_tools_mount(
            env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("would be destroyed and recreated", out)
        for verb in ("machine rm", "machine init"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.podman, self.podman)


class TestAFreshMachineThatCameUpWithoutAMount(_Stage):
    """A machine this run created that still lacks a mount: a re-run would loop, so it is an internal error."""

    def _run(self):
        return self.run_stage({"WK_TEST_ABSENT": "/var/opt/wk-tools",
                               "WK_YES": "1"})

    def test_it_starts_the_machine_it_just_created(self):
        self._run()
        order = [l.split()[1] for l in self.podman.splitlines()
                 if l.startswith("machine ") and l.split()[1] in ("init", "start")]
        self.assertEqual(["init", "start"], order[:2], self.podman)

    def test_it_refuses_and_forbids_the_re_run(self):
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("internal error", out)
        self.assertIn("Do NOT re-run ./setup", out)
        self.assertIn("/var/opt/wk-tools", out)

    def test_it_does_not_destroy_the_machine_it_just_made(self):
        self._run()
        self.assertNotIn("machine rm", self.podman, self.podman)


# `machine init` records each source canonicalised: WK_TEST_CANON=real resolves it, =slash adds a trailing slash.
FAKE_PODMAN_CANON = FAKE_PODMAN.replace(
    'spec = argv[i + 1].split(":")',
    'spec = argv[i + 1].split(":")\n'
    '        import os\n'
    '        how = os.environ.get("WK_TEST_CANON", "")\n'
    '        if how == "real":\n'
    '            spec[0] = os.path.realpath(spec[0])\n'
    '        if how == "slash":\n'
    '            spec[1] = spec[1] + "/"')

# `machine init` adds a mount of its own to what it was asked for.
FAKE_PODMAN_ADDS_A_MOUNT = FAKE_PODMAN.replace(
    'json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))',
    'mounts.append({"Type": "virtiofs", "Source": "/Users",\n'
    '               "Target": "/Users", "ReadOnly": False})\n'
    'json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))')


class TestTwoSpellingsOfOnePathAreOneMount(_Stage):
    def test_a_symlinked_checkout_reads_ok_rather_than_differing(self):
        link = self.tmp / "wk-tools-link"
        link.symlink_to(REPO)
        cp = self.run_stage({"WK_TEST_CANON": "real"}, FAKE_PODMAN_CANON, wk_root=link)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("read-write (verified)", out)
        self.assertNotIn("does not have this design's mounts", out)
        self.assertNotIn("machine rm", self.podman, self.podman)

    def test_a_trailing_slash_on_a_target_reads_ok_too(self):
        cp = self.run_stage({"WK_TEST_CANON": "slash"}, FAKE_PODMAN_CANON)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("read-write (verified)", out)
        self.assertNotIn("machine rm", self.podman, self.podman)

class TestAFreshMachineThatStillDiffersIsAnInternalError(_Stage):
    def test_it_dies_naming_both_spellings_and_never_advises_a_re_run(self):
        cp = self.run_stage(env={"WK_YES": "1"},
                            podman=FAKE_PODMAN_ADDS_A_MOUNT)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("internal error", out)
        self.assertIn("was just created", out)
        self.assertIn("asks %s" % self.agent_rw, out)
        self.assertIn("has  /Users:/Users", out)
        self.assertNotIn("Recreate it with:  ./setup", out)
        self.assertIn("Do NOT re-run ./setup", out)

class TestADryRunTouchesNoDirectory(_Stage):
    def test_neither_source_directory_is_created(self):
        cp = self.run_stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        for d in (self.secrets, self.agent_rw):
            with self.subTest(dir=d.name):
                self.assertFalse(d.exists(), f"{d} was created by a dry run:\n{out}")
                self.assertIn("would create %s" % d, out)

    def test_no_machine_is_created_either(self):
        cp = self.run_stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("machine init", self.podman, self.podman)
        self.assertIn("would be created", cp.stdout + cp.stderr)

class TestEnsureDirDryRun(WkTest):
    def ensure(self, d, mode="0700", env=None):
        return self.bash(f'. "$WK_ROOT/lib/common.sh"; ensure_dir "{d}" {mode}', env=env)

    def test_a_dry_run_changes_neither_the_directory_nor_its_mode(self):
        d = self.tmp / "existing"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        gone = self.tmp / "not-there"
        cp = self.ensure(d, env={"WK_DRY_RUN": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(0o755, d.stat().st_mode & 0o777)
        cp = self.ensure(gone, env={"WK_DRY_RUN": "1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertFalse(gone.exists())
        self.assertIn("would create", cp.stdout + cp.stderr)


class TestTheRightSetMountedTheWrongWayFailsLoudly(_Stage):
    """The right set with a mode dropped is refused, not recreated (a recreate would loop)."""

    def _wrong(self, mounts):
        self.exists()
        write_cfg(self.cfg, mounts)
        cp = self.run_stage(env={"WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("mounted the wrong way", out)
        self.assertIn("wants ", out)
        self.assertNotIn("machine rm", self.podman, self.podman)
        return out

    def test_a_read_only_mount_handed_back_writable(self):
        secrets, tools, rw, mirror = self.want()
        self._wrong([(secrets[0], secrets[1], False),
                     (tools[0], tools[1], False),
                     rw, (mirror[0], mirror[1], False)])

    def test_the_writable_one_handed_back_read_only(self):
        secrets, tools, rw, mirror = self.want()
        self._wrong([secrets, tools, (rw[0], rw[1], True), mirror])


class TestAConfigItCannotReadIsRefused(_Stage):
    def test_an_unreadable_config_is_not_taken_as_proof_of_anything(self):
        self.exists()
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text("{}")
        cp = self.run_stage(env={"WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("could not read mounts", out)
        self.assertNotIn("machine rm", self.podman, self.podman)


@requires_container_place()
@unittest.skipUnless(sys.platform == "darwin", "the podman VM and its mounts are a macOS host's")
class TestTheMountsAreThereOnThisMachine(unittest.TestCase):
    REMEDY = "run ./setup (it recreates the machine with the three mounts)"

    def test_the_checkout_is_executable_at_opt_wk_tools(self):
        cp = container_side("test -x /opt/wk-tools/wk && echo yes")
        self.assertIn("yes", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_it_is_this_checkout_and_not_a_copy(self):
        rel = Path(__file__).relative_to(REPO)
        cp = container_side(f"cat /opt/wk-tools/{rel} 2>/dev/null | head -1")
        self.assertIn(__doc__.splitlines()[0], cp.stdout,
                      f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def _an_image(self):
        cp = container_side("podman images --format '{{.Repository}}:{{.Tag}}' | head -1")
        image = cp.stdout.strip()
        if not image or image.startswith("<none>"):
            self.skipTest("no container image on this machine to run a probe in")
        return image

    def test_the_secrets_directory_is_a_mount(self):
        cp = container_side("findmnt -no TARGET /var/lib/wk/secrets")
        self.assertIn("/var/lib/wk/secrets", cp.stdout,
                      f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_the_agent_credential_directory_is_a_mount_and_is_writable(self):
        cp = container_side("findmnt -no OPTIONS /var/lib/wk/agent-rw")
        opts = cp.stdout.strip().split(",")
        self.assertTrue(cp.stdout.strip(),
                        f"/var/lib/wk/agent-rw is not mounted at all. {self.REMEDY}")
        self.assertIn("rw", opts,
                      f"/var/lib/wk/agent-rw is mounted {cp.stdout.strip()!r}: the "
                      f"Claude CLI cannot rotate the credential in it. {self.REMEDY}")
        cp = container_side(
            "touch /var/lib/wk/agent-rw/.wk-write-probe "
            "&& rm -f /var/lib/wk/agent-rw/.wk-write-probe && echo wrote")
        self.assertIn("wrote", cp.stdout,
                      f"the machine cannot write /var/lib/wk/agent-rw: "
                      f"{cp.stdout}{cp.stderr}. {self.REMEDY}")

    def test_the_read_only_mounts_are_read_only_in_the_vm(self):
        for target in ("/opt/wk-tools", "/var/lib/wk/secrets"):
            with self.subTest(target=target):
                cp = container_side(f"findmnt -no OPTIONS {target}")
                opts = cp.stdout.strip().split(",")
                self.assertIn("ro", opts,
                              f"{target} is mounted {cp.stdout.strip()!r}: podman took "
                              f"the ro option and dropped it, so a workspace can write "
                              f"to it. {self.REMEDY}")

    def test_a_container_can_read_both_through_its_own_mounts(self):
        cp = container_side(
            f"podman run --rm "
            f"-v /opt/wk-tools:/opt/wk-tools:ro -v /var/lib/wk/secrets:/secrets:ro "
            f"--entrypoint /bin/sh {self._an_image()} "
            f"-c 'test -x /opt/wk-tools/wk && test -d /secrets && echo both'",
            timeout=180)
        self.assertIn("both", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_a_container_can_write_the_agent_credential_directory(self):
        cp = container_side(
            f"podman run --rm -v /var/lib/wk/agent-rw:/agent-rw "
            f"--entrypoint /bin/sh {self._an_image()} "
            f"-c 'echo hi > /agent-rw/.wk-probe.tmp "
            f"&& mv /agent-rw/.wk-probe.tmp /agent-rw/.wk-probe "
            f"&& rm -f /agent-rw/.wk-probe && echo wrote'",
            timeout=180)
        self.assertIn("wrote", cp.stdout,
                      f"a container cannot write and rename inside /agent-rw, so the "
                      f"Claude login credential cannot be rotated from a workspace: "
                      f"{cp.stdout}{cp.stderr}. {self.REMEDY}")


class TestOtherMachinesAreRetiredFirst(_Stage):
    """applehv runs one VM at a time, so another machine is offered for removal."""

    OTHERS = '[{"Name":"podman-machine-default"},{"Name":"wk*"}]'

    def _run(self, env=None):
        self.exists()
        write_cfg(self.cfg, list(self.want()))
        e = {"WK_TEST_MACHINE_LIST": self.OTHERS}
        e.update(env or {})
        return self.run_stage(e)

    def test_it_names_the_machine_that_is_in_the_way(self):
        cp = self._run()
        self.assertIn("obsolete podman machine 'podman-machine-default'",
                      cp.stdout + cp.stderr)

    def test_the_wk_machine_itself_is_never_one_of_them(self):
        cp = self._run()
        self.assertNotIn("obsolete podman machine 'wk'", cp.stdout + cp.stderr)
        self.assertNotIn("machine rm -f wk\n", self.podman)

    def test_yes_stops_it_and_removes_it(self):
        cp = self._run(env={"WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertIn("machine stop podman-machine-default", self.podman, out)
        self.assertIn("machine rm -f podman-machine-default", self.podman, out)
        self.assertIn("removed podman machine 'podman-machine-default'", out)

    def test_declining_keeps_it_and_says_what_that_costs(self):
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertIn("keeping 'podman-machine-default'", out)
        self.assertIn("will fail to start", out)
        self.assertNotIn("machine rm -f podman-machine-default", self.podman)


class TestTheResourceEnvelopeIsReapplied(_Stage):
    """`podman machine set` needs the machine stopped: stop, set, start, or nothing."""

    def _envelope(self):
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/lib/common.sh"; '
                           'for v in envelope-cores envelope-mem-mb; do wk_py wk.resources --os "$(wk_os)" "$v" || exit; done'],
            cwd=str(REPO), capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.split()

    def _run(self, cpus=None, mem=None, running=False):
        self.exists()
        if running:
            (self.vm / "state").write_text("running\n")
        write_cfg(self.cfg, list(self.want()))
        env = {}
        if cpus is not None:
            env["WK_TEST_CPUS"], env["WK_TEST_MEM"] = cpus, mem
        return self.run_stage(env)

    def test_a_machine_already_at_the_envelope_is_left_alone(self):
        cores, mem = self._envelope()
        cp = self._run(cpus=cores, mem=mem)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertRegex(out, rf"machine resources \({cores} cpus, {mem} MiB, .*GiB\)")
        self.assertNotIn("machine set", self.podman, self.podman)

    def test_a_machine_that_differs_is_re_sized_and_says_what_it_kept_back(self):
        cores, mem = self._envelope()
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn(f"machine set wk --cpus {cores} --memory {mem}", self.podman)
        self.assertIn(f"machine resources -> {cores} cpus, {mem} MiB", out)
        self.assertIn("host keeps", out)

    def test_a_stopped_machine_is_not_started_to_re_size_it(self):
        self._run()
        verbs = [l for l in self.podman.splitlines() if l.startswith("machine start")]
        self.assertEqual([], verbs, self.podman)

    def test_a_running_one_is_stopped_first_and_started_again(self):
        self._run(running=True)
        order = [l.split()[1] for l in self.podman.splitlines()
                 if l.startswith("machine ") and l.split()[1] in ("stop", "set", "start")]
        self.assertEqual(["stop", "set", "start"], order, self.podman)


class TestReportLossesStripsTheContainerPrefix(unittest.TestCase):
    def _payload(self):
        """The script _report_losses pipes into `podman machine ssh`."""
        text = MACHINE_SH.read_text()
        marker = 'podman machine ssh "$WK_MACHINE" -- \''
        start = text.index(marker) + len(marker)
        end = text.index("' </dev/null", start)
        return text[start:end]

    def test_a_container_name_comes_back_as_the_workspace_name(self):
        fake_podman = '''#!/bin/sh
case "$1 $2" in
"ps -a") printf 'wk-demo\\nwk-other\\n' ;;
esac
'''
        with stub_path({"podman": fake_podman}) as binp:
            cp = subprocess.run(
                ["bash", "-c", self._payload()],
                env={**os.environ, "PATH": f"{binp}:{os.environ['PATH']}"},
                capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("workspaces  demo other", cp.stdout)
        self.assertNotIn("wk-demo", cp.stdout)
        self.assertNotIn("wk-other", cp.stdout)


if __name__ == "__main__":
    unittest.main()
