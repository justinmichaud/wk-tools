"""host/macos/machine.sh: the podman machine's three mounts.

The machine mounts exactly this checkout at /var/opt/wk-tools -- which the
machine OS also spells /opt/wk-tools, /opt being a symlink into /var on an
ostree system -- and this device's secrets directory at $WK_STORE/secrets,
both read-only, plus the one directory a workspace may write --
$WK_STORE/agent-rw, holding the claude.ai login credential the Claude CLI
rewrites in place (wk_agent_rw_dir, lib/store.sh) -- and nothing else:
`/Users` above all, which podman mounts by default. Which mount is writable is
as much a part of the invariant as which mounts there are, so the two are
separate verdicts: a different *set* is recreated, a set mounted the wrong way
round is refused (recreating that would ask podman for the same modes again
and loop). A third verdict comes from the machine rather than its config: a
mount the config asks for and the machine has not got.

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
    # Other machines this host has, for the retire-them-first step; none by
    # default, which is what a host set up by ./setup looks like.
    echo "${WK_TEST_MACHINE_LIST:-[]}" ;;
"machine inspect")
    [ -f "$WK_TEST_VM/exists" ] || exit 1
    case "$*" in
    *"{{.State}}"*)           cat "$WK_TEST_VM/state" ;;
    # What the machine currently has, which the resources step compares the
    # envelope against: what the init was given, or WK_TEST_CPUS/WK_TEST_MEM
    # for a machine no init in this run made -- a value nothing matches by
    # default, so the resize is exercised.
    *"{{.Resources.CPUs}}"*)   cat "$WK_TEST_VM/cpus" 2>/dev/null || echo "${WK_TEST_CPUS:-999}" ;;
    *"{{.Resources.Memory}}"*) cat "$WK_TEST_VM/mem"  2>/dev/null || echo "${WK_TEST_MEM:-999}" ;;
    *) echo '{}' ;;
    esac ;;
"machine init")
    : > "$WK_TEST_VM/exists"
    echo stopped > "$WK_TEST_VM/state"
    # In a subshell: it inherits "$@" and the config write below still needs it.
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
    # The mounts the machine actually has, which is not the same question as
    # what its config asks for: every target by default, and none of the
    # targets named in WK_TEST_ABSENT.
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
                (str(REPO), "/var/opt/wk-tools", True),
                (str(self.agent_rw), "/var/lib/wk/agent-rw", False))

    def run_stage(self, env=None, podman=None):
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
        """An empty --volume is a fourth mount, not an empty list, and an
        argv that names no --volume at all gets podman's own defaults --
        /Users first."""
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


class TestProvisioningHasOnePath(_Stage):
    """podman takes a `--playbook` and runs it at first boot from a generated
    `ConditionFirstBoot=yes` unit whose recap nothing reads: a task that fails
    there is silent, and the failed unit it leaves behind outlives every
    re-run. The vmtools stage runs the same playbook over ssh and dies on its
    recap, and a machine that already exists is provisioned through that path
    regardless -- so that is the only path."""

    VMTOOLS = REPO / "host" / "macos" / "vmtools.sh"

    def test_init_is_handed_no_playbook(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(self.init_argv(), self.podman)
        self.assertNotIn("--playbook", self.init_argv(), self.podman)

    def test_the_stage_that_does_run_it_reads_the_recap(self):
        text = self.VMTOOLS.read_text()
        self.assertIn("ansible-playbook /home/core/playbook.yaml", text)
        self.assertIn("_playbook_verdict", text)


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
    """The recreate condition: no mounts at all, or any set other than the
    three this design wants."""

    CASES = {
        "none": [],
        "users": [("/Users", "/Users", False)],
        "one of three": [(str(REPO), "/var/opt/wk-tools", True)],
    }

    def setUp(self):
        super().setUp()
        # The wanted set minus the writable one: a machine that cannot rotate
        # the agent credential, and has no mount to add it through.
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


class TestEveryTargetIsCanonicalInTheMachineOS(_Stage):
    """The machine OS is an ostree system: /var is the only writable tree, and
    every mutable top-level directory outside it -- /opt, /home, /srv,
    /usr/local, /media, /mnt -- is a symlink into it. podman names each
    generated .mount unit after the target it was handed, and systemd refuses
    a Where= that is not canonical ("Mount path /opt/wk-tools is not canonical
    (contains a symlink)"), so a target outside /var comes up as a failed unit
    and a machine running without the mount.

    Read off the init argv, not off this file's own list: the argv is what
    podman is actually asked for."""

    def test_no_volume_target_is_outside_var(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        argv = self.init_argv().split()
        targets = [argv[i + 1].split(":")[1]
                   for i, a in enumerate(argv) if a == "--volume"]
        self.assertEqual(3, len(targets), self.podman)
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(target.startswith("/var/"), target)


class TestAMountTheMachineAsksForAndHasNotGot(_Stage):
    """The config file records what podman was asked for; whether the machine
    got it is a question only the machine can answer, and it is the one that
    counts. A mount is settable only at creation, so the answer is the same
    recreate -- which is why every remedy in the tree can go on saying
    `./setup`."""

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
        """Answered yes, the machine is destroyed and made again with the same
        triples -- and this fake podman goes on withholding the mount, so the
        run ends at the refusal TestAFreshMachineThatCameUpWithoutAMount is
        about rather than reporting success."""
        cp = self._running_machine_missing_the_tools_mount(env={"WK_YES": "1"})
        self.assertIn("machine rm", self.podman, self.podman)
        self.assertIn("machine init", self.podman, self.podman)
        self.assertIn("internal error", cp.stdout + cp.stderr)

    def test_a_stopped_machine_is_not_started_to_ask(self):
        """This runs from the read that decides whether anything needs
        changing; starting a machine to answer it is a change."""
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
    """The one case a recreate cannot fix: podman was handed these exact
    triples a moment ago and the machine still came up without one, so
    `./setup` would destroy and recreate it into the same state for ever.
    It is caught because the create starts the machine, which is what makes
    the mounts readable at all."""

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


# A `podman` whose `machine init` records each mount source the way the kernel
# spells it rather than the way it was handed it -- which is what any
# canonicalising path handling does, and what macOS's own /var -> /private/var
# symlink does to every path under /var. $WK_TEST_CANON says how: `real`
# resolves the source, `slash` puts a trailing slash on the target.
FAKE_PODMAN_CANON = FAKE_PODMAN.replace(
    'spec = argv[i + 1].split(":")',
    'spec = argv[i + 1].split(":")\n'
    '        import os\n'
    '        how = os.environ.get("WK_TEST_CANON", "")\n'
    '        if how == "real":\n'
    '            spec[0] = os.path.realpath(spec[0])\n'
    '        if how == "slash":\n'
    '            spec[1] = spec[1] + "/"')

# A `podman` that quietly adds a mount of its own to whatever it was asked
# for: the only way this stage can read `differs` about a machine it has just
# created with the right triples.
FAKE_PODMAN_ADDS_A_MOUNT = FAKE_PODMAN.replace(
    'json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))',
    'mounts.append({"Type": "virtiofs", "Source": "/Users",\n'
    '               "Target": "/Users", "ReadOnly": False})\n'
    'json.dump({"Name": "wk", "Mounts": mounts}, open(cfg, "w"))')


class TestTwoSpellingsOfOnePathAreOneMount(_Stage):
    """The verdict is `differs` for a machine whose mount *set* is not this
    design's, and `differs` is what destroys a store. A machine created by this
    very run reads back with whatever spelling podman recorded -- a resolved
    symlink, macOS's /var -> /private/var, a trailing slash -- so a comparison
    by string reports a fresh machine as wrong and `_check_mounts` then advises
    `./setup`, which destroys and recreates it into exactly the same state.
    That is a loop, and it eats a store each time round."""

    def run_canon(self, how, podman=None, env=None):
        e = {"WK_TEST_CANON": how}
        e.update(env or {})
        # The stub is chosen per-test; _Stage.run_stage installs FAKE_PODMAN,
        # so this replaces it for the run.
        import subprocess as sp
        script = f'''
set -euo pipefail
WK_ROOT={self.wk_root}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/resources.sh"
is_macos() {{ return 0; }}
. "{REPO}/host/macos/machine.sh"
'''
        base = dict(os.environ)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND", "WK_MARKER",
                    "WK_YES", "WK_STORE", "WK_IN_VM", "WK_DRY_RUN"):
            base.pop(var, None)
        base.update({
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "WK_HOST_SECRETS": str(self.secrets),
            "WK_STORE": "/var/lib/wk",
            "WK_TEST_VM": str(self.vm),
            "WK_TEST_CFG": str(self.cfg),
            "WK_TEST_PODMAN_LOG": str(self.log),
            "WK_DEBUG": "1",
        })
        base.update(e)
        with stub_path({"podman": podman or FAKE_PODMAN_CANON}) as binp:
            base["PATH"] = f"{binp}:{os.environ['PATH']}"
            cp = sp.run(["bash", "-c", script], cwd=str(REPO), env=base,
                        capture_output=True, text=True, timeout=120)
        self.podman = self.log.read_text()
        return cp

    @property
    def wk_root(self):
        return getattr(self, "_wk_root", REPO)

    def test_a_symlinked_checkout_reads_ok_rather_than_differing(self):
        """`WK_ROOT` is wherever this checkout is reached from, and a person
        whose work tree is behind a symlink is not a person with a wrong
        machine."""
        link = self.tmp / "wk-tools-link"
        link.symlink_to(REPO)
        self._wk_root = link
        cp = self.run_canon("real")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("read-write (verified)", out)
        self.assertNotIn("does not have this design's mounts", out)
        self.assertNotIn("machine rm", self.podman, self.podman)

    def test_a_trailing_slash_on_a_target_reads_ok_too(self):
        cp = self.run_canon("slash")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("read-write (verified)", out)
        self.assertNotIn("machine rm", self.podman, self.podman)

    def test_the_rows_are_still_the_machines_own_spelling(self):
        """A person reading a refusal is looking for what is in the config
        file, so the canonical form is only ever shown beside the raw one."""
        self.exists()
        write_cfg(self.cfg, [("/Users", "/Users", False)])
        cp = self.run_stage()
        self.assertIn("has /Users:/Users rw", cp.stdout + cp.stderr)


class TestAFreshMachineThatStillDiffersIsAnInternalError(_Stage):
    """The other half of the same hazard. If a machine created by this run
    still reads back as a different set, the remedy is not `./setup`: the init
    was handed these exact triples a moment ago, so re-running would destroy
    and recreate it to reach the same place. It is this file's comparison that
    is wrong, and the refusal has to say so and stop."""

    def test_it_dies_naming_both_spellings_and_never_advises_a_re_run(self):
        cp = self.run_stage(env={"WK_YES": "1"},
                            podman=FAKE_PODMAN_ADDS_A_MOUNT)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("internal error", out)
        self.assertIn("was just created", out)
        # Both spellings: what it asked for and what it reads back.
        self.assertIn("asks %s" % self.agent_rw, out)
        self.assertIn("has  /Users:/Users", out)
        self.assertNotIn("Recreate it with:  ./setup", out)
        self.assertIn("Do NOT re-run ./setup", out)

    def test_the_ordinary_differs_still_names_the_remedy(self):
        """Nothing above may be bought by an existing machine's refusal losing
        the one thing that fixes it."""
        self.exists()
        write_cfg(self.cfg, [("/Users", "/Users", False)])
        cp = self.run_stage()
        out = cp.stdout + cp.stderr
        self.assertIn("does not have this design's mounts", out)
        self.assertNotIn("internal error", out)


class TestADryRunTouchesNoDirectory(_Stage):
    """`./setup --dry-run` reports what would change. The two mount-source
    directories were made before anything checked for a dry run, so a report
    created them."""

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

    def test_a_real_run_still_makes_them(self):
        cp = self.run_stage()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for d in (self.secrets, self.agent_rw):
            self.assertTrue(d.is_dir())


class TestThePrivateModeIsAssertedEveryRun(WkTest):
    """`ensure_dir <dir> 0700` chmod'ed only on the run that created the
    directory, so one that already existed as 0755 -- made by an older wk, by
    `mkdir -p` in a shell, or by a umask -- stayed readable by every other
    account on the machine, for ever: the next run reported it unchanged.

    These are the directories holding the private deploy keys, the GitHub API
    token and the claude.ai login."""

    def ensure(self, d, mode="0700", env=None):
        return self.bash(f'. "$WK_ROOT/lib/common.sh"; ensure_dir "{d}" {mode}',
                         env=env)

    def test_an_existing_world_readable_directory_is_made_private(self):
        d = self.tmp / "push-keys"
        d.mkdir(mode=0o755)
        os.chmod(d, 0o755)
        cp = self.ensure(d)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(0o700, d.stat().st_mode & 0o777)

    def test_a_new_one_is_private_from_the_start(self):
        d = self.tmp / "fresh"
        cp = self.ensure(d)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(0o700, d.stat().st_mode & 0o777)

    def test_a_caller_that_names_no_mode_leaves_an_existing_one_alone(self):
        """The assertion is the caller's, not this function's: `ensure_dir
        <dir>` only asks for the directory, and a store shared by a group or a
        home directory is not this call's to narrow."""
        d = self.tmp / "shared"
        d.mkdir(mode=0o775)
        os.chmod(d, 0o775)
        cp = self.bash(f'. "$WK_ROOT/lib/common.sh"; ensure_dir "{d}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(0o775, d.stat().st_mode & 0o777)

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
    """The live half: what the three mounts are for is that the VM and every
    container can reach them. Fails with the remedy on a machine mounting
    anything else, which is what ./setup then fixes."""

    REMEDY = "run ./setup (it recreates the machine with the three mounts)"

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

    def _an_image(self):
        """Any image the machine already has, to run the probe in -- asked
        before the run, not read out of its failure: a machine with none
        expands the substitution to nothing and podman takes the next
        argument as the image name, which fails for a reason that has
        nothing to do with the mounts."""
        cp = podman_vm_ssh("podman images --format '{{.Repository}}:{{.Tag}}' | head -1")
        image = cp.stdout.strip()
        if not image or image.startswith("<none>"):
            self.skipTest("no container image on this machine to run a probe in")
        return image

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

    def test_the_read_only_mounts_are_read_only_in_the_vm(self):
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
            f"podman run --rm "
            f"-v /opt/wk-tools:/opt/wk-tools:ro -v /var/lib/wk/secrets:/secrets:ro "
            f"--entrypoint /bin/sh {self._an_image()} "
            f"-c 'test -x /opt/wk-tools/wk && test -d /secrets && echo both'",
            timeout=180)
        self.assertIn("both", cp.stdout, f"{self.REMEDY}: {cp.stdout}{cp.stderr}")

    def test_a_container_can_write_the_agent_credential_directory(self):
        """The whole reason that mount exists: the Claude CLI in a workspace
        writes its rotated credential back through a temp file and a rename,
        and both have to work as the workspace's own uid. Two host hops of
        virtiofs and a bind mount stand between it and the host directory, and
        this is the only place that is measurable."""
        cp = podman_vm_ssh(
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
    """applehv runs one VM at a time, so a leftover machine does not merely
    waste disk -- it stops the wk machine starting at all, with an error that
    names neither machine. Removing one loses only that machine's own images
    (machine storage is per-machine), so it is offered rather than done."""

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
        """podman marks the default with a trailing `*`; stripping it is what
        keeps this from offering to delete the machine it is setting up."""
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
        """No terminal is a decline (lib/common.sh), which is the routine case
        for a scripted run: nothing is destroyed without an answer."""
        cp = self._run()
        out = cp.stdout + cp.stderr
        self.assertIn("keeping 'podman-machine-default'", out)
        self.assertIn("will fail to start", out)
        self.assertNotIn("machine rm -f podman-machine-default", self.podman)


class TestTheResourceEnvelopeIsReapplied(_Stage):
    """`podman machine set` needs the machine stopped, so re-applying the
    envelope is three steps or none -- and the envelope is recomputed from
    this host every run, since the host it was last applied on may not be
    this one."""

    def _envelope(self):
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"; '
                           'printf "%s %s" "$(envelope_cores)" "$(envelope_mem_mb)"'],
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
        self.assertIn(f"machine resources ({cores} cpus, {mem} MiB)", out)
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
        """Left stopped, a re-run of ./setup would have turned off a machine
        somebody was building in."""
        self._run(running=True)
        order = [l.split()[1] for l in self.podman.splitlines()
                 if l.startswith("machine ") and l.split()[1] in ("stop", "set", "start")]
        self.assertEqual(["stop", "set", "start"], order, self.podman)


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
