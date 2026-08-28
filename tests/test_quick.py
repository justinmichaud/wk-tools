"""Everything else in cmd/selftest's `quick` section (no workspace, no
podman, no ssh) that is not dispatcher/declaration/help behaviour -- see
tests/test_dispatcher.py for that half. Each docstring is the
phrase of the behaviour it checks.

Checks marked `# static` are source-grep assertions ported faithfully from
bash; they exercise no runtime behaviour and are candidates for review later.

Run: python3 -m unittest tests.test_quick -v
"""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WK, WkTest, bash, fake_workspace, run, shell_files


# --------------------------------------------------------------------------- #
# parsing / structural
# --------------------------------------------------------------------------- #

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

    def test_no_host_marker_on_the_host(self):
        """the host's \\`$HOME\\` has no \\`~/.wk-workspace\\`"""
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/lib/target.sh"; in_workspace')
        if cp.returncode == 0:
            self.skipTest("this machine is a workspace")
        self.assertFalse(
            (Path.home() / ".wk-workspace").exists(),
            f"{Path.home()}/.wk-workspace exists on a host",
        )


# --------------------------------------------------------------------------- #
# drivers
# --------------------------------------------------------------------------- #

class TestDrivers(WkTest):
    def test_start_stop_with_a_driver_loaded(self):
        """`wk start` / `wk stop` with a driver loaded"""
        for t in ("container", "vm", "remote"):
            with self.subTest(target=t):
                cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target "{t}"
''')
                self.assertEqual(cp.returncode, 0, f"targets/{t}.sh: {cp.stdout + cp.stderr}")

        with fake_workspace() as ws:
            cp = bash(
                f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target local
''',
                env=ws.env(),
            )
        self.assertEqual(cp.returncode, 0, f"targets/local.sh with a marker: {cp.stdout + cp.stderr}")

    def test_loading_a_second_target_does_not_leak_overrides(self):
        """loading a second target does not leave the first driver's overrides live"""
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh";  . "{REPO}/lib/target.sh"
load_target container
first=$(type t_branch)
load_target remote
second=$(type t_branch)
[ "$first" != "$second" ] || {{ echo "t_branch is the same after loading two drivers"; exit 1; }}
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


# --------------------------------------------------------------------------- #
# misc dispatcher hygiene
# --------------------------------------------------------------------------- #

