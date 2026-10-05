"""A layer is reached through its own Python API: no command runs `wk status` or `wk key push status` as a subprocess to read"""
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import priv, status, statusview  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402


def load_cmd(name):
    path = str(REPO / "cmd" / name)
    loader = importlib.machinery.SourceFileLoader("wk_cmd_" + name, path)
    spec = importlib.util.spec_from_file_location("wk_cmd_" + name, path, loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def no_subprocess(*a, **kw):
    raise AssertionError("a subprocess ran: %r" % (a[0],))


class TestZedStripsTheDispatchersVariables(unittest.TestCase):
    def test_without_reading_common_sh(self):
        zed = load_cmd("zed")
        seen = {}
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(zed, "ROOT", empty), \
                mock.patch.object(zed.Local, "exec", lambda self, argv, env: seen.update(env=env)), \
                mock.patch.dict(os.environ, {"WK_NAME": "a", "WK_CONFIG": "Debug", "KEEP": "1"}):
            zed.exec_clean(["zed", "url"])
        self.assertNotIn("WK_NAME", seen["env"])
        self.assertNotIn("WK_CONFIG", seen["env"])
        self.assertEqual("1", seen["env"]["KEEP"])


class TestPrivilegedHelpersAreAskedOfPython(unittest.TestCase):
    def test_bash_answers_what_python_does(self):
        rows = subprocess.run(["bash", "-c", '. "%s/lib/common.sh"; wk_priv_helpers; wk_priv_path wk-boot-priv; wk_priv_sudoers wk-boot-priv' % REPO],
                              capture_output=True, text=True).stdout
        want = "".join("%s %s %s\n" % (n, w, what) for n, w, what in priv.HELPERS) + priv.path("wk-boot-priv") + "\n" + priv.sudoers("wk-boot-priv") + "\n"
        self.assertEqual(want, rows)

    def test_doctor_runs_no_bash_for_them(self):
        from wk import doctor
        with mock.patch.object(subprocess, "run", no_subprocess):
            self.assertEqual(3, len(doctor.Host.priv_helpers(str(REPO))))


class TestStatusIsReadFromTheLibrary(unittest.TestCase):
    def test_the_web_view_refreshes_without_a_wk_subprocess(self):
        walk = mock.Mock()
        walk.records.return_value = [{"kind": "machine", "name": "m"}]
        live = statusview.Live(str(REPO), 5)
        with mock.patch.object(subprocess, "run", no_subprocess), mock.patch.object(status, "Walk", return_value=walk) as W, \
                mock.patch.object(statusview, "merge", return_value={"machines": ["m"]}):
            live.refresh_once()
        self.assertEqual({"machines": ["m"]}, live.doc)
        self.assertTrue(W.call_args.kwargs["fleet"])

    def test_start_shows_status_without_a_wk_subprocess(self):
        start = load_cmd("start")
        ctr = mock.Mock()
        ctr.machine.run.return_value = Result(0, "", "")
        ctr.podman.return_value = ["podman"]
        reg = mock.Mock()
        reg.load.return_value = ctr
        walk = mock.Mock()
        walk.records.return_value = ["rec"]
        with mock.patch.object(start, "here", lambda: False), mock.patch.object(start.secrets, "Secrets"), \
                mock.patch.object(subprocess, "run", no_subprocess), mock.patch.object(subprocess, "call", no_subprocess), \
                mock.patch.object(start.status, "Walk", return_value=walk) as W, \
                mock.patch.object(start.statusview, "render_text_stream") as render, \
                mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(0, start.start_everything(reg))
        self.assertFalse(W.call_args.kwargs["fleet"])
        render.assert_called_once()


class TestPrOpenAsksWhatPushStatusAsks(unittest.TestCase):
    def test_a_refused_push_status_refuses_without_a_wk_subprocess(self):
        pr = load_cmd("pr")
        target = mock.Mock()
        with mock.patch.object(pr, "pr_open_target", return_value=("WebKit/WebKit", "me:topic", "fork", "topic")), \
                mock.patch.object(subprocess, "run", no_subprocess), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(Refused):
                pr.pr_open(target, "ws", False, False, lambda: 1)
        target.exec.assert_not_called()

    def test_the_answer_is_the_exit_code_of_the_push_switchs_own_status(self):
        pr = load_cmd("pr")
        with mock.patch.object(pr, "Push") as P, mock.patch.object(pr.secrets, "Secrets"), mock.patch.object(subprocess, "run", no_subprocess):
            P.return_value.switch_status.return_value = 4
            self.assertEqual(4, pr.push_rc(mock.Mock()))


if __name__ == "__main__":
    unittest.main()
