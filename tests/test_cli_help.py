"""Every command that takes a build config says in its `--help` that
`--dry-run` prints the command line it would run, and lists the configs it
accepts.

`wk <cmd> -h` is produced by the dispatcher's `explain`: the `changes things:`
line says `--dry-run prints what it would run` for the invocations the
command declares `dryrun` for, and a `valid values (--config):` section
(`<config>` for `wk build`'s positional) lists lib/wk/buildconf.py's names
whenever the command declares `config=`. Both are read here from real `wk
<cmd> -h` invocations.

Run: python3 -m unittest tests.test_cli_help -v
"""
import unittest

from tests.support import WkTest, run


def _help_text(cmd):
    cp = run(cmd, "-h", timeout=30)
    return cp.stdout + cp.stderr


def _values_section(text):
    """From the first `valid values` block to the end: a config named in the
    header's prose is not the command's own closed set."""
    idx = text.find("valid values")
    return text[idx:] if idx != -1 else ""


class TestEveryConfigCommandPreviewsAndListsTheConfigs(WkTest):
    def test_build(self):
        text = _help_text("build")
        self.assertIn("--dry-run prints what it would run", text, text)
        for config in ("jsc-release", "gtk-release", "mac-release"):
            self.assertIn(config, _values_section(text), text)

    def test_run(self):
        text = _help_text("run")
        self.assertIn("--dry-run prints what it would run", text, text)
        self.assertIn("jsc-release", _values_section(text), text)

    def test_gui(self):
        text = _help_text("gui")
        self.assertIn("--dry-run prints what it would run", text, text)
        self.assertIn("wpe-release", _values_section(text), text)

    def test_bench(self):
        """bench's own values= lists plans; the configs its --config takes are listed beside them"""
        text = _help_text("bench")
        self.assertIn("--dry-run prints what it would run", text, text)
        self.assertIn("jsc-release", _values_section(text), text)

    def test_test(self):
        text = _help_text("test")
        self.assertIn("--dry-run prints what it would run", text, text)
        self.assertIn("jsc-release", _values_section(text), text)

    def test_profile(self):
        """profile's own values= lists modes; the configs its --config takes are listed beside them"""
        text = _help_text("profile")
        self.assertIn("--dry-run prints what it would run", text, text)
        self.assertIn("jsc-release", _values_section(text), text)


if __name__ == "__main__":
    unittest.main()
