"""One `machines/` directory, read by lib/wk/fleet.py alone.

lint.one_machine_dir: no file names the three directories machines/
replaced, and no case arm names a machine. The unit half: the reader, its
overlay, its refusals, and machine_cmd.shared_home.

Run: python3 tests/run.py --lint --unit -k test_machines_dir
"""
TIER = "lint"
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import FLEET_ENV, REAL_MACHINES, REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, fleet, targets  # noqa: E402
from wk.boot.cli import load_conf  # noqa: E402
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
        self.conf("b", '# b -- a board\nkind=board\ndisplay="builtin 1280x832"   # a note\nmac=\n')
        self.assertEqual(self.fleet.load("b"), {"kind": "board", "display": "builtin 1280x832", "mac": ""})

    def test_an_expansion_is_refused_since_a_default_belongs_in_code(self):
        self.conf("m", 'kind=mac\nvolume="${WK_BENCH_VOLUME:-WK Bench}"\n')
        with self.assertRaises(fleet.ConfError) as cm:
            self.fleet.load("m")
        self.assertIn("volume is not a literal", str(cm.exception))

    def test_an_uppercase_key_is_refused_naming_the_file_the_key_and_its_new_spelling(self):
        for old, new in (("KIND=board", "kind"), ("NODE_BENCH_SSH=x", "bench_ssh"), ("BR_LAN_MAC=x", "lan_mac"),
                         ("WK_REMOTE_HOST=x", "host"), ("WK_TARGET_CMAKE=x", "cmake"),
                         ("WK_TARGET_KIND=remote", "driver"), ("WK_BUILD_ARGS=x", "build_args")):
            with self.subTest(key=old):
                self.conf("old", "kind=build\n" + old + "\n")
                with self.assertRaises(fleet.ConfError) as cm:
                    self.fleet.load("old")
                msg = str(cm.exception)
                self.assertIn(str(self.dir / "old.conf") + ":2:", msg)
                self.assertIn("%s is spelled %s now" % (old.split("=")[0], new), msg)

    def test_a_conf_that_does_not_parse_refuses_every_listing_by_name(self):
        """A listing that skipped it would hide a machine for as long as nobody asked for it by name."""
        for text in ("host=k\n", "kind=board\nNODE_SSH=k\n"):
            with self.subTest(conf=text):
                self.conf("k", text)
                self.conf("ok", "kind=build\n")
                with self.assertRaises(fleet.ConfError):
                    self.fleet.load("k")
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(act.Refused) as cm:
                    self.fleet.names()
                self.assertEqual(cm.exception.status, 2)
                self.assertIn(str(self.dir / "k.conf"), err.getvalue())

    def test_fleet_list_exits_2_with_the_rename(self):
        self.conf("k", "kind=board\nNODE_SSH=k\n")
        cp = subprocess.run([sys.executable, "-m", "wk.fleet", "list"], capture_output=True, text=True,
                            env=dict(os.environ, PYTHONPATH=str(REPO / "lib"), WK_MACHINES_DIR=str(self.dir),
                                     XDG_CONFIG_HOME=str(self.dir / "cfg")))
        self.assertEqual(cp.returncode, 2, cp.stdout + cp.stderr)
        self.assertIn("NODE_SSH is spelled ssh now", cp.stderr)

    def test_a_listing_names_only_the_kinds_asked_for(self):
        for n, k in (("a", "build"), ("b", "peer"), ("c", "board"), ("d", "bridge")):
            self.conf(n, "kind=%s\n" % k)
        self.assertEqual(self.fleet.names(fleet.TARGET_KINDS), ["a", "b"])
        self.assertEqual(self.fleet.names(("bridge",)), ["d"])
        self.assertIsNone(self.fleet.load("nosuch"))

    def test_the_overlay_sets_keys_over_the_shared_conf_and_can_declare_its_own(self):
        self.conf("br", "kind=bridge\nsegment=10.0.0.0/24\ncard=x\n")
        self.conf("br", "card=rpi5:/dev/sdb\n", self.local)
        self.conf("mine", "kind=bridge\nsegment=10.1.0.0/24\n", self.local)
        loaded = self.fleet.load("br")
        self.assertEqual({k: loaded[k] for k in ("kind", "segment", "card")},
                         {"kind": "bridge", "segment": "10.0.0.0/24", "card": "rpi5:/dev/sdb"})
        self.assertEqual(self.fleet.names(("bridge",)), ["br", "mine"])
        self.assertEqual(self.fleet.path("br"), str(self.dir / "br.conf"))
        self.assertEqual(self.fleet.path("mine"), str(self.local / "mine.conf"))

    def test_wk_bench_volume_names_a_macs_bench_volume(self):
        self.conf("mac", 'kind=mac\nvolume="WK Bench"\n')
        self.conf("pi", 'kind=board\nvolume=""\n')
        env = dict(self.env, WK_BENCH_VOLUME="Other")
        self.assertEqual(fleet.Fleet(REPO, env).load("mac")["volume"], "Other")
        self.assertEqual(fleet.Fleet(REPO, env).load("pi")["volume"], "")
        self.assertEqual(self.fleet.load("mac")["volume"], "WK Bench")

    def test_every_shipped_conf_loads(self):
        real = fleet.Fleet(REPO, FLEET_ENV)
        for p in sorted(REAL_MACHINES.glob("*.conf")):
            with self.subTest(conf=p.name):
                self.assertIn(real.load(p.stem)["kind"], fleet.KINDS)


