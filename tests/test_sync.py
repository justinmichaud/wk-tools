"""Tests for the WK_DEBUG per-stage timing and the mirror-unchanged skip
added to cmd/sync (defect: "why is wk sync so slow on rpi5").

cmd/sync executes a real sync top to bottom the moment it runs, so these
tests never source it for real work -- `bash -c '. cmd/sync functions'`
stops it right after the helper functions are defined (the guard at the top
of the file), which is exactly what lets a test load `stage_begin`,
`stage_end` and `snapshot_current` without touching the network, the store,
or a workspace.

Run: python3 -m unittest tests.test_sync -v
"""

import contextlib
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, bash, fake_workspace, run


def _lift_range(path, start_pattern, end_pattern):
    """Lines from the first line matching start_pattern through the first
    line matching end_pattern (inclusive), sed'd out of `path` -- the same
    technique tests/test_wifi_seed.py's _lift() uses for a whole function,
    generalised to a range for cmd/sync's argument-parsing block (inline
    top-level code, not a function of its own) and the handful of `wk`
    functions the forwarding-rule tests below need."""
    return subprocess.run(
        ["sed", "-n", f"/{start_pattern}/,/{end_pattern}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


def _lift_func(path, func):
    return subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


class TestStageTiming(unittest.TestCase):
    """stage_begin/stage_end is the one place `date +%s` arithmetic happens
    in cmd/sync; every WK_DEBUG timing line -- mirror fetch, snapshot
    publish, checkout/reset/clean, each workspace's fetch -- goes through
    it, so testing the pair once covers the shape of all of them."""

    def test_emits_a_stage_line_under_wk_debug(self):
        cp = bash(
            ". cmd/sync functions\n"
            "stage_begin\n"
            "stage_end mystage\n",
            env={"WK_DEBUG": "1"},
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertRegex(cp.stderr, r"stage mystage: \d+s")

    def test_silent_without_wk_debug(self):
        cp = bash(
            ". cmd/sync functions\n"
            "stage_begin\n"
            "stage_end mystage\n",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("stage mystage", cp.stderr)

    def test_functions_seam_loads_without_running_a_real_sync(self):
        # 'functions' as $1 stops cmd/sync right after its helpers are
        # defined, before it touches the network, the store, or a
        # workspace -- exactly what makes the two tests above possible.
        cp = bash(
            ". cmd/sync functions\n"
            "type stage_begin stage_end snapshot_current >/dev/null "
            "&& echo helpers-ok\n",
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("helpers-ok", cp.stdout)



class TestWorkspaceFetchesRunTogether(unittest.TestCase):
    """A machine with nine workspaces pays nine round trips, and they do not
    depend on each other: sync_workspaces runs them under lib/par.sh and
    replays the records in the order the names were given, so the listing
    reads the same whatever order they finish in."""

    LOOP = _lift_func(REPO / "cmd" / "sync", "sync_workspaces")

    STUBS = """
wk_mirror_default_remotes() { echo origin; }
ws_fetch_one() {
    case "$1" in
        slow) sleep 0.4; printf '  %-24s ok\\n' slow >&3; return 0 ;;
        gone) printf '  %-24s absent -- skipped\\n' gone >&3; return 2 ;;
        bad)  printf '  %-24s FAILED (continuing)\\n' bad >&3; return 1 ;;
    esac
}
"""

    def _run(self, *names, scope="all"):
        script = (f'. "{REPO}/lib/common.sh"\n. "{REPO}/lib/par.sh"\n'
                  + self.STUBS + f"SCOPE={scope}\nONLY=gone\n" + self.LOOP
                  + "\nsync_workspaces " + " ".join(names) + "\n")
        return bash(script, timeout=60)

    def test_the_listing_is_in_the_order_asked_for_not_finished_in(self):
        cp = self._run("slow", "gone", "bad")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        rows = [l.split()[0] for l in cp.stderr.splitlines() if l.startswith("  ")]
        self.assertEqual(rows[:3], ["slow", "gone", "bad"], cp.stderr)

    def test_a_failure_is_counted_and_a_skip_is_not(self):
        cp = self._run("slow", "gone", "bad")
        self.assertIn("1 workspace(s) did not fetch", cp.stderr, cp.stderr)

    def test_a_named_workspace_that_is_not_there_refuses(self):
        """The `ws` scope is one name a person typed, so "absent" is a
        refusal rather than a line in a listing."""
        cp = self._run("gone", scope="ws")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("not there to fetch in", cp.stderr, cp.stderr)


class TestSyncHelpMentionsTiming(unittest.TestCase):
    def test_wk_sync_dash_h_mentions_wk_debug(self):
        cp = run("sync", "-h")
        self.assertIn("WK_DEBUG", cp.stdout)


class TestSnapshotCurrent(unittest.TestCase):
    """snapshot_current <recorded-sha> <mirror-sha> is the pure decision
    behind cmd/sync's fix: a fresh snapshot only matters if it would differ
    from what is already published, so the checkout/reset/clean that
    dominates a sync (measured on rpi5: ~30s against ~10s to fetch the
    mirror) is skippable whenever the previously published base's recorded
    sha already matches the mirror's. Driven on synthetic shas -- no store,
    no mirror, no network needed to exercise the logic itself."""

    def _current(self, recorded, mirror):
        cp = bash(
            ". cmd/sync functions\n"
            f'snapshot_current "{recorded}" "{mirror}"\n'
        )
        return cp.returncode == 0

    def test_matching_shas_are_current(self):
        sha = "d" * 40
        self.assertTrue(self._current(sha, sha))

    def test_differing_shas_are_not_current(self):
        self.assertFalse(self._current("a" * 40, "b" * 40))

    def test_no_recorded_sha_is_not_current(self):
        # An unpublished, or never-verified, snapshot has nothing recorded
        # -- never treated as already matching the mirror.
        self.assertFalse(self._current("", "a" * 40))

    def test_no_mirror_sha_is_not_current(self):
        self.assertFalse(self._current("a" * 40, ""))

    def test_both_empty_is_not_current(self):
        self.assertFalse(self._current("", ""))


class TestWhatAScopeNames(unittest.TestCase):
    """scope_targets is the one list every half of a run walks -- the tooling
    copies, whether this machine's mirror is refreshed, whose snapshot and
    workspaces -- so the scopes are pinned here once: bare is the targets on
    this machine, --target one, --all and a bare --tools every one this
    machine knows. Lifted and driven over a stubbed fleet."""

    FUNCS = (_lift_func(REPO / "cmd" / "sync", "scope_targets")
             + _lift_func(REPO / "cmd" / "sync", "scope_touches_here"))
    FLEET = """
target_here()    { echo container; echo vm; }
walk_targets()   { echo container; echo vm; echo buildbox4; echo moose; }
target_is_here() { case "$1" in container|vm) return 0 ;; *) return 1 ;; esac; }
"""

    def _targets(self, scope, target=""):
        cp = bash(". lib/common.sh\n" + self.FLEET + self.FUNCS
                  + f"SCOPE={shlex.quote(scope)}\nTARGET={shlex.quote(target)}\n"
                  "scope_targets\nscope_touches_here && echo HERE || echo ELSEWHERE\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.split()

    def test_bare_is_every_target_on_this_machine(self):
        self.assertEqual(self._targets("here"), ["container", "vm", "HERE"])

    def test_a_named_target_is_that_one(self):
        self.assertEqual(self._targets("target", "buildbox4"), ["buildbox4", "ELSEWHERE"])
        self.assertEqual(self._targets("target", "vm"), ["vm", "HERE"])

    def test_all_and_a_bare_tools_are_everyone(self):
        for scope in ("all", "tools"):
            with self.subTest(scope=scope):
                self.assertEqual(self._targets(scope),
                                 ["container", "vm", "buildbox4", "moose", "HERE"])

    def test_tools_with_a_target_is_that_one(self):
        self.assertEqual(self._targets("tools", "moose"), ["moose", "ELSEWHERE"])

    def test_no_command_decides_a_stores_locality_by_kind(self):
        """Whose snapshot to publish is each target's own answer (t_needs_base
        and store_is_local), not a kind list in cmd/sync."""
        self.assertNotIn("local_store_named", (REPO / "cmd" / "sync").read_text())


class TestWhatABareSyncMeans(unittest.TestCase):
    """The one decision left after parsing: a bare `wk sync` is this machine,
    whole -- every target that lives here, since the containers and the macOS
    guests read the one mirror this host keeps. Lifted by line range
    (top-level code, not a function)."""

    BLOCK = _lift_range(REPO / "cmd" / "sync", r"^# Bare is this machine", r"^fi$")

    def _resolve(self, scope="", only=""):
        script = (
            ". lib/common.sh\n"
            f"SCOPE={shlex.quote(scope)}\nONLY={shlex.quote(only)}\nTARGET=''\n"
            + self.BLOCK
            + "\nprintf 'SCOPE=%s TARGET=%s\\n' \"$SCOPE\" \"$TARGET\"\n"
        )
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_bare_is_this_machine(self):
        self.assertEqual(self._resolve(), "SCOPE=here TARGET=")

    def test_a_named_workspace_is_left_as_it_was_parsed(self):
        self.assertEqual(self._resolve(scope="ws", only="bug-238"),
                         "SCOPE=ws TARGET=")

    def test_a_scope_flag_is_left_as_it_was_parsed(self):
        for scope in ("all", "tools", "target"):
            with self.subTest(scope=scope):
                self.assertEqual(self._resolve(scope=scope), f"SCOPE={scope} TARGET=")


class TestWhatEachScopeRuns(unittest.TestCase):
    """cmd/sync's tail, in order: the tooling copies, this machine's mirror,
    then each target's snapshot and the fetch in its workspaces -- a snapshot
    is cloned off the mirror and a workspace fetches from it, so either before
    the refresh hands back what the machine already had. The mirror is
    refreshed where it is writable (mirror_is_here) and only when the scope
    names a target of this machine; a snapshot goes to whoever holds the
    target's store -- here, or the podman VM on a macOS host, which is asked
    with the same scope word. Lifted by line range and driven over stubs at
    every boundary (`with_lock store -- <fn>` is where the mirror refresh and
    the publish happen, so stubbing with_lock records them without running
    one)."""

    TAIL = _lift_range(REPO / "cmd" / "sync", r'^if \[ "\$SCOPE" = ws \]', r"^exit 0$")
    SCOPES = (_lift_func(REPO / "cmd" / "sync", "scope_targets")
              + _lift_func(REPO / "cmd" / "sync", "scope_touches_here"))

    STUBS = """
in_workspace()      { return 1; }
store_init()        { :; }
sync_workspaces()   { echo "WORKSPACES: $*"; }
sync_furniture()    { echo "FURNITURE: $(scope_targets | tr '\\n' ' ')"; }
sync_target()       { echo "FETCH-IN: $1"; }
load_target()       { case "$1" in container) _NEEDS=0 ;; *) _NEEDS=1 ;; esac; }
t_needs_base()      { return "$_NEEDS"; }
store_is_local()    { return 0; }
mirror_is_here()    { return 0; }
target_here()       { echo container; echo vm; }
walk_targets()      { echo container; echo vm; echo buildbox4; }
target_is_here()    { [ "$1" != buildbox4 ]; }
t_far_side()        { echo answering; }
t_wk()              { echo "IN-VM: wk $*"; }
with_lock()         { shift 2; echo "STORE: $*"; }
"""

    def _run(self, scope, target="", only="", extra=""):
        script = (
            ". lib/common.sh\n" + self.STUBS + self.SCOPES + extra
            + f"SCOPE={shlex.quote(scope)}\nTARGET={shlex.quote(target)}\n"
            f"ONLY={shlex.quote(only)}\n"
            + self.TAIL
        )
        cp = bash(script)
        return cp, [l.strip() for l in (cp.stdout + cp.stderr).splitlines()
                    if l.strip().split(":")[0] in ("FURNITURE", "STORE", "FETCH-IN", "WORKSPACES", "IN-VM")]

    def test_bare_is_the_tooling_the_mirror_then_each_target_here(self):
        cp, steps = self._run("here")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container vm", "STORE: sync_mirror",
                                 "STORE: sync_snapshot", "FETCH-IN: container",
                                 "FETCH-IN: vm"])

    def test_a_named_target_refreshes_its_furniture_before_fetching_in_it(self):
        cp, steps = self._run("target", target="container")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror",
                                 "STORE: sync_snapshot", "FETCH-IN: container"])

    def test_a_guest_target_gets_the_mirror_and_a_fetch_but_no_snapshot(self):
        """A guest clones its checkout off the host's mirror at first start
        (targets/vm.sh) and overlays nothing, so there is no snapshot to
        publish for it -- and no store of its own to fill with one."""
        cp, steps = self._run("target", target="vm")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: vm", "STORE: sync_mirror", "FETCH-IN: vm"])

    def test_all_visits_every_target_after_the_furniture(self):
        cp, steps = self._run("all")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container vm buildbox4", "STORE: sync_mirror",
                                 "STORE: sync_snapshot", "FETCH-IN: container",
                                 "FETCH-IN: vm", "FETCH-IN: buildbox4"])

    def test_tools_stops_at_the_furniture(self):
        cp, steps = self._run("tools", target="container")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror", "STORE: sync_snapshot"])

    def test_one_workspace_fetches_in_that_one_and_nothing_else(self):
        """`wk sync <ws>` is the cheap form a person types in a loop, and it
        refreshes no mirror: a fetch in one checkout, from the mirror it
        already has."""
        cp, steps = self._run("ws", only="bug-238")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["WORKSPACES: bug-238"])

    def test_a_machine_of_its_own_touches_neither_the_mirror_nor_a_snapshot_here(self):
        """A build box or a peer keeps its own store; this machine's mirror and
        snapshot are not part of syncing it (its own were, in sync_furniture)."""
        cp, steps = self._run("target", target="buildbox4")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: buildbox4", "FETCH-IN: buildbox4"])

    def test_a_store_in_the_podman_vm_is_asked_with_the_same_scope_word(self):
        """On a macOS host the container store is the VM's: the mirror is
        refreshed out here, where it is writable, and the VM -- which runs
        this same tree -- publishes the snapshot off its read-only mount and
        fetches in each workspace."""
        cp, steps = self._run("target", target="container",
                              extra="store_is_local() { return 1; }\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror",
                                 "IN-VM: wk sync --target container"])
        cp, steps = self._run("tools", target="container",
                              extra="store_is_local() { return 1; }\n")
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror",
                                 "IN-VM: wk sync --tools container"])

    def test_a_stopped_podman_machine_is_named_and_is_not_a_success(self):
        cp, steps = self._run("target", target="container",
                              extra="store_is_local() { return 1; }\nt_far_side() { echo stopped; }\n")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror"])
        self.assertIn("podman machine is stopped", cp.stdout + cp.stderr)
        self.assertIn("wk start", cp.stdout + cp.stderr)

    def test_inside_the_podman_vm_the_mirror_is_read_not_refreshed(self):
        """The VM's half of the macOS run above: its mirror is the host's,
        mounted read-only, so the snapshot is published off it and nothing in
        there fetches into it."""
        cp, steps = self._run("target", target="container",
                              extra="mirror_is_here() { return 1; }\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_snapshot",
                                 "FETCH-IN: container"])

    def test_a_target_that_did_not_take_the_tooling_fails_the_run(self):
        """The furniture verdict outlives the halves that follow it: the
        publish and the fetches still run, and a run that lost a target is not
        a success whatever they did."""
        cp, steps = self._run("target", target="container",
                              extra='sync_furniture() { echo "FURNITURE: $TARGET"; return 1; }\n')
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror",
                                 "STORE: sync_snapshot", "FETCH-IN: container"])

    def test_a_failed_publish_does_not_stop_the_fetches_and_is_not_a_success(self):
        cp, steps = self._run("target", target="container",
                              extra="with_lock() { shift 2; echo \"STORE: $*\"; [ \"$1\" != sync_snapshot ]; }\n")
        self.assertNotEqual(cp.returncode, 0, "a run that could not publish is not a success")
        self.assertEqual(steps, ["FURNITURE: container", "STORE: sync_mirror",
                                 "STORE: sync_snapshot", "FETCH-IN: container"])

    def test_the_snapshot_borrows_the_mirrors_objects(self):
        """`--shared`: a machine holds WebKit's history once, in the mirror,
        and a snapshot -- and every workspace overlaid on it -- borrows it."""
        self.assertIn("git clone --quiet --shared", self.TAIL)


