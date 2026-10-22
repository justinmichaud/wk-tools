"""There is no image store: `wk boot` arms what the device itself holds, read off its card."""
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path
from tests.test_fleet_walk import FAKE_BOARD_CONF

# Answers the card helper's read of wk-image.id off partition 1 of the board's medium; partition 3 is bare.
_SSH_STUB = '''#!/bin/sh
case "$*" in
  *"boot-read /dev/sda 1 wk-image.id"*) echo "{fake_id}" ;;
esac
'''


class TestBootArmDefaultsToDeviceImage(WkTest):
    """The default system is what the device holds; a named --system is checked against it."""

    def boot(self, fake_id, *args):
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
                stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = "fakerpi5" + rand_suffix(3)
            (machdir / (name + ".conf")).write_text(FAKE_BOARD_CONF.format(ssh=name, mac="02:00:00:00:00:01", note="fake bench board"))
            return run("boot", name, *args, "--dry-run", timeout=30,
                       env={"WK_MACHINES_DIR": str(machdir), "PATH": "%s:/usr/bin:/bin" % binp})

    def test_the_device_s_id_is_armed_and_another_is_refused_naming_the_write(self):
        fake_id, wanted = ("webkit-2.52-yocto-rpi5-64-" + rand_suffix(12) for _ in range(2))
        cp = self.boot(fake_id)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(fake_id, cp.stdout)
        cp = self.boot(fake_id, "--system", wanted)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        for words in (fake_id, wanted, "wk sysimage write --from"):
            self.assertIn(words, cp.stdout)


if __name__ == "__main__":
    unittest.main()
