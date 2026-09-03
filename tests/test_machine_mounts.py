"""host/macos/machine.sh: the podman machine's three mounts.

The machine mounts exactly this checkout at /opt/wk-tools and this device's
secrets directory at $WK_STORE/secrets, both read-only, plus the one directory
a workspace may write -- $WK_STORE/agent-rw, holding the claude.ai login
credential the Claude CLI rewrites in place (wk_agent_rw_dir, lib/store.sh) --
and nothing else: `/Users` above all, which podman mounts by default. Which
mount is writable is as much a part of the invariant as which mounts there are,
so the two are separate verdicts: a different *set* is recreated, a set mounted
the wrong way round is refused (recreating that would ask podman for the same
modes again and loop).

A mount is settable only at creation, so a machine with any other set is
destroyed and made again rather than patched, after a prompt that says what
that loses.

Nothing here touches a real podman machine: `podman` is a stub on PATH that
records its argv and keeps the machine's existence, state and config file in a
scratch directory, so the init this file drives is the real one and what the
verify reads back is what that init wrote.

The stage is macOS-only, and the test drives it anyway by defining `is_macos`
true in the shell it sources it into: the arm under test is the macOS-host one,
and it is worth having on every machine that runs the suite.

Run: python3 -m unittest tests.test_machine_mounts -v
"""
import os
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, podman_vm_ssh, requires_podman_vm, stub_path

MACHINE_SH = REPO / "host" / "macos" / "machine.sh"

# `podman`, as far as this stage can tell: one machine that may or may not
# exist, whose config file is written by `machine init` from the --volume flags
# it was given -- so the verify below reads back exactly what the init asked
# for, rather than a fixture a test wrote by hand.
FAKE_PODMAN = r'''#!/bin/sh
printf '%s\n' "$*" >> "$WK_TEST_PODMAN_LOG"
case "$1 $2" in
"machine list")
    echo '[]' ;;
"machine inspect")
    [ -f "$WK_TEST_VM/exists" ] || exit 1
    case "$*" in
    *"{{.State}}"*)           cat "$WK_TEST_VM/state" ;;
    *"{{.Resources.CPUs}}"*)  echo 999 ;;
    *"{{.Resources.Memory}}"*) echo 999 ;;
    *) echo '{}' ;;
    esac ;;
"machine init")
    : > "$WK_TEST_VM/exists"
    echo stopped > "$WK_TEST_VM/state"
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
    rm -f "$WK_TEST_VM/exists" "$WK_TEST_CFG" ;;
"machine stop")
    echo stopped > "$WK_TEST_VM/state" ;;
"machine start")
    echo running > "$WK_TEST_VM/state" ;;
"machine ssh")
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
    """The real stage, sourced the way ./setup sources it."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.vm = self.tmp / "vm"
        self.secrets = self.tmp / "secrets"
        # Not an environment variable of its own: wk_agent_rw_dir is a sibling
        # of the secrets directory, so WK_HOST_SECRETS below places both.
        self.agent_rw = self.tmp / "agent-rw"
        self.log = self.tmp / "podman.log"
        for d in (self.home, self.vm):
            d.mkdir(parents=True)
        self.log.write_text("")
        self.cfg = (self.home / ".config" / "containers" / "podman"
                    / "machine" / "applehv" / "wk.json")

    def want(self):
        """source, target, read-only -- the triples the stage asks podman for
        and then holds the machine to."""
        return ((str(self.secrets), "/var/lib/wk/secrets", True),
                (str(REPO), "/opt/wk-tools", True),
                (str(self.agent_rw), "/var/lib/wk/agent-rw", False))

    def run_stage(self, env=None):
        script = f'''
