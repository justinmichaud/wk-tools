"""Git author identity and speed settings, on every machine `wk doctor`
reaches -- this host (git_identity_ok/git_speed_ok, already covered by
running `wk doctor` itself), a shared build box (remote/deps.sh's
wk_remote_findings, covered by tests/test_remote_deps.py), and the two this
module adds: the container target's own machine (probe_store/report_store,
reached the way it already is -- locally on Linux, over `podman machine ssh`
on macOS) and every currently-running tart guest (vm_guest_git_findings,
reached through t_exec, the same per-target exec `wk vm ls`/`wk enter` use).

One comparison, not four: git_config_findings (cmd/doctor) reads a git.*
blob -- probe_store's own fields, or a running guest's, both named the way
remote/probe.sh already names them -- against dotfiles/gitconfig, the one
place this repo declares the identity and the speed settings
(core.fsmonitor, feature.manyFiles) it relies on. Every test below lifts
that exact function (and vm_guest_git_findings, and report_store) rather
than re-typing a second copy of the comparison; see tests/test_battery.py
and tests/test_remote_deps.py for the same technique.

No podman, no tart, no ssh: the container-machine probe is exercised
against a captured blob (as tests/test_remote_deps.py does for a build box),
and the tart-guest walk stubs target_workspaces/t_info/t_exec as plain shell
functions -- the same "drive the real function against a fake of the thing
it calls" idea tests/test_provision_stale.py uses, minus the real driver:
vm_guest_git_findings only ever calls those three names, so faking them
directly is the whole of the seam.

Run: python3 -m unittest tests.test_doctor -v
"""
import contextlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

CMD_DOCTOR = REPO / "cmd" / "doctor"
LIB_COMMON = REPO / "lib" / "common.sh"
GITCONFIG = REPO / "dotfiles" / "gitconfig"

WANT_NAME = subprocess.run(
    ["git", "config", "--file", str(GITCONFIG), "--get", "user.name"],
    capture_output=True, text=True,
).stdout.strip()
WANT_EMAIL = subprocess.run(
    ["git", "config", "--file", str(GITCONFIG), "--get", "user.email"],
    capture_output=True, text=True,
).stdout.strip()
assert WANT_NAME and WANT_EMAIL, "dotfiles/gitconfig no longer declares [user]"


def _lift_func(path, name):
    """A function's body, sed'd out of a shell file -- tests/test_battery.py's
    own helper, repeated here (each such helper is scoped to its own module by
    the tree's own convention: test_remote_deps.py, test_provision_stale.py
    and test_battery.py each keep their own rather than sharing one)."""
    text = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


def _sq(s):
    """A string as a single-quoted bash literal -- unlike Python's repr(),
    which backslash-escapes an embedded newline as the two characters `\\n`
    (fine inside a *Python* string, wrong inside bash single quotes: those
    two characters arrive verbatim, not the newline a multi-line git.* blob
    needs). Single quotes preserve every other byte literally; the one
    thing they cannot hold is a single quote itself, closed and re-opened
    around an escaped one, the standard idiom."""
    return "'" + s.replace("'", "'\\''") + "'"


def _lift_one_liners(path, names):
    """ok()/miss()/unk() (cmd/doctor) are each a complete `name() { ...; }`
    on a single line, unlike the multi-line, brace-alone-on-its-own-line
    convention _lift_func (and tests/support.py's func_body) assume -- so
    each is its own regex match rather than a sed range."""
    text = path.read_text()
    lines = []
    for name in names:
        m = re.search(rf'^{re.escape(name)}\(\).*$', text, re.M)
        assert m, f"{name}() not found in {path}"
        lines.append(m.group(0))
    return "\n".join(lines)


GIT_CONFIG_FINDINGS = _lift_func(CMD_DOCTOR, "git_config_findings")
KV_GET = _lift_func(LIB_COMMON, "kv_get")


