"""Shape and consistency checks for the two conf registries: image/configs
and machines/ (docs/defects, "Conf files need to be more consistent").

Every check here reads the registries and their loaders as they stand on
disk -- nothing is hardcoded that a loader already states, so a field this
suite checks for is a field grepped out of the loader that reads it, not a
copy of the list kept here.

Run:  python3 -m unittest tests.test_confs -v
"""

import re
import sys
import unittest

from tests.support import FLEET_ENV, REAL_MACHINES, REPO, run

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet  # noqa: E402

REGISTRIES = {
    "image/configs": REPO / "image" / "configs",
    "machines": REAL_MACHINES,
}

ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def conf_files(registry, kinds=fleet.KINDS):
    if registry == "machines":
        return [REAL_MACHINES / (n + ".conf") for n in fleet.Fleet(REPO, FLEET_ENV).names(kinds)]
    return sorted(REGISTRIES[registry].glob("*.conf"))


def assigned_fields(path):
    """The set of KEY=... names a conf file assigns, tolerating a value that
    continues across lines inside one still-open double quote (image/configs'
    CFG_NEEDS is one)."""
    fields = set()
    in_quote = False
    for line in path.read_text().splitlines():
        if in_quote:
            in_quote ^= (line.count('"') % 2 == 1)
            continue
        m = ASSIGN_RE.match(line)
        if not m:
            continue
        fields.add(m.group(1))
        in_quote = (line.count('"') % 2 == 1)
    return fields


def loader_fields(*paths_and_prefixes):
    """Every FOO_BAR-shaped token used in the given (path, prefix) pairs --
    the loader's own vocabulary, not a copy of it kept in this test."""
    out = set()
    for path, prefix in paths_and_prefixes:
        text = path.read_text()
        out |= set(re.findall(rf"\b{prefix}[A-Z0-9_]+\b", text))
    return out


class TestConfShape(unittest.TestCase):
    """Every conf in the two registries: only KEY=value and '#' lines (a
    value may continue across lines inside one quoted string), no bare prose,
    and a header line naming the file."""

    def test_every_conf_is_key_value_or_comment(self):
        for registry in REGISTRIES:
            for path in conf_files(registry):
                with self.subTest(registry=registry, conf=path.name):
                    in_quote = False
                    for i, line in enumerate(path.read_text().splitlines(), 1):
                        if in_quote:
                            in_quote ^= (line.count('"') % 2 == 1)
                            continue
                        stripped = line.strip()
                        if stripped == "" or stripped.startswith("#"):
                            continue
                        m = ASSIGN_RE.match(line)
                        self.assertIsNotNone(
                            m,
                            f"{path.relative_to(REPO)}:{i}: not a KEY=value or "
                            f"'#' line: {line!r}",
                        )
                        in_quote = (line.count('"') % 2 == 1)

    def test_every_conf_has_a_header_line(self):
        for registry in REGISTRIES:
            for path in conf_files(registry):
                with self.subTest(registry=registry, conf=path.name):
                    lines = path.read_text().splitlines()
                    self.assertTrue(lines, f"{path} is empty")
                    first = lines[0]
                    name = path.stem
                    self.assertTrue(
                        first.startswith(f"# {name}"),
                        f"{path.relative_to(REPO)}: header line {first!r} does "
                        f"not open with '# {name}'",
                    )


