"""`wk bench --cores`: the cpu-list validator and taskset prefix builder
(lib/wkdata.py `cores-valid` / `cores-wrap`, cmd/bench's bench_cores_valid /
bench_cores_wrap), the target/os refusal decision (cmd/bench's
bench_cores_refusal), and the axis-mismatch warning `wk bench compare` gains
for a fourth axis (lib/wkdata.py `_axis_check_lines`).

Unit tests only -- no workspace, no podman VM, no hardware. `cores-valid` and
`cores-wrap` are driven as a subprocess exactly the way `wk bench` itself
calls them (cmd/bench's `wkdata()` wrapper), the same pattern
tests/test_bench_report.py uses for the rest of lib/wkdata.py. The axis
check is exercised the same way test_bench_report.py's own axis-mismatch
test is: two synthetic env.json fixtures through `wkdata.py report --text`.
bench_cores_refusal is a pure function of (target, os), so it is lifted
verbatim out of cmd/bench and called directly -- the same `sed -n
'/^fn()/,/^}/p'` idiom tests/test_quick.py uses to lift cmd/status's `bump`.

Run: python3 -m unittest tests.test_bench_cores -v
"""
import json
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, scratch_dir

WKDATA = REPO / "lib" / "wkdata.py"


def wkdata(*args, timeout=30):
    return subprocess.run(
        ["python3", str(WKDATA), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def env_record(path, *fields):
    cp = wkdata("env-record", str(path), *fields)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp


class TestCoresValid(unittest.TestCase):
    """wkdata.py cores-valid: the syntax `--cores` accepts, checked before a
    preflight is ever spent on it. A cpu the machine does not have is left
    to taskset itself -- not this parser's job."""

    def _valid(self, spec):
        return wkdata("cores-valid", spec).returncode == 0

    def test_accepts_every_documented_shape(self):
        for spec in ("0-3", "2,3", "0-1,4", "7"):
            with self.subTest(spec=spec):
                self.assertTrue(self._valid(spec), f"{spec!r} should be valid")

    def test_refuses_garbage(self):
        for spec in ("a", "1-", "-1", "1,,2"):
            with self.subTest(spec=spec):
                self.assertFalse(self._valid(spec), f"{spec!r} should be refused")


class TestCoresWrap(unittest.TestCase):
    """wkdata.py cores-wrap: the literal `taskset -c <set> ` prefix
    cmd_run and run_jsc interpolate onto the command line they build for
    t_exec (cmd/bench's bench_cores_wrap is a one-line call through to
    this -- there is no --dry-run on `wk bench <ws> <plan>` to inspect the
    built command line directly, so this is the helper that builds it)."""

    def test_prefix_contains_taskset_dash_c_and_the_set(self):
        cp = wkdata("cores-wrap", "0-3")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("taskset -c 0-3", cp.stdout)

    def test_refuses_an_invalid_set_rather_than_printing_garbage(self):
        cp = wkdata("cores-wrap", "not-a-set")
        self.assertNotEqual(cp.returncode, 0)


class TestCoresRefusal(unittest.TestCase):
    """bench_cores_refusal <target> <os>, lifted straight out of cmd/bench:
    refuses a `vm` target regardless of os (a macOS guest by construction),
    and a `local` target running on macOS; allows container, remote, and
    local-on-linux, where taskset actually pins something."""

    def _refusal(self, target, os):
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
body="$(sed -n '/^bench_cores_refusal()/,/^}}/p' "{REPO}/cmd/bench")"
[ -n "$body" ] || {{ echo "lift bench_cores_refusal failed"; exit 1; }}
eval "$body"
bench_cores_refusal {target!r} {os!r}
'''
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_vm_target_refuses_on_macos(self):
        out = self._refusal("vm", "macos")
        self.assertIn("no pin exists on macOS", out)

    def test_vm_target_refuses_on_linux_too(self):
        """a vm target is a macOS guest by construction, whatever os the
        driving machine reports"""
        out = self._refusal("vm", "linux")
        self.assertIn("no pin exists on macOS", out)

    def test_local_target_on_macos_refuses(self):
        out = self._refusal("local", "macos")
        self.assertIn("no pin exists on macOS", out)

    def test_local_target_on_linux_allows(self):
        out = self._refusal("local", "linux")
        self.assertEqual(out, "")

    def test_container_target_allows(self):
        out = self._refusal("container", "linux")
        self.assertEqual(out, "")

    def test_remote_target_allows(self):
        out = self._refusal("remote", "linux")
        self.assertEqual(out, "")


class TestCoresAxisWarning(WkTest):
    """`wk bench compare` (wkdata.py report's axis check) warns when two
    runs' `cores.set` differ, the same way it already warns on a runner or
    session-mode mismatch -- and stays quiet when they agree."""

    def _pair(self, tmp, a_cores, b_cores):
        a_dir, b_dir = tmp / "a", tmp / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        doc = {"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [1.0, 2.0]}}}}}}
        (a_dir / "result.json").write_text(json.dumps(doc))
        (b_dir / "result.json").write_text(json.dumps(doc))
        base = ["plan=jetstream3", "class=cpu", "runner=jsc", "bench_host=container"]
        a_extra = [f"cores.set={a_cores}"] if a_cores is not None else []
        b_extra = [f"cores.set={b_cores}"] if b_cores is not None else []
        env_record(a_dir / "env.json", *base, *a_extra)
        env_record(b_dir / "env.json", *base, *b_extra)
        return a_dir / "result.json", b_dir / "result.json"

    def test_different_core_sets_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, "0-3", "4-7")
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("different core pins", cp.stdout)
            self.assertIn("0-3", cp.stdout)
            self.assertIn("4-7", cp.stdout)

    def test_equal_core_sets_do_not_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, "0-3", "0-3")
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotIn("different core pins", cp.stdout)

    def test_unpinned_vs_pinned_warns(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, None, "0-3")
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("different core pins", cp.stdout)
            self.assertIn("unpinned", cp.stdout)

    def test_both_unpinned_does_not_warn(self):
        with scratch_dir() as tmp:
            a, b = self._pair(tmp, None, None)
            cp = wkdata("report", str(a), str(b), "--text")
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertNotIn("different core pins", cp.stdout)


if __name__ == "__main__":
    unittest.main()
