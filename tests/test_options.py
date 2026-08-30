"""Option consistency across cmd/*: --quiet (dispatcher-level, once), --json
on `wk ls`/`wk find`, and every command in this audit's file set refusing an
unknown flag instead of silently accepting it.

Run: python3 -m unittest tests.test_options -v
"""
import json
import os
import platform
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, run, temp_store


def _clean(env):
    full_env = dict(os.environ)
    full_env.pop("WK_MARKER", None)
    full_env.pop("XDG_STATE_HOME", None)
    full_env.pop("WK_STORE", None)
    if env:
        full_env.update(env)
    return full_env


def run_impl(name, *args, env=None, timeout=30, split=False):
    """A cmd/<name> file directly, bypassing the dispatcher -- for a flag
    check that runs before the dispatcher's own workspace-name resolution
    would otherwise get in the way. Streams merged by default, like
    tests.support.run (most of these commands' reporting goes to stderr);
    split=True keeps them apart, for a --json command whose stdout contract
    is nothing else."""
    if split:
        return subprocess.run(
            [str(REPO / "cmd" / name), *args],
            cwd=str(REPO), env=_clean(env),
            capture_output=True, text=True, timeout=timeout,
        )
    cp = subprocess.run(
        [str(REPO / "cmd" / name), *args],
        cwd=str(REPO), env=_clean(env),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout,
    )
    cp.stderr = ""
    return cp


def run_wk_split(*args, env=None, timeout=30):
    """`./wk <args>`, stdout and stderr kept apart -- unlike tests.support.run
    (which merges them, correctly, for commands whose reporting is all on
    stderr), a --json command's whole contract is that stdout carries
    nothing else, and merging would hide a narration line that leaked in."""
    return subprocess.run(
        [str(REPO / "wk"), *args],
        cwd=str(REPO), env=_clean(env),
        capture_output=True, text=True, timeout=timeout,
    )


class TestQuietFlag(WkTest):
    def test_dispatcher_strips_quiet_before_the_command_sees_it(self):
        """`wk version --quiet` still prints its result"""
        # cmd/version's own case statement dies on any argument other than
        # --tree or none -- so this only succeeds if the dispatcher removed
        # --quiet before exec'ing cmd/version, the same way it removes --force.
        cp = run("version", "--quiet")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("sha=", cp.stdout)
        self.assertIn("root=", cp.stdout)

    def test_quiet_suppresses_info_and_log_but_not_warn(self):
        """WK_QUIET drops log()/info(); warn() still prints"""
        cp = self.bash(
            '. lib/common.sh\n'
            'info "an info line"\n'
            'log "a log line"\n'
            'warn "a warning"\n',
            env={"WK_QUIET": "1"},
        )
        self.assertNotIn("an info line", cp.stderr)
        self.assertNotIn("a log line", cp.stderr)
        self.assertIn("a warning", cp.stderr)

    def test_without_quiet_info_and_log_print(self):
        """the same script without WK_QUIET prints all three"""
        cp = self.bash(
            '. lib/common.sh\n'
            'info "an info line"\n'
            'log "a log line"\n'
            'warn "a warning"\n'
        )
        self.assertIn("an info line", cp.stderr)
        self.assertIn("a log line", cp.stderr)
        self.assertIn("a warning", cp.stderr)

    def test_find_with_quiet_still_exits_zero_with_no_narration(self):
        """a read-only command run with --quiet: no info lines, still its result"""
        # Offline and deterministic: --segment/--no-ssh/--timeout need no
        # network path (a documentation-only range) and no ssh.
        cp = run("find", "--segment", "203.0.113.0/31", "--timeout", "2",
                  "--no-ssh", "--quiet")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertNotIn("==>", cp.stdout)


class TestSudoQuietGoesThroughEnv(WkTest):
    def test_doctor_calls_sudo_directly_with_env_not_a_literal_flag(self):
        """cmd/doctor no longer passes a literal --quiet to cmd/sudo"""
        text = (REPO / "cmd" / "doctor").read_text()
        self.assertNotIn('"$WK_ROOT/cmd/sudo" status --quiet', text)
        self.assertIn("WK_QUIET=1", text)

    def test_sudo_no_longer_parses_a_local_quiet_flag(self):
        """cmd/sudo reads WK_QUIET, not its own --quiet case arm"""
        text = (REPO / "cmd" / "sudo").read_text()
        self.assertNotIn("--quiet)  QUIET=1", text)
        self.assertIn("WK_QUIET", text)


