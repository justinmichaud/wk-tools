"""Static rules over the tree's sources and confs."""
TIER = "lint"
import collections
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO, WkTest, repo_files, shell_files

sys.path.insert(0, str(REPO / "lib"))
from wk import boot, fleet, gc, images  # noqa: E402
from wk.boot import cli  # noqa: E402
from wk.places import CONF_ENV  # noqa: E402
from wk.store import Store  # noqa: E402

SYSTEM_SSH = "/usr/bin/ssh"


def under(*roots):
    return lambda: [p for p in repo_files() if p.relative_to(REPO).parts[0] in roots]


def code(line):
    return not line.lstrip().startswith("#")


def sources_common(path):
    return "lib/common.sh" in path.read_text(errors="replace")


JSON_PARSERS = (re.compile(r"""(?<![\w-])jq(?=\s+[-'"])|\|\s*jq\b"""), re.compile(r"python3\s+-c\s+.*import\s+json"),
                re.compile(r"(?<![\w-])sed\b[^\n]*[{}][^\n]*\bjson\b", re.I))
RSYNC_REMOTE = re.compile(r'rsync\s[^\n]*(-e\s+"ssh|\$\w+:|@\$)')
VAR = r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
VARIABLE_PATTERN = re.compile(r"^\s*(?:;;\s*)?%s\)|\bcase\b.*\bin\s+%s\)|\S\|%s\)|\[\[[^]]*(?:==|!=)\s*%s" % (VAR, VAR, VAR, VAR))
RUNS_HOSTNAME = re.compile(r"(^|[;&|({`$]|\bthen|\bdo)\s*hostname(\s+-s)?\s*(2>|\||\)|;|$)")
# What each line rule refuses: (the files it reads, the line that breaks it).
LINE_RULES = {
    "flock is called: there is one lock mechanism": (
        under("wk", "cmd", "lib", "boot", "image"),
        lambda p, l: re.search(r"^[^#]*\bflock\b", l) and p.name != "selftest"),
    "a script sourcing lib/common.sh takes the EXIT trap instead of registering a handler": (
        under("cmd", "lib", "boot", "image"),
        lambda p, l: re.search(r"^[^#]*trap .*EXIT", l) and p.name != "common.sh" and sources_common(p)),
    "a bash file parses JSON": (
        shell_files,
        lambda p, l: any(r.search(l) for r in JSON_PARSERS) and p.relative_to(REPO).as_posix() != "bridge/bin/wk-bridge-netwatch"),
    "a tailscale key is passed through argv": (
        under("cmd", "bench", "bridge", "boot", "image", "lib"),
        lambda p, l: re.search(r"--auth-?key", l) and code(l) and "file:" not in l),
    "a cross-machine rsync carries the local umask (no --chmod)": (
        shell_files,
        lambda p, l: code(l) and RSYNC_REMOTE.search(l) and "--chmod=" not in l),
    "a variable is a case pattern: match in Python (lib/wk/job.py match_any)": (
        shell_files, lambda p, l: VARIABLE_PATTERN.search(l)),
    "host-side bash runs hostname: lib/common.sh's wk_machine_name is the bash spelling of this machine's name": (
        lambda: [p for p in shell_files() if p.relative_to(REPO).parts[0] in ("lib", "cmd", "host", "setup")],
        lambda p, l: code(l) and RUNS_HOSTNAME.search(l)),
}


def line_rule_hits(files, broken):
    hits = []
    for p in files:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        hits += ["%s:%d: %s" % (p.relative_to(REPO), n, l.strip()[:90])
                 for n, l in enumerate(text.splitlines(), 1) if broken(p, l)]
    return hits


class TestLineRules(unittest.TestCase):
    def test_no_line_breaks_a_rule(self):
        for why, (files, broken) in LINE_RULES.items():
            with self.subTest(why):
                self.assertEqual(line_rule_hits(files(), broken), [], why)

    def test_each_rule_sees_the_shape_it_is_for(self):
        shapes = {"flock": 'flock -x 9 "$lock"', "EXIT": "trap cleanup EXIT", "JSON": 'echo "$out" | jq -r .name',
                  "tailscale": "tailscale up --authkey=$KEY", "rsync": 'rsync -a "$src" "$host:$dst"',
                  "case pattern": 'case "$x" in $want) ;;', "hostname": 'name=$(hostname -s)'}
        for why, (_, broken) in LINE_RULES.items():
            line = next(v for k, v in shapes.items() if k in why)
            with self.subTest(why), mock.patch.object(Path, "read_text", return_value=". lib/common.sh"):
                self.assertTrue(broken(REPO / "cmd" / "x", line))


