"""lib/wk/kv.py: the lenient probe reader and the one conf parser every conf registry reads through."""
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import kv  # noqa: E402


class TestConf(unittest.TestCase):
    def test_a_conf_is_literal_words_with_quotes_and_comments_dropped(self):
        text = '# head\n\nA=1\n  B="two words"   # note\nC=\'q\'\nD=\n'
        self.assertEqual(kv.conf(text), {"A": "1", "B": "two words", "C": "q", "D": ""})

    def test_a_quote_spans_lines_and_the_key_keeps_its_first_line(self):
        got = kv.conf('A="one\n  two"\nB=3\n')
        self.assertEqual(got, {"A": "one\n  two", "B": "3"})

    def test_each_refusal_names_the_file_and_the_line(self):
        for text, line, why in (("A=1\nprose\n", 2, "not a KEY=value line"),
                                ("A=$HOME\n", 1, "A is not a literal"),
                                ("A=`x`\n", 1, "A is not a literal"),
                                ("A=1\nB=a b\n", 2, "B is more than one word"),
                                ("A=1\nB=\"open\n", 2, "B's quote is never closed")):
            with self.subTest(text=text):
                with self.assertRaises(kv.ConfError) as cm:
                    kv.conf(text, "/x.conf")
                self.assertTrue(str(cm.exception).startswith("/x.conf:%d: " % line), str(cm.exception))
                self.assertIn(why, str(cm.exception))

    def test_check_refuses_a_key_by_its_own_reason(self):
        with self.assertRaises(kv.ConfError) as cm:
            kv.conf("ok=1\nBAD=2\n", "/y.conf", check=lambda k: None if k.islower() else "%s is shouting" % k)
        self.assertEqual(str(cm.exception), "/y.conf:2: BAD is shouting")

    def test_conf_file_reads_a_path_and_a_missing_one_raises(self):
        with self.assertRaises(FileNotFoundError):
            kv.conf_file("/nonexistent/wk-kv-test.conf")


class TestKv(unittest.TestCase):
    def test_the_first_of_a_repeated_key_wins_and_colour_is_dropped(self):
        self.assertEqual(kv.kv("\x1b[1ma=1\x1b[0m\r\na=2\nno equals\nb=x=y\n"), {"a": "1", "b": "x=y"})


if __name__ == "__main__":
    unittest.main()
