"""cmd/selftest's `state` section: read-only is read-only, one walk behind
both `wk ls`/`wk status`, `ws_state`'s five words, status-files-are-claims,
the wk-tools completion marker naming, one status entry per machine, and `wk
zed` refusing inside a workspace. Each docstring is the
phrase the check implements 

Checks marked `# static` are source-grep assertions ported faithfully from
bash; they exercise no runtime behaviour.

Run: python3 -m unittest tests.test_state -v
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WK, WkTest, bash, run


def _have_podman():
    return shutil.which("podman") is not None


def _machine_state(machine="wk"):
    cp = subprocess.run(
        ["podman", "machine", "inspect", machine, "--format", "{{.State}}"],
        capture_output=True, text=True,
    )
    return cp.stdout.strip() if cp.returncode == 0 else "none"


def _state_fingerprint():
    """Mirrors cmd/selftest's _state_fingerprint(): name, mtime and size of
    everything under this host's wk state directory, sorted."""
    cp = bash(f'''
. "{REPO}/lib/common.sh"
d=$(wk_state_dir)
if [ -d "$d" ]; then
    if is_macos; then
        find "$d" -exec stat -f '%N %m %z' {{}} \\; 2>/dev/null | sort
    else
        find "$d" -exec stat -c '%n %Y %s' {{}} \\; 2>/dev/null | sort
    fi
else
    echo "(absent)"
fi
''')
    return cp.stdout


def _tart_guests():
    if shutil.which("tart") is None:
        return ""
    cp = subprocess.run(["tart", "list", "--format", "json"], capture_output=True, text=True)
    return cp.stdout if cp.returncode == 0 else ""


def _readonly_commands():
    """Every read-only command, run for real -- mirrors cmd/selftest's
    _readonly_commands(); return codes are not the point here."""
    run("status")
    run("ls")
    run("logs", "selftest-nonexistent")
    run("doctor")


@unittest.skipUnless(_have_podman(), "no podman on this machine")
class TestReadOnlyIsReadOnly(WkTest):
    """The podman machine must be stopped for these: they check that a
    read-only command does not start it, which cannot be told apart from
    'already running' once it is."""

    def setUp(self):
        super().setUp()
        if _machine_state() == "running":
            self.skipTest("the podman machine is running; 'wk stop' first to check this")

    def test_readonly_starts_nothing(self):
        """machine state identical before and after"""
        before = _machine_state()
        _readonly_commands()
        after = _machine_state()
        self.assertEqual(before, after, f"podman machine went {before} -> {after}")

    def test_readonly_writes_nothing(self):
        """the same four start no guest, write no file, and repair nothing"""
        guests_before = _tart_guests()
        before = _state_fingerprint()

        _readonly_commands()

        after = _state_fingerprint()
        if before != after:
            # A control, before blaming the commands under test: the state
            # dir also holds build logs and status files that a build
            # running on a remote machine writes into once a second.
            import time
            ctl_a = _state_fingerprint()
            time.sleep(3)
            ctl_b = _state_fingerprint()
            if ctl_a != ctl_b:
                self.skipTest(
                    "the wk state dir is being written by something else "
                    "(a build, most likely); cannot tell that apart from a "
                    "read-only command writing"
                )
            diff = "\n".join(
                l for l in before.splitlines() if l not in after.splitlines()
            ) or "\n".join(
                l for l in after.splitlines() if l not in before.splitlines()
            )
            self.fail(f"the wk state dir changed:\n{diff[:2000]}")

        guests_after = _tart_guests()
        self.assertEqual(guests_before, guests_after, "the tart guest list changed")


class TestListingsAgree(WkTest):
    def test_ls_status_same_names(self):
        """print the same workspace-name set on"""
        ls_cp = run("ls")
        names_ls = set()
        lines = ls_cp.stdout.splitlines()
        for line in lines[1:] if lines else []:
            fields = line.split()
            if fields:
                names_ls.add(fields[0])

        status_cp = run("status", "--json")
        try:
            doc = json.loads(status_cp.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"'wk status --json' did not print JSON: {status_cp.stdout[:500]}")

        names_status = set()
        for m in doc.get("machines", []):
            for g in m.get("methods", []):
                for w in g.get("workspaces", []):
                    names_status.add(w["name"])
            for r in m.get("raw", []):
                for ln in r.get("text", "").splitlines():
                    if ln[:1] not in ("", " "):
                        names_status.add(ln.split()[0])

        self.assertEqual(
            names_ls, names_status,
            f"wk ls lists: {sorted(names_ls)}\nwk status  : {sorted(names_status)}",
        )

    def test_status_one_machine_each(self):
        """names each machine once"""
        cp = run("status", "--json")
        try:
            doc = json.loads(cp.stdout)
        except json.JSONDecodeError:
            self.skipTest(f"'wk status --json' did not print JSON: {cp.stdout[:500]}")

        names = [m["name"] for m in doc.get("machines", [])]
        bad = []
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            bad.append("named more than once: " + ", ".join(sorted(dup)))
        kinds = {"container", "vm", "local", "remote", "localhost"}
        wrong = kinds.intersection(names)
        if wrong:
            bad.append("a target kind where a machine belongs: " + ", ".join(sorted(wrong)))
        if not names:
            bad.append("no machines at all")
        self.assertEqual(bad, [], "; ".join(bad))