class TestTheReadersOverIt(FleetTest):
    def test_a_bench_machines_conf_is_its_node_fields_and_another_kind_is_none(self):
        self.conf("b", "kind=board\nnote=\"it's a board\"\ndriver=pi-sd\n")
        self.conf("br", "kind=bridge\nsegment=10.0.0.0/24\n")
        got = load_conf(REPO, "b", self.env)
        self.assertEqual({k: got.get(k) for k in ("note", "driver", "name", "kind")},
                         {"note": "it's a board", "driver": "pi-sd", "name": "b", "kind": None})
        self.assertIsNone(load_conf(REPO, "br", self.env))

    def test_the_target_registry_is_the_build_machines_and_peers(self):
        self.conf("box", "kind=build\nhost=box\n")
        self.conf("pe", "kind=peer\npeer=1\n")
        self.conf("pi", "kind=board\ndriver=pi-sd\nnote=n\n")
        reg = targets.Registry(REPO, env=self.env, machine=Fake())
        self.assertEqual(reg.known(), ["box", "pe"])
        self.assertIsNone(reg.kind("pi"))

    def test_a_target_confs_keys_set_their_wk_variables_over_the_environment(self):
        self.conf("box", "kind=build\ndriver=remote\nhost=box.example\nroot=\ncmake=-DX=1\nbuild_args=--y\n")
        env = dict(self.env, WK_REMOTE_HOST="from-env", WK_REMOTE_ROOT="/env/root", WK_REMOTE_TOOLS="/env/tools")
        t = targets.Registry(REPO, env=env, machine=Fake()).load("box")
        self.assertEqual({k: t.env.get(k) for k in ("WK_TARGET_KIND", "WK_REMOTE_HOST", "WK_REMOTE_ROOT", "WK_REMOTE_TOOLS",
                                                    "WK_TARGET_CMAKE", "WK_BUILD_ARGS")},
                         {"WK_TARGET_KIND": "remote", "WK_REMOTE_HOST": "box.example", "WK_REMOTE_ROOT": "",
                          "WK_REMOTE_TOOLS": "/env/tools", "WK_TARGET_CMAKE": "-DX=1", "WK_BUILD_ARGS": "--y"})

    def test_a_key_no_target_reads_is_refused_naming_it(self):
        self.conf("box", "kind=build\nhots=box.example\n")
        with self.assertRaises(LookupError) as cm:
            targets.Registry(REPO, env=self.env, machine=Fake()).load("box")
        self.assertIn("box.conf: hots is not a key a build machine's conf takes", str(cm.exception))

    def test_one_configs_own_flags_are_a_key_of_their_own(self):
        self.conf("box", "kind=build\ncmake_wpe_release=-DONE=1\nbuild_args_jsc_release=--one\n")
        env = targets.Registry(REPO, env=self.env, machine=Fake()).load("box").env
        self.assertEqual((env["WK_TARGET_CMAKE_wpe_release"], env["WK_BUILD_ARGS_jsc_release"]), ("-DONE=1", "--one"))

    def test_a_config_that_does_not_exist_is_refused(self):
        self.conf("box", "kind=build\ncmake_no_such_config=-DX=1\n")
        with self.assertRaises(LookupError) as cm:
            targets.Registry(REPO, env=self.env, machine=Fake()).load("box")
        self.assertIn("cmake_no_such_config is not a key", str(cm.exception))

    def test_every_old_spelling_the_parser_suggests_is_one_the_loader_takes_or_refuses_by_name(self):
        for old in ("WK_REMOTE_HOST", "WK_TARGET_CMAKE_wpe_release", "WK_BUILD_ARGS_jsc_release", "WK_REMOTE_MAX_JOBS"):
            with self.subTest(key=old):
                new = fleet.renamed(old)
                self.conf("box", "kind=build\n%s=x\n" % new)
                try:
                    targets.Registry(REPO, env=self.env, machine=Fake()).load("box")
                except LookupError as e:
                    self.assertIn("%s is not read:" % new, str(e))


class TestSharedHome(FleetTest):
    """machine_cmd.shared_home: two machines sharing one home, and so one
    ~/.wk-remote, each resolve their own target by hostname, with no ssh."""

    def setUp(self):
        super().setUp()
        self.conf("boxa", "kind=build\nhostname=\n")
        self.conf("boxb", "kind=build\nhostname=bbox-2\n")
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
        self.assertIn("hostname=stranger", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
