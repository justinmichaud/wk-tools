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


class TestLocalStoreNamed(unittest.TestCase):
    """local_store_named <target> is the pure decision behind which half of
    `wk sync --tools` runs: the tooling copies always, and this machine's own
    mirror and snapshot only when what was named includes them. A target with
    a machine of its own keeps its own mirror -- refreshing it is that
    driver's job (t_sync) -- so `wk sync --tools buildbox4` must not publish a
    snapshot here. Driven through `. cmd/sync functions`, so no store, no
    network and no workspace are touched."""

    def _named(self, target):
        cp = bash(f'. cmd/sync functions\nlocal_store_named "{target}"\n')
        return cp.returncode == 0

    def test_naming_nothing_includes_this_machines_store(self):
        self.assertTrue(self._named(""))

    def test_a_local_kind_includes_it(self):
        # container and vm both keep their workspaces' snapshots in this
        # machine's store (on macOS, in the podman VM it owns).
        self.assertTrue(self._named("container"))
        self.assertTrue(self._named("vm"))
        self.assertTrue(self._named("local"))

    def test_a_machine_of_its_own_does_not(self):
        # Every configured machine in the registry: none of them is this
        # machine's store, so none of them publishes a snapshot here.
        confs = sorted((REPO / "targets" / "hosts").glob("*.conf"))
        self.assertTrue(confs, "no machine confs to check")
        for conf in confs:
            self.assertFalse(self._named(conf.stem), conf.stem)

    def test_a_name_that_is_no_target_does_not(self):
        # Nothing here can be its store either; sync_furniture's load_target
        # is what refuses the name itself, in one place for every command.
        self.assertFalse(self._named("not-a-target"))