class TestEveryImageListsADescription(unittest.TestCase):
    """`wk sysimage --list` prints each image's description, and that text is
    the header line's own second half.

    It used to be line 3, read positionally (`sed -n '3s/^# //p'`), so trimming
    a comment above it silently listed an image with a blank description --
    measured on two configs. The reader and this test now use the same rule.
    """

    BLURB = re.compile(r"^# (\S+) -- (.+)$")

    def test_every_image_config_header_carries_its_blurb(self):
        files = conf_files("image/configs")
        self.assertTrue(files, "no image/configs/*.conf found")
        for path in files:
            with self.subTest(conf=path.name):
                first = path.read_text().splitlines()[0]
                m = self.BLURB.match(first)
                self.assertIsNotNone(
                    m, f"{path.name}: header {first!r} is not '# <name> -- <description>'")
                self.assertEqual(m.group(1), path.stem)
                self.assertTrue(m.group(2).strip(), f"{path.name}: empty description")

    def test_the_listing_reads_the_description_from_that_line(self):
        """The one reader, exercised rather than retyped: wk.images.listing
        must print a non-empty description under every image it names."""
        cp = run("sysimage", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        names = {p.stem for p in conf_files("image/configs")}
        lines = cp.stdout.splitlines()
        for i, line in enumerate(lines):
            if line.strip() in names and not line.startswith(" "):
                self.assertLess(i + 1, len(lines), f"{line}: nothing follows it")
                described = lines[i + 1].strip()
                self.assertTrue(
                    described and not described.startswith("--"),
                    f"{line.strip()} lists with no description (got {described!r})")


class TestConfFieldSets(unittest.TestCase):
    """Every conf in a registry declares the same field set (image/configs'
    builder-specific fields are the documented optional subset: grouped by
    IMG_BUILDER instead of checked flat), and declares nothing the loader
    does not know."""

    def test_boot_machines_field_set(self):
        known = loader_fields((REPO / "boot" / "machines.sh", "NODE_"))
        files = conf_files("machines", fleet.BENCH_KINDS)
        self.assertTrue(files, "no bench machine in machines/")
        sets = {p.name: assigned_fields(p) - {"KIND"} for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"machines/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_bridge_hosts_field_set(self):
        known = loader_fields((REPO / "cmd" / "bridge", "BR_"))
        files = conf_files("machines", ("bridge",))
        self.assertTrue(files, "no bridge in machines/")
        sets = {p.name: assigned_fields(p) - {"KIND"} for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"machines/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_targets_hosts_field_set(self):
        # Every file that READS one of these fields, so the vocabulary comes
        # from code. lib/wk/buildconf.py and lib/target.sh are here because they
        # are the only readers of some of them -- WK_TARGET_LIBCXX among them,
        # which otherwise survives in this set only as prose in remote.sh.
        known = loader_fields(
            (REPO / "targets" / "remote.sh", "WK_"),
            (REPO / "lib" / "wk" / "machine_cmd.py", "WK_"),
            (REPO / "lib" / "wk" / "targets.py", "WK_"),
            (REPO / "lib" / "wk" / "build.py", "WK_"),
            (REPO / "lib" / "wk" / "buildconf.py", "WK_"),
            (REPO / "lib" / "target.sh", "WK_"),
            (REPO / "lib" / "wk" / "fleet.py", "WK_"),
        )
        known.add("WK_TARGET_KIND")
        files = conf_files("machines", fleet.TARGET_KINDS)
        self.assertTrue(files, "no build machine or peer in machines/")
        sets = {p.name: assigned_fields(p) - {"KIND"} for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"machines/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_image_configs_field_set_per_builder(self):
        # Different builders (yocto/buildroot/pmos/fetch) read different
        # field subsets by design (lib/wk/images.py's FIELDS groups
        # them the same way) -- the documented optional subset is "same
        # builder, same fields".
        known = loader_fields(*((REPO / "lib" / "wk" / "images.py", p)
                                for p in ("CFG_", "IMG_", "YOC_", "BR_", "FET_", "PMO_")))
        files = conf_files("image/configs")
        self.assertTrue(files, "no image/configs/*.conf found")
        # CFG_NEEDS is the one documented optional field within a builder
        # group: wk.images.listing (lib/wk/images.py) treats its mere
        # presence as "not buildable yet", so it is set only on the configs
        # that need something the others in the same group already have.
        # BR_KERNEL_*: a profile whose board will not boot the kernel its
        # tree builds declares one instead (image/buildroot/kernel-pin.sh).
        # That is a fact about one board, the way CFG_NEEDS is a fact about
        # one configuration -- the other configs in the group are not missing
        # anything, and setting the fields empty on all of them would claim
        # they had a kernel question to answer.
        # YOC_PORT_TARGET_FROM/YOC_MACHINE: a profile whose branch has no
        # section for its cross-target derives one (image/yocto/port-target.py).
        # YOC_MULTILIB/YOC_MULTILIB_TUNE: a profile whose userspace width is
        # not its machine's builds that width as a multilib variant. Both are
        # facts about one configuration, like CFG_NEEDS -- a branch that has
        # the section, or an image at the machine's own width, is not missing
        # anything, and setting them empty everywhere would claim otherwise.
        optional = {"CFG_NEEDS", "BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256",
                    "BR_KERNEL_RELEASE",
                    "YOC_PORT_TARGET_FROM", "YOC_MACHINE",
                    "YOC_MULTILIB", "YOC_MULTILIB_TUNE"}

        by_builder = {}
        for p in files:
            fields = assigned_fields(p)
            self.assertTrue(fields <= known, f"{p.name} sets unknown field(s): {fields - known}")
            builder_lines = [l for l in p.read_text().splitlines() if l.startswith("IMG_BUILDER=")]
            self.assertTrue(builder_lines, f"{p.name} sets no IMG_BUILDER")
            builder = builder_lines[0].split("=", 1)[1]
            by_builder.setdefault(builder, {})[p.name] = fields - optional
        for builder, sets in by_builder.items():
            first_name, first_fields = next(iter(sets.items()))
            for name, fields in sets.items():
                self.assertEqual(
                    fields, first_fields,
                    f"image/configs/{name} ({builder}) field set differs from "
                    f"{first_name}: {fields ^ first_fields}",
                )


class TestBootListsEveryMachine(unittest.TestCase):
    def test_wk_boot_list_covers_every_conf(self):
        cp = run("boot", "--list")
        names = {p.stem for p in conf_files("machines", fleet.BENCH_KINDS)}
        for name in names:
            with self.subTest(machine=name):
                self.assertRegex(
                    cp.stdout, rf"(?m)^{re.escape(name)}\b",
                    f"'wk boot --list' does not mention '{name}'",
                )


def registry_machine_names():
    """Every machine name in machines/ -- what a default value is not allowed to hardcode."""
    return {p.stem for p in conf_files("machines")}


def code_lines(dirs):
    """(path, line-number, line) for every non-.conf file under the given
    repo-relative directories -- the code half of the tree, never the data."""
    for d in dirs:
        base = REPO / d
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix == ".conf":
                continue
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                yield path, i, line


class TestNoHardcodedMachineDefaults(unittest.TestCase):
    """CLAUDE.md: 'New devices arrive as config, never code' (the case-arm
    half is lint.one_machine_dir, tests/test_machines_dir.py). A line escapes
    only by being marked '# static' on the same line."""

    def test_no_default_value_names_a_registry_machine(self):
        # A '${VAR:-name}' fallback picks a machine exactly as silently as a
        # case arm does. Scoped to
        # lib/cmd/image/bench: the four directories where code reaches for a
        # fleet machine by name (targets/ and bridge/ have their own
        # registries and are covered by the case-arm check above instead).
        names = registry_machine_names()
        alt = "|".join(re.escape(n) for n in sorted(names))
        default_re = re.compile(rf":-({alt})\b")
        violations = []
        for path, i, line in code_lines(["lib", "cmd", "image", "bench"]):
            if "# static" in line:
                continue
            m = default_re.search(line)
            if m:
                violations.append(f"{path.relative_to(REPO)}:{i}: {line.strip()!r} defaults to '{m.group(1)}'")
        self.assertEqual(
            violations, [],
            "default value(s) naming a registry machine (add '# static' to "
            "keep one deliberately, or require the name instead of guessing "
            "it):\n" + "\n".join(violations),
        )


class TestPiConfsSetDtb(unittest.TestCase):
    """A Pi's firmware halts, not panics, if it cannot find its device tree
    (the card helper's boot-check) -- so the write refuses to guess one, and
    every Pi conf has to set NODE_DTB for real."""

    PI_NAMES = {"rpi3", "rpi4", "rpi5"}

    def test_every_pi_conf_sets_mach_dtb(self):
        for path in conf_files("machines", fleet.BENCH_KINDS):
            if path.stem not in self.PI_NAMES:
                continue
            with self.subTest(machine=path.stem):
                fields = {}
                for line in path.read_text().splitlines():
                    m = re.match(r'^NODE_DTB=(.*)$', line)
                    if m:
                        fields["NODE_DTB"] = m.group(1).strip().strip('"')
                self.assertIn("NODE_DTB", fields, f"{path.name} sets no NODE_DTB")
                self.assertTrue(fields["NODE_DTB"], f"{path.name} sets NODE_DTB to an empty value")


class TestMachinesSetNet(unittest.TestCase):
    """wants_wifi (lib/wk/sysimage/write.py) keys on NODE_NET rather than a case
    arm naming machines, so every bench machine's conf has to set it to one of
    the two words that function checks against."""

    def test_every_machine_conf_sets_mach_net(self):
        for path in conf_files("machines", fleet.BENCH_KINDS):
            with self.subTest(machine=path.stem):
                value = None
                for line in path.read_text().splitlines():
                    m = re.match(r'^NODE_NET=(.*)$', line)
                    if m:
                        value = m.group(1).strip().strip('"')
                self.assertIn(
                    value, ("wifi", "ethernet"),
                    f"{path.name} sets NODE_NET to {value!r}, not 'wifi' or 'ethernet'",
                )


if __name__ == "__main__":
    unittest.main()


class TestUnknownTargetRefusal(unittest.TestCase):
    """A mistyped machine name is answered by the names that exist, not by
    instructions for provisioning the machine the typo invented."""

    def known(self):
        return fleet.Fleet(REPO, FLEET_ENV).names(fleet.TARGET_KINDS)

    def test_the_refusal_names_the_machines_that_do_have_a_conf(self):
        """`--target <typo>` lists the registry rather than only offering to
        write a conf for the typo"""
        names = self.known()
        self.assertTrue(names, "no machine confs to check against")
        typo = names[0][::-1]
        # The real registry: what the refusal has to name is the machines
        # this repo ships, and the suite is otherwise pointed at one with no
        # target (tests.support.BLIND_FLEET).
        cp = run("push", "status", "--target", typo,
                 env={"WK_MACHINES_DIR": str(REAL_MACHINES)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(f"unknown target '{typo}'", cp.stdout)
        for n in names:
            self.assertIn(n, cp.stdout, f"{n} is not named in the refusal")

    def test_the_refusal_still_says_how_to_add_a_new_machine(self):
        """the name may genuinely be a machine that has no conf yet"""
        cp = run("push", "status", "--target", "a-machine-with-no-conf")
        self.assertIn("wk machine setup a-machine-with-no-conf", cp.stdout)
        self.assertIn("WK_REMOTE_HOST", cp.stdout)