def _findings(fn_source, call, env=None):
    """Run one findings-emitting function and parse its tab-separated rows
    (state, what, remedy) -- the vm_base_findings/wk_remote_findings
    convention every caller in cmd/doctor reads."""
    cp = bash(f'''
set -euo pipefail
{KV_GET}
{fn_source}
{call}
''', env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    out = []
    for line in cp.stdout.splitlines():
        parts = line.split("\t")
        assert len(parts) == 3, f"not a 3-field finding: {line!r}"
        out.append(tuple(parts))
    return out


class TestGitConfigFindings(unittest.TestCase):
    """git_config_findings: the one comparison a git.* blob is read through,
    whoever probed it."""

    def _blob(self, name=WANT_NAME, email=WANT_EMAIL, fsmonitor="true",
              manyfiles="true"):
        return (f"git.name={name}\ngit.email={email}\n"
                f"git.fsmonitor={fsmonitor}\ngit.manyfiles={manyfiles}\n")

    def test_matching_identity_and_speed_settings_are_all_ok(self):
        f = _findings(GIT_CONFIG_FINDINGS,
                      f'git_config_findings label {_sq(self._blob())} remedy')
        states = {row[0] for row in f}
        self.assertEqual(states, {"ok"}, f)
        self.assertEqual(len(f), 3, f)  # name, email, speed
        for _, what, _ in f:
            self.assertTrue(what.startswith("label: "), what)

    def test_unset_identity_is_wrong_and_names_the_remedy(self):
        blob = self._blob(name="", email="")
        f = _findings(GIT_CONFIG_FINDINGS,
                      f'git_config_findings label {_sq(blob)} "the remedy"')
        name_row = [r for r in f if "user.name" in r[1]][0]
        self.assertEqual(name_row[0], "wrong")
        self.assertIn("not set", name_row[1])
        self.assertEqual(name_row[2], "the remedy")

    def test_a_different_email_is_named_with_both_values(self):
        blob = self._blob(email="someone-else@example.com")
        f = _findings(GIT_CONFIG_FINDINGS,
                      f'git_config_findings label {_sq(blob)} "the remedy"')
        email_row = [r for r in f if "user.email" in r[1]][0]
        self.assertEqual(email_row[0], "wrong")
        self.assertIn("someone-else@example.com", email_row[1])
        self.assertIn(WANT_EMAIL, email_row[1])
        self.assertEqual(email_row[2], "the remedy")

    def test_speed_settings_are_a_finding_of_their_own(self):
        ok_blob = self._blob()
        bad_blob = self._blob(fsmonitor="", manyfiles="true")
        ok_f = _findings(GIT_CONFIG_FINDINGS, f'git_config_findings l {_sq(ok_blob)} r')
        bad_f = _findings(GIT_CONFIG_FINDINGS, f'git_config_findings l {_sq(bad_blob)} r')
        self.assertTrue(any(r[0] == "ok" and "speed settings" in r[1] for r in ok_f), ok_f)
        speed_row = [r for r in bad_f if "speed settings" in r[1]][0]
        self.assertEqual(speed_row[0], "wrong")
        self.assertEqual(speed_row[2], "r")

    def test_git_not_installed_is_a_note_not_a_wrong(self):
        f = _findings(GIT_CONFIG_FINDINGS, 'git_config_findings label "" remedy')
        self.assertEqual(len(f), 1, f)
        self.assertEqual(f[0][0], "note")
        self.assertIn("not installed", f[0][1])


VM_GUEST_GIT_FINDINGS = _lift_func(CMD_DOCTOR, "vm_guest_git_findings")


class TestVmGuestGitFindings(unittest.TestCase):
    """vm_guest_git_findings: every *running* tart guest, reached through
    t_exec -- stubbed here as a plain function, the whole of the seam."""

    def _run(self, target_workspaces, t_info, t_exec):
        cp = bash(f'''
set -euo pipefail
VM_GIT_PROBE="unused -- t_exec is stubbed and ignores it"
{KV_GET}
{GIT_CONFIG_FINDINGS}
{VM_GUEST_GIT_FINDINGS}
target_workspaces() {{ {target_workspaces} ; }}
t_info() {{ {t_info} ; }}
t_exec() {{ {t_exec} ; }}
vm_guest_git_findings
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = []
        for line in cp.stdout.splitlines():
            parts = line.split("\t")
            self.assertEqual(len(parts), 3, f"not a 3-field finding: {line!r}")
            out.append(tuple(parts))
        return out

    def test_a_matching_running_guest_is_all_ok(self):
        blob = (f"git.name={WANT_NAME}\ngit.email={WANT_EMAIL}\n"
                "git.fsmonitor=true\ngit.manyfiles=true")
        f = self._run(
            'printf "mac-rel\\n"',
            'echo running',
            f'printf "%s\\n" {_sq(blob)}',
        )
        self.assertEqual({r[0] for r in f}, {"ok"}, f)
        self.assertTrue(all("mac-rel (tart guest)" in r[1] for r in f), f)

    def test_a_stopped_guest_is_skipped_not_reported(self):
        f = self._run(
            'printf "mac-rel\\n"',
            'echo stopped',
            'echo "t_exec must not run for a stopped guest" >&2; exit 1',
        )
        self.assertEqual(f, [])

    def test_a_running_guest_that_does_not_answer_is_a_note(self):
        f = self._run(
            'printf "mac-rel\\n"',
            'echo running',
            'true',  # t_exec succeeds but prints nothing
        )
        self.assertEqual(len(f), 1, f)
        state, what, remedy = f[0]
        self.assertEqual(state, "note")
        self.assertIn("did not answer", what)
        self.assertIn("wk vm check mac-rel", remedy)

    def test_an_unset_identity_names_a_refreshed_base_as_the_remedy(self):
        blob = "git.name=\ngit.email=\ngit.fsmonitor=\ngit.manyfiles="
        f = self._run(
            'printf "mac-rel\\n"',
            'echo running',
            f'printf "%s\\n" {_sq(blob)}',
        )
        name_row = [r for r in f if "user.name" in r[1]][0]
        self.assertEqual(name_row[0], "wrong")
        self.assertIn("wk vm base --refresh, then wk rm mac-rel", name_row[2])
        self.assertIn("wk new", name_row[2])

    def test_multiple_guests_only_the_running_one_is_checked(self):
        blob = (f"git.name={WANT_NAME}\ngit.email={WANT_EMAIL}\n"
                "git.fsmonitor=true\ngit.manyfiles=true")
        f = self._run(
            'printf "stopped-one\\nrunning-one\\n"',
            'if [ "$1" = running-one ]; then echo running; else echo stopped; fi',
            f'printf "%s\\n" {_sq(blob)}',
        )
        self.assertTrue(f, f)
        self.assertTrue(all("running-one" in r[1] for r in f), f)
        self.assertFalse(any("stopped-one" in r[1] for r in f), f)


class TestProbeStoreGit(unittest.TestCase):
    """probe_store: the container target's own machine's git.* fields --
    reached locally on Linux, or over `podman machine ssh` on macOS, both
    already exercised by whatever calls probe_store; this drives the
    function directly, against a scratch HOME so it reads no real git
    identity."""

    def _probe(self, home, path=None):
        cp = bash(f'''
set -euo pipefail
. "$WK_ROOT/lib/common.sh"
{_lift_func(CMD_DOCTOR, "probe_store")}
WK_STORE={_sq(str(home / "store"))}
mkdir -p "$WK_STORE"
probe_store
''', env={"HOME": str(home), **({"PATH": path} if path else {})})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_git_present_and_configured_is_reported(self):
        with self._scratch_home() as home:
            out = self._probe(home)
            self.assertIn(f"git.name={WANT_NAME}", out)
            self.assertIn(f"git.email={WANT_EMAIL}", out)
            self.assertIn("git.fsmonitor=true", out)
            self.assertIn("git.manyfiles=true", out)

    def test_git_absent_prints_none_of_the_git_fields(self):
        with self._scratch_home() as home:
            out = self._probe(home, path=self._path_without_git())
            self.assertNotIn("git.name=", out)
            self.assertNotIn("git.email=", out)

    def _path_without_git(self):
        needed = ["sh", "bash", "ls", "test", "printf", "mkdir", "cat",
                  "systemctl", "id", "grep"]
        d = tempfile.mkdtemp(prefix="wk-test-doctor-nogit-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for tool in needed:
            found = shutil.which(tool)
            if found:
                try:
                    os.symlink(found, f"{d}/{tool}")
                except FileExistsError:
                    pass
        return d

    @contextlib.contextmanager
    def _scratch_home(self):
        d = Path(tempfile.mkdtemp(prefix="wk-test-doctor-home-"))
        try:
            (d / ".gitconfig").write_text(
                f"[user]\n\tname = {WANT_NAME}\n\temail = {WANT_EMAIL}\n"
                "[core]\n\tfsmonitor = true\n"
                "[feature]\n\tmanyFiles = true\n"
            )
            yield d
        finally:
            shutil.rmtree(d, ignore_errors=True)


REPORT_STORE = _lift_func(CMD_DOCTOR, "report_store")
DOCTOR_OK_MISS_UNK = _lift_one_liners(CMD_DOCTOR, ["ok", "miss", "unk"])

FULL_STORE_BLOB = (
    "mirror=ok\nbase=ok\nskills=ok\nproxy=ok\npihosts=ok\nbroker=ok\n"
    f"git.name={WANT_NAME}\ngit.email={WANT_EMAIL}\n"
    "git.fsmonitor=true\ngit.manyfiles=true\n"
)


class TestReportStoreGitScope(unittest.TestCase):
    """report_store only reports the container machine's git identity when
    handed a remedy for it -- the doctor-side rule that a probe run locally
    (Linux, or macOS already inside the VM) is the same machine the "config"
    section above already checked, so it is not printed a second time."""

    def _run(self, blob, gitremedy):
        cp = bash(f'''
set -euo pipefail
missing=0
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
{DOCTOR_OK_MISS_UNK}
{KV_GET}
{GIT_CONFIG_FINDINGS}
{REPORT_STORE}
report_store {_sq(blob)} {_sq(gitremedy)}
''', env={"WK_HOST_SECRETS": "/nonexistent-wk-test"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        # ok()/miss()/unk() (cmd/doctor) print to stderr, like every row
        # `wk doctor` prints -- only warn()/die() have a reason to.
        return cp.stdout + cp.stderr

    def test_a_remedy_reports_the_container_machine(self):
        out = self._run(FULL_STORE_BLOB, "podman machine ssh wk -- git config ...")
        self.assertIn("container machine", out)
        self.assertIn("git user.name", out)
        self.assertIn("git speed settings", out)

    def test_no_remedy_means_the_same_machine_already_checked(self):
        out = self._run(FULL_STORE_BLOB, "")
        self.assertNotIn("container machine", out)
        self.assertNotIn("git user.name", out)


class TestTheCredentialsSection(unittest.TestCase):
    """`wk doctor` reports what each credential can do from an answer taken at
    that moment, so the block is lifted and run rather than read. Every arm of
    it is one verdict from lib/credcheck.py's rules, driven here by what is in
    a scratch store."""

    # Anchored on the sections themselves, not on the comment banners above
    # them: a banner is not structure, and slicing by one made this suite
    # demand that two comments keep existing.
    START = 'section "credentials"'
    END = 'section "root access"'

    def block(self):
        text = CMD_DOCTOR.read_text()
        return text[text.index(self.START):text.index(self.END)]

    def run_block(self, secrets):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-doctor-cred-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel, value in secrets.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value + "\n")
        harness = (_lift_one_liners(CMD_DOCTOR, ("ok", "miss", "unk", "section"))
                   + "\nmissing=0\n")
        cp = bash('set -euo pipefail\n'
                  f'. "{REPO}/lib/common.sh"\n. "{REPO}/lib/store.sh"\n'
                  + harness + self.block(),
                  env={"WK_HOST_SECRETS": str(tmp / "secrets"),
                       "WK_TS_AUTHKEY": str(tmp / "tailscale-authkey"),
                       "WK_TS_API_SECRET": str(tmp / "tailscale-api-key"),
                       "WK_GITHUB_API": "http://127.0.0.1:1",
                       "WK_TAILNET_API": "http://127.0.0.1:1"})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout + cp.stderr

    def test_nothing_stored_is_reported_and_is_not_a_fault(self):
        out = self.run_block({})
        self.assertIn("credentials", out)
        for name in ("github-pat", "claude", "litellm", "claude-login",
                     "tailnet", "tailnet-api"):
            self.assertIn(name, out)
        self.assertIn("nothing stored", out)
        self.assertNotIn("\033[31m", out)

    def test_a_credential_that_breaks_its_rule_is_a_finding_with_the_remedy(self):
        out = self.run_block({"tailscale-authkey": "tskey-api-k1-abc"})
        self.assertIn("administers the whole tailnet", out)
        self.assertIn("login.tailscale.com", out)

    def test_a_credential_that_could_not_be_judged_is_reported_unverified(self):
        """No network: the token is there, nothing about it was established,
        and the next run asks again."""
        out = self.run_block({"push-keys/github-pat": "github_pat_11ABC_x"})
        self.assertIn("github-pat", out)
        self.assertIn("could not reach", out)

    def test_an_acceptable_credential_reports_what_it_can_do(self):
        out = self.run_block({"secrets/claude-token": "sk-ant-oat01-abc"})
        self.assertIn("inference-only", out)

    def test_nothing_stored_is_ever_printed(self):
        secret = "sk-ant-oat01-do-not-print-this"
        out = self.run_block({"secrets/claude-token": secret})
        self.assertNotIn(secret, out)


class TestSyntax(unittest.TestCase):
    def test_cmd_doctor_parses(self):
        cp = subprocess.run(["bash", "-n", str(CMD_DOCTOR)], capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)


if __name__ == "__main__":
    unittest.main()
