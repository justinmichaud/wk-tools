"""The disk model (lib/wk/sysimage/disk.py), parsed from captured `lsblk -J` outputs and a card helper faked at the
Channel. No real disk is read; `_image_wants_wifi` is tests/test_wifi_seed.py's TestImageWantsWifi.

Run: python3 tests/run.py --unit -k test_disk_logic
"""
import contextlib
import io
import os
import shlex
import sys
import unittest
from unittest import mock

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.driver import CARD_PRIV, Channel  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import disk  # noqa: E402

# util-linux 2.38 (Debian 12, a Pi 5 host on NVMe): a USB reader with a written card, an unwritten USB stick, the
# NVMe it runs from, a zram swap disk and a loop device.
LSBLK_238 = """{
   "blockdevices": [
      {"name":"/dev/loop0", "size":"4K", "tran":null, "rm":false, "type":"loop", "model":null, "label":null},
      {"name":"/dev/sda", "size":"59.5G", "tran":"usb", "rm":true, "type":"disk", "model":"STORAGE DEVICE  ", "label":null,
         "children": [
            {"name":"/dev/sda1", "size":"512M", "tran":null, "rm":true, "type":"part", "model":null, "label":"bootfs"},
            {"name":"/dev/sda2", "size":"59G", "tran":null, "rm":true, "type":"part", "model":null, "label":"root"}
         ]
      },
      {"name":"/dev/sdb", "size":"28.7G", "tran":"usb", "rm":false, "type":"disk", "model":"Extreme", "label":null},
      {"name":"/dev/zram0", "size":"2G", "tran":null, "rm":false, "type":"disk", "model":null, "label":null},
      {"name":"/dev/nvme0n1", "size":"476.9G", "tran":"nvme", "rm":false, "type":"disk", "model":"WD SN740", "label":null,
         "children": [
            {"name":"/dev/nvme0n1p1", "size":"512M", "tran":null, "rm":false, "type":"part", "model":null, "label":"bootfs"},
            {"name":"/dev/nvme0n1p2", "size":"476.4G", "tran":null, "rm":false, "type":"part", "model":null, "label":"rootfs"}
         ]
      }
   ]
}
"""

# util-linux 2.34 (Ubuntu 20.04): RM is a string there, and a built-in SD slot is mmc and not removable.
LSBLK_234 = """{
   "blockdevices": [
      {"name": "/dev/mmcblk0", "size": "29.7G", "tran": "mmc", "rm": "0", "type": "disk", "model": null, "label": null,
         "children": [
            {"name": "/dev/mmcblk0p1", "size": "256M", "tran": null, "rm": "0", "type": "part", "model": null, "label": "boot"},
            {"name": "/dev/mmcblk0p2", "size": "29.5G", "tran": null, "rm": "0", "type": "part", "model": null, "label": null}
         ]
      },
      {"name": "/dev/sdc", "size": "14.9G", "tran": "usb", "rm": "1", "type": "disk", "model": "Card Reader", "label": null}
   ]
}
"""

BOOTED_REFUSAL = ("wk-card-priv: REFUSED: '/dev/nvme0n1' is a disk this machine is running from (/, /boot, swap or\n"
                  "    the kernel's root=). A second system beside this one is '/dev/nvme0n1@second'.\n")


class Writer:
    """A writing machine at the Channel: `lsblk` answers `lsblk`, `whose` maps a disk to the helper's answer."""

    def __init__(self, lsblk=LSBLK_238, whose=None, status=(0, "ok\nsecond=yes\nthird=yes\n"), check=(0, "ok"),
                 has_whose=True):
        self.lsblk, self.whose, self.status, self.check, self.has_whose = lsblk, whose or {}, status, check, has_whose
        self.calls = []

    def call(self, fn, *args, input=None, mutates=False):
        self.calls.append((fn,) + args)
        if fn == "m_ssh":
            return Result(0, self.lsblk)
        verb, dev = args[0], (args[1:] or ("",))[0]
        if verb == "whose" and not self.has_whose:
            return Result(1, "", "usage: wk-card-priv status|check|write <device>\n")
        if verb == "whose":
            rc, out, err = self.whose.get(dev, (0, "marker: none\n", ""))
            return Result(rc, out, err)
        if verb == "status":
            return Result(*self.status)
        if verb == "check":
            return Result(self.check[0], self.check[1] + "\n")
        raise AssertionError("unexpected call %r" % ((fn,) + args,))


def disks(w=None, name="rpi5", device="/dev/sda"):
    return disk.Disks(w or Writer(), {"NODE_NAME": name, "NODE_DEVICE": device})