class TestSyncArgParsing(unittest.TestCase):
    """The argument-parsing block at the top of cmd/sync: what a workspace
    name, --target, --all and --tools resolve to, and what the --machine
    tombstone and an unknown flag refuse with. It is top-level code rather than
    a function (what it parses is this run's arguments), so it is lifted by
    line range rather than by name. Every case here dies (or finishes parsing) before
    store_init -- lib/common.sh is the only other thing sourced, and it is
    pure at source time (tests/test_prompts.py relies on the same fact) -- so
    nothing here touches the network, the store or a workspace."""

    ARGPARSE = _lift_range(REPO / "cmd" / "sync", r'^USAGE="usage: wk sync', r"scope_set ws")

    def _parse(self, *args):
        set_line = "set -- " + " ".join(shlex.quote(a) for a in args) + "\n" if args else "set --\n"
        script = (
            ". lib/common.sh\n" + set_line + self.ARGPARSE
            + "\nprintf 'SCOPE=%s ONLY=%s TARGET=%s\\n' \"$SCOPE\" \"$ONLY\" \"$TARGET\"\n"
        )
        return bash(script)

    def _parsed(self, *args):
        cp = self._parse(*args)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_bare_is_no_scope_and_no_name(self):
        self.assertEqual(self._parsed(), "SCOPE= ONLY= TARGET=")

    def test_a_workspace_name_is_the_ws_scope(self):
        self.assertEqual(self._parsed("myws"), "SCOPE=ws ONLY=myws TARGET=")

    def test_all_sets_scope_all(self):
        self.assertEqual(self._parsed("--all"), "SCOPE=all ONLY= TARGET=")

    def test_target_takes_the_next_word(self):
        self.assertEqual(self._parsed("--target", "moose"), "SCOPE=target ONLY= TARGET=moose")

    def test_target_takes_an_equals_value_too(self):
        self.assertEqual(self._parsed("--target=moose"), "SCOPE=target ONLY= TARGET=moose")

    def test_an_unknown_target_is_refused_by_name(self):
        # Not "no workspaces on nosuchthing", and not "1 target(s) did not
        # take the tooling": the name is checked once, by load_target, which
        # is the one thing that knows what a target is.
        for args in (("--target", "nosuchthing"), ("--tools", "nosuchthing"),
                     ("--target=nosuchthing",)):
            cp = run("sync", *args)
            self.assertNotEqual(cp.returncode, 0, f"{args} was accepted")
            self.assertIn("unknown target 'nosuchthing'", cp.stdout)

    def test_target_with_no_value_is_refused(self):
        cp = self._parse("--target")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--target names the target", cp.stderr)

    def test_tools_alone_is_every_target(self):
        # The target is optional: no name means every copy this machine owns.
        self.assertEqual(self._parsed("--tools"), "SCOPE=tools ONLY= TARGET=")

    def test_tools_takes_an_optional_target(self):
        self.assertEqual(self._parsed("--tools", "buildbox4"), "SCOPE=tools ONLY= TARGET=buildbox4")

    def test_tools_does_not_eat_a_following_flag_as_its_target(self):
        # `--tools --all` is two scopes, not a target called "--all": the
        # optional argument is taken only when the next word is not a flag.
        cp = self._parse("--tools", "--all")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("ask for different things", cp.stderr)

    def test_two_scopes_are_refused_rather_than_last_one_wins(self):
        for pair in (("--all", "--tools"), ("--tools", "--all"),
                     ("--all", "--target", "moose"), ("--target", "moose", "--all")):
            cp = self._parse(*pair)
            self.assertNotEqual(cp.returncode, 0, f"{pair} was accepted")
            self.assertIn("ask for different things -- one at a time", cp.stderr)

    def test_a_name_and_a_scope_together_is_refused(self):
        cp = self._parse("myws", "--all")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("ask for different things", cp.stderr)
        self.assertIn("'myws'", cp.stderr)

    def test_a_second_target_is_refused_rather_than_overwriting_the_first(self):
        for pair in (("--target", "moose", "--target", "buildbox4"),):
            cp = self._parse(*pair)
            self.assertNotEqual(cp.returncode, 0, f"{pair} was accepted")
            self.assertIn("one target at a time (got 'moose' and 'buildbox4')", cp.stderr)

    def test_machine_is_a_tombstone_naming_both_replacements(self):
        # One flag for two unrelated pieces of work is what the scopes
        # replace, so the old spelling is refused by name rather than aliased
        # to either of them.
        cp = self._parse("--machine")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'wk sync --machine' is gone", cp.stderr)
        self.assertIn("wk sync --all", cp.stderr)
        self.assertIn("wk sync --tools", cp.stderr)


