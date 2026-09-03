"""Every command's `--help` prints the actual command line it would run and
the configurations it accepts -- owed by docs/HANDOFF-wk-cli.md: "every
command's `--help` prints the actual command line it would run and the
configurations it accepts [needs a test]".

`wk <cmd> -h` is produced by `wk`'s own `explain_cmd`: a `preview:` line
appears only when `grep -q -- '--dry-run' "$impl"` finds the flag in the
command's own file, and a `valid values:` section appears only when the
command's `# wk:` line declares `values=<flag>` -- the dispatcher then runs
`wk <cmd> <flag>` itself, so the list can never fall out of step with the
code. Both mechanisms are read here exactly as `wk` reads them (real `wk
<cmd> -h` invocations, not a retyped copy of the decision).

`wk build -h` is the one place both halves of the claim are true together:
`--dry-run` prints the exact command line (prefixed `running:`, see
tests/test_build.py), and `values=--list` prints every build config by name.

Every other command that also takes a build `--config <config>` (bench, gui,
profile, run, test) reuses the *word* "config" but not the mechanism:
`--config`'s own help text just points at `wk build --list` in prose, and
two of them (gui, run) have no `--dry-run` at all. `bench` and `profile` do
declare their own `values=--list`, but for something else entirely --
benchmark plans and profiler modes -- not for the build configs their own
`--config` flag accepts, so their "valid values" sections exist but never
name a build config.

Run: python3 -m unittest tests.test_owed_cli_help -v
"""
import unittest

from tests.support import WkTest, run


def _help_text(cmd):
    cp = run(cmd, "-h", timeout=30)
    return cp.stdout + cp.stderr


def _values_section(text):
    """Only the text of the `valid values (wk <cmd> <flag>):` block itself
    (to end of output) -- so a config name mentioned in passing prose
    elsewhere in the header (bench's own body names `jsc-release` as an
    example runner) cannot be mistaken for that command's own closed set."""
    marker = "valid values"
    idx = text.find(marker)
    return text[idx:] if idx != -1 else ""


class TestBuildDocumentsBothThePreviewAndTheConfigs(WkTest):
    """The one command where the handoff's claim is true today."""

    def test_build_h_shows_a_dry_run_preview_and_lists_every_config(self):
        text = _help_text("build")
        self.assertIn("preview:", text, text)
        self.assertIn("valid values", text, text)
        for config in ("jsc-release", "gtk-release", "mac-release"):
            self.assertIn(config, text, text)


class TestOtherConfigCommandsDoNotFullyMeetTheClaim(WkTest):
    """bench/gui/profile/run/test all take a build `--config`, but none of
    them reuse build's `values=`/`--dry-run` mechanism for it -- each
    `expectedFailure` below names exactly what its own `-h` is missing."""

    @unittest.expectedFailure
    def test_run_has_neither_a_dry_run_preview_nor_a_config_list(self):
        """defect: cmd/run has no --dry-run and declares no values=, so `wk run -h` shows neither the command it would run nor the configs it accepts"""
        text = _help_text("run")
        self.assertIn("preview:", text, text)
        self.assertIn("valid values", text, text)

    @unittest.expectedFailure
    def test_gui_has_neither_a_dry_run_preview_nor_a_config_list(self):
        """defect: cmd/gui has no --dry-run and declares no values=, so `wk gui -h` shows neither the command it would run nor the configs it accepts"""
        text = _help_text("gui")
        self.assertIn("preview:", text, text)
        self.assertIn("valid values", text, text)

    @unittest.expectedFailure
    def test_bench_lists_plans_not_the_build_configs_its_own_flag_takes(self):
        """defect: cmd/bench's values=--list enumerates benchmark plans, so `wk bench -h`'s 'valid values' section never names a build config despite --config being one of its own flags"""
        text = _help_text("bench")
        self.assertIn("preview:", text, text)  # true already
        self.assertIn("jsc-release", _values_section(text), text)  # only plans are listed -- fails

    @unittest.expectedFailure
    def test_test_previews_the_run_but_not_the_config_list(self):
        """defect: cmd/test has --dry-run (prints a 'would run' line) but declares no values=, so `wk test -h` never lists the configs its own --config accepts"""
        text = _help_text("test")
        self.assertIn("preview:", text, text)  # true already
        self.assertIn("valid values", text, text)  # not declared -- fails

    @unittest.expectedFailure
    def test_profile_lists_modes_not_the_build_configs_its_own_flag_takes(self):
        """defect: cmd/profile's values=--list enumerates profiler modes, so `wk profile -h`'s 'valid values' section never names a build config despite --config being one of its own flags"""
        text = _help_text("profile")
        self.assertIn("preview:", text, text)  # true already
        self.assertIn("jsc-release", _values_section(text), text)  # only modes are listed -- fails


if __name__ == "__main__":
    unittest.main()
