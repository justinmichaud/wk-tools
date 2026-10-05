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
    path.write_text(json.dumps({"Name": "wk", "Mounts": [{"Type": "virtiofs", "Source": s, "Target": t, "ReadOnly": ro}
                                                         for s, t, ro in mounts]}))


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
        self.cfg = self.home / ".config" / "containers" / "podman" / "machine" / "applehv" / "wk.json"

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
        for var in ("WK_NAME", "WK_PLACE", "WK_DRIVER", "WK_MARKER", "WK_YES", "WK_STORE", "WK_IN_VM", "WK_DRY_RUN"):
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
        e.update(env or {})
        with stub_path({"podman": podman or FAKE_PODMAN}) as binp:
            e["PATH"] = f"{binp}:{os.environ['PATH']}"
            cp = subprocess.run(["bash", "-c", script], cwd=str(REPO), env=e, capture_output=True, text=True, timeout=120)
        self.podman = self.log.read_text()
        return cp

    def stage(self, ok=True, env=None, mounts=None, state="stopped", **kw):
        """The stage over an existing machine holding `mounts` (None: no machine), asserted to succeed or fail as `ok`
        says (None: either); what it printed."""
        if mounts is not None:
            self.exists(state)
            write_cfg(self.cfg, mounts)
        cp = self.run_stage(env, **kw)
        out = cp.stdout + cp.stderr
        if ok is not None:
            (self.assertEqual if ok else self.assertNotEqual)(cp.returncode, 0, out)
        return out

    def exists(self, state="stopped"):
        (self.vm / "exists").write_text("")
        (self.vm / "state").write_text(state + "\n")

    def init_argv(self):
        return next((l for l in self.podman.splitlines() if l.startswith("machine init")), "")

    def verbs(self, *verbs):
        return [l.split()[1] for l in self.podman.splitlines() if l.startswith("machine ") and l.split()[1] in verbs]

    def assertRan(self, *verbs, ran=True):
        for verb in verbs:
            with self.subTest(verb=verb):
                (self.assertIn if ran else self.assertNotIn)(verb, self.podman, self.podman)


class TestInitAsksForExactlyFourMountsOnlyOneWritable(_Stage):
    def test_the_init_argv_and_the_source_directories_made_first_the_credential_ones_private(self):
        out = self.stage()
        argv = self.init_argv()
        for src, target, ro in self.want():
            with self.subTest(target=target):
                self.assertIn(f"--volume {src}:{target}:{'ro' if ro else 'rw'}", argv)
        self.assertEqual(4, argv.count("--volume"), argv)
        self.assertIn("--rootful", argv)
        self.assertNotIn("--playbook", argv)
        self.assertIn("read-write (verified)", out)
        for d in (self.secrets, self.agent_rw):
            with self.subTest(dir=d.name):
                self.assertEqual(0o700, d.stat().st_mode & 0o777)
        self.assertTrue(self.mirror_dir.is_dir(), "the mirror directory is not made before the init")

    def test_a_machine_with_the_wanted_mounts_is_left_alone(self):
        self.assertIn("read-write (verified)", self.stage(mounts=self.want()))
        self.assertRan("machine rm", "machine init", ran=False)