class TestScopeRouting(unittest.TestCase):
    """Where each scope's work is actually sent, driven against the two
    functions that decide it -- sync_target (--target/--all) and
    sync_furniture (--tools) -- lifted out of cmd/sync and run over stubs.

    load_target is the stub: a real one sources the target's driver, and the
    driver redefines t_sync/t_wk/target_workspaces over anything defined
    before it -- so stubbing only those reaches the real machine over ssh
    instead of the stub. Nothing here touches the network."""

    SYNC_TARGET = _lift_func(REPO / "cmd" / "sync", "sync_target")
    SYNC_FURNITURE = _lift_func(REPO / "cmd" / "sync", "sync_furniture")

    def _run(self, funcs, stubs, call):
        return bash(". lib/common.sh\n" + funcs + "\n" + stubs + "\n" + call + "\n")

    PLAIN_STUBS = """
load_target() { case "$1" in moose) WK_REMOTE_PEER=1 ;; *) WK_REMOTE_PEER="" ;; esac; }
t_needs_base() { return 1; }
store_is_local() { return 0; }
target_workspaces() { echo ws-a; echo ws-b; }
t_wk() { echo "OVER-THERE: wk $*"; }
sync_workspaces() { echo "FETCH: $* target=${WK_TARGET:-}"; }
"""

    def test_a_plain_target_fetches_in_its_workspaces_from_here(self):
        cp = self._run(self.SYNC_TARGET, self.PLAIN_STUBS, "sync_target buildbox4")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("FETCH: ws-a ws-b", cp.stdout)

    def test_a_plain_targets_fetch_is_pinned_to_that_target(self):
        # WK_TARGET, so ws_target answers from what is already known here
        # rather than probing every other target over ssh, once per workspace.
        cp = self._run(self.SYNC_TARGET, self.PLAIN_STUBS, "sync_target buildbox4")
        self.assertIn("target=buildbox4", cp.stdout)

    def test_a_peer_is_asked_for_each_workspace_by_name(self):
        # A peer's workspaces are containers on the peer: their checkouts are
        # inside them, so nothing here can cd into one. It is asked one
        # workspace at a time and never with a scope word -- what a scope
        # means is decided by *that* machine's copy of wk-tools, and an older
        # one spelling `--all` differently is how a command naming one
        # machine reached machines it never named.
        cp = self._run(self.SYNC_TARGET, self.PLAIN_STUBS, "sync_target moose")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(
            [l for l in cp.stdout.splitlines() if l.startswith("OVER-THERE:")],
            ["OVER-THERE: wk sync ws-a", "OVER-THERE: wk sync ws-b"])
        self.assertNotIn("--all", cp.stdout)
        self.assertNotIn("--tools", cp.stdout)
        self.assertNotIn("FETCH:", cp.stdout)

    FURNITURE_STUBS = """
load_target() { :; }
scope_targets() { if [ -n "$TARGET" ]; then echo "$TARGET"; else echo container; echo vm; echo buildbox4; fi; }
t_sync() { echo "furniture: $_t named=${WK_SYNC_NAMED:-no}"; }
"""

    def test_tools_with_no_target_visits_every_one(self):
        cp = self._run(self.SYNC_FURNITURE, self.FURNITURE_STUBS, 'TARGET=""; sync_furniture')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(
            [l for l in cp.stdout.splitlines() if l.startswith("furniture:")],
            ["furniture: container named=no", "furniture: vm named=no",
             "furniture: buildbox4 named=no"])

    def test_tools_with_a_target_visits_only_that_one_and_names_it(self):
        # WK_SYNC_NAMED is the difference a peer reads: a snapshot is
        # published on somebody else's workstation only when it was named,
        # never as part of a sweep (targets/remote.sh, t_sync).
        cp = self._run(self.SYNC_FURNITURE, self.FURNITURE_STUBS, "TARGET=buildbox4; sync_furniture")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(
            [l for l in cp.stdout.splitlines() if l.startswith("furniture:")],
            ["furniture: buildbox4 named=1"])


