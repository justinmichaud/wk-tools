"""The static rules over the tree: each check here reads sources and confs"""
TIER = "lint"
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WK, WkTest, shell_files

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402

SYSTEM_SSH = "/usr/bin/ssh"

JSON_PARSING_EXEMPT = {"bridge/bin/wk-bridge-netwatch"}


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
            names = re.findall(r"(?m)^([a-zA-Z_][a-zA-Z0-9_]*)\(\)", text)
            seen = {}
            for n in names:
                seen[n] = seen.get(n, 0) + 1
            twice = [n for n, c in seen.items() if c > 1]
            if twice:
                dups.append(f"{f.name}: {' '.join(twice)}")
        self.assertEqual(dups, [], f"defined twice in one file: {dups}")

    def test_one_lock_mechanism_nothing_calls_flock(self):
        hits = []
        for d in ("cmd", "lib", "boot", "image"):
            cp = subprocess.run(
                ["grep", "-rn", r"\bflock\b", str(REPO / d)],
                capture_output=True, text=True,
            )
            for line in cp.stdout.splitlines():
                if "/selftest:" in line:
                    continue
                path_rest = line.split(":", 2)
                if len(path_rest) == 3 and re.match(r"^\s*#", path_rest[2]):
                    continue
                if "# " in line:
                    continue
                hits.append(line)
        cp = subprocess.run(["grep", "-n", r"\bflock\b", str(WK)], capture_output=True, text=True)
        for line in cp.stdout.splitlines():
            if not re.match(r"^\d+:\s*#", line.split(":", 1)[1] if ":" in line else ""):
                hits.append(f"{WK}:{line}")
        self.assertEqual(hits, [], f"flock is still called here: {hits}")

    def test_no_bash_file_parses_json(self):
        jq_call = re.compile(r"""(?<![\w-])jq(?=\s+[-'"])|\|\s*jq\b""")
        py_json = re.compile(r"python3\s+-c\s+.*import\s+json")
        sed_json = re.compile(r"(?<![\w-])sed\b[^\n]*[{}][^\n]*\bjson\b", re.IGNORECASE)
        hits = []
        for f in shell_files():
            rel = str(f.relative_to(REPO))
            if rel in JSON_PARSING_EXEMPT:
                continue
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for pat in (jq_call, py_json, sed_json):
                if pat.search(text):
                    hits.append(rel)
                    break
        self.assertEqual(sorted(set(hits)), [])


class TestExitTrapOwnership(WkTest):
    def test_only_lib_common_sh_takes_the_exit_trap(self):
        claimants = []
        cp = subprocess.run(
            ["grep", "-rn", r"^[^#]*trap .*EXIT", str(REPO / "cmd"), str(REPO / "lib"),
             str(REPO / "boot"), str(REPO / "image")],
            capture_output=True, text=True,
        )
        for line in cp.stdout.splitlines():
            if "lib/common.sh" in line:
                continue
            path = line.split(":", 1)[0]
            try:
                text = Path(path).read_text(errors="replace")
            except OSError:
                continue
            if "lib/common.sh" in text:
                claimants.append(line)
        self.assertEqual(claimants, [], f"these take the EXIT trap instead of registering a handler: {claimants}")


class TestMachineRegistry(WkTest):
    def test_machine_registry_every_machine_is_a_conf(self):
        from wk import boot, fleet
        from wk.boot import cli
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
            if not conf.get("profile"):
                bad.append("%s: no profile" % n)
            elif conf["os"] != "macos" and images.quiet_load(conf["profile"]) is None:
                bad.append("%s: profile '%s' does not resolve" % (n, conf["profile"]))
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
        self.assertTrue(bridges, "no pmos profile in image/configs")
        for p in bridges:
            missing = [r for r in required if r not in p["PMO_PACKAGES"].split(",")]
            self.assertEqual(missing, [], f"bridge/provision.sh needs these and {p['IMG_PROFILE']} does not carry them: {missing}")


class TestTailnetHygiene(WkTest):

    def test_no_authkey_in_argv(self):
        cp = subprocess.run(
            ["grep", "-rnI", "-e", r"--auth-\{0,1\}key",
             str(REPO / "cmd"), str(REPO / "bench"), str(REPO / "bridge"),
             str(REPO / "boot"), str(REPO / "image"), str(REPO / "lib")],
            capture_output=True, text=True,
        )
        bad = []
        for line in cp.stdout.splitlines():
            body = line.split(":", 2)
            content = body[2] if len(body) == 3 else line
            if content.lstrip().startswith("#"):
                continue
            if "file:" not in content:
                bad.append(line)
        self.assertEqual(bad, [], f"a tailscale key is passed through argv: {bad}")


class TestBuildLocations(WkTest):
    def test_gc_searches_every_build_location(self):
        from wk import gc
        from wk.store import Store
        builders = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        declared = set(gc.build_outputs(Store({"WK_STORE": "/s"}), {"WK_STORE": "/s"}))
        self.assertEqual(sorted(b for b in builders if b and b not in declared), [])

    def test_buildroot_external_exists(self):
        d = REPO / "image" / "buildroot" / "external"
        users = [f for f in (REPO / "image" / "configs").glob("*.conf")
                 if re.search(r"(?m)^BR_EXTERNAL=1", f.read_text(errors="replace"))]
        if not users:
            self.skipTest("no configuration sets BR_EXTERNAL=1")
        bad = []
        for f in ("external.desc", "external.mk", "Config.in"):
            if not (d / f).exists():
                bad.append(f"{len(users)} configuration(s) set BR_EXTERNAL=1 and {d}/{f} does not exist")
        self.assertEqual(bad, [], "; ".join(bad))
        if not bad:
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
            opts = cp2.stdout
            mode = ""
            timeout = ""
            batch = ""
            for line in opts.splitlines():
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "stricthostkeychecking":
                    mode = parts[1] if len(parts) > 1 else ""
                if parts[0] == "connecttimeout":
                    timeout = parts[1] if len(parts) > 1 else ""
                if parts[0] == "batchmode":
                    batch = parts[1] if len(parts) > 1 else ""
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