class TestAMachineWithAnyOtherMountSetIsRecreated(_Stage):
    def cases(self):
        w = self.want()
        return {"none": [], "users": [("/Users", "/Users", False)], "one of four": [(str(REPO), "/var/opt/wk-tools", True)],
                "no agent-rw": [w[0], w[1], w[3]], "no mirror": list(w[:3])}

    def test_it_refuses_without_an_answer_and_destroys_nothing(self):
        for case, mounts in self.cases().items():
            with self.subTest(case=case):
                self.setUp()
                self.assertIn("does not have this design's mounts", self.stage(False, mounts=mounts))
                self.assertRan("machine rm", ran=False)

    def test_the_prompt_lists_no_blank_mount_row_and_what_the_recreate_loses(self):
        out = self.stage(None, mounts=[])
        self.assertNotIn("has \n", out)
        self.assertNotIn("    has  ", out)
        self.assertIn("machine start", self.podman, out)
        for line in ("workspaces  wk-demo", "/var/lib/wk/bench", "tar -C /var/lib/wk -cf - bench", "wk key deploy"):
            self.assertIn(line, out)

    def test_the_mounts_it_does_have_are_named(self):
        self.assertIn("has /Users:/Users rw", self.stage(None, mounts=[("/Users", "/Users", False)]))

    def test_a_headless_yes_stops_removes_and_recreates_it(self):
        out = self.stage(env={"WK_YES": "1"}, mounts=[])
        self.assertEqual(["stop", "rm", "init"], self.verbs("stop", "rm", "init"), self.podman)
        self.assertIn("--volume", self.init_argv())
        self.assertIn("read-write (verified)", out)

    def test_a_dry_run_reports_and_touches_nothing(self):
        out = self.stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"}, mounts=[])
        self.assertIn("would be destroyed and recreated", out)
        self.assertRan("machine rm", "machine init", "machine start", ran=False)
        self.assertIn("a dry run does not start it", out)

    def test_a_dry_run_against_a_running_machine_still_lists_the_losses(self):
        self.assertIn("workspaces  wk-demo", self.stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"}, mounts=[], state="running"))
        self.assertRan("machine rm", ran=False)


class TestAMountTheMachineAsksForAndHasNotGot(_Stage):
    ABSENT = "/var/opt/wk-tools"

    def missing(self, ok=True, env=None, state="running"):
        return self.stage(ok, {"WK_TEST_ABSENT": self.ABSENT, **(env or {})}, mounts=self.want(), state=state)

    def test_it_is_refused_naming_the_absent_mount_and_destroys_nothing(self):
        out = self.missing(False)
        self.assertNotIn("read-write (verified)", out)
        for line in ("has not got them", f"absent {self.ABSENT}", "systemctl --failed"):
            self.assertIn(line, out)
        self.assertRan("machine rm", ran=False)

    def test_a_headless_yes_recreates_it(self):
        self.assertIn("internal error", self.missing(None, env={"WK_YES": "1"}))
        self.assertRan("machine rm", "machine init")

    def test_a_stopped_machine_is_not_started_to_ask(self):
        self.assertIn("read-write (verified)", self.missing(state="stopped"))
        self.assertRan("machine start", ran=False)

    def test_a_dry_run_reads_it_and_still_touches_nothing(self):
        self.assertIn("would be destroyed and recreated", self.missing(env={"WK_DRY_RUN": "1", "WK_YES": "1"}))
        self.assertRan("machine rm", "machine init", ran=False)


class TestAFreshMachineThatCameUpWithoutAMount(_Stage):
    """A machine this run created that still lacks a mount: a re-run would loop, so it is an internal error."""

    def test_it_starts_it_then_refuses_forbidding_the_re_run_and_keeps_it(self):
        out = self.stage(False, {"WK_TEST_ABSENT": "/var/opt/wk-tools", "WK_YES": "1"})
        self.assertEqual(["init", "start"], self.verbs("init", "start")[:2], self.podman)
        for line in ("internal error", "Do NOT re-run ./setup", "/var/opt/wk-tools"):
            self.assertIn(line, out)
        self.assertRan("machine rm", ran=False)


# `machine init` records each source canonicalised: WK_TEST_CANON=real resolves it, =slash adds a trailing slash.
FAKE_PODMAN_CANON = FAKE_PODMAN.replace(
    'spec = argv[i + 1].split(":")',
    "spec = argv[i + 1].split(\":\")\n        import os\n        how = os.environ.get(\"WK_TEST_CANON\", \"\")\n       "
    " if how == \"real\":\n            spec[0] = os.path.realpath(spec[0])\n        if how == \"slash\":\n            "
    "spec[1] = spec[1] + \"/\"")

# `machine init` adds a mount of its own to what it was asked for.
FAKE_PODMAN_ADDS_A_MOUNT = FAKE_PODMAN.replace(
    'json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))',
    "mounts.append({\"Type\": \"virtiofs\", \"Source\": \"/Users\",\n               \"Target\": \"/Users\", "
    "\"ReadOnly\": False})\njson.dump({\"Name\": \"wk\", \"Mounts\": mounts}, open(cfg, \"w\"))")


class TestTwoSpellingsOfOnePathAreOneMount(_Stage):
    def test_a_symlinked_checkout_or_a_trailing_slash_on_a_target_reads_ok_rather_than_differing(self):
        link = self.tmp / "wk-tools-link"
        link.symlink_to(REPO)
        for canon, root in (("real", link), ("slash", REPO)):
            with self.subTest(canon=canon):
                out = self.stage(env={"WK_TEST_CANON": canon}, podman=FAKE_PODMAN_CANON, wk_root=root)
                self.assertIn("read-write (verified)", out)
                self.assertNotIn("does not have this design's mounts", out)
                self.assertRan("machine rm", ran=False)


class TestAFreshMachineThatStillDiffersIsAnInternalError(_Stage):
    def test_it_dies_naming_both_spellings_and_never_advises_a_re_run(self):
        out = self.stage(False, env={"WK_YES": "1"}, podman=FAKE_PODMAN_ADDS_A_MOUNT)
        for line in ("internal error", "was just created", "asks %s" % self.agent_rw, "has  /Users:/Users",
                     "Do NOT re-run ./setup"):
            self.assertIn(line, out)
        self.assertNotIn("Recreate it with:  ./setup", out)


class TestADryRunCreatesNothing(_Stage):
    def test_neither_source_directory_nor_the_machine_is_created(self):
        out = self.stage(env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        for d in (self.secrets, self.agent_rw):
            with self.subTest(dir=d.name):
                self.assertFalse(d.exists(), f"{d} was created by a dry run:\n{out}")
                self.assertIn("would create %s" % d, out)
        self.assertRan("machine init", ran=False)
        self.assertIn("would be created", out)


class TestEnsureDirDryRun(WkTest):
    def test_a_dry_run_changes_neither_the_directory_nor_its_mode(self):
        d, gone = self.tmp / "existing", self.tmp / "not-there"
        d.mkdir()
        os.chmod(d, 0o755)
        for path in (d, gone):
            cp = self.bash(f'. "$WK_ROOT/lib/common.sh"; ensure_dir "{path}" 0700', env={"WK_DRY_RUN": "1"})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(0o755, d.stat().st_mode & 0o777)
        self.assertFalse(gone.exists())
        self.assertIn("would create", cp.stdout + cp.stderr)


class TestTheRightSetMountedTheWrongWayFailsLoudly(_Stage):
    """The right set with a mode dropped is refused, not recreated (a recreate would loop)."""

    def test_a_read_only_mount_handed_back_writable_or_the_writable_one_read_only(self):
        secrets, tools, rw, mirror = self.want()
        for case, mounts in (("ro as rw", [secrets[:2] + (False,), tools[:2] + (False,), rw, mirror[:2] + (False,)]),
                             ("rw as ro", [secrets, tools, rw[:2] + (True,), mirror])):
            with self.subTest(case=case):
                out = self.stage(False, env={"WK_YES": "1"}, mounts=mounts)
                self.assertIn("mounted the wrong way", out)
                self.assertIn("wants ", out)
                self.assertRan("machine rm", ran=False)


class TestAConfigItCannotReadIsRefused(_Stage):
    def test_an_unreadable_config_is_not_taken_as_proof_of_anything(self):
        self.exists()
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text("{}")
        self.assertIn("could not read mounts", self.stage(False, env={"WK_YES": "1"}))
        self.assertRan("machine rm", ran=False)


@requires_container_place()
@unittest.skipUnless(sys.platform == "darwin", "the podman VM and its mounts are a macOS host's")
class TestTheMountsAreThereOnThisMachine(unittest.TestCase):
    REMEDY = "run ./setup (it recreates the machine with the three mounts)"

    def test_the_checkout_is_executable_at_opt_wk_tools(self):
        cp = container_side("test -x /opt/wk-tools/wk && echo yes")
        self.assertIn("yes", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_it_is_this_checkout_and_not_a_copy(self):
        cp = container_side(f"cat /opt/wk-tools/{Path(__file__).relative_to(REPO)} 2>/dev/null | head -1")
        self.assertIn(__doc__.splitlines()[0], cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def _an_image(self):
        image = container_side("podman images --format '{{.Repository}}:{{.Tag}}' | head -1").stdout.strip()
        if not image or image.startswith("<none>"):
            self.skipTest("no container image on this machine to run a probe in")
        return image

    def test_the_secrets_directory_is_a_mount(self):
        cp = container_side("findmnt -no TARGET /var/lib/wk/secrets")
        self.assertIn("/var/lib/wk/secrets", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_the_agent_credential_directory_is_a_mount_and_is_writable(self):
        cp = container_side("findmnt -no OPTIONS /var/lib/wk/agent-rw")
        self.assertTrue(cp.stdout.strip(), f"/var/lib/wk/agent-rw is not mounted at all. {self.REMEDY}")
        self.assertIn("rw", cp.stdout.strip().split(","), f"/var/lib/wk/agent-rw is mounted {cp.stdout.strip()!r}: the "
                      f"Claude CLI cannot rotate the credential in it. {self.REMEDY}")
        cp = container_side(
            "touch /var/lib/wk/agent-rw/.wk-write-probe && rm -f /var/lib/wk/agent-rw/.wk-write-probe && echo wrote")
        self.assertIn("wrote", cp.stdout,
                      f"the machine cannot write /var/lib/wk/agent-rw: {cp.stdout}{cp.stderr}. {self.REMEDY}")

    def test_the_read_only_mounts_are_read_only_in_the_vm(self):
        for target in ("/opt/wk-tools", "/var/lib/wk/secrets"):
            with self.subTest(target=target):
                cp = container_side(f"findmnt -no OPTIONS {target}")
                self.assertIn("ro", cp.stdout.strip().split(","), f"{target} is mounted {cp.stdout.strip()!r}: podman "
                              f"took the ro option and dropped it, so a workspace can write to it. {self.REMEDY}")

    def test_a_container_can_read_both_through_its_own_mounts(self):
        cp = container_side(f"podman run --rm -v /opt/wk-tools:/opt/wk-tools:ro -v /var/lib/wk/secrets:/secrets:ro "
                            f"--entrypoint /bin/sh {self._an_image()} "
                            f"-c 'test -x /opt/wk-tools/wk && test -d /secrets && echo both'", timeout=180)
        self.assertIn("both", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_a_container_can_write_the_agent_credential_directory(self):
        cp = container_side(f"podman run --rm -v /var/lib/wk/agent-rw:/agent-rw --entrypoint /bin/sh {self._an_image()} "
                            f"-c 'echo hi > /agent-rw/.wk-probe.tmp && mv /agent-rw/.wk-probe.tmp /agent-rw/.wk-probe "
                            f"&& rm -f /agent-rw/.wk-probe && echo wrote'", timeout=180)
        self.assertIn("wrote", cp.stdout, f"a container cannot write and rename inside /agent-rw, so the Claude login "
                      f"credential cannot be rotated from a workspace: {cp.stdout}{cp.stderr}. {self.REMEDY}")


class TestOtherMachinesAreRetiredFirst(_Stage):
    """applehv runs one VM at a time, so another machine is offered for removal."""

    def others(self, env=None):
        return self.stage(None, {"WK_TEST_MACHINE_LIST": '[{"Name":"podman-machine-default"},{"Name":"wk*"}]',
                                 **(env or {})}, mounts=self.want())

    def test_yes_stops_it_and_removes_it(self):
        out = self.others(env={"WK_YES": "1"})
        self.assertRan("machine stop podman-machine-default", "machine rm -f podman-machine-default")
        self.assertIn("removed podman machine 'podman-machine-default'", out)

    def test_declining_names_it_never_wk_keeps_it_and_says_what_that_costs(self):
        out = self.others()
        self.assertIn("obsolete podman machine 'podman-machine-default'", out)
        self.assertNotIn("obsolete podman machine 'wk'", out)
        self.assertRan("machine rm -f wk\n", "machine rm -f podman-machine-default", ran=False)
        self.assertIn("keeping 'podman-machine-default'", out)
        self.assertIn("will fail to start", out)


class TestTheResourceEnvelopeIsReapplied(_Stage):
    """`podman machine set` needs the machine stopped: stop, set, start, or nothing."""

    def envelope(self):
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/lib/common.sh"; '
                           'for v in envelope-cores envelope-mem-mb; do wk_py wk.resources --os "$(wk_os)" "$v" || exit; done'],
            cwd=str(REPO), capture_output=True, text=True, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.split()

    def test_a_machine_already_at_the_envelope_is_left_alone(self):
        cores, mem = self.envelope()
        out = self.stage(env={"WK_TEST_CPUS": cores, "WK_TEST_MEM": mem}, mounts=self.want())
        self.assertRegex(out, rf"machine resources \({cores} cpus, {mem} MiB, .*GiB\)")
        self.assertRan("machine set", ran=False)

    def test_a_stopped_machine_that_differs_is_re_sized_unstarted_and_says_what_it_kept_back(self):
        cores, mem = self.envelope()
        out = self.stage(mounts=self.want())
        self.assertIn(f"machine set wk --cpus {cores} --memory {mem}", self.podman)
        self.assertIn(f"machine resources -> {cores} cpus, {mem} MiB", out)
        self.assertIn("host keeps", out)
        self.assertNotIn("machine start", self.podman, "a stopped machine is started to re-size it")

    def test_a_running_one_is_stopped_first_and_started_again(self):
        self.stage(None, mounts=self.want(), state="running")
        self.assertEqual(["stop", "set", "start"], self.verbs("stop", "set", "start"), self.podman)


class TestReportLossesStripsTheContainerPrefix(unittest.TestCase):
    def test_a_container_name_comes_back_as_the_workspace_name(self):
        text = MACHINE_SH.read_text()
        marker = 'podman machine ssh "$WK_MACHINE" -- \''
        start = text.index(marker) + len(marker)
        payload = text[start:text.index("' </dev/null", start)]   # what _report_losses pipes into the machine
        fake = '#!/bin/sh\ncase "$1 $2" in\n"ps -a") printf \'wk-demo\\nwk-other\\n\' ;;\nesac\n'
        with stub_path({"podman": fake}) as binp:
            cp = subprocess.run(["bash", "-c", payload], env={**os.environ, "PATH": f"{binp}:{os.environ['PATH']}"},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("workspaces  demo other", cp.stdout)
        self.assertNotIn("wk-demo", cp.stdout)
        self.assertNotIn("wk-other", cp.stdout)


if __name__ == "__main__":
    unittest.main()