class TestZedRefusesInsideAWorkspace(WkTest):
    def test_zed_refuses_inside_a_workspace(self):
        """there is no Zed in here"""
        marker = self.tmp / "ws-marker"
        marker.write_text("name=selftest-ws\nsrc=/src/WebKit\n")
        cp = run("zed", "something", env={"WK_MARKER": str(marker)})
        self.assertNotEqual(cp.returncode, 0, "opened an editor from inside a workspace")
        self.assertIn("wk zed selftest-ws", cp.stdout, cp.stdout)


class TestWsStateWords(WkTest):
    def test_ws_state_words(self):
        """five words, each from the evidence that decides it"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
WK_STORE="{self.tmp}/store"; export WK_STORE
mkdir -p "$WK_STORE/ws"
wk_ws_dir() {{ echo "$WK_STORE/ws/$1"; }}
t_needs_base() {{ return 1; }}

_env=absent; _marker=""
t_info()    {{ echo "$_env"; }}
t_created() {{ [ -n "$_marker" ]; }}

# nothing anywhere
echo "1:$(ws_state nothing)"
# the environment is up and creation finished
mkdir -p "$WK_STORE/ws/done"; _env=running; _marker=1
echo "2:$(ws_state done)"
# the driver says so itself: still being made
_env=creating
echo "3:$(ws_state done)"
# the machine did not answer
_env=unreachable
echo "4:$(ws_state done)"
# a workspace directory with no environment behind it, and no marker
_env=absent; _marker=""
echo "5:$(ws_state done)"
# the same, but creation had finished: something outside wk removed it
_marker=1
echo "6:$(ws_state done)"
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, f"ws_state failed: {cp.stdout + cp.stderr}")
        want = "1:absent\n2:present\n3:creating\n4:unreachable\n5:creating\n6:broken"
        self.assertEqual(cp.stdout.strip(), want, f"got:\n{cp.stdout}\nwant:\n{want}")


class TestStatusFilesAreClaims(WkTest):
    def test_status_files_are_claims(self):
        """a status file written by an older schema"""
        f = self.tmp / "ws.status"
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/detach.sh"
f="{f}"

status_write "$f" state=creating "pid=$$" stage=create
[ "$(status_field "$f" state)" = creating ] || {{ echo "state did not round-trip"; exit 1; }}
detach_alive "$f" || {{ echo "a live pid was read as dead"; exit 1; }}

# An unknown key is ignored, and a missing key is empty rather than an error.
status_write "$f" state=creating "pid=$$" "future_key=whatever"
[ -z "$(status_field "$f" stage)" ] || {{ echo "a missing key was not empty"; exit 1; }}

# A pid above every default pid_max on both platforms: dead by construction.
status_write "$f" state=creating pid=4194304 stage=create
! detach_alive "$f" || {{ echo "a dead pid was read as alive"; exit 1; }}

# Garbage: still readable for what is there, never a crash.
printf 'not a status file at all' > "$f"
got=$(status_field "$f" state) || {{ echo "a garbage file failed the reader"; exit 1; }}
[ -z "$got" ] || {{ echo "a garbage file produced a state: $got"; exit 1; }}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestReadyMarkerOneName(WkTest):
    def test_ready_marker_one_name(self):
        """every driver writes the same completion marker"""
        # static
        missing = []
        for f in ("targets/container.sh", "targets/vm.sh", "targets/remote.sh"):
            text = (REPO / f).read_text(errors="replace")
            if "WK_READY_MARKER" not in text:
                missing.append(f)
        firstrun = (REPO / "container/firstrun.sh").read_text(errors="replace")
        if ".wk-ready" not in firstrun:
            missing.append("container/firstrun.sh")
        self.assertEqual(missing, [], f"no ready marker in: {missing}")

        # Nothing may hardcode the name where the variable is in scope.
        hits = []
        candidates = list((REPO / "targets").glob("*.sh")) + list((REPO / "lib").glob("*.sh"))
        for f in candidates:
            text = f.read_text(errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if ".wk-ready" not in line:
                    continue
                if "WK_READY_MARKER=" in line:
                    continue
                if re.match(r"^\s*#", line):
                    continue
                hits.append(f"{f}:{i}:{line}")
        self.assertEqual(hits, [], f"hardcoded marker name:\n{chr(10).join(hits)}")


@unittest.skipUnless(_have_podman(), "no podman on this machine")
class TestPushKeysNotCopied(WkTest):
    def setUp(self):
        super().setUp()
        if _machine_state() != "running":
            self.skipTest("the podman machine is stopped; the keys live in its store")

    def test_push_keys_not_copied(self):
        """never copies"""
        cp = run("push", "status")
        self.assertNotIn(
            "hold their own copy", cp.stdout,
            f"a workspace holds its own copy of a push key:\n{cp.stdout}",
        )


if __name__ == "__main__":
    unittest.main()