def wk_overrides():
    """Every WK_* read with a default (`${WK_X:-...}`) under wk, lib, cmd and build."""
    found = {}
    for p in under("wk", "lib", "cmd", "build")():
        for m in re.finditer(r"\$\{(WK_[A-Z_]+):-", p.read_text(errors="replace")):
            found.setdefault(m.group(1), p.relative_to(REPO).as_posix())
    return found


def documented(var, files):
    """Named in README.md, or in a comment line anywhere outside tests/."""
    word = re.compile(r"\b%s\b" % re.escape(var))
    for p in files:
        text = p.read_text(errors="replace")
        if word.search(text) if p.name == "README.md" else any(word.search(l) for l in text.splitlines() if not code(l)):
            return True
    return False


class TestEveryWkOverrideIsDocumentedOrRemoved(unittest.TestCase):
    def test_no_wk_override_is_read_with_a_default_and_never_explained(self):
        files = [p for p in repo_files() if p.relative_to(REPO).parts[0] != "tests"]
        self.assertEqual(sorted("%s (read in %s)" % (v, where) for v, where in wk_overrides().items()
                                if v not in CONF_ENV.values() and not documented(v, files)), [])


class TestParsing(WkTest):
    def test_every_shell_file_parses_under_bash5_and_bash32(self):
        bad = []
        bash32 = Path("/bin/bash")
        interpreters = [str(bash32)] if bash32.exists() else []
        interpreters.append("bash")
        for f in shell_files():
            for interp in interpreters:
                cp = subprocess.run([interp, "-n", str(f)], capture_output=True, text=True)
                if cp.returncode != 0:
                    bad.append(f"{interp}: {f}")
        self.assertEqual(bad, [], f"does not parse: {bad}")

    def test_no_duplicate_function_definitions(self):
        dups = []
        for f in shell_files():
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            seen = collections.Counter(re.findall(r"(?m)^([a-zA-Z_][a-zA-Z0-9_]*)\(\)", text))
            twice = [n for n, c in seen.items() if c > 1]
            if twice:
                dups.append(f"{f.name}: {' '.join(twice)}")
        self.assertEqual(dups, [], f"defined twice in one file: {dups}")


class TestMachineRegistry(WkTest):
    def test_machine_registry_every_machine_is_a_conf(self):
        env = {"XDG_CONFIG_HOME": str(self.tmp / "no-config")}
        names = fleet.Fleet(REPO, env).names(fleet.BENCH_KINDS)
        self.assertTrue(names, "no bench machines at all")
        bad = []
        for n in names:
            conf = cli.load_conf(REPO, n, env)
            if conf is None:
                bad.append("%s: no driver or note" % n)
                continue
            if conf["driver"] not in boot.drivers():
                bad.append("%s: driver %s missing" % (n, conf["driver"]))
            if conf["role"] not in ("workstation", "bench-device"):
                bad.append("%s: bad role" % n)
            if conf["os"] not in ("any", "macos", "linux"):
                bad.append("%s: bad os" % n)
            if not conf.get("image_preset"):
                bad.append("%s: no image_preset" % n)
            elif conf["os"] != "macos" and images.quiet_load(conf["image_preset"]) is None:
                bad.append("%s: image_preset '%s' does not resolve" % (n, conf["image_preset"]))
        self.assertEqual(bad, [], "machine confs that do not stand alone")
        listed = sorted(line.split()[0] for line in cli.listing(REPO, env).splitlines())
        self.assertEqual(listed, sorted(names), "wk boot's listing and machines/ disagree")


class TestBridgeDeclarations(WkTest):
    def test_bridge_kconfig_delta_is_declarable(self):
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            p = images.quiet_load(prof)
            if p is None or not p["PMO_KCONFIG"]:
                continue
            if "/" not in p["PMO_KERNEL_APORT"]:
                bad.append("%s: PMO_KCONFIG with no PMO_KERNEL_APORT path" % prof)
            bad += ["%s: '%s' malformed" % (prof, o) for o in p["PMO_KCONFIG"].split()
                    if not re.match(r"^CONFIG_\w+=[ymn]$", o)]
        self.assertEqual(bad, [], "kernel config deltas that would fail mid-build")

    def test_bridge_image_carries_role_packages(self):
        provision = (REPO / "bridge" / "provision.sh").read_text(errors="replace")
        req_m = re.search(r'(?m)^REQUIRED="(.*)"$', provision)
        self.assertIsNotNone(req_m, "could not read the package lists out of bridge/provision.sh")
        required = req_m.group(1).split()
        bridges = [p for p in map(images.load, images.names()) if p["IMG_BUILDER"] == "pmos"]
        self.assertTrue(bridges, "no pmos image preset in image/presets")
        for p in bridges:
            missing = [r for r in required if r not in p["PMO_PACKAGES"].split(",")]
            self.assertEqual(missing, [], f"bridge/provision.sh needs these and {p['IMG_PRESET']} does not carry them: {missing}")


