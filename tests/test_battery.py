"""`wk doctor --all` reads this Mac's battery state."""
import unittest

from tests.test_doctor import doctor


class TestMacBatteryLine(unittest.TestCase):

    def test_the_power_source_and_charge_or_no_line_without_a_battery(self):
        for pmset, line in (("Now drawing from 'AC Power'\n -InternalBattery-0 (id=4325193)\t87%; charging; 0:45 remaining present: true\n",
                             "plugged in, 87% -- no OS limit exists"),
                            ("Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4325193)\t62%; discharging; 3:12 remaining present: true\n",
                             "on battery, 62% -- no OS limit exists"),
                            ("Now drawing from 'AC Power'\n", None)):
            with self.subTest(line=line):
                self.assertEqual(line, doctor.mac_battery_line(pmset))


if __name__ == "__main__":
    unittest.main()
