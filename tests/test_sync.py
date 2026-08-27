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

import shlex
import subprocess
import unittest

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


class TestSyncScopeDecide(unittest.TestCase):
    """sync_scope_decide <workspace-count> <has-tty 0|1>
    [<pick>] is the pure decision behind cmd/sync's "autodetect, then ask":
    bare / --machine / --all is parsed above it and --machine/--all never
    reach this function, but a bare `wk sync` with no workspace named does,
    and this is the whole of what it decides. Driven the same way as
    snapshot_current -- `. cmd/sync functions` loads it with no network, no
    store, no workspace and no terminal needed."""

    def _decide(self, n, tty, pick=""):
        cp = bash(
            ". cmd/sync functions\n"
            f'sync_scope_decide "{n}" "{tty}" "{pick}"\n'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_no_workspaces_syncs_the_machine(self):
        self.assertEqual(self._decide(0, 0), "empty")
        self.assertEqual(self._decide(0, 1), "empty")

    def test_exactly_one_workspace_is_unambiguous(self):
        self.assertEqual(self._decide(1, 0), "one")
        self.assertEqual(self._decide(1, 1), "one")

    def test_several_and_no_terminal_refuses(self):
        self.assertEqual(self._decide(3, 0), "refuse")

    def test_several_and_a_terminal_asks_before_a_pick_is_given(self):
        self.assertEqual(self._decide(3, 1), "ask")

    def test_picking_the_last_entry_means_all_of_them(self):
        # n=3: entries 1-3 are workspaces, entry 4 ("all of them ... --machine").
        self.assertEqual(self._decide(3, 1, "4"), "all")

    def test_picking_a_workspace_number(self):
        self.assertEqual(self._decide(3, 1, "2"), "pick 2")
        self.assertEqual(self._decide(3, 1, "1"), "pick 1")
        self.assertEqual(self._decide(3, 1, "3"), "pick 3")

    def test_picking_nothing_or_non_numeric_is_invalid(self):
        self.assertEqual(self._decide(3, 1, ""), "ask")  # no pick at all
        self.assertEqual(self._decide(3, 1, "x"), "invalid")
        self.assertEqual(self._decide(3, 1, "2.5"), "invalid")

    def test_picking_a_number_outside_the_menu_is_out_of_range(self):
        self.assertEqual(self._decide(3, 1, "0"), "out-of-range")
        self.assertEqual(self._decide(3, 1, "5"), "out-of-range")


class TestSyncArgParsing(unittest.TestCase):
    """The argument-parsing block at the top of cmd/sync (cmd/sync:104-134):
    what --machine/--all/a workspace name resolve to, and what the old
    --tools/--target spellings and an unknown flag refuse with. Not a
    function of its own (sync_scope_decide, above, is the part that is), so
    lifted by line range rather than by name. Every case here dies (or
    finishes parsing) before store_init -- lib/common.sh is the only other
    thing sourced, and it is pure at source time (tests/test_prompts.py
    relies on the same fact) -- so nothing here touches the network, the
    store or a workspace."""

    ARGPARSE = _lift_range(REPO / "cmd" / "sync", r'^SCOPE=""$', r"ask for different things")

    def _parse(self, *args):
        set_line = "set -- " + " ".join(shlex.quote(a) for a in args) + "\n" if args else "set --\n"
        script = (
            ". lib/common.sh\n" + set_line + self.ARGPARSE
            + "\nprintf 'SCOPE=%s ONLY=%s\\n' \"$SCOPE\" \"$ONLY\"\n"
        )
        return bash(script)

    def test_bare_is_no_scope_and_no_name(self):
        cp = self._parse()
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "SCOPE= ONLY=")

    def test_machine_sets_scope_machine(self):
        cp = self._parse("--machine")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "SCOPE=machine ONLY=")

    def test_all_sets_scope_all(self):
        cp = self._parse("--all")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "SCOPE=all ONLY=")

    def test_machine_and_all_together_is_last_flag_wins_not_a_refusal(self):
        # Not documented as either "mutually exclusive" or "composed": the
        # parsing loop (cmd/sync:107-131) has no conflict check between
        # --machine and --all, only between a scope and a workspace name
        # (cmd/sync:133-134) -- so combining the two scope flags silently
        # takes whichever was seen last, in either order. Recorded here as
        # the actual current behaviour, not a claim that it is the right one.
        self.assertEqual(self._parse("--machine", "--all").stdout.strip(), "SCOPE=all ONLY=")
        self.assertEqual(self._parse("--all", "--machine").stdout.strip(), "SCOPE=machine ONLY=")

    def test_a_workspace_name_sets_only(self):
        cp = self._parse("myws")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "SCOPE= ONLY=myws")

    def test_a_name_and_a_scope_together_is_refused(self):
        cp = self._parse("myws", "--machine")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'myws' and --machine ask for different things", cp.stderr)

    def test_two_names_is_refused(self):
        cp = self._parse("a", "b")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("one workspace at a time (got 'a' and 'b')", cp.stderr)

    def test_unknown_flag_is_refused(self):
        cp = self._parse("--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("unknown option: --bogus", cp.stderr)

    def test_old_tools_spelling_is_refused_naming_the_replacement(self):
        cp = self._parse("--tools")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("is now part of a scope, not a flag of its own", cp.stderr)
        self.assertIn("wk sync --machine", cp.stderr)
        self.assertIn("wk sync --all", cp.stderr)

    def test_old_target_spelling_is_refused_naming_the_scope_flags(self):
        cp = self._parse("--target", "foo")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'wk sync --target foo' is gone: the scope is how far", cp.stderr)
        self.assertIn("wk sync --machine", cp.stderr)
        self.assertIn("wk sync --all", cp.stderr)


class TestDispatcherForwardingRuleForSync(unittest.TestCase):
    """The dispatcher's (`wk`) forwarding rule, driven directly against
    cmd/sync's own declaration (`# wk: flag --machine,--all where=host`,
    cmd/sync:5): whether a container workspace on a macOS host gets `wk
    sync ...` forwarded whole into the podman VM turns on two things in
    `wk` -- cmd_where() (wk:164-171, using the flag/sub overrides
    decl_load loaded) and resolve_target() (wk:344-354) -- combined by
    `[ "$where" = workspace ] || exec "$impl" "$@"` (wk:707, which skips
    the whole VM-forwarding block below it whenever `where` is not
    `workspace`) and, inside that block, `[ "$(resolve_target "$@")" =
    container ]` (wk:721-722). Lifted rather than run for real: this
    machine is macOS (Platform: darwin), so an actual forwarding
    `./wk sync ...` would try to start the podman VM -- a machine mutation
    this suite must not cause."""

    FUNCS = "\n".join(
        _lift_func(REPO / "wk", f)
        for f in ("decl_load", "in_list", "sub_override", "flag_override", "cmd_where", "resolve_target")
    )

    def _where(self, *args):
        script = (
            ". lib/common.sh\n" + self.FUNCS
            + "\ndecl_load cmd/sync\ncmd_where cmd/sync " + " ".join(shlex.quote(a) for a in args) + "\n"
        )
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def _target(self, *args, ws_target_returns=None):
        # ws_target (lib/store.sh) is only called when resolve_target finds
        # a name; stubbed here so this stays a pure decision-logic test with
        # no real workspace registry.
        stub = ""
        if ws_target_returns is not None:
            stub = f'ws_target() {{ printf %s {shlex.quote(ws_target_returns)}; }}\n'
        script = (
            ". lib/common.sh\n" + stub + self.FUNCS
            + "\nresolve_target " + " ".join(shlex.quote(a) for a in args) + "\n"
        )
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_bare_sync_is_where_workspace(self):
        # No name, no scope flag: the default declaration (cmd/sync:4) applies.
        self.assertEqual(self._where(), "workspace")

    def test_a_named_workspace_is_where_workspace(self):
        self.assertEqual(self._where("myws"), "workspace")

    def test_machine_flag_is_where_host(self):
        # cmd/sync:5's flag override -- this is what makes wk:707 skip the
        # VM-forwarding block entirely for --machine, regardless of what
        # resolve_target would say.
        self.assertEqual(self._where("--machine"), "host")

    def test_all_flag_is_where_host(self):
        self.assertEqual(self._where("--all"), "host")

    def test_bare_sync_resolves_to_container_the_forwarding_default(self):
        # No name and no --target means resolve_target's fallback
        # (wk:352-353) applies: container. Combined with where=workspace
        # above, this is the case that actually gets forwarded into the VM.
        self.assertEqual(self._target(), "container")

    def test_machine_flag_still_resolves_to_container_but_where_makes_it_moot(self):
        # resolve_target on its own does not know about --machine/--all --
        # it just sees no name (a flag is not a positional) and defaults to
        # container. It is cmd_where's "host" (tested above) that actually
        # stops this from forwarding: wk:707 never reaches wk:721's
        # resolve_target check at all once `where` is not `workspace`.
        self.assertEqual(self._target("--machine"), "container")

    def test_a_named_container_workspace_resolves_to_container(self):
        self.assertEqual(self._target("myws", ws_target_returns="container"), "container")

    def test_a_named_vm_workspace_does_not_resolve_to_container(self):
        # A workspace-scoped sync only forwards into the VM when the named
        # workspace's own target actually is container; a vm/remote
        # workspace's sync runs on the host instead (cmd/sync drives it
        # over t_exec from out here).
        self.assertEqual(self._target("myws", ws_target_returns="vm"), "vm")


class TestSyncRefusedInsideWorkspace(unittest.TestCase):
    """`wk sync` is refused unconditionally from inside a workspace: cmd/sync
    declares `outside` (cmd/sync:4), and the dispatcher's own refusal
    (`in_workspace && { ... || -n "$D_OUTSIDE"; }`, wk ~678-687) fires
    before cmd/sync's argument parsing ever runs -- so this holds the same
    way for a bare `wk sync`, every scope flag, and a named workspace.
    Real `./wk sync` invocations (not lifted) are safe here: every case
    below dies in the dispatcher itself, before store_init, the mirror, or
    any workspace is touched -- verified by hand (see the module-level
    note) before this class was written."""

    def _refused(self, *args):
        with fake_workspace() as ws:
            cp = ws.run("sync", *args)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'wk sync' acts on a host, and this is workspace 'selftest-ws'", cp.stdout)
        self.assertIn("From the host:  wk sync selftest-ws", cp.stdout)

    def test_bare_sync_is_refused(self):
        self._refused()

    def test_machine_flag_is_refused_the_same_way(self):
        self._refused("--machine")

    def test_all_flag_is_refused_the_same_way(self):
        self._refused("--all")

    def test_a_named_workspace_is_refused_the_same_way(self):
        self._refused("someotherws")

    def test_an_unknown_flag_is_refused_by_the_dispatcher_first(self):
        # cmd/sync would itself refuse --bogus (TestSyncArgParsing above),
        # but the dispatcher's in-workspace refusal fires first -- cmd/sync
        # never even starts, so it is the host-command message that shows,
        # not "unknown option".
        self._refused("--bogus")


if __name__ == "__main__":
    unittest.main()