class TestBuildLocations(WkTest):
    def test_gc_searches_every_build_location(self):
        builders = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        declared = set(gc.build_outputs(Store({"WK_STORE": "/s"}), {"WK_STORE": "/s"}))
        self.assertEqual(sorted(b for b in builders if b and b not in declared), [])

    def test_buildroot_external_exists(self):
        d = REPO / "image" / "buildroot" / "external"
        users = [f for f in (REPO / "image" / "presets").glob("*.conf")
                 if re.search(r"(?m)^BR_EXTERNAL=1", f.read_text(errors="replace"))]
        if not users:
            self.skipTest("no configuration sets BR_EXTERNAL=1")
        bad = []
        for f in ("external.desc", "external.mk", "Config.in"):
            if not (d / f).exists():
                bad.append(f"{len(users)} configuration(s) set BR_EXTERNAL=1 and {d}/{f} does not exist")
        self.assertEqual(bad, [], "; ".join(bad))
        self.assertRegex((d / "external.desc").read_text(), r"(?m)^name: ")

    def test_card_helper_gate(self):
        f = REPO / "admin" / "wk-card-priv"
        self.assertTrue(f.exists(), f"no card helper at {f}")
        text = f.read_text(errors="replace")
        bad = []
        for v in ("v_check", "v_write", "v_fleet", "v_joins", "v_tailnet", "v_identity", "v_grow", "v_eject"):
            m = re.search(rf"(?ms)^{v}\(\) \{{.*?^\}}", text)
            if not m or "gate " not in m.group(0):
                bad.append(f"{v} does not call gate")
        gate_m = re.search(r"(?ms)^gate\(\).*?^\}", text)
        gate_body = gate_m.group(0) if gate_m else ""
        if "usb|mmc" not in gate_body:
            bad.append("gate no longer restricts the transport to usb and mmc")
        if "booted_disks" not in gate_body:
            bad.append("gate no longer checks whether the machine is running from the disk")
        case_m = re.search(r'(?ms)^case "\$verb" in.*?^esac', text)
        if not case_m or "*) fail" not in case_m.group(0):
            bad.append("the verb dispatcher has a default that is not a refusal")
        install_sh = (REPO / "admin" / "install.sh").read_text(errors="replace")
        if 'install -o root -m 0755 "$src" "$tgt"' not in install_sh:
            bad.append("admin/install.sh does not install the card helper root-owned")
        self.assertEqual(bad, [], "; ".join(bad))


class TestSshJumpHosts(WkTest):
    @staticmethod
    def _no_batchmode_by_design(conf):
        allowed, hosts = set(), []
        for line in conf.read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            if parts[0].lower() == "host":
                hosts = parts[1:]
            elif parts[0].lower() == "batchmode" and parts[1:2] == ["no"]:
                allowed.update(hosts)
        return allowed

    def test_ssh_jump_hosts_are_bounded(self):
        if not Path(SYSTEM_SSH).exists():
            self.skipTest(f"no {SYSTEM_SSH} on this machine")
        conf = REPO / "dotfiles" / "ssh" / "config"
        cp = subprocess.run(["awk", '$1 == "ProxyJump" { print $2 }', str(conf)], capture_output=True, text=True)
        jumps = set()
        for tok in cp.stdout.split(","):
            for j in tok.split():
                j = re.sub(r".*@", "", j).strip()
                j = re.sub(r":\d*$", "", j)
                if j:
                    jumps.add(j)
        bad = []
        for j in sorted(jumps):
            cp2 = subprocess.run([SYSTEM_SSH, "-G", "-F", str(conf), j], capture_output=True, text=True)
            opts = dict((line.split() + [""])[:2] for line in cp2.stdout.splitlines() if line.strip())
            mode, timeout, batch = (opts.get(k, "") for k in ("stricthostkeychecking", "connecttimeout", "batchmode"))
            if mode not in ("accept-new", "no", "false", "off"):
                bad.append(f"{j}: StrictHostKeyChecking is '{mode or 'unset'}'")
            if timeout in ("", "none", "0"):
                bad.append(f"{j}: no ConnectTimeout")
            if batch != "yes" and j not in self._no_batchmode_by_design(conf):
                bad.append(f"{j}: BatchMode is '{batch or 'unset'}', so the hop "
                           f"can ask for a password inside a probe")
        self.assertEqual(bad, [], "; ".join(bad))


if __name__ == "__main__":
    unittest.main()
