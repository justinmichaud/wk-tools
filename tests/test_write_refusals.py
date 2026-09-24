"""`unit sysimage.write_refusals`: a write refuses by name before anything is erased (lib/wk/sysimage/disk.py). Two
unmarked disks of one transport are listed rather than picked; a missing or out-ranked card helper names the remedy;
the disk the machine runs from is refused in the helper's own words, with the disks there listed.

Run: python3 tests/run.py --unit -k test_write_refusals
"""
import unittest

from tests.test_disk_logic import BOOTED_REFUSAL, Writer, disks, quietly
from wk import act


class TestWriteRefusals(unittest.TestCase):
    def test_two_unmarked_disks_of_one_transport_are_listed_not_picked(self):
        d = disks()
        self.assertEqual(d.resolve_own(), "")
        text, _ = quietly(d.listing)
        self.assertIn("    /dev/sda 59.5G usb", text)
        self.assertIn("    /dev/sdb 28.7G usb", text)
        self.assertEqual(text.count("no wk system on it"), 2)

    def test_a_missing_helper_names_the_remedy(self):
        w = Writer(status=(1, "", "sudo: a password is required\n"))
        e, err = quietly(disks(w).refuse_unless_safe, "/dev/sda")
        self.assertIsInstance(e, act.Refused)
        self.assertIn("rpi5 cannot write a disk: its card helper is missing", err)
        self.assertIn("sudo -n /usr/local/libexec/wk-card-priv status", err)
        self.assertIn("./setup --stage quiesce", err)
        self.assertNotIn(("card_priv", "check", "/dev/sda"), w.calls)

    def test_a_helper_that_predates_the_slot_names_the_remedy(self):
        for dev, status, slot in (("/dev/sda@second", "ok\n", "second"), ("/dev/sda@third", "ok\nsecond=yes\n", "third")):
            w = Writer(status=(0, status))
            e, err = quietly(disks(w).refuse_unless_safe, dev)
            self.assertIsInstance(e, act.Refused, dev)
            self.assertIn("predates %s systems (@%s)" % (slot, slot), err)
            self.assertIn("rebuild the\n    rescue image", err)
            self.assertNotIn(("card_priv", "check", dev), w.calls)

    def test_a_current_helper_is_asked_the_rule(self):
        for dev in ("/dev/sda", "/dev/sda@second", "/dev/sda@third"):
            w = Writer()
            e, err = quietly(disks(w).refuse_unless_safe, dev)
            self.assertIsNone(e, err)
            self.assertIn(("card_priv", "check", dev), w.calls)

    def test_the_disk_the_machine_runs_from_is_refused(self):
        w = Writer(check=(3, BOOTED_REFUSAL.rstrip("\n")),
                   whose={"/dev/sda": (0, "machine=rpi5\n", "")})
        e, err = quietly(disks(w).refuse_unless_safe, "/dev/nvme0n1")
        self.assertIsInstance(e, act.Refused)
        self.assertIn("rpi5 will not write /dev/nvme0n1:\n    wk-card-priv: REFUSED: '/dev/nvme0n1' is a disk this "
                      "machine is running from", err)
        self.assertIn("    Disks there:\n    /dev/sda 59.5G usb", err)
        self.assertIn("holds this machine's own system", err)


if __name__ == "__main__":
    unittest.main()
