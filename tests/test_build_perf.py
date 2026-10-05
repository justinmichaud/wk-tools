"""lib/wk/sudo.py requires visudo outright."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.machine import HAVE, Fake  # noqa: E402
from wk.sudo import Sudo  # noqa: E402


class TestSudoRequiresVisudo(unittest.TestCase):
    def test_absent_visudo_refuses_outright_before_any_effect(self):
        f = Fake("here")
        with contextlib.redirect_stderr(io.StringIO()) as buf, self.assertRaises(Refused):
            Sudo(f, {}).setup()
        self.assertIn("visudo", buf.getvalue())
        self.assertEqual(f.effects, [("run", HAVE + ("visudo",))])


if __name__ == "__main__":
    unittest.main()
