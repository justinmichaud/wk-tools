"""Boot-image write logic owed by docs/HANDOFF-test-runner.md:

  - `_from_filter`'s decompressor selection by extension (cmd/sysimage)
  - `disk_unique_identity` gives two disks written from one image distinct
    identities (boot/disk.sh)
  - a `--from` write with no profile installs the identity marker and the
    driving key but not the systemd units, and the reverse for a real
    profile (cmd/sysimage: cmd_write_from)

`image_fast_path_ok` / `wic_of` / `disk_sha256` (also named in that handoff
entry) do not exist anywhere in this tree (`grep -r` turns up nothing outside
the handoff itself) -- there is no provenance fast-path left to test, so that
part of the entry is stale and this file does not cover it.

Run: python3 -m unittest tests.test_owed_image_write -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, run, scratch_dir, stub_path

SYSIMAGE = REPO / "cmd" / "sysimage"


def _lift(path, func):
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"could not lift {func} from {path}"
    return text


class TestFromFilterPicksTheDecompressor(WkTest):
    """cmd/sysimage's `_from_filter`: the command that undoes a builder's own
    compression, by the extension the builder is known to produce -- yocto's
    .wic.xz, an ad-hoc .zst or .gz, and a plain buildroot .img (or anything
    else) passed through unchanged."""

    def _filter(self, path):
        cp = self.bash(_lift(SYSIMAGE, "_from_filter") + f"\n_from_filter {path!r}\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_xz_is_decompressed_with_xz(self):
        self.assertEqual(self._filter("rpi3.wic.xz"), "xz -dc")

    def test_zst_is_decompressed_with_zstd(self):
        self.assertEqual(self._filter("rpi3.img.zst"), "zstd -dc")

    def test_gz_is_decompressed_with_gzip(self):
        self.assertEqual(self._filter("rpi3.img.gz"), "gzip -dc")

    def test_an_uncompressed_image_passes_through_cat(self):
        self.assertEqual(self._filter("rpi3.img"), "cat")

    def test_an_unrecognised_extension_also_passes_through_cat(self):
        self.assertEqual(self._filter("rpi3.wic"), "cat")


class TestDiskUniqueIdentityIsDistinctPerDisk(WkTest):
    """disk_unique_identity (boot/disk.sh) reads the image's own PARTUUID
    (same for every card written from one image), generates a fresh random
    one and asks the card helper to stamp it, then reads the card back to
    confirm the stamp took. Driven for two disks in one process, with
    `disk_root_spec` stubbed to the one shared 'old' identity every copy of
    the image carries, `card_priv` stubbed to record what it was asked to
    stamp, and `m_ssh` stubbed to answer the read-back with exactly that --
    proving the two calls generate two different identities without needing
    a card in a reader."""

    def test_two_disks_from_one_image_get_different_identities(self):
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/boot/machines.sh"
disk_root_spec() { printf 'PARTUUID=deadbeef-02'; }
LAST_NEW=""
card_priv() { LAST_NEW="$4"; return 0; }
m_ssh() { printf '%s-02\\n' "$LAST_NEW"; }
disk_unique_identity /dev/sdX1
echo "ID1=$LAST_NEW"
LAST_NEW=""
disk_unique_identity /dev/sdY1
echo "ID2=$LAST_NEW"
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        ids = dict(line.split("=", 1) for line in cp.stdout.splitlines() if "=" in line)
        id1, id2 = ids.get("ID1"), ids.get("ID2")
        self.assertTrue(id1, cp.stdout + cp.stderr)
        self.assertTrue(id2, cp.stdout + cp.stderr)
        self.assertNotEqual(id1, id2, "two disks written from one image took the same identity")

    def test_a_second_disk_beside_a_rescue_keeps_the_rescues_identity(self):
        """disk_is_second: the second system's partitions take their
        PARTUUIDs from the rescue already on the card, so this is a no-op --
        no new identity is generated at all."""
        cp = self.bash('''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/boot/machines.sh"
card_priv() { echo "card_priv should not have run for a second system" >&2; exit 1; }
disk_unique_identity /dev/sdX1@second
echo DONE
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("DONE", cp.stdout)
        self.assertIn("keeps the rescue disk's identity", cp.stdout + cp.stderr)


class TestWriteFromInstallsMarkerAndKeyOnly(WkTest):
    """cmd_write_from (cmd/sysimage): whether a card gets the whole fleet
    integration (identity marker, driving key, systemd units, retargeted
    root) or only the marker and key depends on whether the image came from
    a builder this repo knows (IMG_BUILDER=yocto|buildroot, set by
    --profile) -- not on --rescue, which only picks the tailnet role. A real
    profile (tests/test_card_edits.py's TestWriteDryRunIsTheWholeSequence)
    gets the units line; this is the complementary case, a `--from` write
    with no profile the checkout can identify, which does not."""

    _SSH = '''#!/bin/sh
case "$*" in
  *card-priv*status*) exit 0 ;;
  *card-priv*check*)  echo "wk-card-priv: /dev/sdX may be written: usb 64G"; exit 0 ;;
  *) exit 0 ;;
esac
'''

    def test_no_profile_gets_marker_and_key_and_not_the_fleet_steps(self):
        img = self.tmp / "unknown-source.img"
        img.write_text("not a wic, not a profile this checkout knows\n")
        key = self.tmp / "id.pub"
        key.write_text("ssh-ed25519 AAAAtest test@example\n")
        with stub_path({"ssh": self._SSH}) as binp, scratch_dir() as store, scratch_dir() as reg:
            cp = run(
                "sysimage", "write", "--from", str(img),
                "--disk", "rpi5:/dev/sdX", "--dry-run",
                env={"PATH": f"{binp}:{__import__('os').environ['PATH']}",
                     "WK_IMAGE_KEY": str(key), "WK_STORE": str(store),
                     "WK_TARGET_REGISTRY": str(reg)},
            )
        out = cp.stdout
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("identity marker and driving key only", out, out)
        self.assertNotIn("would install the fleet units", out, out)
        self.assertNotIn("would retarget", out, out)
        self.assertNotIn("would append this profile's firmware block", out, out)
        self.assertNotIn("would name the system on", out, out)
        # The marker and key step still runs, unconditionally.
        self.assertIn("would install the identity marker and the driving ssh key", out, out)


if __name__ == "__main__":
    unittest.main()