set -euo pipefail
WK_ROOT={REPO}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
# The stage under test is the macOS one; this is the machine it is about.
is_macos() {{ return 0; }}
. "$WK_ROOT/host/macos/machine.sh"
'''
        e = dict(os.environ)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_MARKER",
                    "WK_YES", "WK_STORE", "WK_IN_VM"):
            e.pop(var, None)
        e.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_STORE": "/var/lib/wk",
            "WK_TEST_VM": str(self.vm),
            "WK_TEST_CFG": str(self.cfg),
            "WK_TEST_PODMAN_LOG": str(self.log),
            # `unchanged` is debug-level, and "verified" is the line that says
            # the invariant held (lib/common.sh).
            "WK_DEBUG": "1",
        })
        if env:
            e.update(env)
        with stub_path({"podman": FAKE_PODMAN}) as binp:
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


class TestInitAsksForExactlyThreeMountsOnlyOneWritable(_Stage):
    def test_the_init_argv(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        argv = self.init_argv()
        self.assertTrue(argv, self.podman)
        for src, target, ro in self.want():
            with self.subTest(target=target):
                self.assertIn(f"--volume {src}:{target}:{'ro' if ro else 'rw'}", argv)
        self.assertEqual(3, argv.count("--volume"), argv)
        self.assertIn("--rootful", argv)

    def test_exactly_one_of_them_is_writable(self):
        """Every mount but the agent credential directory is a thing a
        workspace must not be able to rewrite."""
        self.run_stage()
        argv = self.init_argv()
        self.assertEqual(1, argv.count(":rw"), argv)
        self.assertIn(f"{self.agent_rw}:/var/lib/wk/agent-rw:rw", argv)

    def test_no_empty_volume_hands_back_podmans_defaults(self):
        """An empty --volume was how the mount list was emptied; with three
        wanted mounts it would be a fourth, and podman's own /Users default is
        what an argv that names none falls back to."""
        self.run_stage()
        self.assertNotIn('--volume  ', self.init_argv() + " ")
        self.assertNotIn("/Users:/Users", self.init_argv())

    def test_both_source_directories_are_made_first_and_are_private(self):
        """podman refuses to init against a mount source that is not there."""
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for d in (self.secrets, self.agent_rw):
            with self.subTest(dir=d.name):
                self.assertTrue(d.is_dir())
                self.assertEqual(0o700, d.stat().st_mode & 0o777)

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
    """The recreate condition: no mounts at all (a machine made the old way),
    or a mount this design does not want."""

    CASES = {
        "none": [],
        "users": [("/Users", "/Users", False)],
        "one of three": [(str(REPO), "/opt/wk-tools", True)],
    }

    def setUp(self):
        super().setUp()
        # The wanted set minus the writable one: a machine made before the
        # agent credential directory existed, which is the case this converges.
        self.CASES = dict(self.CASES)
        self.CASES["no agent-rw"] = list(self.want()[:2])

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
        # From the machine, at the moment of asking -- not a list in the code.
        self.assertIn("workspaces  wk-demo", out)
        self.assertIn("machine ssh", self.podman)
        # The one thing that is not regenerable, and how to keep it.
        self.assertIn("/var/lib/wk/bench", out)
        self.assertIn("tar -C /var/lib/wk -cf - bench", out)
        # The keys are not in the loss list: they are on this host already.
        self.assertIn("wk key register", out)

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
        """`./setup --dry-run` says what would change; a prompt that could
        destroy a store on the way to a report is not a report."""
        cp = self._run("none", env={"WK_DRY_RUN": "1", "WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("would be destroyed and recreated", out)
        for verb in ("machine rm", "machine init", "machine start"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.podman, self.podman)
        # A stopped machine cannot be read and is not started to read it, so
        # the list says that rather than claiming there is nothing to lose.
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


class TestTheRightSetMountedTheWrongWayFailsLoudly(_Stage):
    """The mode of each mount is asked for at init; a podman that takes the
    option and drops it would leave a workspace able to rewrite this checkout
    or its own keys -- or, the other way round, unable to write the one file it
    is supposed to rotate. That is a refusal, not a second recreate: a recreate
    would ask for the same modes again and loop."""

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
        """Even with a headless yes: it would destroy the store each time
        round and end up exactly here again."""
        secrets, tools, rw = self.want()
        self._wrong([(secrets[0], secrets[1], False),
                     (tools[0], tools[1], False),
                     rw])

    def test_the_writable_one_handed_back_read_only(self):
        """Not cosmetic: the Claude CLI would fail every refresh, and the
        credential every workspace shares would go stale rather than rotate."""
        secrets, tools, rw = self.want()
        self._wrong([secrets, tools, (rw[0], rw[1], True)])


class TestAConfigItCannotReadIsRefused(_Stage):
    def test_an_unreadable_config_is_not_taken_as_proof_of_anything(self):
        """Mounts absent from the file is not "no mounts": inspect does not
        expose them at all, and a check that passes by accident is worse than
        none. Refused rather than recreated, even with a headless yes: no
        evidence has been taken that a recreate would change anything."""
        self.exists()
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text("{}")
        cp = self.run_stage(env={"WK_YES": "1"})
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("could not read mounts", out)
        self.assertNotIn("machine rm", self.podman, self.podman)


@requires_podman_vm()
class TestTheMountsAreThereOnThisMachine(unittest.TestCase):
    """The live half: what the two mounts are for is that the VM and every
    container can read them. Fails with the remedy on a machine made before
    this design, which is what ./setup then fixes."""

    REMEDY = "run ./setup (it recreates the machine with the two mounts)"

    def test_the_checkout_is_executable_at_opt_wk_tools(self):
        cp = podman_vm_ssh("test -x /opt/wk-tools/wk && echo yes")
        self.assertIn("yes", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_it_is_this_checkout_and_not_a_copy(self):
        """A copy answers with its own bytes; the mount answers with this
        file's, and this test is in it."""
        rel = Path(__file__).relative_to(REPO)
        cp = podman_vm_ssh(f"cat /opt/wk-tools/{rel} 2>/dev/null | head -1")
        self.assertIn(__doc__.splitlines()[0], cp.stdout,
                      f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_the_secrets_directory_is_a_mount(self):
        cp = podman_vm_ssh("findmnt -no TARGET /var/lib/wk/secrets")
        self.assertIn("/var/lib/wk/secrets", cp.stdout,
                      f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_the_agent_credential_directory_is_a_mount_and_is_writable(self):
        """The one thing in this design a workspace may write, and the only
        place the measurement can be taken: whether this podman's virtiofs
        gives the machine write access to a host directory is not answerable
        from the config file."""
        cp = podman_vm_ssh("findmnt -no OPTIONS /var/lib/wk/agent-rw")
        opts = cp.stdout.strip().split(",")
        self.assertTrue(cp.stdout.strip(),
                        f"/var/lib/wk/agent-rw is not mounted at all. {self.REMEDY}")
        self.assertIn("rw", opts,
                      f"/var/lib/wk/agent-rw is mounted {cp.stdout.strip()!r}: the "
                      f"Claude CLI cannot rotate the credential in it. {self.REMEDY}")
        cp = podman_vm_ssh(
            "touch /var/lib/wk/agent-rw/.wk-write-probe "
            "&& rm -f /var/lib/wk/agent-rw/.wk-write-probe && echo wrote")
        self.assertIn("wrote", cp.stdout,
                      f"the machine cannot write /var/lib/wk/agent-rw: "
                      f"{cp.stdout}{cp.stderr}. {self.REMEDY}")

    def test_the_two_read_only_mounts_are_read_only_in_the_vm(self):
        """`--volume src:target:ro` is what host/macos/machine.sh asks for, and
        whether this podman honours the option is answerable only here: the
        kernel's own view of the mount, read without writing to it."""
        for target in ("/opt/wk-tools", "/var/lib/wk/secrets"):
            with self.subTest(target=target):
                cp = podman_vm_ssh(f"findmnt -no OPTIONS {target}")
                opts = cp.stdout.strip().split(",")
                self.assertIn("ro", opts,
                              f"{target} is mounted {cp.stdout.strip()!r}: podman took "
                              f"the ro option and dropped it, so a workspace can write "
                              f"to it. {self.REMEDY}")

    def test_a_container_can_read_both_through_its_own_mounts(self):
        """Nested: a container bind-mounts the VM's mount of this checkout at
        /opt/wk-tools and of the secrets at /secrets."""
        cp = podman_vm_ssh(
            "podman run --rm "
            "-v /opt/wk-tools:/opt/wk-tools:ro -v /var/lib/wk/secrets:/secrets:ro "
            "--entrypoint /bin/sh "
            "$(podman images --format '{{.Repository}}:{{.Tag}}' | head -1) "
            "-c 'test -x /opt/wk-tools/wk && test -d /secrets && echo both'",
            timeout=180)
        if "no such" in (cp.stdout + cp.stderr).lower() and "images" in cp.stderr:
            self.skipTest("no container image on this machine to run in")
        self.assertIn("both", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_a_container_can_write_the_agent_credential_directory(self):
        """The whole reason that mount exists: the Claude CLI in a workspace
        writes its rotated credential back through a temp file and a rename,
        and both have to work as the workspace's own uid. Two host hops of
        virtiofs and a bind mount stand between it and the host directory, and
        this is the only place that is measurable."""
        cp = podman_vm_ssh(
            "podman run --rm -v /var/lib/wk/agent-rw:/agent-rw "
            "--entrypoint /bin/sh "
            "$(podman images --format '{{.Repository}}:{{.Tag}}' | head -1) "
            "-c 'echo hi > /agent-rw/.wk-probe.tmp "
            "&& mv /agent-rw/.wk-probe.tmp /agent-rw/.wk-probe "
            "&& rm -f /agent-rw/.wk-probe && echo wrote'",
            timeout=180)
        if "no such" in (cp.stdout + cp.stderr).lower() and "images" in cp.stderr:
            self.skipTest("no container image on this machine to run in")
        self.assertIn("wrote", cp.stdout,
                      f"a container cannot write and rename inside /agent-rw, so the "
                      f"Claude login credential cannot be rotated from a workspace: "
                      f"{cp.stdout}{cp.stderr}. {self.REMEDY}")


class TestReportLossesStripsTheContainerPrefix(unittest.TestCase):
    """_report_losses (host/macos/machine.sh) lists what a recreate would
    lose by asking the machine's own podman for the container names -- and a
    container is named `wk-<workspace>` (_ctr, targets/container.sh), never
    the workspace name itself. Printing the raw name reads as a container
    catalog; a person deciding whether to recreate the machine wants to know
    which workspaces this loses, so the report strips the prefix the same
    way `t_list` (targets/container.sh) and `wk start` (cmd/start) do."""

    def _payload(self):
        """The script _report_losses pipes into `podman machine ssh`, lifted
        rather than retyped -- a second copy here could drift from the real
        one silently."""
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
