"""Shape and consistency of the two conf registries, image/configs and machines/, against the loaders that read them."""

import re
import sys
import unittest

from tests.support import FLEET_ENV, REAL_MACHINES, REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, images, places  # noqa: E402

REGISTRIES = {
    "image/configs": REPO / "image" / "configs",
    "machines": REAL_MACHINES,
}

ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def conf_files(registry, kinds=fleet.KINDS):
    if registry == "machines":
        return [REAL_MACHINES / (n + ".conf") for n in fleet.Fleet(REPO, FLEET_ENV).names(kinds)]
    return sorted(REGISTRIES[registry].glob("*.conf"))


def conf_lines(path):
    """(line-number, line, key) for every line: key is "" in a quoted continuation, None where no KEY= starts."""
    in_quote = False
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if in_quote:
            in_quote ^= (line.count('"') % 2 == 1)
            yield i, line, ""
            continue
        m = ASSIGN_RE.match(line)
        if m:
            in_quote = (line.count('"') % 2 == 1)
        yield i, line, m.group(1) if m else None


def assigned(path):
    return {k: line.split("=", 1)[1].strip().strip('"') for _, line, k in conf_lines(path) if k}


def loader_keys(*paths):
    """Every quoted lowercase name in the given readers: a machine conf key nothing quotes is read by nothing."""
    out = set()
    for path in paths:
        out |= set(re.findall(r"[\"']([a-z][a-z0-9_]*)[\"']", path.read_text()))
    return out


class TestConfShape(unittest.TestCase):
    def test_every_conf_is_key_value_or_comment_under_a_header_naming_it(self):
        for registry in REGISTRIES:
            for path in conf_files(registry):
                with self.subTest(registry=registry, conf=path.name):
                    lines = path.read_text().splitlines()
                    self.assertTrue(lines and lines[0].startswith("# " + path.stem), path)
                    for i, line, key in conf_lines(path):
                        stripped = line.strip()
                        self.assertFalse(key is None and stripped and not stripped.startswith("#"),
                                         "%s:%d: %r" % (path.name, i, line))


class TestEveryImageListsADescription(unittest.TestCase):
    def test_every_image_conf_has_a_blurb(self):
        for name in images.names({"WK_ROOT": str(REPO)}):
            with self.subTest(conf=name):
                self.assertTrue(images.blurb(name, {"WK_ROOT": str(REPO)}))


class TestConfFieldSets(unittest.TestCase):
    """Every conf of one kind declares the same field set, and nothing its loader does not read."""

    def assert_one_field_set(self, files, known, optional=frozenset()):
        self.assertTrue(files)
        sets = {p.name: set(assigned(p)) - {"kind"} for p in files}
        first = next(iter(sets.values()))
        for name, fields in sets.items():
            self.assertTrue(fields <= known, "%s sets unknown field(s): %s" % (name, fields - known))
            self.assertEqual(fields - optional, first - optional, name)

    def test_machines_of_each_kind(self):
        wk = REPO / "lib" / "wk"
        bench = loader_keys(*(p for d in ("boot", "sysimage") for p in sorted((wk / d).glob("*.py"))), wk / "reach.py")
        bridge = loader_keys(*sorted((wk / "bridge").glob("*.py")), wk / "fleet.py")
        for kinds, known in ((fleet.BENCH_KINDS, bench), (("bridge",), bridge), (fleet.PLACE_KINDS, set(places.CONF_ENV))):
            with self.subTest(kinds=kinds):
                self.assert_one_field_set(conf_files("machines", kinds), known)

    def test_image_configs_per_builder(self):
        text = (REPO / "lib" / "wk" / "images.py").read_text()
        known = set(re.findall(r"\b(?:CFG_|IMG_|YOC_|BR_|FET_|PMO_)[A-Z0-9_]+\b", text))
        # Facts about one configuration, not fields its builder group is missing.
        optional = {"CFG_NEEDS", "BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE",
                    "YOC_PORT_TARGET_FROM", "YOC_MACHINE", "YOC_MULTILIB", "YOC_MULTILIB_TUNE"}
        by_builder = {}
        for p in conf_files("image/configs"):
            by_builder.setdefault(assigned(p).get("IMG_BUILDER"), []).append(p)
        self.assertNotIn(None, by_builder)
        for builder, files in by_builder.items():
            with self.subTest(builder=builder):
                self.assert_one_field_set(files, known, optional)


class TestNoHardcodedMachineDefaults(unittest.TestCase):
    """CLAUDE.md: 'New devices arrive as config, never code'. A line escapes only by a '# static' mark."""

    def test_no_default_value_names_a_registry_machine(self):
        alt = "|".join(re.escape(p.stem) for p in sorted(conf_files("machines")))
        default_re = re.compile(rf":-({alt})\b")
        violations = []
        for d in ("lib", "cmd", "image", "bench"):
            for path in (REPO / d).rglob("*"):
                if not path.is_file() or path.suffix == ".conf" or "__pycache__" in path.parts:
                    continue
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if "# static" not in line and default_re.search(line):
                        violations.append("%s:%d: %s" % (path.relative_to(REPO), i, line.strip()))
        self.assertEqual(violations, [])


class TestBenchConfFields(unittest.TestCase):
    def test_every_pi_sets_a_dtb_and_every_machine_a_net(self):
        for path in conf_files("machines", fleet.BENCH_KINDS):
            with self.subTest(machine=path.stem):
                fields = assigned(path)
                self.assertIn(fields.get("net"), ("wifi", "ethernet"))
                if path.stem in ("rpi3", "rpi4", "rpi5"):
                    self.assertTrue(fields.get("dtb"))


if __name__ == "__main__":
    unittest.main()
