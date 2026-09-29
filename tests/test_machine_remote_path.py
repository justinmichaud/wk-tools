"""`unit machine.remote_path`: a probe over non-interactive ssh finds the tools a login shell does.

Run: python3 tests/run.py -k tests.test_machine_remote_path
"""
import re
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import machine  # noqa: E402

LOGIN_SHELL = re.compile(r"^\"\$SHELL\" -lc ")


class TestRemotePath(unittest.TestCase):
    def sent(self, call):
        via = machine.Fake("here")
        via.react(["ssh"], lambda argv, f: machine.Result(0, ""))
        call(machine.Ssh("box", via=via))
        return [e[1][-1] for e in via.effects if e[0] == "run"]

    def test_remote_path_a_probe_runs_under_a_login_shell(self):
        for name, call in (("run", lambda m: m.run(["tart", "list"])), ("have", lambda m: m.have("tart")),
                           ("exists", lambda m: m.exists("/x"))):
            with self.subTest(call=name):
                sent = self.sent(call)
                self.assertTrue(sent and all(LOGIN_SHELL.search(s) for s in sent), sent)


if __name__ == "__main__":
    unittest.main()
