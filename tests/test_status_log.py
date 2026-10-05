"""`wk status <ws> --log` (cmd/status, driven directly with WK_NAME/WK_PLACE/WK_VM_STORE): build.log, else the image stage
log its builder wrote under home/, and the errors it names."""
import unittest

from tests.support import REPO, WkTest, bash

CMD_STATUS = REPO / "cmd" / "status"


class TestLogsPicksTheRightFile(WkTest):
    def _run(self, name, store, args=()):
        env = {"WK_NAME": name, "WK_PLACE": "vm", "WK_VM_STORE": str(store)}
        return bash(f'exec "{CMD_STATUS}" --log {" ".join(args)}', env=env)

    def test_with_no_build_log_an_image_stage_log_is_shown_and_named(self):
        for builder in ("yocto", "buildroot"):
            with self.subTest(builder=builder):
                name = builder + "ws"
                home = self.tmp / "ws" / name / "home"
                home.mkdir(parents=True)
                log = home / (builder + "-image.log")
                log.write_text("the %s stage output\n" % builder)
                cp = self._run(name, self.tmp)
                out = cp.stdout + cp.stderr
                self.assertEqual(cp.returncode, 0, out)
                self.assertIn("the %s stage output" % builder, out)
                self.assertIn(str(log), out)

    def test_build_log_wins_when_both_exist(self):
        name = "bothws"
        wsdir = self.tmp / "ws" / name
        home = wsdir / "home"
        home.mkdir(parents=True)
        (home / "yocto-image.log").write_text("stale image-stage output\n")
        (wsdir / "build.log").write_text("ninja: building places\n")
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("ninja: building places", out, out)
        self.assertNotIn("stale image-stage output", out, out)

    def test_neither_log_names_both_remedies(self):
        name = "emptyws"
        (self.tmp / "ws" / name).mkdir(parents=True)
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("wk build", out, out)
        self.assertIn("wk sysimage build", out, out)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        name = "goodws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building place foo\n"
            "-- no error: handling here, everything is fine\n"
            "ninja: no work to do.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("(none)", out, out)
        errors_section = out.split("errors:", 1)[1].split("last output:", 1)[0]
        self.assertNotIn("no error: handling here", errors_section, out)

    def test_a_real_ninja_failure_still_reports_by_the_same_path(self):
        name = "badws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building place foo\n"
            "foo.cpp:10:5: error: use of undeclared identifier 'x'\n"
            "ninja: build stopped: subcommand failed.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertNotIn("(none)", out, out)
        self.assertIn("ninja: build stopped", out, out)


if __name__ == "__main__":
    unittest.main()
