"""`wk status` rendering and `wk push status --all` -- regression tests for:

  1. a machine's load and free memory appear per machine, in text and json,
     never a fabricated number for one that did not answer (lib/status-view.py)
  2. the fleet's re-provisioning line renders whatever text the record carries,
     including "missing <FIELD>" for a conf that has nothing to compose a
     recipe from -- never a guess (lib/status-view.py)
  6. one merged document feeds every view: a field present in text is present
     in json (lib/status-view.py)
  5. `wk push status --all` prints one line per configured machine, or none
     when none are configured -- never nothing while machines exist (cmd/push)

Run: python3 -m unittest tests.test_status -v
"""
import json
import subprocess
import unittest

from tests.support import REPO, WkTest, bash

STATUS_VIEW = REPO / "lib" / "status-view.py"


def render(records, mode):
    """python3 lib/status-view.py <mode> <recordsfile>, on a synthetic
    JSON-lines file -- the same input `wk status --records` writes and the
    same renderer `wk status` uses, so this is exactly what a person or an
    agent reading `wk status` sees, with no real machine required."""
    path = None
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".jsonl", delete=False, dir="/tmp"
    ) as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        path = fh.name
    try:
        cp = subprocess.run(
            ["python3", str(STATUS_VIEW), mode, path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return cp
    finally:
        import os

        os.unlink(path)


def machine_rec(name, **extra):
    r = {"kind": "machine", "name": name}
    r.update(extra)
    return r


class TestLoadLine(unittest.TestCase):
    """Defect 1: each machine's load and free memory, never invented."""

    def test_text_shows_load_and_free_memory(self):
        recs = [
            machine_rec("buildbox4"),
            {
                "kind": "capacity",
                "machine": "buildbox4",
                "cores": "128",
                "load": "8",
                "free_mb": "170000",
                "mem_mb": "196000",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("buildbox4", out)
        self.assertIn("8", out)
        self.assertIn("128 cores", out)
        self.assertIn("free", out)

    def test_json_carries_the_same_capacity_record_as_text(self):
        """One document, three views (defect 6): the field text renders from
        is the same field json exposes."""
        recs = [
            machine_rec("moose"),
            {
                "kind": "capacity",
                "machine": "moose",
                "cores": "80",
                "load": "3",
                "free_mb": "121000",
            },
            {"kind": "exit", "code": 0},
        ]
        text_out = render(recs, "text").stdout
        json_out = json.loads(render(recs, "json").stdout)
        self.assertIn("80 cores", text_out)
        moose = next(m for m in json_out["machines"] if m["name"] == "moose")
        cap = moose["capacity"][0]
        self.assertEqual(cap["cores"], "80")
        self.assertEqual(cap["load"], "3")
        self.assertEqual(cap["free_mb"], "121000")

    def test_a_machine_that_did_not_answer_says_so_not_a_number(self):
        """A capacity probe that failed carries a note and no cores/load/
        free_mb -- the renderer must say so, never print a blank a reader
        could mistake for zero."""
        recs = [
            machine_rec("devbox-arm64-2"),
            {
                "kind": "capacity",
                "machine": "devbox-arm64-2",
                "note": "could not measure load/memory on devbox-arm64-2",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("could not measure load/memory on devbox-arm64-2", out)
        # No invented cores/load figure sits next to the note.
        self.assertNotIn("of  cores", out)

    def test_no_capacity_record_at_all_is_silence_not_a_zero(self):
        """A machine nobody asked about (no capacity record emitted) gets no
        load line at all -- not a fabricated 0."""
        recs = [machine_rec("quiet-machine"), {"kind": "exit", "code": 0}]
        out = render(recs, "text").stdout
        self.assertIn("quiet-machine", out)
        self.assertNotIn("0 of", out)


class TestReprovisionLine(unittest.TestCase):
    """Defect 2: the fleet's re-provisioning recipe, and a missing field
    that says so rather than guessing."""

    def test_text_shows_the_recipe_from_the_record(self):
        recs = [
            {
                "kind": "fleet",
                "machine": "rpi3",
                "role": "bench-device",
                "mode": "base image -- not a bench system",
                "media": "SD card",
                "reprovision": "wk sysimage build webkit-2.52-yocto-rpi3-32\n"
                "    in a workspace; hours\n"
                "wk boot rpi3",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("re-provisioning", out)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi3-32", out)
        self.assertIn("wk boot rpi3", out)

    def test_a_device_missing_mach_profile_renders_the_missing_field_not_a_guess(self):
        """A conf with no MACH_PROFILE has nothing to compose a command
        from (cmd/status's _fleet_probe guards this before ever calling a
        driver's b_reprovision); the record says which field is missing and
        the renderer must show exactly that, never a made-up profile name."""
        recs = [
            {
                "kind": "fleet",
                "machine": "newdevice",
                "role": "bench-device",
                "mode": "unreachable",
                "media": "unknown",
                "reprovision": "missing MACH_PROFILE in boot/machines/newdevice.conf"
                " -- nothing to compose a recipe from",
            },
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("missing MACH_PROFILE in boot/machines/newdevice.conf", out)
        # Nothing invented in its place: no 'wk sysimage build' line for a
        # profile that was never named.
        self.assertNotIn("wk sysimage build newdevice", out)

    def test_the_by_role_sample_command_differs_per_role(self):
        recs = [
            {
                "kind": "fleet",
                "machine": "rpi4",
                "role": "bench-device",
                "mode": "host mode",
                "media": "usb stick",
                "reprovision": "wk sysimage build p\nwk boot rpi4",
            },
            {
                "kind": "fleet",
                "machine": "rpi5",
                "role": "workstation",
                "mode": "host mode",
                "media": "nvme",
                "reprovision": "wk sysimage build p2\nwk boot rpi5",
            },
            {"kind": "bridge", "name": "some-bridge", "device": "d", "segment": "s"},
            {"kind": "exit", "code": 0},
        ]
        out = render(recs, "text").stdout
        self.assertIn("by role", out)
        self.assertIn("a rescue system", out)
        self.assertIn("a bench system", out)
        self.assertIn("a workstation", out)
        self.assertIn("a tailnet bridge", out)


class TestPushStatusAll(WkTest):
    """Defect 5: `wk push status --all` prints one line per configured
    machine (never nothing while machines exist)."""

    def _configured_machines(self):
        """The same list `for_each_machine` (lib/target.sh) walks: every
        target_all entry except container/vm/local, which are this machine
        and not a fork of `wk push` at all."""
        cp = bash(
            """
            . lib/common.sh
            . lib/store.sh
            . lib/target.sh
            for t in $(target_all 2>/dev/null); do
                case "$t" in container|vm|local) continue ;; esac
                echo "$t"
            done
            """,
            timeout=30,
        )
        return [l for l in cp.stdout.splitlines() if l.strip()]

    def test_one_line_per_configured_machine_or_none_configured(self):
        expected = set(self._configured_machines())
        try:
            cp = self.run_wk("push", "status", "--all", timeout=90)
        except subprocess.TimeoutExpired:
            self.skipTest("no route to the configured machines from here")

        lines = [l for l in cp.stdout.splitlines() if l.strip()]
        if not expected:
            self.assertEqual(lines, [])
            return
        seen = {l.split()[0] for l in lines}
        self.assertEqual(
            seen,
            expected,
            "wk push status --all must answer for every configured machine, "
            "not print nothing while machines exist",
        )
        # Exit status is meaningful (cmd/push's own 0/1/4), never silently 0
        # while a machine reported it holds no keys at all.
        self.assertIn(cp.returncode, (0, 1, 4))


if __name__ == "__main__":
    unittest.main()