def quietly(fn, *args):
    """(result or Refused, stderr)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            return fn(*args), err.getvalue()
        except act.Refused as e:
            return e, err.getvalue()


class TestParse(unittest.TestCase):
    def test_candidates_are_removable_or_usb_or_mmc_whole_disks(self):
        got = disk.parse_lsblk(LSBLK_238)
        self.assertEqual([d.name for d in got], ["/dev/sda", "/dev/sdb"])
        self.assertEqual(got[0], disk.Disk("/dev/sda", "59.5G", "usb", "1", "STORAGE DEVICE", ["bootfs", "root"]))
        self.assertEqual(disk.line(got[1]), "/dev/sdb 28.7G usb 0 disk Extreme")

    def test_older_lsblk_spells_rm_as_a_string(self):
        got = disk.parse_lsblk(LSBLK_234)
        self.assertEqual([(d.name, d.tran, d.rm, d.labels) for d in got],
                         [("/dev/mmcblk0", "mmc", "0", ["boot", "-"]), ("/dev/sdc", "usb", "1", [])])

    def test_output_that_is_not_json_is_refused_by_name(self):
        e, err = quietly(disk.parse_lsblk, "lsblk: unknown option -- 'J'\n")
        self.assertIsInstance(e, act.Refused)
        self.assertIn("did not print JSON", err)

    def test_an_unreachable_writer_has_no_candidates(self):
        w = Writer()
        w.call = lambda fn, *a, **k: Result(255, "", "ssh: connect: no route")
        self.assertEqual(disks(w).candidates(), [])

    def test_spec_and_transport_and_partition_names(self):
        self.assertEqual(disk.parse_spec("rpi5:/dev/sda@second"), ("rpi5", "/dev/sda@second"))
        self.assertEqual(disk.parse_spec("rpi5"), ("rpi5", ""))
        e, err = quietly(disk.parse_spec, ":/dev/sda")
        self.assertIsInstance(e, act.Refused)
        self.assertIn("--disk needs a machine", err)
        self.assertEqual([disk.tran_of_name(d) for d in ("/dev/sda", "/dev/mmcblk0", "/dev/nvme0n1", "/dev/vda", "")],
                         ["usb", "mmc", "nvme", "", ""])


class TestResolve(unittest.TestCase):
    def test_the_one_disk_of_the_declared_transport_is_the_machines_own(self):
        self.assertEqual(disks(Writer(LSBLK_234), device="/dev/mmcblk0").resolve_own(), "/dev/mmcblk0")

    def test_two_disks_of_one_transport_are_told_apart_by_marker(self):
        w = Writer(whose={"/dev/sda": (0, "machine=rpi4\n", ""), "/dev/sdb": (0, "marker: none\n", "")})
        self.assertEqual(disks(w).resolve_own(), "/dev/sdb")
        w = Writer(whose={"/dev/sdb": (0, "id=x\nmachine=rpi5\n", "")})
        self.assertEqual(disks(w).resolve_own(), "/dev/sdb")

    def test_no_declared_transport_resolves_nothing(self):
        self.assertEqual(disks(device="").resolve_own(), "")

    def test_own_or_declared_warns_when_the_kernel_name_moved(self):
        w = Writer(whose={"/dev/sda": (0, "machine=rpi4\n", "")})
        got, err = quietly(disks(w).own_or_declared)
        self.assertEqual(got, "/dev/sdb")
        self.assertIn("its own medium is /dev/sdb right now", err)

    def test_own_or_declared_falls_to_the_conf_when_the_machine_cannot_say(self):
        got, err = quietly(disks().own_or_declared)
        self.assertEqual((got, err), ("/dev/sda", ""))

    def test_for_machine_reads_the_marker_and_asks_each_disk_once(self):
        w = Writer(whose={"/dev/sdb": (0, "machine=rpi4\n", "")})
        d = disks(w)
        self.assertEqual(d.for_machine("rpi4"), "/dev/sdb")
        self.assertEqual(d.for_machine("rpi3"), "")
        self.assertEqual(d.for_machine(""), "")
        self.assertEqual(sum(1 for c in w.calls if c[:2] == ("card_priv", "whose") and len(c) == 3), 2)
        self.assertEqual(sum(1 for c in w.calls if c[0] == "m_ssh"), 1)


class TestListing(unittest.TestCase):
    def test_each_disk_says_what_holds_it(self):
        w = Writer(whose={"/dev/sda": (0, "machine=rpi5\n", ""), "/dev/sdb": (0, "machine=rpi4\n", "")})
        text, err = quietly(disks(w).listing)
        self.assertEqual(text.splitlines(), [
            "    /dev/sda 59.5G usb 1 disk STORAGE DEVICE   <- rpi5 is configured to boot from this one (wk boot rpi5)",
            "        labels: bootfs,root  --  holds this machine's own system",
            "    /dev/sdb 28.7G usb 0 disk Extreme",
            "        empty -- no partition table  --  holds a system for rpi4"])
        self.assertEqual(err, "")

    def test_the_booted_disk_says_so_instead_of_no_wk_system(self):
        """The helper refuses `whose` for the disk it runs from; that refusal is read, not taken as an empty answer."""
        w = Writer(LSBLK_234, whose={"/dev/mmcblk0": (3, "", BOOTED_REFUSAL.replace("nvme0n1", "mmcblk0"))})
        text, _ = quietly(disks(w, device="/dev/sdc").listing)
        self.assertIn("labels: boot,-  --  this machine's own system (booted)", text)
        self.assertIn("    /dev/sdc 14.9G usb 1 disk Card Reader   <- rpi5", text)
        self.assertIn("empty -- no partition table  --  no wk system on it", text)

    def test_a_helper_without_whose_lists_by_label_and_names_the_remedy(self):
        text, err = quietly(disks(Writer(has_whose=False)).listing)
        self.assertIn("        labels: bootfs,root\n", text + "\n")
        self.assertNotIn("no wk system", text)
        self.assertIn("has no 'whose' verb", err)
        self.assertIn("./setup --stage quiesce", err)

    def test_a_moved_kernel_name_is_warned(self):
        w = Writer(whose={"/dev/sdb": (0, "machine=rpi5\n", "")})
        _, err = quietly(disks(w).listing)
        self.assertIn("the disk holding\n  rpi5's own system is /dev/sdb", err)

    def test_no_candidate_says_none_is_attached(self):
        text, _ = quietly(disks(Writer('{"blockdevices": []}')).listing)
        self.assertEqual(text, "    (none -- no removable disk is attached to rpi5)")


class TestChannel(unittest.TestCase):
    def test_reads_and_the_helper_go_over_ssh_to_the_conf_machine(self):
        """lsblk under `sh -c` and the card helper under `sudo -n`, both on NODE_SSH, and no bash in between."""
        m = Fake()
        m.react(("ssh",), lambda argv, f: Result(0, LSBLK_234 if "lsblk" in argv[-1] else "marker: none\n"))
        conf = {"NODE_NAME": "rpi4", "NODE_SSH": "rpi4", "NODE_DEVICE": "/dev/sdc"}
        d = disk.Disks(Channel(REPO, conf, "host", env={}, via=m), conf)
        self.assertEqual(d.for_machine("rpi4"), "")
        sent = [e[1] for e in m.effects if e[1][0] == "ssh"]
        self.assertEqual(sent[0][-2:], ("rpi4", "sh -c %s" % shlex.quote(disk.LSBLK)))
        self.assertIn(("rpi4", "sudo -n %s whose /dev/sdc" % CARD_PRIV), [e[-2:] for e in sent])
        self.assertFalse([e for e in m.effects if e[1][0] in ("bash", "env")])


class TestShims(unittest.TestCase):
    """boot/disk.sh's functions are one line each over `python3 -m wk.sysimage.disk`."""

    def test_a_disk_spec_names_its_machine(self):
        self.assertEqual(disk.parse_spec("rpi5:/dev/sd a"), ("rpi5", "/dev/sd a"))
        e, err = quietly(disk.parse_spec, ":x")
        self.assertIsInstance(e, act.Refused)
        self.assertIn("--disk needs a machine", err)

    def test_the_shims_answer_through_the_verbs(self):
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/boot/disk.sh"; disk_tran_of_name /dev/mmcblk1; '
                  'disk_part /dev/mmcblk0 2; disk_of_part /dev/sda2; disk_partno /dev/nvme0n1p3')
        self.assertEqual(cp.stdout.split(), ["mmc", "/dev/mmcblk0p2", "/dev/sda", "3"], cp.stderr)

    def test_main_names_its_verbs(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(disk.main(["nope"], env={}), 2)
        self.assertIn("own-or-declared", err.getvalue())

    def test_a_verb_answers_on_stdout(self):
        with mock.patch.object(disk, "_disks", lambda env, root: disks()), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(disk.main(["own-or-declared"], env={}), 0)
        self.assertEqual(out.getvalue(), "/dev/sda\n")

    def test_the_writer_is_this_process_node_and_channel(self):
        env = {"NODE_NAME": "rpi5", "NODE_DEVICE": "/dev/sda", "MODE_CHANNEL": "bench", "HOME": "/x"}
        d = disk._disks(env, str(REPO))
        self.assertEqual((d.name, d.device, d.ch.channel), ("rpi5", "/dev/sda", "bench"))
        self.assertNotIn("HOME", d.ch.conf)


if __name__ == "__main__":
    unittest.main()