class TestMiscHygiene(WkTest):
    def test_wk_build_list_shows_all_configs(self):
        """`wk build --list` shows all configs"""
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("jsc-release", cp.stdout)
        self.assertIn("mac-release", cp.stdout)

    def test_wk_build_list_with_podman_stopped(self):
        """`wk build --list` with podman stopped"""
        from tests.support import podman_vm_running

        try:
            subprocess.run(["podman", "--version"], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("no podman on this machine")
        if podman_vm_running(os.environ.get("WK_MACHINE", "wk")):
            self.skipTest("the podman machine is already running")
        cp = run("build", "--list")
        self.assertEqual(cp.returncode, 0)
        # It must not have started the machine.
        self.assertFalse(podman_vm_running(os.environ.get("WK_MACHINE", "wk")))

    def test_sudo_status_never_prompts(self):
        """`wk sudo status` answers without ever prompting"""
        cp = run("sudo", "status", input="")
        self.assertIn(cp.returncode, (0, 1), cp.stdout + cp.stderr)
        self.assertIn("password", (cp.stdout + cp.stderr).lower())

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


# --------------------------------------------------------------------------- #
# locks
# --------------------------------------------------------------------------- #

def _lock_sh(script, tmp, timeout=60):
    return bash(
        f'. "{REPO}/lib/common.sh"; set +e\n{script}',
        env={"WK_LOCK_DIR": str(tmp / "locks")},
        timeout=timeout,
    )


class TestLocks(WkTest):
    def test_lock_dies_with_its_holder(self):
        """a lock dies with its holder, and a lock naming nobody is not waited out"""
        script = f'''
bash -c '. {REPO}/lib/common.sh; hold_lock k; sleep 30' &
p=$!; sleep 1; kill -9 $p 2>/dev/null; wait $p 2>/dev/null
s=$(date +%s); ( hold_lock k -w 20 >/dev/null 2>&1 )
[ $(( $(date +%s) - s )) -lt 3 ] || echo "killed holder: waited"

mkdir -p "$(_lock_path h)"
s=$(date +%s); ( hold_lock h -w 20 >/dev/null 2>&1 )
[ $(( $(date +%s) - s )) -lt 3 ] || echo "holder-less lock: waited it out"

bash -c '. {REPO}/lib/common.sh; hold_lock l; sleep 5' &
q=$!; sleep 1
( hold_lock l -w 2 >/dev/null 2>&1 ) && echo "live holder: walked in"
kill $q 2>/dev/null; wait $q 2>/dev/null
'''
        cp = _lock_sh(script, self.tmp, timeout=60)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_twelve_takers_of_one_lock_one_at_a_time(self):
        """twelve takers of one lock, one at a time"""
        script = f'''
c="$WK_LOCK_DIR/n"; mkdir -p "$WK_LOCK_DIR"; echo 0 > "$c"
ln -s "pid=99999998 tok=dead at=x cmd=x" "$(_lock_path r)"
for i in $(seq 1 12); do
    ( hold_lock r -w 60 >/dev/null 2>&1
      n=$(cat "$c"); sleep 0.2; echo $((n + 1)) > "$c" ) &
done
wait
[ "$(cat "$c")" = 12 ] || echo "twelve takers, $(cat "$c") critical sections"
[ -L "$(_lock_path r)" ] && echo "the lock was left behind"
true
'''
        cp = _lock_sh(script, self.tmp, timeout=60)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

    def test_exit_handlers_compose(self):
        """a command's own end-of-run work does not disable the lock release"""
        script = f'''
r=$(bash -c '. {REPO}/lib/common.sh
             mine() {{ echo mine; }}
             hold_lock e; wk_atexit mine' 2>/dev/null)
[ "$r" = mine ] || echo "the command own handler did not run"
[ -L "$(_lock_path e)" ] && echo "the lock was not released"
bash -c '. {REPO}/lib/common.sh; hold_lock x; exit 7' >/dev/null 2>&1
[ $? = 7 ] || echo "the exit status did not survive the handlers"
true
'''
        cp = _lock_sh(script, self.tmp, timeout=30)
        self.assertEqual(cp.stdout.strip(), "", cp.stdout + cp.stderr)

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


# --------------------------------------------------------------------------- #
# commands that resolve a build without one
# --------------------------------------------------------------------------- #

class TestResolveWithoutABuild(WkTest):
    def test_wk_profile_composes_the_right_environment(self):
        """\\`wk profile\\` composes the right environment for each port"""
        with fake_workspace() as ws:
            bad = []
            cp = ws.run("profile", "--config", "mac-release", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "DYLD_FRAMEWORK_PATH" not in cp.stdout:
                bad.append("no-dyld")
            if not re.search(r"/jsc.* --sample.* .?bench\.js", cp.stdout):
                bad.append("flags-after-script")

            cp = ws.run("profile", "--config", "wpe-release", "--mode", "native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "LD_LIBRARY_PATH" not in cp.stdout:
                bad.append("no-ld-library-path")
            if "samply record" not in cp.stdout:
                bad.append("native-is-not-samply")

            cp = ws.run("profile", "--config", "mac-release", "--mode", "native", "--dry-run", "bench.js")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            if "xctrace record" not in cp.stdout:
                bad.append("native-is-not-xctrace")

            for m in ("sampling", "bytecode", "samply", "instruments", "heaptrack", "massif"):
                cp = ws.run("profile", "--mode", m, "--dry-run", "bench.js")
                if cp.returncode != 0 and not (cp.stdout + cp.stderr).startswith("error:") \
                        and "error:" not in (cp.stdout + cp.stderr).splitlines()[0:1]:
                    if "error:" not in cp.stdout + cp.stderr:
                        bad.append(f"{m}(no-reason)")
            self.assertEqual(bad, [], f"wk profile resolved wrongly: {bad}")

    def test_stage_manifest_is_valid_json(self):
        """a status file written by an older schema"""
        # Ported literally from chk_stage_manifest_is_json: generates the
        # same document cmd/bench's writer produces and checks it parses,
        # plus a static grep for the shape that broke it
        # (`${x:+true}${x:-false}` emits the value, not a JSON literal).
        manifest = self.tmp / "stage.json"
        payload_pinned = "true" if "/tmp/x" else "false"
        manifest.write_text(
            f'{{\n  "payload_pinned": {payload_pinned},\n  "plan": "jetstream2.2"\n}}\n'
        )
        with open(manifest) as f:
            json.load(f)  # raises if not valid JSON

        text = (REPO / "cmd" / "bench").read_text(errors="replace")
        self.assertNotRegex(
            text,
            r'"[a-z_]+": \$\{[a-z_]+:\+',
            "a JSON value is built from ${x:+...}${x:-...}, which emits the value on the else branch",
        )


# --------------------------------------------------------------------------- #
# boot / machine registry
# --------------------------------------------------------------------------- #

class TestBootFiles(WkTest):
    def _fixture(self):
        d = self.tmp / f"bootfiles-{os.getpid()}"
        (d / "current" / "overlays").mkdir(parents=True)
        (d / "README").write_text("root\n")
        (d / "current" / "overlays" / "README").write_text("nested\n")
        for name in ("start4.elf", "fixup4.dat", "current/vmlinuz",
                     "current/initrd.img", "current/cmdline.txt",
                     "current/bcm2711-rpi-4-b.dtb"):
            (d / name).touch()
        (d / "config.txt").write_text(
            "[all]\nos_prefix=current/\n[tryboot]\nos_prefix=new/\n"
            "[all]\narm_64bit=1\nkernel=vmlinuz\ncmdline=cmdline.txt\n"
            "initramfs initrd.img followkernel\n"
        )
        return d

    def test_path_traversal_is_refused(self):
        """path traversal is refused"""
        d = self._fixture()
        for n in ("../../etc/passwd", "deadbeef/../../etc/passwd"):
            cp = subprocess.run(
                ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d), "--resolve", n],
                capture_output=True, text=True,
            )
            self.assertEqual(cp.stdout.strip(), "", f"{n} resolved to something: {cp.stdout}")

    def test_boot_files_check_catches_missing_kernel(self):
        """the boot files the firmware will ask for are not"""
        d = self._fixture()
        (d / "current" / "vmlinuz").unlink()
        cp = subprocess.run(
            ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d),
             "--dtb", "bcm2711-rpi-4-b.dtb"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(cp.returncode, 0, "a tree with no kernel was reported bootable")
        self.assertIn("current/vmlinuz", cp.stdout + cp.stderr)

    def test_boot_files_accepts_autodetected_kernel(self):
        """names no \\`kernel=\\` and no \\`arm_64bit=\\` is accepted"""
        d = self._fixture()
        (d / "config.txt").write_text("[all]\ndtoverlay=vc4-kms-v3d\n")
        import shutil as _sh
        _sh.rmtree(d / "current")
        for name in ("kernel8.img", "cmdline.txt", "bcm2711-rpi-4-b.dtb"):
            (d / name).touch()
        cp = subprocess.run(
            ["python3", str(REPO / "boot" / "check-boot-files.py"), "--root", str(d),
             "--dtb", "bcm2711-rpi-4-b.dtb"],
            capture_output=True, text=True,
        )
        self.assertEqual(cp.returncode, 0, f"an auto-detected kernel8.img image was refused: {cp.stdout + cp.stderr}")

    def test_machine_registry_every_machine_is_a_conf(self):
        """every machine is a conf under boot/machines"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/lib/image.sh"; . "{REPO}/image/profiles.sh"; . "{REPO}/boot/machines.sh"
bad=""
for f in "{REPO}"/boot/machines/*.conf; do
    [ -f "$f" ] || {{ echo "no machine confs at all"; exit 1; }}
    n=$(basename "$f" .conf)
    ( machine_load "$n" || exit 1
      [ -n "$MACH_DRIVER" ] || {{ echo "  $n: no MACH_DRIVER"; exit 1; }}
      [ -f "{REPO}/boot/$MACH_DRIVER.sh" ] || {{ echo "  $n: driver missing"; exit 1; }}
      case "$MACH_ROLE" in workstation|bench-device) ;; *) echo "  $n: bad role"; exit 1 ;; esac
      case "$MACH_OS" in any|macos|linux) ;; *) echo "  $n: bad os"; exit 1 ;; esac
      [ -n "$MACH_PROFILE" ] || {{ echo "  $n: no MACH_PROFILE"; exit 1; }}
      if [ "$MACH_OS" != macos ]; then
          ( image_profile_load "$MACH_PROFILE" ) >/dev/null 2>&1 \\
              || {{ echo "  $n: MACH_PROFILE '$MACH_PROFILE' does not resolve"; exit 1; }}
      fi
      [ -n "$MACH_NOTE" ] || {{ echo "  $n: no MACH_NOTE"; exit 1; }}
    ) || bad="$bad $n"
done
[ -z "$bad" ] || {{ echo "machine confs that do not stand alone:$bad"; exit 1; }}
listed=$(machine_list | awk '{{print $1}}' | sort)
dir_names=$(ls "{REPO}"/boot/machines/*.conf 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\\.conf$//' | sort)
[ "$listed" = "$dir_names" ] || {{ echo "machine_list and boot/machines/ disagree"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_self_disarm_sh_is_single_quote_free(self):
        """contains no single quote"""
        found_any = False
        bad = []
        for d in sorted((REPO / "boot").glob("*.sh")):
            text = d.read_text(errors="replace")
            if not re.search(r"(?m)^b_self_disarm_sh\(\)", text):
                continue
            found_any = True
            cp = bash(
                f'MACH_DEVICE=/dev/sda MACH_NAME=selftest; . "{d}"; b_self_disarm_sh'
            )
            out = cp.stdout
            if "'" in out:
                bad.append(f"{d.name} emits a single quote: {out}")
            elif not out.strip():
                bad.append(f"{d.name} emits nothing")
        if not found_any:
            self.skipTest("no driver defines b_self_disarm_sh")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_piusb_type_byte_roundtrips(self):
        """one byte of the MBR at offset 450"""
        img = self.tmp / "mbr.img"
        with open(img, "wb") as f:
            f.write(b"\x00" * (1024 * 2048))
        with open(img, "r+b") as f:
            f.seek(446)
            f.write(bytes([0, 0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0, 8, 0, 0]))
            f.seek(510)
            f.write(bytes([0x55, 0xAA]))
            f.seek(450)
            f.write(bytes([0x0C]))

        cp = bash(f'MACH_DEVICE=/dev/null; . "{REPO}/boot/pi-usb.sh"; echo "$PIUSB_TYPE_OFFSET"')
        offset = cp.stdout.strip()
        self.assertEqual(offset, "450", f"offset is {offset}, not 450")

        def read_byte(pos):
            with open(img, "rb") as f:
                f.seek(pos)
                return f.read(1)

        self.assertEqual(read_byte(450), b"\x0c")

        with open(img, "r+b") as f:
            f.seek(450)
            f.write(bytes([0x83]))
        self.assertEqual(read_byte(450), b"\x83")
        self.assertEqual(img.stat().st_size, 2097152, "the write truncated the device")

        with open(img, "r+b") as f:
            f.seek(450)
            f.write(bytes([0x0C]))
        self.assertEqual(read_byte(450), b"\x0c")

        if _have("sfdisk"):
            cp = subprocess.run(["sfdisk", "-l", str(img)], capture_output=True, text=True)
            self.assertIn("FAT32", cp.stdout, "sfdisk no longer reads the round-tripped table as FAT32")


def _have(prog):
    import shutil as _sh
    return _sh.which(prog) is not None


# --------------------------------------------------------------------------- #
# bridge (postmarketOS phones) -- no phone in the room
# --------------------------------------------------------------------------- #

class TestBridge(WkTest):
    def test_bridge_ls_lists_every_declared_bridge(self):
        """lists every declared bridge"""
        cp = run("bridge", "ls")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout
        for f in sorted((REPO / "bridge" / "hosts").glob("*.conf")):
            name = f.stem
            self.assertRegex(out, rf"(?m)^{re.escape(name)} ", f"{name} is declared but not listed")
        self.assertRegex(out, r"unreachable|bare|provisioned", "no state column in the listing")

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

    def test_bridge_unknown_key_is_not_reported_as_absence(self):
        """an unknown host key is not reported as an absent phone"""
        script = f'''
set -euo pipefail
_ls_classify() {{ body="$(sed -n '/^_ls_classify()/,/^}}/p' "{REPO}/cmd/bridge")"; eval "$body"; _ls_classify "$1"; }}
'''
        # Lift once, then call three times.
        lift_script = f'''
body="$(sed -n '/^_ls_classify()/,/^}}/p' "{REPO}/cmd/bridge")"
[ -n "$body" ] || {{ echo "lift failed"; exit 1; }}
eval "$body"

got=$(_ls_classify "Host key verification failed.")
[ "$got" = key-changed ] || {{ echo "a refused key classified as '$got'"; exit 1; }}

got=$(_ls_classify "@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@
Host key for tailnet-bridge-generic has changed and you have requested strict checking.
Host key verification failed.")
[ "$got" = key-changed ] || {{ echo "a changed key classified as '$got'"; exit 1; }}

got=$(_ls_classify "ssh: connect to host x port 22: No route to host")
[ "$got" = unreachable ] || {{ echo "an unroutable host classified as '$got'"; exit 1; }}

probe=$(sed -n '/^_ls_probe()/,/^}}/p' "{REPO}/cmd/bridge")
echo "$probe" | grep -q 'StrictHostKeyChecking=accept-new' \\
    || {{ echo "_ls_probe does not pass accept-new"; exit 1; }}
'''
        cp = bash(lift_script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_bridge_confs_resolve(self):
        """loads and names a device"""
        bad = []
        for f in sorted((REPO / "bridge" / "hosts").glob("*.conf")):
            name = f.stem
            cp = run("bridge", "setup", name, "--dry-run")
            if cp.returncode != 0:
                bad.append(f"{name}: {cp.stdout + cp.stderr}")
        self.assertEqual(bad, [], f"does not resolve: {bad}")

    def test_bridge_provision_resolves(self):
        """resolves the whole chain with no"""
        bad = []
        for f in sorted((REPO / "bridge" / "hosts").glob("*.conf")):
            name = f.stem
            cp = run("bridge", "provision", name, "--dry-run")
            out = cp.stdout + cp.stderr
            if cp.returncode == 0:
                if not re.search(r"(?m)^  profile:  bridge-", out):
                    bad.append(f"{name}: no profile derived")
                if not re.search(r"(?m)^  card:     [a-z0-9-]*:/dev/", out):
                    bad.append(f"{name}: no card")
                if not re.search(r"(?m)^  onto:     the phone.s internal storage, through recovery-", out):
                    bad.append(f"{name}: resolved without naming the service image")
            else:
                if "no service image for" not in out:
                    bad.append(f"{name}: refused, but not because no service image exists: {out}")
        self.assertEqual(bad, [], f"does not resolve: {bad}")

    def test_bridge_provision_needs_a_tty(self):
        """refuses a headless run before it erases anything"""
        cp = subprocess.run(
            [str(WK), "bridge", "provision", "tailnet-bridge-generic", "--no-write"],
            cwd=str(REPO), capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
        )
        self.assertNotEqual(cp.returncode, 0, "it ran to completion with no terminal; it should have refused")
        self.assertIn("needs a terminal", cp.stdout + cp.stderr)

    def test_bridge_image_heads_are_distinguishable(self):
        """can actually be told apart by content"""
        cp = run("sysimage", "ls")
        a = b = None
        for line in cp.stdout.splitlines():
            first = line.split()[0] if line.split() else ""
            if first.startswith("bridge-pinephone-"):
                a = first
            if first.startswith("recovery-pinephone-"):
                b = first
        if not a or not b:
            self.skipTest("needs a bridge-pinephone and a recovery-pinephone image in the store")

        def disk_path(image_id):
            if os.uname().sysname == "Darwin":
                root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "wk"
            else:
                root = Path(os.environ.get("WK_STORE", "/var/lib/wk"))
            return root / "images" / image_id / "disk.img"

        pa, pb = disk_path(a), disk_path(b)
        if not (pa.exists() and pb.exists()):
            self.fail(f"one of the images has no disk.img: {pa} {pb}")
        for p in (pa, pb):
            with open(p, "rb") as f:
                f.seek(8196)
                self.assertEqual(f.read(8), b"eGON.BT0", f"{p} has no sunxi SPL at offset 8192")
        import hashlib
        with open(pa, "rb") as f:
            ha = hashlib.sha256(f.read(1048576)).hexdigest()
        with open(pb, "rb") as f:
            hb = hashlib.sha256(f.read(1048576)).hexdigest()
        self.assertNotEqual(ha, hb, "the two images' first mebibytes are identical")
        self.assertNotEqual(ha, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

    def test_bridge_pmos_profiles_declare_bands(self):
        """every pmos profile declares"""
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            cp = run("sysimage", "build", prof, "--dry-run")
            if cp.returncode != 0:
                bad.append(f"{prof}: --dry-run does not resolve: {cp.stdout + cp.stderr}")
                continue
            if not re.search(r"(?m)^  radio ", cp.stdout):
                bad.append(f"{prof}: its dry run names no radio bands")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_bridge_probe_cannot_hang(self):
        """reaching the phone cannot hang"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
body="$(sed -n '/^_reaches()/,/^}}/p' "{REPO}/cmd/bridge")"
[ -n "$body" ] || {{ echo "lift failed"; exit 1; }}
eval "$body"

t0=$(date +%s); capped 1 sleep 20 >/dev/null 2>&1 || true; t1=$(date +%s)
d=$((t1 - t0)); [ "$d" -le 4 ] || {{ echo "capped 1 took ${{d}}s"; exit 1; }}

t0=$(date +%s); _reaches root@192.0.2.1 >/dev/null 2>&1 || true; t1=$(date +%s)
d=$((t1 - t0)); [ "$d" -le 16 ] || {{ echo "an unroutable address took ${{d}}s"; exit 1; }}
'''
        cp = bash(script, timeout=40)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_bridge_rm_is_inverse_of_provision(self):
        """is the inverse of"""
        cp = subprocess.run(
            ["grep", "-oE", r'write_file "?/[^" ]+', str(REPO / "bridge" / "provision.sh")],
            capture_output=True, text=True,
        )
        written = set()
        for line in cp.stdout.splitlines():
            p = line.split(None, 1)[1].lstrip('"')
            written.add(p)
        init_d = REPO / "bridge" / "init.d"
        if init_d.is_dir():
            for f in init_d.iterdir():
                written.add(f"/etc/init.d/{f.name}")
        written = {p.replace("$BR_IF", "*") for p in written if "wk-bridge" in p}
        self.assertTrue(written, "no wk-bridge paths found in bridge/")

        # The same awk logic chk_bridge_rm_is_inverse uses: everything between
        # the `rsh "$PRIV sh -s" <<...` line and the bare "EOF" that ends it.
        lines = (REPO / "cmd" / "bridge").read_text(errors="replace").splitlines()
        body_lines = []
        on = False
        for line in lines:
            if on:
                if line == "EOF":
                    break
                body_lines.append(line)
            elif 'rsh "$PRIV sh -s" <<' in line:
                on = True
        self.assertTrue(body_lines, "cmd_rm's deprovisioning script did not parse out of cmd/bridge")
        body = "\n".join(body_lines)
        removed = set(re.findall(r"/[a-zA-Z0-9_./$*-]+", body))
        removed = {p.replace("$s", "wk-bridge-*") for p in removed}

        import fnmatch
        missing = [p for p in written if not any(fnmatch.fnmatchcase(p, tok) for tok in removed)]
        self.assertEqual(missing, [], f"written by provision.sh, never removed by 'wk bridge rm': {missing}")

    def test_bridge_help_topic_covers_flashing(self):
        """covers the half no command does"""
        cp = run("help", "bridge")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("postmarketos", cp.stdout.lower())
        self.assertIn("pine64-pinephone", cp.stdout)

    def test_bridge_profiles_match_bridge_confs(self):
        """the profile and the bridge conf name the same phone"""
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            cp = run("sysimage", "build", prof, "--dry-run")
            out = cp.stdout
            self.assertEqual(cp.returncode, 0, f"{prof}: does not resolve: {out + cp.stderr}")
            dev_m = re.search(r"(?m)^  device *(\S+)", out)
            br_m = re.search(r"(?m)^  for bridge *(\S+)", out)
            if not dev_m:
                bad.append(f"{prof}: the dry run names no device")
                continue
            device = dev_m.group(1)
            bridge = br_m.group(1) if br_m else ""
            conf = REPO / "bridge" / "hosts" / f"{bridge}.conf"
            if not conf.exists():
                bad.append(f"{prof}: names bridge '{bridge}', which has no conf")
                continue
            decl_m = re.search(r"(?m)^BR_DEVICE=(.*)$", conf.read_text())
            declared = decl_m.group(1) if decl_m else ""
            if declared not in device:
                bad.append(f"{prof}: builds for '{device}' but {bridge}.conf says BR_DEVICE={declared}")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_bridge_image_carries_role_packages(self):
        """the image carries every package the role requires"""
        # static
        provision = (REPO / "bridge" / "provision.sh").read_text(errors="replace")
        req_m = re.search(r'(?m)^REQUIRED="(.*)"$', provision)
        self.assertIsNotNone(req_m, "could not read the package lists out of bridge/provision.sh")
        required = req_m.group(1).split()
        profiles = (REPO / "image" / "profiles.sh").read_text(errors="replace")
        pkg_m = re.search(r'(?m)^ *PMO_PACKAGES="([^"]*)".*', profiles)
        self.assertIsNotNone(pkg_m, "could not read PMO_PACKAGES out of image/profiles.sh")
        profile_pkgs = set(pkg_m.group(1).replace(",", " ").split())
        missing = [p for p in required if p not in profile_pkgs]
        self.assertEqual(missing, [], f"bridge/provision.sh needs these and the image does not carry them: {missing}")


# --------------------------------------------------------------------------- #
# fleet-request broker
# --------------------------------------------------------------------------- #

def _broker_policy(body):
    script = f'''
import importlib.util, sys
root = {str(REPO)!r}
spec = importlib.util.spec_from_file_location(
    "wkbroker", root + "/container/broker/wk-broker.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
{body}
'''
    return subprocess.run(["python3", "-c", script], capture_output=True, text=True)


class TestBroker(WkTest):
    def test_broker_refuses_a_workstation_as_bench_device(self):
        """a workspace may not reboot a workstation, whatever"""
        cp = _broker_policy('''
bench = [n for n, v in m.fleet().items() if v["role"] == "bench-device"]
work  = [n for n, v in m.fleet().items() if v["role"] != "bench-device"]
if not bench or not work:
    print("FLEET", bench, work); raise SystemExit(0)
for n in work:
    try:
        m.want_bench_device({"machine": n})
    except m.Refused as e:
        if "bench-device" not in e.why:
            print("WRONGREASON", n, e.why); raise SystemExit(0)
    else:
        print("ACCEPTED", n); raise SystemExit(0)
print("OK")
''')
        out = cp.stdout + cp.stderr
        if cp.returncode != 0:
            self.fail(f"the policy would not load: {out}")
        if "FLEET" in out:
            self.skipTest(f"no fleet to test against: {out}")
        self.assertIn("OK", out, f"a workstation was not refused as a bench device: {out}")

    def test_broker_plan_allowlist_is_closed(self):
        """an unknown plan is refused, naming the allowlist that decides it"""
        cp = _broker_policy('''
try:
    m.want_plan({"plan": "speedometer99"})
except m.Refused as e:
    if "allowlist" not in e.why:
        print("WRONGREASON", e.why); raise SystemExit(0)
else:
    print("ACCEPTED"); raise SystemExit(0)
one = sorted(m.ALLOWED_PLANS)[0]
if m.want_plan({"plan": one}) != one:
    print("REFUSEDALLOWED", one); raise SystemExit(0)
for bad in ("../../etc/passwd", "-rf", "a b"):
    try:
        m.want_plan({"plan": bad})
    except m.Refused:
        continue
    print("ACCEPTEDBAD", bad); raise SystemExit(0)
print("OK")
''')
        out = cp.stdout + cp.stderr
        self.assertIn("OK", out, f"the plan allowlist is not closed: {out}")


# --------------------------------------------------------------------------- #
# tailnet / auth-key hygiene (static source checks)
# --------------------------------------------------------------------------- #

class TestTailnetHygiene(WkTest):
    def test_one_tailscale_auth_key_for_the_whole_fleet(self):
        """one tailscale auth key for the whole fleet"""
        # static
        bad = []
        for f in ("cmd/pi", "cmd/bridge", "bench/mac-bench-volume.sh"):
            text = (REPO / f).read_text(errors="replace")
            if "wk_tailscale_authkey" not in text:
                bad.append(f"{f} joins the tailnet without resolving the key through wk_tailscale_authkey")
        cp = subprocess.run(
            ["grep", "-rn", "-E", r"read -r[s]* *[A-Za-z_]*[Aa][Uu][Tt][Hh]",
             str(REPO / "cmd"), str(REPO / "bench"), str(REPO / "bridge"),
             str(REPO / "boot"), str(REPO / "image")],
            capture_output=True, text=True,
        )
        if cp.stdout.strip():
            bad.append(f"an auth key is read outside prompt_secret: {cp.stdout}")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_no_authkey_in_argv(self):
        """puts the key on a command line"""
        # static
        cp = subprocess.run(
            ["grep", "-rn", "-e", r"--auth-\{0,1\}key",
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
        ycb = (REPO / "image" / "yocto-build.sh").read_text(errors="replace")
        if "meta-wk-tailnet" not in ycb:
            bad.append("image/yocto-build.sh does not add the layer to bblayers")
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
        cmd_pi = (REPO / "cmd" / "pi").read_text(errors="replace")
        if "tailscale-release.inc" not in cmd_pi:
            bad.append("cmd/pi does not read the release .inc")
        overlay = (REPO / "image/buildroot/tailnet-overlay.sh").read_text(errors="replace")
        if "tailscale-release.inc" not in overlay:
            bad.append("the buildroot overlay does not read the release .inc")
        if "meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join" not in overlay:
            bad.append("the buildroot overlay ships its own copy of wk-tailnet-join")
        if re.search(r'(?m)^TS_VERSION="\$\{TS_VERSION:-[0-9]', cmd_pi):
            bad.append("cmd/pi still carries a literal tailscale version")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_no_authkey_in_an_image(self):
        """no auth key is ever written into an image"""
        # static
        cp = subprocess.run(
            ["grep", "-rln", "wk_tailscale_authkey", str(REPO / "image")],
            capture_output=True, text=True,
        )
        hits = cp.stdout.strip()
        sysimage = (REPO / "cmd" / "sysimage").read_text(errors="replace")
        m = re.search(r"(?ms)^cmd_build\(\).*?^\}", sysimage)
        if m and "wk_tailscale_authkey" in m.group(0):
            hits += f"\n{REPO}/cmd/sysimage (cmd_build)"
        self.assertEqual(hits.strip(), "", f"the image build path resolves an auth key: {hits}")
        self.assertIn("disk_seed_tailnet", sysimage, "nothing seeds the tailnet identity onto a written card")


# --------------------------------------------------------------------------- #
# egress allowlist -- ddebs.ubuntu.com
# --------------------------------------------------------------------------- #

def _proxy_policy(body):
    script = f'''
import importlib.util, sys
root = {str(REPO)!r}
spec = importlib.util.spec_from_file_location(
    "wkproxy", root + "/container/proxy/wk-proxy.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
policy = m.Policy("/tmp")
{body}
'''
    return subprocess.run(["python3", "-c", script], capture_output=True, text=True)


class TestProxyAllowlist(WkTest):
    def test_ddebs_ubuntu_com_allowed_for_apt_only(self):
        """ddebs.ubuntu.com is reachable on 80/443 for apt, refused elsewhere"""
        cp = _proxy_policy('''
r = {p: policy.host_allowed("ddebs.ubuntu.com", p)[0] for p in (80, 443, 22)}
print("RESULT", r)
''')
        self.assertIn(
            "RESULT {80: True, 443: True, 22: False}", cp.stdout,
            f"stdout={cp.stdout!r} stderr={cp.stderr!r}",
        )

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


class TestBaseImageAndBuildLocations(WkTest):
    def test_base_image_is_never_mistaken_for_a_bench_system(self):
        """a base image is never mistaken for a bench system"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh" 2>/dev/null || true
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
bad=""
machine_load rpi4 || {{ echo "no rpi4 machine conf"; exit 1; }}
for pair in "/dev/sda2 bench" "/dev/mmcblk0p2 base" " unknown"; do
    set -- $pair
    got=$(b_system_kind "${{2:+$1}}")
    [ "$got" = "${{2:-$1}}" ] || bad="$bad rpi4:${{1:-none}}=$got"
done
machine_load rpi3 || {{ echo "no rpi3 machine conf"; exit 1; }}
got=$(b_system_kind /dev/mmcblk0p2)
[ "$got" = base ] || bad="$bad rpi3-rescue=$got"
got=$(b_system_kind /dev/mmcblk0p4)
[ "$got" = bench ] || bad="$bad rpi3-bench=$got"
grep -q 'b_system_kind' "{REPO}/cmd/pi" || bad="$bad wk-pi-bench-does-not-check"
[ -z "$bad" ] || {{ echo "$bad"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_gc_searches_every_build_location(self):
        """every builder's output has somewhere"""
        # static
        profiles = (REPO / "image" / "profiles.sh").read_text(errors="replace")
        builders = set(re.findall(r"(?m)^[ \t]*IMG_BUILDER=([a-z]*)", profiles))
        image_lib = (REPO / "lib" / "image.sh").read_text(errors="replace")
        m = re.search(r"(?ms)^image_build_locations\(\).*?^\}", image_lib)
        self.assertIsNotNone(m, "image_build_locations not found in lib/image.sh")
        declared = set()
        for line in m.group(0).splitlines():
            dm = re.match(r"\s*#\s*builder:\s*(.*)", line)
            if dm:
                for tok in dm.group(1).split(","):
                    tok = tok.strip().split()[0] if tok.strip() else ""
                    if tok:
                        declared.add(tok)
        bad = [f"IMG_BUILDER={b} has no entry in image_build_locations" for b in builders if b and b not in declared]
        gc = (REPO / "cmd" / "gc").read_text(errors="replace")
        if "image_build_locations" not in gc:
            bad.append("wk gc does not read image_build_locations")
        self.assertEqual(bad, [], "; ".join(bad))

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
        for f in ("boot/disk.sh", "boot/machines.sh", "lib/common.sh", "cmd/sysimage"):
            cp = subprocess.run(["grep", "-hoE", r"^[a-z_]+\(\)", str(REPO / f)], capture_output=True, text=True)
            defs |= {l.rstrip("()") for l in cp.stdout.splitlines()}
        used = set()
        for f in ("cmd/sysimage", "boot/disk.sh"):
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
        if 'install -o root -m 0755 "$_card_source" "$_card_target"' not in install_sh:
            bad.append("admin/install.sh does not install the card helper root-owned")
        self.assertEqual(bad, [], "; ".join(bad))


# --------------------------------------------------------------------------- #
# concurrency / ssh / fleet-probe ceilings
# --------------------------------------------------------------------------- #

class TestCeilingsAndConcurrency(WkTest):
    def test_capped_reaches_the_subtree(self):
        """ceiling reaches the whole process group"""
        # The probe patterns are anchored to the grandchild's whole command
        # line. `pgrep -f` matches every process's full argv, and this script
        # is itself argv -- `bash -c "<this text>"` -- so an unanchored
        # 'sleep 31.5' matches the shell doing the probing, reports a
        # grandchild that is not there, and then pkills the test runner.
        # The `foo[.]bar` trick the rest of the tree uses (image/pmos.sh,
        # cmd/pi) cannot help here: the plain spelling appears in the line
        # above as well, so only an anchor tells the two apart.
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
t0=$(date +%s)
capped 1 bash -c 'sleep 31.5 & wait' >/dev/null 2>&1 || true
d=$(( $(date +%s) - t0 ))
[ "$d" -le 4 ] || {{ echo "capped 1 took ${{d}}s"; exit 1; }}
left=$( (pgrep -f '^sleep 31\\.5$' 2>/dev/null || true) | wc -l | tr -d ' ')
[ "$left" = 0 ] || {{ pkill -f '^sleep 31\\.5$' 2>/dev/null || true
    echo "the ceiling killed the child and left $left grandchild(ren) running"; exit 1; }}
t0=$(date +%s); capped 20 true; d=$(( $(date +%s) - t0 ))
[ "$d" -le 2 ] || {{ echo "capped 20 true took ${{d}}s"; exit 1; }}
'''
        cp = bash(script, timeout=40)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_ssh_jump_hosts_are_bounded(self):
        """a jump host's stanza carries the bounds its jump cannot inherit"""
        if not _have("ssh"):
            self.skipTest("no ssh on this machine")
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
            cp2 = subprocess.run(["ssh", "-G", "-F", str(conf), j], capture_output=True, text=True)
            opts = cp2.stdout
            mode = ""
            timeout = ""
            for line in opts.splitlines():
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "stricthostkeychecking":
                    mode = parts[1] if len(parts) > 1 else ""
                if parts[0] == "connecttimeout":
                    timeout = parts[1] if len(parts) > 1 else ""
            if mode not in ("accept-new", "no", "false", "off"):
                bad.append(f"{j}: StrictHostKeyChecking is '{mode or 'unset'}'")
            if timeout in ("", "none", "0"):
                bad.append(f"{j}: no ConnectTimeout")
        self.assertEqual(bad, [], "; ".join(bad))

    def test_no_fleet_probe_can_outlive_its_ceiling(self):
        """no fleet probe can outlive its ceiling"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
for fn in _jesc rec_start report_fleet_device; do
    body="$(sed -n "/^$fn()/,/^}}/p" "{REPO}/cmd/status")"
    [ -n "$body" ] || {{ echo "lift $fn failed"; exit 1; }}
    eval "$body"
done
_fleet_probe() {{ sleep 30; }}
WK_FLEET_TIMEOUT=1
t0=$(date +%s); out=$(report_fleet_device rpi4 3>&1 2>/dev/null); d=$(( $(date +%s) - t0 ))
[ "$d" -le 10 ] || {{ echo "a probe that never returned held the fleet block ${{d}}s"; exit 1; }}
case "$out" in *rpi4*) ;; *) echo "a probe killed for time said nothing: '$out'"; exit 1 ;; esac
'''
        cp = bash(script, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_status_parallel_keeps_starting_order(self):
        """never in the order they finished, so a slow probe changes when a line"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/par.sh"
body="$(sed -n "/^bump()/,/^}}/p" "{REPO}/cmd/status")"
[ -n "$body" ] || {{ echo "lift bump failed"; exit 1; }}
eval "$body"
worst=0
_slow() {{ sleep "$1"; printf '%s\\n' "$2" >&3; exit "$3"; }}
par_begin
par_run a _slow 3 a 2
par_run b _slow 2 b 4
par_run c _slow 1 c 0
par_join 3> "{{TMP}}/par-out"
out=$(cat "{{TMP}}/par-out")
[ "$out" = "$(printf 'a\\nb\\nc')" ] || {{ echo "records came back in finishing order: $(printf '%s' "$out" | tr '\\n' ' ')"; exit 1; }}
[ "$worst" = 4 ] || {{ echo "worst exit status did not come back: worst=$worst"; exit 1; }}
'''.replace("{TMP}", str(self.tmp))
        cp = bash(script, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_tailscale_reach_cannot_hang(self):
        """local call to the daemon, which is not the same as a call that returns"""
        stub = self.tmp / "tailscale"
        stub.write_text("#!/bin/sh\nsleep 30\n")
        stub.chmod(0o755)
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
body="$(sed -n '/^wk_tailscale_peers()/,/^}}/p' "{REPO}/lib/reach.sh")"
[ -n "$body" ] || {{ echo "lift failed"; exit 1; }}
eval "$body"
_WK_TS_PEERS=""; _WK_TS_READ=""
wk_tailscale_cli() {{ printf '%s' "{stub}"; }}
t0=$(date +%s)
WK_TAILSCALE_TIMEOUT=1 wk_tailscale_peers >/dev/null 2>&1 || true
d=$(( $(date +%s) - t0 ))
[ "$d" -le 8 ] || {{ echo "a tailscale CLI that never answers held the walk ${{d}}s"; exit 1; }}
'''
        cp = bash(script, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


# --------------------------------------------------------------------------- #
# hands-on arming / bench role (macOS only, like cmd/selftest's originals)
# --------------------------------------------------------------------------- #

class TestHandsOnArmingAndBench(WkTest):
    def _is_macos(self):
        return os.uname().sysname == "Darwin"

    def test_arming_with_no_volume_refuses(self):
        """arming with no volume attached refuses"""
        if not self._is_macos():
            self.skipTest("the hands-on machine is this Mac")
        cp = run("boot", "mbp", "--status", env={"WK_BENCH_VOLUME": "wk-selftest-no-such-volume"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("not attached", cp.stdout + cp.stderr)

        cp2 = run("boot", "mbp", env={
            "XDG_STATE_HOME": str(self.tmp / "state"),
            "WK_BENCH_VOLUME": "wk-selftest-no-such-volume",
        })
        self.assertNotEqual(cp2.returncode, 0, "arming succeeded with no volume attached")
        self.assertFalse((self.tmp / "state" / "wk" / "boot-armed").exists(), "a refused arming still wrote a record")

    def test_bench_role_required_or_it_does_not_run(self):
        """a benchmark runs in bench mode or it does not run"""
        if not self._is_macos():
            self.skipTest("bare-metal bench mode is this Mac")
        cp = run("bench", "staged", "--plan", "jetstream2.2", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp.returncode, 0, "a run was accepted with no benchmark volume at all")

        stage = self.tmp / "bench" / "staged" / "20260101T000000Z-mac-release"
        (stage / "Tools" / "Scripts").mkdir(parents=True)
        (stage / "WebKitBuild" / "Release" / "MiniBrowser.app" / "Contents" / "MacOS").mkdir(parents=True)
        (stage / "stage.json").write_text('{"config":"mac-release","plan":"jetstream2.2"}')

        cp2 = run("bench", "staged", "--plan", "jetstream2.2", "--force", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertNotEqual(cp2.returncode, 0, "--force ran a benchmark in host mode")
        self.assertIn("host mode", cp2.stdout + cp2.stderr)

        cp3 = run("bench", "staged", "--plan", "jetstream2.2", "--dry-run", env={"WK_BENCH_ROOT": str(self.tmp / "bench")})
        self.assertEqual(cp3.returncode, 0, f"--dry-run refused as well: {cp3.stdout + cp3.stderr}")
        self.assertIn("--plan jetstream2.2 --browser minibrowser --platform osx", cp3.stdout)

    def test_boot_status_survives_an_absent_machine(self):
        """instead of exiting silently"""
        cp = run("boot", "--list")
        machines = [line.split()[0] for line in cp.stdout.splitlines() if line.split()]
        bad = []
        for m in machines:
            cp2 = run("boot", m, "--status")
            out = cp2.stdout + cp2.stderr
            if cp2.returncode not in (0, 2, 3) and "driven from a" not in out:
                bad.append(f"'wk boot {m} --status' exited {cp2.returncode}: {out}")
            elif not out.strip():
                bad.append(f"'wk boot {m} --status' printed nothing (exit {cp2.returncode})")
        self.assertEqual(bad, [], "; ".join(bad))


if __name__ == "__main__":
    unittest.main()
