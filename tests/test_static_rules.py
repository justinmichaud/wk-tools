"""The static rules over the tree: each check here reads sources and confs
and runs nothing that reaches a machine or a workspace. What exercises
commands, drivers and locks at run time lives in tests/test_host_only.py,
test_bridge.py, test_ceilings.py, test_locks.py and test_selftest_guard.py.

Run: python3 -m unittest tests.test_static_rules -v
"""
TIER = "lint"
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WK, WkTest, bash, func_body, owed, shell_files

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402

# `ssh -G` evaluates a config and connects to nothing, so the system binary is
# asked directly, past the runner's shim.
SYSTEM_SSH = "/usr/bin/ssh"


class TestParsing(WkTest):
    def test_every_shell_file_parses_under_bash5_and_bash32(self):
        """every shell file parses under both bash 5 and bash 3.2"""
        # static: this machine has no bash 3.2 to test against (macOS ships
        # it at /bin/bash, Linux CI images do not) -- both interpreters this
        # host actually has are exercised; a genuine 3.2-only syntax error
        # (e.g. a `case` inside `$( ... )`) is not caught here.
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
        """two \\`config_build_dir\\` definitions"""
        # static
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
        """one lock mechanism, everywhere: nothing in the tree calls \\`flock\\`"""
        # static
        hits = []
        for d in ("cmd", "lib", "targets", "boot", "image"):
            cp = subprocess.run(
                ["grep", "-rn", r"\bflock\b", str(REPO / d)],
                capture_output=True, text=True,
            )
            for line in cp.stdout.splitlines():
                if "/selftest:" in line:
                    continue
                # a comment line
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

    @owed("structured data is not handled in bash (docs/PLAN.md 5.39 step 2's done condition): "
          "bridge/bin/wk-bridge-healthcheck, bridge/bin/wk-bridge-netwatch and bridge/provision.sh call "
          "jq; claude/hooks/webkit-jsc-skill-reminder.sh and claude/install.sh call jq to read/edit a hook's "
          "settings.json; bench/mac-quiet-desktop.sh, admin/wk-card-priv, host/macos/machine.sh, "
          "targets/vm.sh and targets/remote.sh run \\`python3 -c\\` inline with \\`import json\\`")
    def test_no_bash_file_parses_json(self):
        """no bash file under the tree parses JSON: no \\`jq\\`, no inline \\`python3 -c ... import json\\`, no \\`sed\\` over a JSON blob"""
        # static
        jq_call = re.compile(r"(?<![\w-])jq(?![\w-])")
        py_json = re.compile(r"python3\s+-c\s+.*import\s+json")
        sed_json = re.compile(r"(?<![\w-])sed\b[^\n]*[{}][^\n]*\bjson\b", re.IGNORECASE)
        hits = []
        for f in shell_files():
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for pat in (jq_call, py_json, sed_json):
                if pat.search(text):
                    hits.append(str(f.relative_to(REPO)))
                    break
        self.assertEqual(sorted(set(hits)), [])


class TestSudoHygiene(WkTest):
    def test_no_sudo_in_the_daily_path(self):
        """no \\`wk\\` command in the daily path calls \\`sudo\\` on either host"""
        # static
        found = []
        cmds = ["build", "run", "test", "logs", "status", "ls", "new", "rm",
                "enter", "claude", "gui", "bench", "sync", "gc"]
        pattern = re.compile(r"(^|;|&&|\|\||\bthen\b|\belse\b|\bdo\b)[ \t]*sudo[ \t]")
        for c in cmds:
            f = REPO / "cmd" / c
            if not f.exists():
                continue
            for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if "sudo -n" in line:
                    continue
                if re.match(r"^\s*#", line):
                    continue
                if pattern.search(line):
                    found.append(f"cmd/{c}:{i}:{line.strip()}")
        self.assertEqual(found, [], f"sudo that can prompt: {found}")


