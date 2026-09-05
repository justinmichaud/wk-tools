"""Shape and consistency checks for the four conf registries: image/configs,
boot/machines, targets/hosts, bridge/hosts (docs/defects, "Conf files need to
be more consistent").

Every check here reads the registries and their loaders as they stand on
disk -- nothing is hardcoded that a loader already states, so a field this
suite checks for is a field grepped out of the loader that reads it, not a
copy of the list kept here.

Run:  python3 -m unittest tests.test_confs -v
"""

import re
import unittest
from pathlib import Path

from tests.support import REAL_REGISTRY, REPO, run

# --- the four registries ------------------------------------------------------

REGISTRIES = {
    "image/configs": REPO / "image" / "configs",
    "boot/machines": REPO / "boot" / "machines",
    "targets/hosts": REPO / "targets" / "hosts",
    "bridge/hosts": REPO / "bridge" / "hosts",
}

# Directories a case arm naming a machine must not hide in (docs/defects item
# 2/5: "no CODE file ... contains a case arm naming a machine from the
# registries"). Conf files themselves are data, not code, and are excluded by
# suffix below regardless of which of these dirs they live in.
CODE_DIRS = ["cmd", "lib", "targets", "boot", "bench", "image", "bridge"]

ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def conf_files(registry):
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
    """Every conf in the four registries: only KEY=value and '#' lines (a
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
        """The one reader, exercised rather than retyped: image_config_list
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
        files = conf_files("boot/machines")
        self.assertTrue(files, "no boot/machines/*.conf found")
        sets = {p.name: assigned_fields(p) for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"boot/machines/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_bridge_hosts_field_set(self):
        known = loader_fields((REPO / "cmd" / "bridge", "BR_"))
        files = conf_files("bridge/hosts")
        self.assertTrue(files, "no bridge/hosts/*.conf found")
        sets = {p.name: assigned_fields(p) for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"bridge/hosts/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_targets_hosts_field_set(self):
        # Every file that READS one of these fields, so the vocabulary comes
        # from code. build/configs.sh and lib/target.sh are here because they
        # are the only readers of some of them -- WK_TARGET_LIBCXX among them,
        # which otherwise survives in this set only as prose in remote.sh.
        known = loader_fields(
            (REPO / "targets" / "remote.sh", "WK_"),
            (REPO / "cmd" / "remote", "WK_"),
            (REPO / "cmd" / "build", "WK_"),
            (REPO / "build" / "configs.sh", "WK_"),
            (REPO / "lib" / "target.sh", "WK_"),
        )
        known.add("WK_TARGET_KIND")
        files = conf_files("targets/hosts")
        self.assertTrue(files, "no targets/hosts/*.conf found")
        sets = {p.name: assigned_fields(p) for p in files}
        for name, fields in sets.items():
            self.assertTrue(fields <= known, f"{name} sets unknown field(s): {fields - known}")
        first_name, first_fields = next(iter(sets.items()))
        for name, fields in sets.items():
            self.assertEqual(
                fields, first_fields,
                f"targets/hosts/{name} field set differs from {first_name}: "
                f"{fields ^ first_fields}",
            )

    def test_image_configs_field_set_per_builder(self):
        # Different builders (yocto/buildroot/pmos/fetch) read different
        # field subsets by design (image/profiles.sh's own reset list groups
        # them the same way) -- the documented optional subset is "same
        # builder, same fields".
        known = loader_fields((REPO / "image" / "profiles.sh", "CFG_"),
                               (REPO / "image" / "profiles.sh", "IMG_"),
                               (REPO / "image" / "profiles.sh", "YOC_"),
                               (REPO / "image" / "profiles.sh", "BR_"),
                               (REPO / "image" / "profiles.sh", "FET_"),
                               (REPO / "image" / "profiles.sh", "PMO_"))
        files = conf_files("image/configs")
        self.assertTrue(files, "no image/configs/*.conf found")
        # CFG_NEEDS is the one documented optional field within a builder
        # group: image_config_list (image/profiles.sh) treats its mere
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
        names = {p.stem for p in conf_files("boot/machines")}
        for name in names:
            with self.subTest(machine=name):
                self.assertRegex(
                    cp.stdout, rf"(?m)^{re.escape(name)}\b",
                    f"'wk boot --list' does not mention '{name}'",
                )


def registry_machine_names():
    """Every machine name in the three name-keyed registries -- what a case
    arm or a default value is not allowed to hardcode."""
    names = set()
    for registry in ("boot/machines", "targets/hosts", "bridge/hosts"):
        names |= {p.stem for p in conf_files(registry)}
    return names


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


class TestNoHardcodedMachineCaseArms(unittest.TestCase):
    """CLAUDE.md: 'New devices arrive as config, never code -- a case
    statement naming a machine is the shape being replaced.' A line escapes
    either check in this class only by being marked '# static' on the same
    line."""

    def test_no_case_arm_names_a_registry_machine(self):
        names = registry_machine_names()
        arm_re = re.compile(r"^\s*((?:[A-Za-z0-9_.-]+\|)*[A-Za-z0-9_.-]+)\)")
        violations = []
        for path, i, line in code_lines(CODE_DIRS):
            if "# static" in line:
                continue
            m = arm_re.match(line)
            if not m:
                continue
            arms = m.group(1).split("|")
            hit = names & set(arms)
            if hit:
                violations.append(f"{path.relative_to(REPO)}:{i}: {line.strip()!r} names {hit}")
        self.assertEqual(
            violations, [],
            "case arm(s) naming a registry machine by hand (add '# static' to "
            "keep one deliberately, or move the fact onto the machine's conf "
            "as a field):\n" + "\n".join(violations),
        )

    def test_no_default_value_names_a_registry_machine(self):
        # A '${VAR:-name}' fallback picks a machine exactly as silently as a
        # case arm does -- lib/image.sh's image_dtb_for and cmd/bench's
        # staged_root used to default to rpi5 / mbp this way. Scoped to
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
    (image_check_boot_files, lib/image.sh) -- so image_dtb_for refuses to
    guess one, and every Pi conf has to set NODE_DTB for real."""

    PI_NAMES = {"rpi3", "rpi4", "rpi5"}

    def test_every_pi_conf_sets_mach_dtb(self):
        for path in conf_files("boot/machines"):
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
    """_image_wants_wifi (boot/disk.sh) keys on NODE_NET rather than a case
    arm naming machines, so every boot/machines conf has to set it to one of
    the two words that function checks against."""

    def test_every_machine_conf_sets_mach_net(self):
        for path in conf_files("boot/machines"):
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
        return sorted(f.stem for f in REAL_REGISTRY.glob("*.conf"))

    def test_the_refusal_names_the_machines_that_do_have_a_conf(self):
        """`--target <typo>` lists the registry rather than only offering to
        write a conf for the typo"""
        names = self.known()
        self.assertTrue(names, "no machine confs to check against")
        typo = names[0][::-1]
        # The real registry: what the refusal has to name is the machines
        # this repo ships, and the suite is otherwise pointed at an empty one
        # (tests.support.NO_REGISTRY).
        cp = run("push", "status", "--target", typo,
                 env={"WK_TARGET_REGISTRY": str(REAL_REGISTRY)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(f"unknown target '{typo}'", cp.stdout)
        for n in names:
            self.assertIn(n, cp.stdout, f"{n} is not named in the refusal")

    def test_the_refusal_still_says_how_to_add_a_new_machine(self):
        """the name may genuinely be a machine that has no conf yet"""
        cp = run("push", "status", "--target", "a-machine-with-no-conf")
        self.assertIn("wk remote setup a-machine-with-no-conf", cp.stdout)
        self.assertIn("WK_REMOTE_HOST", cp.stdout)



class TestTheWatchdogIsOneValue(unittest.TestCase):
    """A borrowed board hands itself back on its own: every system reboots out
    of bench mode after IMG_WATCHDOG seconds unless claimed, which is what stops
    one staying borrowed until somebody notices.

    All 21 profiles carried `IMG_WATCHDOG=900` and the same four lines of
    reasoning above it -- one number that could then be changed in no single
    place. It lives in image/profiles.sh now, and a profile names it only to
    differ."""

    CONFIGS = sorted((REPO / "image" / "configs").glob("*.conf"))
    PROFILES = REPO / "image" / "profiles.sh"

    def test_the_default_is_five_minutes(self):
        m = re.search(r"^\s*IMG_WATCHDOG=(\d+)\s*$", self.PROFILES.read_text(), re.M)
        self.assertIsNotNone(m, "image/profiles.sh sets no IMG_WATCHDOG default")
        self.assertEqual(300, int(m.group(1)))

    def test_no_profile_restates_the_default(self):
        """Restating it is how 21 copies happened. A profile may still override,
        but not with the value it would have got anyway."""
        for conf in self.CONFIGS:
            m = re.search(r"^IMG_WATCHDOG=(\d+)", conf.read_text(), re.M)
            if m:
                with self.subTest(profile=conf.stem):
                    self.assertNotEqual(300, int(m.group(1)),
                                        f"{conf.name} restates the default")

    def test_the_reset_still_clears_every_img_field(self):
        """profiles.sh resets each field before sourcing a conf, so a second
        load cannot inherit the first profile's answers. The watchdog now
        resets to a value rather than to empty, and must still be in that
        block."""
        text = self.PROFILES.read_text()
        block = text[text.index("IMG_BUILDER=\"\""):]
        block = block[:block.index("YOC_BRANCH=")]
        for field in ("IMG_MACHINE", "IMG_ARCH", "IMG_HOSTNAME", "IMG_WATCHDOG"):
            with self.subTest(field=field):
                self.assertIn(field + "=", block, f"{field} is not reset per load")