class TestSyncScopeDecide(unittest.TestCase):
    """sync_scope_decide <workspace-count> <has-tty 0|1>
    [<pick>] is the pure decision behind cmd/sync's "autodetect, then ask":
    a scope flag is parsed above it and never reaches this function, but a
    bare `wk sync` with no workspace named does, and this is the whole of
    what it decides. The menu it describes is the workspaces here, then two
    more entries: n+1 "all of them here" (--target) and n+2 "this machine's
    tooling, mirror and snapshot" (--tools). Driven the same way as
    snapshot_current -- `. cmd/sync functions` loads it with no network, no
    store, no workspace and no terminal needed."""

    def _decide(self, n, tty, pick=""):
        cp = bash(
            ". cmd/sync functions\n"
            f'sync_scope_decide "{n}" "{tty}" "{pick}"\n'
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout.strip()

    def test_no_workspaces_leaves_only_the_furniture(self):
        # Nothing to fetch in, so cmd/sync takes this to --tools: the mirror
        # and the first snapshot are what a machine with no workspaces needs
        # (`wk new` refuses with "run 'wk sync' first" until one is published).
        self.assertEqual(self._decide(0, 0), "empty")
        self.assertEqual(self._decide(0, 1), "empty")

    def test_exactly_one_workspace_is_unambiguous(self):
        self.assertEqual(self._decide(1, 0), "one")
        self.assertEqual(self._decide(1, 1), "one")

    def test_several_and_no_terminal_refuses(self):
        self.assertEqual(self._decide(3, 0), "refuse")

    def test_several_and_a_terminal_asks_before_a_pick_is_given(self):
        self.assertEqual(self._decide(3, 1), "ask")

    def test_picking_the_entry_after_the_workspaces_means_all_of_them(self):
        # n=3: entries 1-3 are workspaces, entry 4 is "all of them here".
        self.assertEqual(self._decide(3, 1, "4"), "all")

    def test_picking_the_last_entry_means_the_machines_furniture(self):
        # Entry n+2, the one that reaches the tooling, the mirror and the
        # snapshot -- the only route to them from a bare `wk sync`, and so
        # the only route at all on a macOS host, where a bare `wk sync` is
        # what the dispatcher forwards into the podman VM.
        self.assertEqual(self._decide(3, 1, "5"), "tools")

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
        self.assertEqual(self._decide(3, 1, "6"), "out-of-range")


class TestSyncArgParsing(unittest.TestCase):
    """The argument-parsing block at the top of cmd/sync: what a workspace
    name, --target, --all and --tools resolve to, and what the --machine
    tombstone and an unknown flag refuse with. Not a function of its own
    (sync_scope_decide, above, is the part that is), so lifted by line range
    rather than by name. Every case here dies (or finishes parsing) before
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
        self.assertEqual(self._parsed("--tools=buildbox4"), "SCOPE=tools ONLY= TARGET=buildbox4")

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

    def test_two_names_is_refused(self):
        cp = self._parse("a", "b")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("one workspace at a time (got 'a' and 'b')", cp.stderr)

    def test_unknown_flag_is_refused_with_the_usage(self):
        cp = self._parse("--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("unknown option: --bogus", cp.stderr)
        self.assertIn("--tools", cp.stderr)

    def test_an_empty_equals_value_is_refused_not_read_as_every_target(self):
        # `--tools` alone means every target; `--tools=` has said there is
        # one, so an empty one is a mistake rather than a second spelling of
        # "all of them".
        cp = self._parse("--tools=")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("--tools= names one target", cp.stderr)

    def test_a_second_target_is_refused_rather_than_overwriting_the_first(self):
        for pair in (("--target", "moose", "--target", "buildbox4"),
                     ("--tools=moose", "--tools=buildbox4")):
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

    def test_a_target_whose_records_are_not_readable_here_says_so(self):
        # The container store on macOS: it is inside the podman VM, so every
        # workspace would read as "creating" from a base-id not visible here.
        stubs = """
load_target() { WK_REMOTE_PEER=""; }
t_needs_base() { return 0; }
store_is_local() { return 1; }
target_workspaces() { echo ws-a; }
sync_workspaces() { echo "FETCH: $*"; }
"""
        cp = self._run(self.SYNC_TARGET, stubs, "sync_target container")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("FETCH:", cp.stdout)
        self.assertIn("podman VM", cp.stderr)

    FURNITURE_STUBS = """
load_target() { :; }
walk_targets() { echo container; echo vm; echo buildbox4; }
t_sync() { echo "furniture: $_t named=${WK_SYNC_NAMED:-no}"; }
"""

    def test_tools_with_no_target_visits_every_one(self):
        cp = self._run(self.SYNC_FURNITURE, self.FURNITURE_STUBS, 'sync_furniture ""')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(
            [l for l in cp.stdout.splitlines() if l.startswith("furniture:")],
            ["furniture: container named=no", "furniture: vm named=no",
             "furniture: buildbox4 named=no"])

    def test_tools_with_a_target_visits_only_that_one_and_names_it(self):
        # WK_SYNC_NAMED is the difference a peer reads: a snapshot is
        # published on somebody else's workstation only when it was named,
        # never as part of a sweep (targets/remote.sh, t_sync).
        cp = self._run(self.SYNC_FURNITURE, self.FURNITURE_STUBS, "sync_furniture buildbox4")
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
        cp = subprocess.run([str(REPO / "cmd" / "version"), "--tree"],
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


class TestDispatcherForwardingRuleForSync(unittest.TestCase):
    """The dispatcher's (`wk`) forwarding rule, driven directly against
    cmd/sync's own declaration (`# wk: flag --target,--all,--tools,--machine
    where=host`, cmd/sync:5): whether a container workspace on a macOS host gets `wk
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

    def test_every_scope_flag_is_where_host(self):
        # cmd/sync:5's flag override -- this is what makes the dispatcher skip
        # the VM-forwarding block entirely for a scope flag, regardless of
        # what resolve_target would say. --machine is declared with them so
        # its tombstone refuses here rather than inside the podman VM.
        for flag in ("--all", "--tools", "--target", "--machine"):
            self.assertEqual(self._where(flag), "host", flag)

    def test_bare_sync_resolves_to_container_the_forwarding_default(self):
        # No name and no --target means resolve_target's fallback
        # (wk:352-353) applies: container. Combined with where=workspace
        # above, this is the case that actually gets forwarded into the VM.
        self.assertEqual(self._target(), "container")

    def test_all_still_resolves_to_container_but_where_makes_it_moot(self):
        # resolve_target on its own does not know about --all -- it just sees
        # no name (a flag is not a positional) and defaults to container. It
        # is cmd_where's "host" (tested above) that actually stops this from
        # forwarding: the dispatcher never reaches its resolve_target check
        # at all once `where` is not `workspace`.
        self.assertEqual(self._target("--all"), "container")

    def test_the_equals_spelling_is_a_flag_too(self):
        # `--target=moose` is the same flag as `--target moose`, and the
        # dispatcher has to see it as one: unrecognised, `where` stays at its
        # default and a host command is sent to the podman VM, which can see
        # none of the fleet.
        self.assertEqual(self._where("--target=moose"), "host")
        self.assertEqual(self._where("--tools=buildbox4"), "host")
        self.assertEqual(self._target("--target=moose"), "moose")

    def test_a_named_target_is_what_resolve_target_reports(self):
        # `--target <t>` is the dispatcher's own spelling for "which target"
        # (resolve_target, wk:379-388), shared with `wk new --target`: so
        # `wk sync --target moose` is never mistaken for a container command.
        self.assertEqual(self._target("--target", "moose"), "moose")

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

    def test_every_scope_flag_is_refused_the_same_way(self):
        self._refused("--all")
        self._refused("--tools")
        self._refused("--target", "container")

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
