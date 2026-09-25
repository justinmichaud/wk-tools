"""One `machines/` directory, read by lib/wk/fleet.py alone.

lint.one_machine_dir: no file names the three directories machines/
replaced, and no case arm names a machine. The unit half: the reader, its
overlay, its refusals, and machine_cmd.shared_home.

Run: python3 tests/run.py --lint --unit -k test_machines_dir
"""
TIER = "lint"
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import FLEET_ENV, REAL_MACHINES, REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, targets  # noqa: E402
from wk.machine import Fake  # noqa: E402

OLD = [re.compile(p) for p in (r"boot/machines(?!\.sh)\b", "targets/" + "hosts", "bridge/" + "hosts",
                                r"config/wk/" + "bridges")]
# The user's own text, which no agent edits: README.md above its marker, CLAUDE.md, the plan that names the move.
USERS_OWN = {"CLAUDE.md", "docs/PLAN.md"}
README_MARKER = "*** Claude edit below here ***"
ARM = re.compile(r"^\s*\(?((?:[\"']?[A-Za-z0-9_.-]+[\"']?\s*\|\s*)*[\"']?[A-Za-z0-9_.-]+[\"']?)\)")


def tracked():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
                         stdout=subprocess.PIPE, universal_newlines=True, check=True).stdout
    for rel in out.splitlines():
        p = REPO / rel
        if rel in USERS_OWN or not p.is_file() or rel == "tests/test_machines_dir.py":
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if rel == "README.md":
            text = text.split(README_MARKER, 1)[-1]
        yield rel, text


def shell_case_arms(text):
    """(line number, arm words) for every arm of every `case ... esac`."""
    depth = 0
    for i, line in enumerate(text.splitlines(), 1):
        code = line.split(" #", 1)[0]
        if re.search(r"\bcase\b.*\bin\b", code):
            depth += 1
        if depth:
            m = ARM.match(code)
            if m:
                yield i, {w.strip().strip("\"'") for w in m.group(1).split("|")}
        if re.search(r"\besac\b", code):
            depth = max(0, depth - 1)


class TestOneMachineDir(unittest.TestCase):
    def test_no_file_names_an_old_machine_directory(self):
        hits = ["%s:%d: %s" % (rel, i, line.strip())
                for rel, text in tracked() for i, line in enumerate(text.splitlines(), 1)
                if any(p.search(line) for p in OLD)]
        self.assertEqual(hits, [], "a machine is machines/<name>.conf:\n" + "\n".join(hits))

    def test_no_case_arm_names_a_machine(self):
        names = {p.stem for p in REAL_MACHINES.glob("*.conf")}
        self.assertTrue(names)
        hits = []
        for rel, text in tracked():
            if rel.endswith(".conf") or not (rel.endswith(".sh") or text.startswith("#!/usr/bin/env bash")
                                             or text.startswith("#!/bin/sh")):
                continue
            for i, words in shell_case_arms(text):
                if words & names:
                    hits.append("%s:%d names %s" % (rel, i, sorted(words & names)))
        self.assertEqual(hits, [], "a fact about one machine is a key in its conf:\n" + "\n".join(hits))


class FleetTest(unittest.TestCase):
    wk_tier = "unit"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-fleet-"))
        self.dir = self.tmp / "machines"
        self.local = self.tmp / "config" / "wk" / "machines"
        self.dir.mkdir()
        self.env = {"WK_MACHINES_DIR": str(self.dir), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "HOME": str(self.tmp / "home")}
        self.fleet = fleet.Fleet(REPO, self.env)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conf(self, name, text, where=None):
        d = where or self.dir
        d.mkdir(parents=True, exist_ok=True)
        (d / (name + ".conf")).write_text(text)