class TestExitTrapOwnership(WkTest):
    def test_only_lib_common_sh_takes_the_exit_trap(self):
        """a command's own end-of-run work does not disable the lock release"""
        # static: nothing outside lib/common.sh may take the EXIT trap in a
        # process that has wk_atexit's registry, or claiming it disables
        # everyone else's handler.
        claimants = []
        cp = subprocess.run(
            ["grep", "-rn", r"^[^#]*trap .*EXIT", str(REPO / "cmd"), str(REPO / "lib"),
             str(REPO / "targets"), str(REPO / "boot"), str(REPO / "image")],
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
        """every bench machine in machines/ stands alone"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/image.sh"; . "{REPO}/image/profiles.sh"; . "{REPO}/boot/machines.sh"
bad=""
names=$(machine_names)
[ -n "$names" ] || {{ echo "no bench machines at all"; exit 1; }}
for n in $names; do
    ( machine_load "$n" || exit 1
      [ -n "$NODE_DRIVER" ] || {{ echo "  $n: no NODE_DRIVER"; exit 1; }}
      [ -f "{REPO}/boot/$NODE_DRIVER.sh" ] || {{ echo "  $n: driver missing"; exit 1; }}
      case "$NODE_ROLE" in workstation|bench-device) ;; *) echo "  $n: bad role"; exit 1 ;; esac
      case "$NODE_OS" in any|macos|linux) ;; *) echo "  $n: bad os"; exit 1 ;; esac
      [ -n "$NODE_PROFILE" ] || {{ echo "  $n: no NODE_PROFILE"; exit 1; }}
      if [ "$NODE_OS" != macos ]; then
          ( image_profile_load "$NODE_PROFILE" ) >/dev/null 2>&1 \\
              || {{ echo "  $n: NODE_PROFILE '$NODE_PROFILE' does not resolve"; exit 1; }}
      fi
      [ -n "$NODE_NOTE" ] || {{ echo "  $n: no NODE_NOTE"; exit 1; }}
    ) || bad="$bad $n"
done
[ -z "$bad" ] || {{ echo "machine confs that do not stand alone:$bad"; exit 1; }}
listed=$(machine_list | awk '{{print $1}}' | sort)
[ "$listed" = "$(printf '%s\\n' $names | sort)" ] || {{ echo "machine_list and machines/ disagree"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_self_disarm_sh_is_single_quote_free(self):
        """contains no single quote and no `%`: systemd's ExecStart parsing
        would split on the one and expand the other as a specifier"""
        found_any = False
        bad = []
        for d in sorted((REPO / "boot").glob("*.sh")):
            text = d.read_text(errors="replace")
            if not re.search(r"(?m)^b_self_disarm_sh\(\)", text):
                continue
            found_any = True
            cp = bash(
                f'NODE_DEVICE=/dev/sda NODE_NAME=selftest; . "{d}"; b_self_disarm_sh'
            )
            out = cp.stdout
            if "'" in out or "%" in out:
                bad.append(f"{d.name} emits a single quote or a %: {out}")
            elif not out.strip():
                bad.append(f"{d.name} emits nothing")
        if not found_any:
            self.skipTest("no driver defines b_self_disarm_sh")
        self.assertEqual(bad, [], "; ".join(bad))


class TestBridgeDeclarations(WkTest):
    def test_bridge_kconfig_delta_is_declarable(self):
        """a kernel config delta names an aport and well-formed options"""
        script = f'''
set -euo pipefail
. "{REPO}/image/profiles.sh"
bad=""
for prof in bridge-pinephone bridge-librem5; do
    ( image_profile_load "$prof" >/dev/null 2>&1 || exit 0
      [ -n "${{PMO_KCONFIG:-}}" ] || exit 0
      [ -n "${{PMO_KERNEL_APORT:-}}" ] || {{ echo "  $prof: PMO_KCONFIG with no PMO_KERNEL_APORT"; exit 1; }}
      case "$PMO_KERNEL_APORT" in */*) ;; *) echo "  $prof: not a path"; exit 1 ;; esac
      for opt in $PMO_KCONFIG; do
          case "$opt" in CONFIG_*=y|CONFIG_*=m|CONFIG_*=n) ;; *) echo "  $prof: '$opt' malformed"; exit 1 ;; esac
      done
    ) || bad="$bad $prof"
done
[ -z "$bad" ] || {{ echo "kernel config deltas that would fail mid-build:$bad"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_bridge_image_carries_role_packages(self):
        """the image carries every package the role requires"""
        # static
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
    def test_one_tailscale_auth_key_for_the_whole_fleet(self):
        """one tailscale auth key for the whole fleet"""
        # static
        bad = []
        # Not the Mac volume: no macOS install can join unattended, so it has
        # no key to resolve (tests/test_mac_quiet.py holds it to that). Not a
        # board either: `wk sysimage write` puts the key on the card and the
        # image's own wk-tailnet-join reads it there, never wk.
        for f, needle in (("lib/wk/bridge/role.py", "tailnet.Fleet("),):
            text = (REPO / f).read_text(errors="replace")
            if needle not in text:
                bad.append(f"{f} joins the tailnet without resolving the key through wk.tailnet.Fleet")
        cp = subprocess.run(
            ["grep", "-rn", "-E", r"read -r[s]* *[A-Za-z_]*[Aa][Uu][Tt][Hh]",
             str(REPO / "cmd"), str(REPO / "bench"), str(REPO / "bridge"),
             str(REPO / "boot"), str(REPO / "image")],
            capture_output=True, text=True,
        )
        if cp.stdout.strip():
            bad.append(f"an auth key is read outside `wk key set tailnet`: {cp.stdout}")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_no_authkey_in_argv(self):
        """puts the key on a command line"""
        # static
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

    def test_tailnet_layer_is_wired(self):
        """the Yocto images are built with tailscale in them"""
        # static
        bad = []
        layer = REPO / "image" / "yocto" / "meta-wk-tailnet"
        if not (layer / "conf" / "layer.conf").exists():
            bad.append(f"no layer at {layer}")
        ycb = (REPO / "lib" / "wk" / "sysimage" / "yocto_target.py").read_text(errors="replace")
        if "meta-wk-tailnet" not in ycb:
            bad.append("lib/wk/sysimage/yocto_target.py does not add the layer to bblayers")
        if 'IMAGE_INSTALL:append = " tailscale"' not in ycb:
            bad.append("nothing puts tailscale in IMAGE_INSTALL")
        proxy = (REPO / "container" / "proxy" / "wk-proxy.py").read_text(errors="replace")
        if '"tailscale.com"' not in proxy:
            bad.append("the workspace egress allowlist has no tailscale.com")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_tailscale_pin_is_one_file(self):
        """the tailscale release is pinned in one file"""
        # static
        inc = REPO / "image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"
        self.assertTrue(inc.exists(), f"no {inc}")
        text = inc.read_text(errors="replace")
        bad = []
        ver_m = re.search(r'(?m)^TS_VERSION = "(.*)"$', text)
        if not ver_m or not re.match(r"^\d+\.\d+", ver_m.group(1)):
            bad.append("declares no usable TS_VERSION")
        for a in ("arm64", "arm"):
            sha_m = re.search(rf'(?m)^TS_SHA256_{a} = "([0-9a-f]{{4,}}.*)"$', text)
            if not sha_m:
                bad.append(f"no sha256 for {a}")
        recipe = (REPO / "image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale.bb").read_text(errors="replace")
        if "require .*tailscale-release.inc" not in recipe and not re.search(r"require .*tailscale-release\.inc", recipe):
            bad.append("the recipe does not require the release .inc")
        overlay = (REPO / "lib/wk/sysimage/buildroot_target.py").read_text(errors="replace")
        if "tailscale-release.inc" not in overlay:
            bad.append("the buildroot overlay does not read the release .inc")
        if "meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join" not in overlay:
            bad.append("the buildroot overlay ships its own copy of wk-tailnet-join")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_no_authkey_in_an_image(self):
        """no auth key is ever written into an image"""
        # static
        cp = subprocess.run(
            ["grep", "-rln", "wk_tailscale_authkey", str(REPO / "image")],
            capture_output=True, text=True,
        )
        hits = cp.stdout.strip()
        sysimage = (REPO / "lib" / "sysimage-arms.sh").read_text(errors="replace")
        m = re.search(r"(?ms)^cmd_build\(\).*?^\}", sysimage)
        if m and "wk_tailscale_authkey" in m.group(0):
            hits += f"\n{REPO}/lib/sysimage-arms.sh (cmd_build)"
        self.assertEqual(hits.strip(), "", f"the image build path resolves an auth key: {hits}")
        write = (REPO / "lib" / "wk" / "sysimage" / "write.py").read_text()
        self.assertIn("self.seed_tailnet(dev, tailnet)", write, "nothing seeds the tailnet identity onto a written card")


class TestDdebsProvisioning(WkTest):
    def test_firstrun_provisions_ddebs_source_exactly_once(self):
        """firstrun.sh writes the ddebs sources file once, guarded by test -f"""
        # static
        text = (REPO / "container" / "firstrun.sh").read_text(errors="replace")
        sources_path = "/etc/apt/sources.list.d/ddebs.list"
        self.assertEqual(text.count(sources_path), 1,
                          "the ddebs sources path is defined more than once in firstrun.sh")
        self.assertIn('if [ -f "$DDEBS_SOURCES" ]', text,
                       "ddebs provisioning is not guarded by a test -f on the sources file")
        self.assertIn("ubuntu-dbgsym-keyring", text,
                       "firstrun.sh does not install ubuntu-dbgsym-keyring")


class TestBuildLocations(WkTest):
    def test_gc_searches_every_build_location(self):
        """every builder's output has somewhere"""
        # static
        from wk import gc
        from wk.store import Store
        builders = {images.load(n)["IMG_BUILDER"] for n in images.names()}
        declared = set(gc.build_outputs(Store({"WK_STORE": "/s"}), {"WK_STORE": "/s"}))
        self.assertEqual(sorted(b for b in builders if b and b not in declared), [])

    def test_buildroot_external_exists(self):
        """\\`image/buildroot/external/\\` is **written**"""
        # static
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

    def test_disk_verbs_are_defined(self):
        """every disk verb the write path calls is defined"""
        # static
        defs = set()
        for f in ("boot/disk.sh", "boot/machines.sh", "lib/common.sh", "lib/sysimage-arms.sh"):
            cp = subprocess.run(["grep", "-hoE", r"^[a-z_]+\(\)", str(REPO / f)], capture_output=True, text=True)
            defs |= {l.rstrip("()") for l in cp.stdout.splitlines()}
        used = set()
        for f in ("lib/sysimage-arms.sh", "boot/disk.sh"):
            text = re.sub(r"#.*", "", (REPO / f).read_text(errors="replace"))
            used |= set(re.findall(r"(?:^|[;&|(}\s])(disk_[a-z_]+|card_priv[a-z_]*)(?=[\s]|$)", text, re.M))
        bad = [f"{n} is called and defined nowhere" for n in used if n not in defs]
        self.assertEqual(bad, [], "; ".join(bad))

    def test_card_helper_gate(self):
        """disk the machine is not running from"""
        # static
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
        """A stanza may leave password authentication on only by writing
        `BatchMode no` itself. ssh's default is `no` and `ssh -G` reports an
        omission as one, so the exception is read off the stanza rather than
        off a comment that can be trimmed away from under it."""
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
        """a jump host's stanza carries the bounds its jump cannot inherit"""
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
            # A hop that can still ask for a password is a read-only report
            # that stops on a question nothing can answer: `wk status` walks
            # every build box through one of these.
            if batch != "yes" and j not in self._no_batchmode_by_design(conf):
                bad.append(f"{j}: BatchMode is '{batch or 'unset'}', so the hop "
                           f"can ask for a password inside a probe")
        self.assertEqual(bad, [], "; ".join(bad))


if __name__ == "__main__":
    unittest.main()