class TestLsJson(WkTest):
    def test_ls_json_is_one_valid_document(self):
        """`wk ls --json` (run directly, WK_TARGET=container) is one valid
        JSON document shaped {"workspaces": [...]} -- the container target
        answers from the real podman state on this machine, not from the
        scratch WK_STORE, so the row count itself is not asserted here."""
        with temp_store() as store:
            cp = run_impl("ls", "--json", split=True,
                           env={"WK_STORE": store["WK_STORE"], "WK_TARGET": "container"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        doc = json.loads(cp.stdout)
        self.assertIsInstance(doc, dict)
        self.assertIsInstance(doc["workspaces"], list)
        for row in doc["workspaces"]:
            self.assertEqual(
                set(row.keys()),
                {"name", "target", "state", "base", "snap", "arch", "changes"},
            )

    def test_ls_json_nothing_else_on_stdout(self):
        """the header row and hints do not leak into --json output"""
        with temp_store() as store:
            cp = run_impl("ls", "--json", split=True,
                           env={"WK_STORE": store["WK_STORE"], "WK_TARGET": "container"})
        self.assertNotIn("NAME", cp.stdout)
        self.assertEqual(len(cp.stdout.strip().splitlines()), 1, cp.stdout)

    def test_ls_refuses_an_unknown_flag(self):
        """`wk ls --bogus` is refused, not silently accepted"""
        cp = run("ls", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_json_merge_list_merges_concatenated_documents(self):
        """json_merge_list (lib/common.sh) merges N files, each zero or more
        JSON documents concatenated with no delimiter -- the shape a
        multi-process listing (or a missing/empty file) produces."""
        with temp_store() as store:
            d = Path(store["WK_STORE"])
            (d / "a.json").write_text('{"workspaces": [{"name": "a"}]}\n')
            (d / "b.json").write_text(
                '{"workspaces": [{"name": "b"}]}{"workspaces": [{"name": "c"}]}\n'
            )
            (d / "empty.json").write_text("")
            cp = self.bash(
                f'. lib/common.sh\n'
                f'json_merge_list workspaces "{d}/a.json" "{d}/b.json" '
                f'"{d}/empty.json" "{d}/does-not-exist.json"\n'
            )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        doc = json.loads(cp.stdout)
        names = sorted(w["name"] for w in doc["workspaces"])
        self.assertEqual(names, ["a", "b", "c"])


class TestFindJson(WkTest):
    def test_find_json_is_one_valid_document(self):
        """`wk find --json` against an offline, empty sweep is valid JSON"""
        cp = run_wk_split("find", "--segment", "203.0.113.0/31", "--timeout", "2",
                           "--no-ssh", "--json")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        doc = json.loads(cp.stdout)
        for key in ("want", "want_mac", "seen", "swept", "blind", "vantages", "hits"):
            self.assertIn(key, doc)
        self.assertEqual(doc["seen"], 0)
        self.assertEqual(doc["swept"], 1)
        self.assertEqual(doc["hits"], [])

    def test_find_json_nothing_else_on_stdout(self):
        cp = run_wk_split("find", "--segment", "203.0.113.0/31", "--timeout", "2",
                           "--no-ssh", "--json")
        self.assertEqual(len(cp.stdout.strip().splitlines()), 1, cp.stdout)

    def test_find_refuses_an_unknown_flag(self):
        cp = run("find", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)


class TestUnknownFlagRefused(WkTest):
    """A parameter this command does not recognise is refused, not silently
    ignored (docs/defects, "a parameter silently ignored instead of
    refused"). Every case here needs no real workspace or machine."""

    def test_sudo(self):
        cp = run("sudo", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_push(self):
        cp = run("push", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_sync(self):
        cp = run("sync", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_doctor(self):
        cp = run("doctor", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_remote_extra_positional(self):
        """`wk remote setup <target> <extra>` -- an argument past <target>"""
        cp = run("remote", "setup", "some-bogus-host", "extra-garbage")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_verify_unknown_flag(self):
        """cmd/verify's own flag check runs before any store/target lookup,
        so this needs no real workspace -- just a name past require_name."""
        cp = run_impl("verify", "--bogus", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_logs_unknown_flag(self):
        """cmd/logs checks the flag only after finding a build.log, so this
        one needs a scratch store with a workspace directory in it."""
        with temp_store() as store:
            d = Path(store["WK_STORE"])
            (d / "ws" / "fakews").mkdir(parents=True)
            (d / "ws" / "fakews" / "build.log").write_text("fake\n")
            cp = run_impl("logs", "--bogus",
                           env={"WK_STORE": store["WK_STORE"], "WK_TARGET": "container",
                                "WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_build_list_extra_positional_refused(self):
        """`wk build --list <extra>` previously exited 0, silently ignoring
        anything typed after --list."""
        cp = run_impl("build", "--list", "extra-garbage")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_disk_probe_store_extra_positional_refused(self):
        """cmd/disk's internal --probe-store branch (used when piping this
        file into the podman VM's bash) previously ignored a trailing
        argument."""
        cp = run_impl("disk", "--probe-store", "extra-garbage")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_build_passthrough_is_documented_not_a_silent_ignore(self):
        """cmd/build forwards an unrecognised flag to build-webkit, on
        purpose -- confirm the file still says so, since that is what makes
        the passthrough a decision rather than an oversight."""
        text = (REPO / "cmd" / "build").read_text()
        self.assertIn("everything left passes\n# through to the build untouched", text)


class TestSecondHalfUnknownFlagRefused(WkTest):
    """The second half of the option-consistency audit (docs/defects):
    status, bench, pr, run, gc, pick, session, quiesce, test, enter, zed,
    gui, profile, claude. Each refuses an unknown flag with 'usage:', or --
    run, enter, claude -- documents a deliberate passthrough to what it runs
    instead (confirmed by a doc-text test, not a refusal one)."""

    def test_status_unknown_flag(self):
        cp = run_impl("status", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_status_extra_positional_refused(self):
        """a second bare word is refused, not silently dropped -- both used
        to land in $_args with only the first ever read back out."""
        cp = run_impl("status", "ws1", "ws2")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_bench_unknown_flag(self):
        cp = run_impl("bench", "someplan", "--bogus", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_bench_report_extra_positional_refused(self):
        """a fourth bare argument to 'wk bench report' is refused; the third
        is `wk bench compare`'s <ws> and stays accepted-and-ignored (see the
        header comment on cmd/bench's 'compare' line)."""
        cp = run_impl("bench", "report", "a", "b", "ws", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_bench_seed_extra_positional_refused(self):
        cp = run_impl("bench", "seed", "somews", "someplan", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_bench_count_documented_as_iterations_per_run(self):
        text = (REPO / "cmd" / "bench").read_text()
        self.assertIn("iterations per run", text)

    def test_run_passthrough_is_documented(self):
        """cmd/run hands an unrecognised token to jsc on purpose -- confirm
        the file still says so."""
        text = (REPO / "cmd" / "run").read_text()
        self.assertIn("Everything after `--` goes to jsc verbatim.", text)

    def test_pr_open_unknown_flag(self):
        """'wk pr open' is the sub-verb with its own flag loop."""
        cp = run_impl("pr", "open", "somews", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pr_extra_positional_refused(self):
        cp = run_impl("pr", "someuser:somebranch", "extra", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_pr_rebase_extra_positional_refused(self):
        cp = run_impl("pr", "rebase", "fakews", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_gc_unknown_flag(self):
        with temp_store() as store:
            cp = run_impl("gc", "--bogus", env={
                "WK_STORE": store["WK_STORE"], "WK_TARGET": "container",
                "XDG_STATE_HOME": store["WK_STORE"],
            })
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_gc_second_flag_no_longer_dropped(self):
        """previously the parser only ever looked at $1: a second flag (a
        typo next to a real one) was silently ignored instead of applied or
        refused. It is a real loop over argv now."""
        with temp_store() as store:
            cp = run_impl("gc", "--refresh-net", "--bogus", env={
                "WK_STORE": store["WK_STORE"], "WK_TARGET": "container",
                "XDG_STATE_HOME": store["WK_STORE"],
            })
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pick_requires_at_least_one_commit(self):
        """wk pick's grammar is variadic commit specs with no flags to typo;
        the equivalent refusal is a missing commit argument."""
        cp = run_impl("pick", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Linux", "wk session is Linux-only")
    def test_session_unknown_flag(self):
        cp = run_impl("session", "on", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_quiesce_unknown_verb(self):
        cp = run_impl("quiesce", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_quiesce_extra_positional_refused(self):
        """previously only $1 was ever read as the action; a second word
        (e.g. a stray flag after 'on') was silently dropped."""
        cp = run_impl("quiesce", "on", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_test_unknown_flag(self):
        cp = run_impl("test", "--bogus", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_enter_passthrough_is_documented(self):
        text = (REPO / "cmd" / "enter").read_text()
        self.assertIn("run one command there and exit", text)

    def test_zed_unknown_flag(self):
        cp = run_impl("zed", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_gui_unknown_flag(self):
        cp = run_impl("gui", "--bogus", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_gui_extra_url_refused(self):
        cp = run_impl("gui", "url1", "url2", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_profile_unknown_flag(self):
        cp = run_impl("profile", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_claude_passthrough_is_documented(self):
        text = (REPO / "cmd" / "claude").read_text()
        self.assertIn("everything after it is Claude's, verbatim", text)


class TestThirdHalfUnknownFlagRefused(WkTest):
    """The remaining commands from the unknown-arg audit (docs/defects):
    rm, vm, backup (read-only, not fixed -- see docs/defects)."""

    def test_rm_unknown_flag_refused(self):
        """a token shaped like a flag is not silently treated as a second
        workspace name to destroy; require_name refuses it before the
        confirmation prompt."""
        cp = run_impl("rm", "--bogus-flag-xyz", env={"WK_NAME": "fakews"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "wk vm is macOS-only")
    def test_vm_unknown_subverb_refused(self):
        cp = run_impl("vm", "bogus-sub")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "wk vm is macOS-only")
    def test_vm_new_extra_positional_refused(self):
        cp = run_impl("vm", "new", "somename", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "wk vm is macOS-only")
    def test_vm_ls_extra_positional_refused(self):
        cp = run_impl("vm", "ls", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "wk vm is macOS-only")
    def test_vm_base_extra_positional_refused(self):
        cp = run_impl("vm", "base", "--refresh", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    @unittest.skipUnless(platform.system() == "Linux", "wk session is Linux-only")
    def test_session_gdm_unknown_flag(self):
        cp = run_impl("session", "gdm", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_session_mirror_alias_is_documented(self):
        """--mirror (cmd/session's --bmc synonym) is named in the header,
        not just the case arm -- docs/defects 'list every valid value'."""
        text = (REPO / "cmd" / "session").read_text()
        self.assertIn("--mirror is an accepted synonym for --bmc", text)


class TestPiPickSessionOptions(WkTest):
    """The third half of the option-consistency audit (docs/defects): pi,
    pick, session. Each case here parses (and refuses) before touching a
    machine or network, so it needs no hardware."""

    def test_pi_setup_unknown_flag_refused(self):
        """`wk pi setup <host>` takes no flags at all; previously anything
        past the host was silently dropped rather than refused."""
        cp = run_impl("pi", "setup", "somehost", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pi_setup_extra_positional_refused(self):
        cp = run_impl("pi", "setup", "somehost", "extra-garbage")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_pi_deploy_unknown_flag_refused(self):
        cp = run_impl("pi", "deploy", "somews", "somemachine", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pi_deploy_extra_positional_refused(self):
        cp = run_impl("pi", "deploy", "somews", "somemachine", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_pi_bench_unknown_flag_refused(self):
        cp = run_impl("pi", "bench", "somemachine", "someplan", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pi_bench_extra_positional_refused(self):
        cp = run_impl("pi", "bench", "somemachine", "someplan", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_pi_bench_count_documented_as_iterations_per_run(self):
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn("iterations per run", text)

    def test_pi_boot_order_unknown_flag_refused(self):
        cp = run_impl("pi", "boot-order", "somehost", "local", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

    def test_pi_boot_order_extra_positional_refused(self):
        cp = run_impl("pi", "boot-order", "somehost", "local", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)
        self.assertIn("unexpected argument", cp.stdout)

    def test_pi_flash_tombstone_names_sysimage_not_an_image_store(self):
        """there is no image store (a built image stays in the workspace
        that built it); the tombstone used to point at one that doesn't
        exist."""
        text = (REPO / "cmd" / "pi").read_text()
        self.assertIn("it lives with wk sysimage", text)
        self.assertNotIn("it lives with the image store", text)

    def test_pick_help_prints_synopsis(self):
        """bare `wk pick -h`-shaped help (run with no args and no name) is
        the dispatcher's job; this confirms cmd/pick's own header still
        carries the synopsis the dispatcher's -h reads out of it."""
        text = (REPO / "cmd" / "pick").read_text()
        self.assertIn("wk pick [<workspace>] <commit>...", text)

    @unittest.skipUnless(platform.system() == "Linux", "wk session is Linux-only")
    def test_session_extra_positional_refused(self):
        """`wk session status extra` -- a stray word past the action is
        refused by the same loop that refuses an unknown flag, since neither
        matches the one recognised token (--bmc/--mirror)."""
        cp = run_impl("session", "status", "extra")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("usage:", cp.stdout)

if __name__ == "__main__":
    unittest.main()