class TestTheReader(FleetTest):
    def test_values_are_one_literal_word_with_quotes_and_comments_dropped(self):
        self.conf("b", '# b -- a board\nKIND=board\nNODE_DISPLAY="builtin 1280x832"   # a note\nNODE_MAC=\n')
        self.assertEqual(self.fleet.load("b"), {"KIND": "board", "NODE_DISPLAY": "builtin 1280x832", "NODE_MAC": ""})

    def test_an_expansion_is_refused_since_a_default_belongs_in_code(self):
        self.conf("m", 'KIND=mac\nNODE_VOLUME="${WK_BENCH_VOLUME:-WK Bench}"\n')
        with self.assertRaises(fleet.ConfError) as cm:
            self.fleet.load("m")
        self.assertIn("NODE_VOLUME is not a literal", str(cm.exception))

    def test_a_conf_without_a_known_kind_is_refused_and_left_out_of_every_listing(self):
        self.conf("k", "WK_REMOTE_HOST=k\n")
        self.conf("ok", "KIND=build\n")
        with self.assertRaises(fleet.ConfError):
            self.fleet.load("k")
        self.assertEqual(self.fleet.names(), ["ok"])

    def test_a_listing_names_only_the_kinds_asked_for(self):
        for n, k in (("a", "build"), ("b", "peer"), ("c", "board"), ("d", "bridge")):
            self.conf(n, "KIND=%s\n" % k)
        self.assertEqual(self.fleet.names(fleet.TARGET_KINDS), ["a", "b"])
        self.assertEqual(self.fleet.names(("bridge",)), ["d"])
        self.assertIsNone(self.fleet.load("nosuch"))

    def test_the_overlay_sets_keys_over_the_shared_conf_and_can_declare_its_own(self):
        self.conf("br", "KIND=bridge\nBR_SEGMENT=10.0.0.0/24\nBR_CARD=x\n")
        self.conf("br", "BR_CARD=rpi5:/dev/sdb\n", self.local)
        self.conf("mine", "KIND=bridge\nBR_SEGMENT=10.1.0.0/24\n", self.local)
        loaded = self.fleet.load("br")
        self.assertEqual({k: loaded[k] for k in ("KIND", "BR_SEGMENT", "BR_CARD")},
                         {"KIND": "bridge", "BR_SEGMENT": "10.0.0.0/24", "BR_CARD": "rpi5:/dev/sdb"})
        self.assertEqual(self.fleet.names(("bridge",)), ["br", "mine"])
        self.assertEqual(self.fleet.path("br"), str(self.dir / "br.conf"))
        self.assertEqual(self.fleet.path("mine"), str(self.local / "mine.conf"))

    def test_wk_bench_volume_names_a_macs_bench_volume(self):
        self.conf("mac", 'KIND=mac\nNODE_VOLUME="WK Bench"\n')
        self.conf("pi", 'KIND=board\nNODE_VOLUME=""\n')
        env = dict(self.env, WK_BENCH_VOLUME="Other")
        self.assertEqual(fleet.Fleet(REPO, env).load("mac")["NODE_VOLUME"], "Other")
        self.assertEqual(fleet.Fleet(REPO, env).load("pi")["NODE_VOLUME"], "")
        self.assertEqual(self.fleet.load("mac")["NODE_VOLUME"], "WK Bench")

    def test_every_shipped_conf_loads(self):
        real = fleet.Fleet(REPO, FLEET_ENV)
        for p in sorted(REAL_MACHINES.glob("*.conf")):
            with self.subTest(conf=p.name):
                self.assertIn(real.load(p.stem)["KIND"], fleet.KINDS)


class TestTheShellReaders(FleetTest):
    def test_load_prints_assignments_a_shell_evaluates_and_refuses_another_kind(self):
        self.conf("b", "KIND=board\nNODE_NOTE=\"it's a board\"\nNODE_DRIVER=pi-sd\n")
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/boot/machines.sh"\n'
                  'machine_load b && printf "%s|%s|%s\\n" "$NODE_NOTE" "$NODE_DRIVER" "${KIND:-}"\n'
                  'wk_fleet load b --kind bridge || echo "rc=$?"', env=self.env)
        self.assertIn("it's a board|pi-sd|\n", cp.stdout, cp.stderr)
        self.assertIn("rc=1", cp.stdout)

    def test_the_target_registry_is_the_build_machines_and_peers(self):
        self.conf("box", "KIND=build\nWK_REMOTE_HOST=box\n")
        self.conf("pe", "KIND=peer\nWK_REMOTE_PEER=1\n")
        self.conf("pi", "KIND=board\nNODE_DRIVER=pi-sd\nNODE_NOTE=n\n")
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/target.sh"\n'
                  'target_known | tr "\\n" " "; target_kind pi || echo "pi-not-a-target"', env=self.env)
        self.assertEqual(cp.stdout, "box pe pi-not-a-target\n", cp.stderr)


class TestSharedHome(FleetTest):
    """machine_cmd.shared_home: two machines sharing one home, and so one
    ~/.wk-remote, each resolve their own target by hostname, with no ssh."""

    def setUp(self):
        super().setUp()
        self.conf("boxa", "KIND=build\nWK_REMOTE_HOSTNAME=\n")
        self.conf("boxb", "KIND=build\nWK_REMOTE_HOSTNAME=bbox-2\n")
        home = self.tmp / "home"
        home.mkdir()
        (home / ".wk-remote").write_text("target=boxb\nroot=%s/wk\n" % home)

    def registry(self, host):
        far = Fake(host)
        far.answer(["hostname", "-s"], out=host.upper() + "\n")
        return targets.Registry(REPO, env=self.env, machine=far), far

    def test_each_machine_of_a_shared_home_is_its_own_target(self):
        for host, name in (("boxa", "boxa"), ("bbox-2", "boxb")):
            with self.subTest(host=host):
                reg, far = self.registry(host)
                self.assertEqual(reg.default(), name)
                self.assertIn(name, reg.all())
                self.assertEqual([e for e in far.effects if e[0] == "run" and e[1][0] == "ssh"], [])

    def test_a_host_no_conf_names_is_refused_with_the_key_to_add(self):
        reg, _ = self.registry("stranger")
        with self.assertRaises(LookupError) as cm:
            reg.default()
        self.assertIn("WK_REMOTE_HOSTNAME=stranger", str(cm.exception))

    def test_the_shell_default_target_agrees(self):
        stub = self.tmp / "bin"
        stub.mkdir()
        for host, want in (("BBOX-2", "boxb\n"), ("stranger", "")):
            with self.subTest(host=host):
                (stub / "hostname").write_text("#!/bin/sh\necho %s\n" % host)
                (stub / "hostname").chmod(0o755)
                env = dict(self.env, PATH="%s:%s" % (stub, os.environ["PATH"]),
                           WK_REMOTE_MARKER=str(self.tmp / "home" / ".wk-remote"))
                cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/target.sh"; default_target', env=env)
                self.assertEqual(cp.stdout, want, cp.stderr)
                if not want:
                    self.assertIn("WK_REMOTE_HOSTNAME=stranger", cp.stderr)


if __name__ == "__main__":
    unittest.main()