class TestPeerFurnitureVersionGate(unittest.TestCase):
    """targets/remote.sh's t_sync, the `wk sync --tools <peer>` half: a peer
    is a workstation under git, so its tooling is pulled rather than pushed,
    and only once that pull has actually converged is it asked to publish its
    own snapshot. A copy that still differs is not handed a scope word --
    what `--tools` means over there is that copy's to decide, and an older
    one spells it differently, which is how a command naming one machine
    reaches machines it never named.

    Lifted and stubbed at the ssh boundary (_rsh_q), so no machine is
    reached: the pull and the version question both stop here."""

    T_SYNC = _lift_func(REPO / "targets" / "remote.sh", "t_sync")

    def _run(self, theirs, named):
        stubs = f"""
WK_TARGET=apeer
WK_REMOTE_HOST=apeer
{'WK_SYNC_NAMED=1' if named else ''}
_remote_probe() {{ :; }}
_remote_peer() {{ return 0; }}
t_tools() {{ printf /remote/wk-tools; }}
_peer_why_behind() {{ printf 'stubbed reason'; }}
_rsh_q() {{ case "$*" in *cmd/version*) printf '%s' {shlex.quote(theirs)} ;; *) return 0 ;; esac; }}
t_wk() {{ echo "ASKED: wk $*"; }}
"""
        return bash(". lib/common.sh\n" + stubs + self.T_SYNC + "\nt_sync\n")

    def _mine(self):
        cp = subprocess.run([str(REPO / "cmd" / "version")],
                            cwd=str(REPO), capture_output=True, text=True, timeout=15)
        return cp.stdout.strip()

    def test_a_copy_that_still_differs_is_asked_for_nothing_more(self):
        cp = self._run("0000stale0000", named=True)
        self.assertNotEqual(cp.returncode, 0, "a copy that differs is not a success")
        self.assertIn("still DIFFERS", cp.stderr)
        self.assertNotIn("ASKED:", cp.stdout)

    def test_a_converged_copy_named_by_this_run_publishes_its_own_snapshot(self):
        cp = self._run(self._mine(), named=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("ASKED: wk sync --tools", cp.stdout)

    def test_a_converged_copy_not_named_keeps_its_store_untouched(self):
        # The sweep case (`wk sync --tools` with no target): a snapshot is
        # never published on somebody else's workstation unasked.
        cp = self._run(self._mine(), named=False)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("ASKED:", cp.stdout)
        self.assertIn("wk sync --tools apeer", cp.stderr)


class TestSyncingAPeerReachesItsOwnStore(unittest.TestCase):
    """`wk sync --target <peer>`, end to end over stubs: a peer is a
    workstation with a store of its own, so its mirror, its snapshot and the
    fetch in each of its workspaces all happen over there -- and none of it
    needs `--tools` typed, because naming the machine is what says whose
    furniture to refresh. The defect this pins: a peer's mirror was refreshed
    only when `--tools <peer>` named it, so every workspace there fetched from
    a mirror weeks old and reached for the network instead.

    Both halves are the shipping code -- cmd/sync's sync_furniture and
    sync_target, targets/remote.sh's t_sync -- stubbed at the ssh boundary and
    at load_target, which a real run would let redefine t_sync out from under
    the lift."""

    T_SYNC = _lift_func(REPO / "targets" / "remote.sh", "t_sync")
    SYNC_FURNITURE = _lift_func(REPO / "cmd" / "sync", "sync_furniture")
    SYNC_TARGET = _lift_func(REPO / "cmd" / "sync", "sync_target")

    def _mine(self):
        cp = subprocess.run([str(REPO / "cmd" / "version")],
                            cwd=str(REPO), capture_output=True, text=True, timeout=15)
        return cp.stdout.strip()

    def _run(self):
        stubs = f"""
WK_TARGET=apeer
WK_REMOTE_HOST=apeer
TARGET=apeer
load_target()       {{ WK_TARGET="$1"; WK_REMOTE_PEER=1; }}
scope_targets()     {{ echo apeer; }}
target_workspaces() {{ echo ws-a; echo ws-b; }}
_remote_probe()     {{ :; }}
_remote_peer()      {{ return 0; }}
t_tools()           {{ printf /remote/wk-tools; }}
_peer_why_behind()  {{ printf 'stubbed reason'; }}
_rsh_q()            {{ case "$*" in *cmd/version*) printf '%s' {shlex.quote(self._mine())} ;; *) return 0 ;; esac; }}
t_wk()              {{ echo "OVER-THERE: wk $*"; }}
sync_workspaces()   {{ echo "FROM-HERE: $*"; }}
t_needs_base()      {{ return 1; }}
store_is_local()    {{ return 0; }}
"""
        return bash(". lib/common.sh\n" + stubs + self.T_SYNC + self.SYNC_FURNITURE
                    + self.SYNC_TARGET + "\nsync_furniture\nsync_target apeer\n")

    def test_the_peer_refreshes_its_own_mirror_and_snapshot_and_each_workspace(self):
        cp = self._run()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        asked = [l for l in cp.stdout.splitlines() if l.startswith("OVER-THERE:")]
        self.assertEqual(asked, ["OVER-THERE: wk sync --tools",
                                 "OVER-THERE: wk sync ws-a",
                                 "OVER-THERE: wk sync ws-b"])

    def test_nothing_of_the_peers_is_fetched_from_this_side(self):
        """Its workspaces are containers on it: their checkouts are inside
        them, and nothing here can cd into one."""
        cp = self._run()
        self.assertNotIn("FROM-HERE:", cp.stdout)


class TestDispatcherForwardingRuleForSync(unittest.TestCase):
    """cmd/sync declares `where=dynamic` and answers the dispatcher's
    `wk sync --where <args>` itself: a workspace's name goes to the machine
    holding it (the podman VM for a container workspace on a macOS host), and
    every other shape is this host's -- the mirror is written here, and the
    VM and the guests read it. Driven through the dispatcher's own cmd_where,
    lifted, and through cmd/sync's answer directly; nothing is forwarded."""

    FUNCS = "\n".join(
        _lift_func(REPO / "wk", f)
        for f in ("decl_load", "in_list", "sub_override", "flag_override", "cmd_where")
    )

    def _where(self, *args, env=None):
        script = (
            ". lib/common.sh\n" + self.FUNCS
            + "\ndecl_load cmd/sync\ncmd_where cmd/sync " + " ".join(shlex.quote(a) for a in args) + "\n"
        )
        cp = bash(script, env=env)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_bare_sync_is_this_host(self):
        self.assertEqual(self._where(), "host")

    def test_a_named_workspace_goes_to_the_machine_holding_it(self):
        self.assertEqual(self._where("myws"), "workspace")

    def test_every_scope_flag_is_this_host(self):
        for args in (("--all",), ("--tools",), ("--target", "moose"), ("--tools", "buildbox4"),
                     ("--target=moose",), ("--tools=buildbox4",), ("--machine",),
                     ("--tools", "--all")):
            with self.subTest(args=args):
                self.assertEqual(self._where(*args), "host", args)

    def test_inside_a_workspace_bare_is_that_workspace(self):
        """Inside a workspace a bare `wk sync` is that one fetch, and the
        dispatcher refuses a host command in there -- so the answer is
        `workspace`, and the scope flags stay `host` to be refused."""
        with fake_workspace() as ws:
            self.assertEqual(self._where(env=ws.env()), "workspace")
            self.assertEqual(self._where("--all", env=ws.env()), "host")

    def test_the_declaration_is_dynamic_and_nothing_else_decides(self):
        text = (REPO / "cmd" / "sync").read_text()
        self.assertIn("where=dynamic", text)
        self.assertNotIn("where=host", text.split("set -euo pipefail")[0],
                         "a flag override decides where= beside the --where answer")


# A broker that answers one request and records it: enough for the client in
# container/broker/wk-broker-client.py to speak to, without a real one.
STUB_BROKER = """
import json, os, socket, sys
sock, record = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sock)
s.listen(1)
sys.stderr.write("ready\\n"); sys.stderr.flush()
c, _ = s.accept()
line = b""
while not line.endswith(b"\\n"):
    chunk = c.recv(65536)
    if not chunk:
        break
    line += chunk
open(record, "w").write(line.decode())
c.sendall(json.dumps({"event": "done", "ok": True, "request": "stub"}).encode() + b"\\n")
c.close()
"""


@contextlib.contextmanager
def stub_broker(tmp):
    """A listening socket at a path, and the request it was sent."""
    sock, record = tmp / "broker.sock", tmp / "request.json"
    script = tmp / "stub-broker.py"
    script.write_text(STUB_BROKER)
    p = subprocess.Popen([sys.executable, str(script), str(sock), str(record)],
                         stderr=subprocess.PIPE, text=True)
    p.stderr.readline()                      # it says "ready" once bound
    try:
        yield sock, record
    finally:
        p.kill()
        p.wait()


class TestSyncInsideWorkspace(unittest.TestCase):
    """Inside a workspace `wk sync` means two things: bring this machine's
    mirror up to date, then fetch in this workspace from it. The mirror is
    the machine's and a container mounts it read-only, so the refresh is a
    request over the broker socket (lib/store.sh mirror_refresh_request) --
    and when no broker is listening the fetch still runs, says so, and the
    command exits non-zero. cmd/sync declares no `outside`;
    --target/--all/--tools/--machine answer `host` to the dispatcher's
    `--where` question and are refused before this file even starts, and
    cmd/sync refuses a name that is not this workspace's own the same way."""

    def _refused(self, *args):
        with fake_workspace() as ws:
            cp = ws.run("sync", *args)
        self.assertNotEqual(cp.returncode, 0, f"'wk sync {' '.join(args)}' was accepted inside a workspace")
        self.assertIn("'wk sync", cp.stdout)
        self.assertIn("acts on a host, and this is workspace 'selftest-ws'", cp.stdout)
        self.assertIn(f"From the host:  wk sync {' '.join(args)}", cp.stdout)

    def test_the_scope_flags_are_refused_naming_the_host_invocation(self):
        self._refused("--all")
        self._refused("--tools")
        self._refused("--target", "container")

    def test_a_different_workspaces_name_is_refused_by_the_dispatcher(self):
        # Inside a workspace the name is implicit, so a positional is one too many.
        with fake_workspace() as ws:
            cp = ws.run("sync", "someotherws")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("unexpected argument: someotherws", cp.stdout)

    def test_an_unrecognised_flag_is_refused_as_unknown_not_as_host_only(self):
        # Not one of the scope flags the dispatcher intercepts -- cmd/sync's
        # own parser is what refuses this one, same as outside a workspace.
        with fake_workspace() as ws:
            cp = ws.run("sync", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("unknown option: --bogus", cp.stdout)
        self.assertNotIn("acts on a host", cp.stdout)

    def _bare_repo_with_a_commit(self, tmp_path):
        """A bare repo standing in for the mirror, with one commit on main --
        real git, no network. Returns (path, sha-of-main)."""
        bare = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)],
                        check=True, capture_output=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)],
                        check=True, capture_output=True)
        (seed / "file.txt").write_text("hello\n")
        subprocess.run(["git", "-C", str(seed), "add", "file.txt"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(seed), "-c", "user.email=t@t.example", "-c", "user.name=t",
             "commit", "-q", "-m", "seed"],
            check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", str(seed), "rev-parse", "main"],
                              capture_output=True, text=True, check=True).stdout.strip()
        return bare, sha

    def _wired_checkout(self, ws):
        bare, sha = self._bare_repo_with_a_commit(ws.tmp)
        src = ws.ws_dir / "WebKit"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(src)],
                        check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)],
                        check=True, capture_output=True)
        return src, sha

    def test_bare_sync_asks_the_machine_to_refresh_its_mirror_first(self):
        with fake_workspace() as ws:
            src, sha = self._wired_checkout(ws)
            with stub_broker(ws.tmp) as (sock, record):
                cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(sock)})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertEqual(json.loads(record.read_text()),
                             {"verb": "sync", "args": {}}, record.read_text())
            got = subprocess.run(["git", "-C", str(src), "rev-parse", "refs/remotes/origin/main"],
                                  capture_output=True, text=True)
            self.assertEqual(got.stdout.strip(), sha, got.stderr)

    def test_with_no_broker_the_fetch_still_runs_and_the_miss_is_reported(self):
        with fake_workspace() as ws:
            src, sha = self._wired_checkout(ws)
            cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(ws.tmp / "nothing.sock")})
            self.assertEqual(cp.returncode, 1, cp.stdout)
            self.assertIn("mirror was not", cp.stdout)
            self.assertIn("./setup --stage broker", cp.stdout)
            got = subprocess.run(["git", "-C", str(src), "rev-parse", "refs/remotes/origin/main"],
                                  capture_output=True, text=True)
            self.assertEqual(got.stdout.strip(), sha, got.stderr)

    def test_bare_sync_fetches_in_this_workspaces_own_checkout(self):
        # No mirror mounted on the machine running this test, so the fetch
        # takes sync_workspaces' other branch -- the workspace's own "origin"
        # remote, over what would be the egress-proxy path on a real
        # workspace. The mirror branch is the same function (ws_fetch_script),
        # exercised against a local mirror in tests/test_mirror_path.py.
        with fake_workspace() as ws:
            bare, sha = self._bare_repo_with_a_commit(ws.tmp)
            src = ws.ws_dir / "WebKit"
            subprocess.run(["git", "init", "--quiet", "-b", "main", str(src)],
                            check=True, capture_output=True)
            subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)],
                            check=True, capture_output=True)

            with stub_broker(ws.tmp) as (sock, _):
                cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(sock)})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("selftest-ws", cp.stdout)

            got = subprocess.run(["git", "-C", str(src), "rev-parse", "refs/remotes/origin/main"],
                                  capture_output=True, text=True)
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout.strip(), sha)

    def test_bare_sync_does_not_touch_this_machines_own_store(self):
        # store_init preps *this machine's* mirror/base/ws/cache dirs -- a
        # host concept a workspace has no business touching (and, run for
        # real, would try to create /var/lib/wk on whatever machine runs
        # the test). Guarded by `in_workspace || store_init`; this asserts
        # the guard by checking the fetch still succeeds with no real
        # mirror and no WK_STORE override, which only holds if store_init's
        # ensure_dir calls are not the thing that would have failed first.
        with fake_workspace() as ws:
            bare, _ = self._bare_repo_with_a_commit(ws.tmp)
            src = ws.ws_dir / "WebKit"
            subprocess.run(["git", "init", "--quiet", "-b", "main", str(src)],
                            check=True, capture_output=True)
            subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)],
                            check=True, capture_output=True)
            with stub_broker(ws.tmp) as (sock, _):
                cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(sock)})
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("Permission denied", cp.stdout)


if __name__ == "__main__":
    unittest.main()
