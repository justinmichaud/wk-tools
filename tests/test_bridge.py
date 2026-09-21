"""`wk bridge` with no phone in the room: listing, dry runs, the probe's
classifier and the refusals, against a stub ssh that answers nothing.

Run: python3 -m unittest tests.test_bridge -v
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WK, WkTest, bash, run, stub_path


NO_ROUTE = 'echo "ssh: connect to host $* port 22: No route to host" >&2; exit 255\n'


class TestBridge(WkTest):
    def test_bridge_ls_lists_every_declared_bridge(self):
        """lists every declared bridge, and what each answers to"""
        with stub_path({"ssh": NO_ROUTE}) as binp:
            cp = run("bridge", "ls", env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout
        for f in sorted((REPO / "bridge" / "hosts").glob("*.conf")):
            name = f.stem
            self.assertRegex(out, rf"(?m)^{re.escape(name)} ", f"{name} is declared but not listed")
        self.assertRegex(out, r"unreachable|bare|provisioned", "no state column in the listing")

    def test_bridge_unknown_key_is_not_reported_as_absence(self):
        """an unknown host key is not reported as an absent phone"""
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
        """`wk bridge provision --dry-run` resolves the profile, the card and the service image for every declared bridge, or refuses because no service image exists"""
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
        """a bridge image and the recovery image for the same phone differ in content, not only in name"""
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
        """every postmarketOS profile declares the radio bands its dry run names"""
        bad = []
        for prof in ("bridge-pinephone", "bridge-librem5"):
            cp = run("sysimage", "build", prof, "--dry-run")
            if cp.returncode != 0:
                bad.append(f"{prof}: --dry-run does not resolve: {cp.stdout + cp.stderr}")
                continue
            if not re.search(r"(?m)^  radio ", cp.stdout):
                bad.append(f"{prof}: its dry run names no radio bands")
        self.assertEqual(bad, [], "; ".join(bad))

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


if __name__ == "__main__":
    unittest.main()
