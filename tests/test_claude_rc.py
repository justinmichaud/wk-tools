"""`wk ai claude <ws> --rc`: Claude Code Remote Control, started detached inside
a workspace and tracked through lib/detach.sh's status-file schema. Each
docstring is the phrase of the behaviour it checks.

cmd/ai refuses a `local` target outright ("already inside workspace"),
which is what a FakeWorkspace's marker trick simulates -- so, per the task
brief, the real driver call (container/vm/remote) is not exercisable here.
Instead this sources cmd/ai's own rc_* functions in library mode
(`WK_CLAUDE_LIB=1 . cmd/ai`, mirrored by the guard cmd/ai defines
just for this) with fakes for t_exec/t_spawn/t_home/t_src standing in for a
real target: a fake `t_spawn` backgrounds the command for real (no `setsid`,
which this Mac does not have -- ssh's own nohup-survives-disconnect property
is not what is under test), a fake `t_exec` runs a command directly (the
same "driver" a real one would reach over ssh or podman), and a fake `claude`
script on PATH-like a spot logs its own argv and sleeps, standing in for the
long-running remote-control server.

Run: python3 -m unittest tests.test_claude_rc -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, bash, run, temp_store

# What rc_start requires before it will spawn anything: the claude.ai login
# credential, in this machine's writable agent directory (wk_agent_rw_dir,
# lib/store.sh -- a sibling of the secrets directory, so WK_HOST_SECRETS
# places both). Deliberately nothing like a real one.
FAKE_LOGIN = '{"claudeAiOauth":{"accessToken":"x","refreshToken":"y","scopes":["user:profile"]}}'


def credential_env(tmp, login=True):
    """A scratch secrets/agent-rw pair for a probe, with or without a login."""
    secrets = Path(tmp) / "secrets"
    rw = Path(tmp) / "agent-rw"
    secrets.mkdir(parents=True, exist_ok=True)
    rw.mkdir(parents=True, exist_ok=True)
    if login:
        (rw / ".credentials.json").write_text(FAKE_LOGIN)
    return {"WK_HOST_SECRETS": str(secrets)}


# The library-mode probe: sources cmd/ai for its rc_* functions only
# (WK_CLAUDE_LIB=1, the guard cmd/ai defines for exactly this), then
# fakes the driver contract those functions call through, then drives
# rc_start/rc_stop/rc_alive directly and prints markers this test greps.
_PROBE = r'''
set -euo pipefail
export WK_ROOT="__REPO__"
export WK_CLAUDE_LIB=1
. "__REPO__/cmd/ai"

WS="probe-ws"
mkdir -p "$(wk_ws_dir "$WS")"

TMP_HOME=$(mktemp -d)
TMP_SRC=$(mktemp -d)
CLAUDE_LOG=$(mktemp)
export CLAUDE_LOG

# The fake driver: t_exec runs a command directly (a real driver's ssh/podman
# exec, minus the network hop), t_spawn backgrounds it and records the real
# pid (no setsid -- this host has none, and surviving an ssh disconnect is
# not what these functions are tested for), t_home/t_src are fixed scratch
# dirs standing in for the target's own filesystem.
# A container's shell rc points the CLI at the mounted directory, which is
# where the login lands and where t_agent_secret_present looks for it.
export CLAUDE_SECURESTORAGE_CONFIG_DIR="$(wk_agent_rw_dir)"
t_exec()  { local name="$1"; shift; "$@"; }
t_home()  { printf '%s' "$TMP_HOME"; }
t_src()   { printf '%s' "$TMP_SRC"; }
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    "$@" > "$log" 2>&1 < /dev/null &
    printf '%s' "$!" > "$pidf"
}

FAKE_CLAUDE="$TMP_HOME/claude"
cat > "$FAKE_CLAUDE" <<'EOS'
#!/bin/sh
echo "$@" >> "$CLAUDE_LOG"
sleep 20
EOS
chmod +x "$FAKE_CLAUDE"

echo "MARK:first-start"
rc_start "$WS" "$FAKE_CLAUDE"
sleep 0.3

echo "MARK:second-start"
rc_start "$WS" "$FAKE_CLAUDE"
sleep 0.3

echo "MARK:claude-argv"
cat "$CLAUDE_LOG"
echo "MARK:claude-argv-lines:$(wc -l < "$CLAUDE_LOG" | tr -d ' ')"

echo "MARK:status-running"
_t=$(rc_task "$WS")
printf 'verdict=%s\npid=%s\nlog=%s\nkill=%s\nplan=%s\n' \
    "$(task_verdict "$_t")" "$(task_field "$_t" pid)" "$(task_field "$_t" log)" \
    "$(task_field "$_t" kill)" "$(task_field "$_t" plan)"

rc_stop "$WS"
sleep 0.3

echo "MARK:status-stopped"
printf 'verdict=%s\n' "$(task_verdict "$(rc_task "$WS")")"

echo "MARK:alive-after-stop"
if rc_alive "$WS"; then echo yes; else echo no; fi

# Never leave the fake server behind if something above went wrong.
kill "$(cat "$TMP_HOME/claude-remote-control.pid" 2>/dev/null)" 2>/dev/null || true
'''


def _section(stdout, mark, next_mark=None):
    """The lines between `MARK:<mark>` and the next `MARK:` line (or EOF)."""
    lines = stdout.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l == f"MARK:{mark}":
            start = i + 1
            break
    if start is None:
        return None
    out = []
    for l in lines[start:]:
        if l.startswith("MARK:"):
            break
        out.append(l)
    return "\n".join(out)


class TestClaudeRcHelp(WkTest):
    def test_help_mentions_rc_and_stop(self):
        """`wk ai claude -h` documents --rc and --rc --stop"""
        cp = run("ai", "-h")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--rc", cp.stdout)
        self.assertIn("--stop", cp.stdout)
        self.assertIn("remote-control", cp.stdout.lower())

    def test_wk_root_line_is_static_not_hardcoded(self):  # static
        """cmd/ai's WK_ROOT line respects a pre-set WK_ROOT, like lib/common.sh's own"""
        text = (REPO / "cmd" / "ai").read_text()
        self.assertIn('WK_ROOT="${WK_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"', text)


class TestClaudeRcLifecycle(unittest.TestCase):
    def setUp(self):
        self._store = temp_store()
        store = self._store.__enter__()
        self.env = {"WK_STORE": store["path"].as_posix()}
        self.env.update(credential_env(store["path"]))
        self.addCleanup(self._store.__exit__, None, None, None)

    def _run_probe(self):
        cp = bash(_PROBE.replace("__REPO__", str(REPO)), env=self.env, timeout=60)
        self.assertEqual(cp.returncode, 0, f"probe failed: {cp.stdout}\n{cp.stderr}")
        return cp.stdout

    def test_start_spawns_remote_control_with_expected_argv(self):
        """rc_start's t_spawn argv contains remote-control --spawn=same-dir --name <ws>"""
        out = self._run_probe()
        argv = _section(out, "claude-argv")
        self.assertIsNotNone(argv, out)
        self.assertIn("remote-control --spawn=same-dir --name probe-ws", argv)

    def test_second_start_is_a_no_op(self):
        """a second rc_start finds the recorded pid alive and does not spawn again"""
        out = self._run_probe()
        self.assertIn("MARK:claude-argv-lines:1", out, out)

    def test_the_record_carries_the_pid_the_log_and_what_stops_it(self):
        """One record shape for every long-running command (lib/task.sh): the
        pid liveness is asked of, the log, and the command a person types."""
        out = self._run_probe()
        sf = _section(out, "status-running")
        self.assertIn("verdict=running", sf, out)
        self.assertRegex(sf, r"(?m)^pid=\d+$")
        self.assertRegex(sf, r"(?m)^log=.+")
        self.assertIn("kill=wk ai claude probe-ws --rc --stop", sf, out)
        self.assertIn("plan=claude remote-control in probe-ws", sf, out)

    def test_stop_ends_the_record(self):
        """rc_stop records the end, and rc_alive is false afterwards"""
        out = self._run_probe()
        self.assertIn("verdict=stopped", _section(out, "status-stopped"), out)
        alive = _section(out, "alive-after-stop")
        self.assertEqual(alive.strip(), "no", out)


if __name__ == "__main__":
    unittest.main()


class TestTheStartUpDialogsAreAnsweredBeforeAnythingStarts(WkTest):
    """Three prompts of Claude Code's wait on a terminal, and a spawned
    remote-control server has none: it is given /dev/null, answers "no" to
    "Enable Remote Control? (y/n)" and exits 0. `wk new` has already made every
    one of those decisions, so cmd/ai records them in the workspace
    (claude/workspace-config.py) before either kind of session starts.

    Measured against Claude Code 2.1.268: the global config keys are
    hasCompletedOnboarding and remoteDialogSeen, and trust is per project under
    `projects`."""

    AI = (REPO / "cmd" / "ai").read_text()
    SCRIPT = REPO / "claude" / "workspace-config.py"
    ACCOUNT = {"organizationUuid": "org-1", "emailAddress": "someone@example.invalid"}

    def _record(self, home, checkout="/src/WebKit", shared=None):
        env = dict(os.environ, HOME=str(home))
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        if shared is not None:
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = str(shared)
        return subprocess.run(["python3", str(self.SCRIPT), checkout],
                              env=env, capture_output=True, text=True, timeout=60)

    def _shared(self, name, credential=True, account=ACCOUNT):
        """The directory a container mounts read-write: the credential, and
        the CLI's config file beside it carrying the account record."""
        d = self.tmp / name
        d.mkdir()
        if credential:
            (d / ".credentials.json").write_text('{"claudeAiOauth": {}}')
        if account is not None:
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": account}))
        return d

    def test_it_records_the_three_answers(self):
        home = self.tmp / "ws-home"
        home.mkdir()
        cp = self._record(home)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        doc = json.loads((home / ".claude.json").read_text())
        self.assertIs(True, doc["hasCompletedOnboarding"])
        self.assertIs(True, doc["remoteDialogSeen"])
        self.assertIs(True, doc["projects"]["/src/WebKit"]["hasTrustDialogAccepted"])

    def test_it_keeps_what_the_cli_already_wrote(self):
        """~/.claude.json is the CLI's own live state, so this merges into it."""
        home = self.tmp / "ws-home-existing"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps(
            {"numStartups": 7, "projects": {"/src/WebKit": {"lastCost": 1.5}}}))
        self._record(home)
        doc = json.loads((home / ".claude.json").read_text())
        self.assertEqual(7, doc["numStartups"])
        self.assertEqual(1.5, doc["projects"]["/src/WebKit"]["lastCost"])
        self.assertIs(True, doc["projects"]["/src/WebKit"]["hasTrustDialogAccepted"])

    def test_running_it_twice_changes_nothing_the_second_time(self):
        home = self.tmp / "ws-home-twice"
        home.mkdir()
        self._record(home)
        first = (home / ".claude.json").read_text()
        cp = self._record(home)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("", cp.stdout.strip())
        self.assertEqual(first, (home / ".claude.json").read_text())

    def test_the_account_record_is_copied_in_from_beside_the_credential(self):
        """Measured 2026-09-11 against 2.1.269: remote control reads
        organizationUuid from the workspace's own config and exits with
        "Unable to determine your organization" without it. The login is
        shared, so its record is delivered the same way, before any session."""
        home = self.tmp / "ws-home-account"
        home.mkdir()
        cp = self._record(home, shared=self._shared("agent-rw"))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("account record", cp.stdout)
        doc = json.loads((home / ".claude.json").read_text())
        self.assertEqual("org-1", doc["oauthAccount"]["organizationUuid"])

    def test_a_rotated_login_converges_on_the_next_start(self):
        home = self.tmp / "ws-home-rotated"
        home.mkdir()
        self._record(home, shared=self._shared("agent-rw-old"))
        new = self._shared("agent-rw-new", account={"organizationUuid": "org-2"})
        self._record(home, shared=new)
        doc = json.loads((home / ".claude.json").read_text())
        self.assertEqual("org-2", doc["oauthAccount"]["organizationUuid"])

    def test_a_credential_with_no_record_beside_it_is_refused_with_the_remedy(self):
        """Rather than a server that starts and dies three lines deep in a log:
        the remedy is the host's, and it is named."""
        home = self.tmp / "ws-home-norecord"
        home.mkdir()
        for account in (None, {"emailAddress": "x"}):
            with self.subTest(account=account):
                shared = self._shared("agent-rw-%s" % (account is None), account=account)
                cp = self._record(home, shared=shared)
                self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("no account record", cp.stderr)
                self.assertIn("wk key set claude-login --replace", cp.stderr)
        self.assertFalse((home / ".claude.json").exists())

    def test_a_guest_that_logged_in_for_itself_keeps_its_own_record(self):
        """A macOS guest is handed no shared login: its directory holds no
        credential and no record, and its own ~/.claude.json has both."""
        home = self.tmp / "ws-home-guest"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"organizationUuid": "org-guest"}}))
        empty = self._shared("claude-login", credential=False, account=None)
        cp = self._record(home, shared=empty)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        doc = json.loads((home / ".claude.json").read_text())
        self.assertEqual("org-guest", doc["oauthAccount"]["organizationUuid"])

    def test_a_file_it_cannot_read_is_left_alone(self):
        """It holds the CLI's live state, including the account: overwriting
        one that will not parse would log the workspace out."""
        home = self.tmp / "ws-home-corrupt"
        home.mkdir()
        (home / ".claude.json").write_text("{not json")
        cp = self._record(home)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("not readable as JSON", cp.stderr)
        self.assertEqual("{not json", (home / ".claude.json").read_text())

    def test_both_kinds_of_session_record_them_first(self):
        self.assertEqual(2, self.AI.count("claude_workspace_config "),
                         "the foreground session and --rc each record them once")
        self.assertIn("workspace-config.py", self.AI)

    def test_nothing_runs_a_session_of_its_own_to_answer_a_dialog(self):
        """The old remedy for the trust dialog: start an interactive session
        and ask the person to accept it. There is nothing left to accept."""
        self.assertNotIn('"$WK_ROOT/wk" ai claude "$NAME"', self.AI)

    def test_a_plain_session_does_not_start_remote_control(self):
        """`wk ai claude <ws> --rc` is the one path that starts it, and it says
        so in argv. Shipping remoteControlAtStartup made every plain session
        try, and fail, on a credential remote control refuses."""
        settings = json.loads((REPO / "claude" / "settings.json").read_text())
        self.assertNotIn("remoteControlAtStartup", settings)


class TestRemoteControlIsOnByDefault(WkTest):
    """A workspace is reachable from the phone without anybody having
    remembered to ask: `wk new` starts remote control in the workspace it just
    made, and `wk start` starts it in every container it brings back up. Both
    call `wk ai claude --rc` -- the one implementation -- and neither may fail
    because of it.

    What is checked here is the wiring, statically: exercising it needs a real
    container (a workspace, a running podman machine and a Claude CLI in it),
    which tests/support cannot conjure. The live check is owed -- docs/defects.
    """

    NEW = (REPO / "cmd" / "new").read_text()
    START = (REPO / "cmd" / "start").read_text()

    def test_new_starts_it_through_the_one_command(self):
        self.assertIn('ai claude "$NAME" --rc', self.NEW,
                      "wk new no longer starts remote control")
        self.assertNotIn("rc_status_file", self.NEW,
                         "wk new reaches into the rc_* functions instead of "
                         "calling `wk ai claude --rc`")

    def test_start_starts_it_for_every_container_it_brings_back(self):
        self.assertIn('ai claude "$_ws" --rc', self.START,
                      "wk start no longer starts remote control")

    def test_both_honour_one_switch(self):
        for name, text in (("new", self.NEW), ("start", self.START)):
            with self.subTest(cmd=name):
                self.assertIn("WK_NO_CLAUDE_RC", text,
                              f"wk {name} has no way to turn it off")

    def test_neither_can_fail_because_of_it(self):
        """A workspace that exists must not be reported as a failed creation,
        and one agent that will not start is not a machine that will not start.

        The invocation -- the line that ends in a continuation, not the prose
        around it -- is followed by a warning, never a `die`."""
        for name, text, call in (("new", self.NEW, 'ai claude "$NAME" --rc'),
                                 ("start", self.START, 'ai claude "$_ws" --rc')):
            lines = text.splitlines()
            idx = [i for i, l in enumerate(lines) if call in l and not l.strip().startswith("#")]
            self.assertEqual(len(idx), 1, f"wk {name}: {len(idx)} invocations, expected 1")
            tail = "\n".join(lines[idx[0]:idx[0] + 5])
            with self.subTest(cmd=name):
                self.assertRegex(tail, r'\bwarn "',
                              f"wk {name} does not warn when the agent will not start")
                self.assertNotIn("|| die", tail,
                                 f"wk {name} dies when the agent will not start")

    def test_a_shared_build_machine_is_skipped(self):
        """`wk ai claude` on a remote target is a barrier -- a prompt about a
        machine with no sandbox -- so `wk new` must not walk into it unattended."""
        self.assertIn('"$WK_TARGET_KIND" != remote', self.NEW)


_PROBE_DIES_AT_ONCE = r'''
set -euo pipefail
export WK_ROOT="__REPO__"
export WK_CLAUDE_LIB=1
. "__REPO__/cmd/ai"

WS="probe-ws-dies"
mkdir -p "$(wk_ws_dir "$WS")"
TMP_HOME=$(mktemp -d); TMP_SRC=$(mktemp -d)
# A container's shell rc points the CLI at the mounted directory, which is
# where the login lands and where t_agent_secret_present looks for it.
export CLAUDE_SECURESTORAGE_CONFIG_DIR="$(wk_agent_rw_dir)"
t_exec()  { local name="$1"; shift; "$@"; }
t_home()  { printf '%s' "$TMP_HOME"; }
t_src()   { printf '%s' "$TMP_SRC"; }
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    "$@" > "$log" 2>&1 < /dev/null &
    printf '%s' "$!" > "$pidf"
}
FAKE_CLAUDE="$TMP_HOME/claude"
cat > "$FAKE_CLAUDE" <<'EOS'
#!/bin/sh
echo "Error: Remote Control requires a full-scope login token." >&2
exit 1
EOS
chmod +x "$FAKE_CLAUDE"
# A subshell: die exits the shell it runs in, and the probe has more to say.
if ( rc_start "$WS" "$FAKE_CLAUDE" ) 2>"$TMP_HOME/err"; then echo "MARK:started"; else echo "MARK:refused"; fi
echo "MARK:err"; cat "$TMP_HOME/err"
echo "MARK:status"
_t=$(rc_task "$WS")
printf 'verdict=%s\npid=%s\n' "$(task_verdict "$_t")" "$(task_field "$_t" pid)"
'''


class TestAServerThatExitsAtOnceIsNotReportedRunning(unittest.TestCase):
    """rc_start reads the pid the target wrote and then waits for evidence
    that the process is still there; one that exits within its first
    seconds (a credential remote control refuses -- measured live with a
    `claude setup-token` token -- or a binary that cannot start) is reported
    with its log's last lines, and the record says stopped."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="wk-rc-dies-")
        env = {"WK_STORE": tmp}
        env.update(credential_env(tmp))
        cls.cp = bash(_PROBE_DIES_AT_ONCE.replace("__REPO__", str(REPO)), env=env, timeout=60)

    def test_it_refuses_rather_than_claiming_running(self):
        self.assertIn("MARK:refused", self.cp.stdout, self.cp.stdout + self.cp.stderr)

    def test_the_logs_last_line_is_in_the_message(self):
        self.assertIn("exited at once", self.cp.stdout)
        self.assertIn("full-scope login token", self.cp.stdout)

    def test_the_record_does_not_say_running(self):
        status = self.cp.stdout.split("MARK:status", 1)[1]
        self.assertIn("verdict=failed", status)
        self.assertRegex(status, r"(?m)^pid=$")


# The same library-mode shape as the probe above, with the one thing rc_start
# checks before it spawns taken away. A fake `claude` that would have started
# happily, so a "refused" here is the gate and nothing else.
_PROBE_NO_CREDENTIAL = r'''
set -euo pipefail
export WK_ROOT="__REPO__"
export WK_CLAUDE_LIB=1
. "__REPO__/cmd/ai"

WS="probe-ws-nocred"
mkdir -p "$(wk_ws_dir "$WS")"
TMP_HOME=$(mktemp -d); TMP_SRC=$(mktemp -d)
# A container's shell rc points the CLI at the mounted directory, which is
# where the login lands and where t_agent_secret_present looks for it.
export CLAUDE_SECURESTORAGE_CONFIG_DIR="$(wk_agent_rw_dir)"
t_exec()  { local name="$1"; shift; "$@"; }
t_home()  { printf '%s' "$TMP_HOME"; }
t_src()   { printf '%s' "$TMP_SRC"; }
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    echo "SPAWNED" >> "$TMP_HOME/spawned"
    "$@" > "$log" 2>&1 < /dev/null &
    printf '%s' "$!" > "$pidf"
}
FAKE_CLAUDE="$TMP_HOME/claude"
printf '#!/bin/sh\nsleep 20\n' > "$FAKE_CLAUDE"
chmod +x "$FAKE_CLAUDE"
if ( rc_start "$WS" "$FAKE_CLAUDE" ) 2>"$TMP_HOME/err"; then echo "MARK:started"; else echo "MARK:refused"; fi
echo "MARK:err"; cat "$TMP_HOME/err"
echo "MARK:spawned"; cat "$TMP_HOME/spawned" 2>/dev/null || true
echo "MARK:status"; cat "$(rc_status_file "$WS")" 2>/dev/null || echo "(no status file)"
kill "$(cat "$TMP_HOME/claude-remote-control.pid" 2>/dev/null)" 2>/dev/null || true
'''


# The same probe again, with the target's answer about the login forced and
# this machine's store stocked the other way round: the gate has to follow the
# target, and a store full of credentials is the case that would hide it.
_PROBE_TARGET_ANSWERS = r"""
set -euo pipefail
export WK_ROOT="__REPO__"
export WK_CLAUDE_LIB=1
. "__REPO__/cmd/ai"

WS="probe-ws-target"
mkdir -p "$(wk_ws_dir "$WS")"
TMP_HOME=$(mktemp -d); TMP_SRC=$(mktemp -d)
# A container's shell rc points the CLI at the mounted directory, which is
# where the login lands and where t_agent_secret_present looks for it.
export CLAUDE_SECURESTORAGE_CONFIG_DIR="$(wk_agent_rw_dir)"
t_exec()  { local name="$1"; shift; "$@"; }
t_home()  { printf '%s' "$TMP_HOME"; }
t_src()   { printf '%s' "$TMP_SRC"; }
t_spawn() {
    local name="$1" log="$2" pidf="$3"; shift 3
    echo "SPAWNED" >> "$TMP_HOME/spawned"
    "$@" > "$log" 2>&1 < /dev/null &
    printf '%s' "$!" > "$pidf"
}
# The driver hook cmd/ai asks, standing in for a guest that has -- or has not
# -- had its own `claude auth login`.
t_agent_secret_present() { [ "$TARGET_SAYS" = yes ]; }
t_agent_secret_remedy()  { printf 'log in inside the guest: claude auth login'; }

FAKE_CLAUDE="$TMP_HOME/claude"
printf '#!/bin/sh\nsleep 20\n' > "$FAKE_CLAUDE"
chmod +x "$FAKE_CLAUDE"
if ( rc_start "$WS" "$FAKE_CLAUDE" ) 2>"$TMP_HOME/err"; then echo "MARK:started"; else echo "MARK:refused"; fi
echo "MARK:err"; cat "$TMP_HOME/err"
echo "MARK:spawned"; cat "$TMP_HOME/spawned" 2>/dev/null || true
kill "$(cat "$TMP_HOME/claude-remote-control.pid" 2>/dev/null)" 2>/dev/null || true
"""


class TestTheGateFollowsTheTargetAndNotThisMachine(unittest.TestCase):
    """A guest is never handed the rotating login and holds one of its own, so
    "can this workspace authenticate" is a question for the target driver
    (t_agent_secret_present, lib/target.sh). Asking this machine's store would
    let a container's credential vouch for a guest that has none, and refuse a
    guest that has logged in on a machine that never did."""

    @staticmethod
    def _probe(target_says, login):
        tmp = tempfile.mkdtemp(prefix="wk-rc-target-")
        env = {"WK_STORE": tmp, "TARGET_SAYS": target_says}
        env.update(credential_env(tmp, login=login))
        return bash(_PROBE_TARGET_ANSWERS.replace("__REPO__", str(REPO)),
                    env=env, timeout=60)

    def test_a_target_that_says_no_refuses_though_the_store_is_full(self):
        cp = self._probe("no", login=True)
        self.assertIn("MARK:refused", cp.stdout, cp.stdout + cp.stderr)
        self.assertEqual("", _section(cp.stdout, "spawned").strip(), cp.stdout)

    def test_the_refusal_prints_the_targets_own_remedy(self):
        """The remedy differs by target -- store one here, or log in in there
        -- so it comes from the driver rather than from one baked sentence."""
        cp = self._probe("no", login=True)
        err = _section(cp.stdout, "err")
        self.assertIn("log in inside the guest", err, err)

    def test_a_target_that_says_yes_starts_though_the_store_is_empty(self):
        cp = self._probe("yes", login=False)
        self.assertIn("MARK:started", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("SPAWNED", _section(cp.stdout, "spawned"), cp.stdout)


class TestNothingAsksTheStoreDirectly(unittest.TestCase):
    """One implementation of the rule: the commands that gate on the login ask
    the driver, so a target whose workspaces authenticate for themselves is not
    a second code path in each of them."""

    def test_neither_command_reads_this_machines_store_for_it(self):
        for f in ("cmd/ai", "cmd/verify"):
            with self.subTest(command=f):
                text = (REPO / f).read_text()
                self.assertNotIn("wk_agent_secret_present claude-login", text)
                self.assertIn("t_agent_secret_present", text)


class TestRemoteControlRefusesWithoutTheLoginCredential(unittest.TestCase):
    """Remote control needs the claude.ai account login: measured in the CLI,
    a long-lived `claude setup-token` token is inference-only and refused for
    lacking the user:profile scope. Without one, rc_start refuses *before* it
    spawns -- otherwise the answer is a server that starts, prints that
    refusal and dies, reported three lines deep in a log."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.mkdtemp(prefix="wk-rc-nocred-")
        env = {"WK_STORE": tmp}
        env.update(credential_env(tmp, login=False))
        cls.cp = bash(_PROBE_NO_CREDENTIAL.replace("__REPO__", str(REPO)),
                      env=env, timeout=60)

    def test_it_refuses(self):
        self.assertIn("MARK:refused", self.cp.stdout, self.cp.stdout + self.cp.stderr)

    def test_it_names_the_remedy(self):
        err = _section(self.cp.stdout, "err")
        self.assertIn("wk key set claude-login", err, err)
        self.assertIn("claude auth login", err, err)

    def test_nothing_was_spawned(self):
        """Before the spawn, not after: a server started and killed by its own
        credential check is a running process, a pid file and a log to read."""
        self.assertEqual("", _section(self.cp.stdout, "spawned").strip(),
                         self.cp.stdout)

    def test_it_says_the_inference_token_is_not_a_substitute(self):
        """`wk key set claude` stores one, and it is the obvious wrong guess."""
        err = _section(self.cp.stdout, "err")
        self.assertIn("setup-token", err, err)


# What stands between a pid a workspace wrote into a file in its own home and a
# signal: the pattern the job declared (lib/watchdog.sh). The fake driver runs
# the command here, so `ps -o args= -p <pid>` reads this machine's process
# table -- which is what a wkdev container's shares with it anyway.
_SIGNAL_PROBE = r'''
set -euo pipefail
exec 2>&1
export WK_ROOT="__REPO__"
export WK_CLAUDE_LIB=1
. "__REPO__/cmd/ai"

WS="probe-ws"
mkdir -p "$(wk_ws_dir "$WS")"
TMP=$(mktemp -d)

t_exec() { local name="$1"; shift; "$@"; }
t_home() { printf '%s' "$TMP"; }

cat > "$TMP/claude" <<'EOS'
#!/bin/sh
sleep 20
EOS
chmod +x "$TMP/claude"
"$TMP/claude" remote-control --spawn=same-dir --name "$WS" & SERVER=$!
sleep 20 & OTHER=$!

begin() { task_begin rc target "$WS" "wk ai claude $WS --rc --stop" "$TMP/log" \
              "claude remote-control in $WS"; }
alive() { kill -0 "$1" 2>/dev/null && echo "$2=alive" || echo "$2=gone"; }

echo "MARK:adopt-wrong-argv"
T=$(begin)
job_pid_adopt "$WS" "$T" "$OTHER" "$RC_PID_MATCH" && echo ADOPTED || echo REFUSED
echo "pid=$(task_field "$T" pid)"
alive "$OTHER" other

echo "MARK:signal-wrong-argv"
T=$(begin)
job_pid_adopt "$WS" "$T" "$SERVER" "$RC_PID_MATCH" && echo ADOPTED || echo REFUSED
echo "pid_match=$(task_field "$T" pid_match)"
task_pid "$T" "$OTHER"
( rc_stop "$WS" ) && echo STOPPED || echo REFUSED
alive "$OTHER" other
alive "$SERVER" server

echo "MARK:stop-matching-argv"
T=$(begin)
job_pid_adopt "$WS" "$T" "$SERVER" "$RC_PID_MATCH" && echo ADOPTED || echo REFUSED
( rc_stop "$WS" ) && echo STOPPED || echo REFUSED
alive "$SERVER" server
echo "verdict=$(task_verdict "$(rc_task "$WS")")"

kill "$OTHER" "$SERVER" 2>/dev/null || true
'''


class TestOnlyTheServerItStartedIsSignalled(unittest.TestCase):
    """The pid rc_stop signals comes out of a file in the workspace's own home,
    and a wkdev container shares the host's PID namespace (wkdev-create passes
    --pid host), so an unchecked pid is a signal at another workspace's build.
    The rc record declares the command line its pid must have (RC_PID_MATCH)
    and lib/watchdog.sh refuses every signal at a pid that has another."""

    @classmethod
    def setUpClass(cls):
        cls._store = temp_store()
        store = cls._store.__enter__()
        cls.out = bash(_SIGNAL_PROBE.replace("__REPO__", str(REPO)),
                       env={"WK_STORE": store["path"].as_posix()}, timeout=120).stdout

    @classmethod
    def tearDownClass(cls):
        cls._store.__exit__(None, None, None)

    def test_a_pid_running_something_else_is_not_adopted(self):
        s = _section(self.out, "adopt-wrong-argv")
        self.assertIn("REFUSED", s, self.out)
        self.assertIn("pid=\n", s + "\n", self.out)
        self.assertIn("other=alive", s, self.out)

    def test_the_server_is_adopted_with_its_pattern_recorded(self):
        s = _section(self.out, "signal-wrong-argv")
        self.assertIn("ADOPTED", s, self.out)
        self.assertIn("pid_match=*claude*remote-control*", s, self.out)

    def test_a_record_whose_pid_is_another_process_signals_nothing(self):
        s = _section(self.out, "signal-wrong-argv")
        self.assertIn("REFUSED", s, self.out)
        self.assertIn("other=alive", s, self.out)
        self.assertIn("server=alive", s, self.out)
        self.assertIn("wk enter probe-ws", self.out)

    def test_the_matching_server_is_stopped_and_the_record_says_so(self):
        s = _section(self.out, "stop-matching-argv")
        self.assertIn("STOPPED", s, self.out)
        self.assertIn("server=gone", s, self.out)
        self.assertIn("verdict=stopped", s, self.out)


# `wk stop <ws>` and `wk rm <ws>` take a workspace away from a running remote
# control, and a checkout on a build machine has no container or guest whose
# end takes the server with it -- so both stop it themselves, through the one
# implementation (rc_stop, lib/watchdog.sh), pid pattern and all.
#
# The target is a real one: a `remote` whose machine is this one
# (WK_REMOTE_LOCAL), named by a conf in a scratch registry, so `wk` resolves
# the name, loads the driver and runs t_exec for itself.
_CMD_PROBE = r"""
set -uo pipefail
exec 2>&1
export WK_ROOT="__REPO__"
TMP=$(mktemp -d)
mkdir -p "$TMP/ws/probe-ws/WebKit" "$TMP/reg"
cat > "$TMP/reg/probehost.conf" <<EOF
WK_TARGET_KIND=remote
WK_REMOTE_LOCAL=1
WK_REMOTE_ROOT=$TMP
WK_REMOTE_HOST=localhost
EOF
export WK_TARGET_REGISTRY="$TMP/reg" WK_TARGET=probehost WK_STORE="$TMP" WK_YES=1

. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/task.sh"
. "$WK_ROOT/lib/watchdog.sh"
set +e
t_exec() { local n="$1"; shift; "$@"; }

cat > "$TMP/claude" <<'EOC'
#!/bin/sh
sleep 30
EOC
chmod +x "$TMP/claude"
# >/dev/null, or the command substitution waits on the pipe the background
# server holds open.
server() { "$TMP/claude" remote-control --spawn=same-dir --name probe-ws >/dev/null 2>&1 & echo $!; }
begin() { task_begin rc target probe-ws "wk ai claude probe-ws --rc --stop" "$TMP/log" \
              "claude remote-control in probe-ws"; }
alive() { kill -0 "$1" 2>/dev/null && echo "$2=alive" || echo "$2=gone"; }
adopt() { job_pid_adopt probe-ws "$1" "$2" '*claude*remote-control*' >/dev/null \
              || echo NOT-ADOPTED; }

SERVER=$(server)
sleep 30 & OTHER=$!

echo "MARK:stop-wrong-pid"
T=$(begin); adopt "$T" "$SERVER"; task_pid "$T" "$OTHER"
"$WK_ROOT/wk" stop probe-ws
alive "$OTHER" other
alive "$SERVER" server

echo "MARK:stop-matching-pid"
T=$(begin); adopt "$T" "$SERVER"
"$WK_ROOT/wk" stop probe-ws
alive "$SERVER" server
echo "verdict=$(task_verdict "$(task_find rc probe-ws)")"

echo "MARK:rm-matching-pid"
SERVER=$(server)
T=$(begin); adopt "$T" "$SERVER"
"$WK_ROOT/wk" rm probe-ws
alive "$SERVER" server
[ -d "$TMP/ws/probe-ws" ] && echo "workspace=here" || echo "workspace=gone"

kill "$OTHER" "$SERVER" 2>/dev/null
rm -rf "$TMP"
"""


@unittest.skipUnless(shutil.which("podman"), "wk stop needs podman on PATH")
class TestStopAndRmStopItThroughTheOneImplementation(unittest.TestCase):
    """A workspace that goes away takes its remote control with it, and the
    pid it signals is checked against the pattern the record declares -- the
    same refusal `wk ai claude <ws> --rc --stop` makes, because it is the
    same function."""

    @classmethod
    def setUpClass(cls):
        cls.out = bash(_CMD_PROBE.replace("__REPO__", str(REPO)), timeout=180).stdout

    def test_wk_stop_refuses_a_pid_running_something_else(self):
        s = _section(self.out, "stop-wrong-pid")
        self.assertIn("refusing to send TERM", s, self.out)
        self.assertIn("other=alive", s, self.out)
        self.assertIn("server=alive", s, self.out)

    def test_wk_stop_stops_the_server_it_matches(self):
        s = _section(self.out, "stop-matching-pid")
        self.assertIn("stopping claude remote-control", s, self.out)
        self.assertIn("server=gone", s, self.out)
        self.assertIn("verdict=stopped", s, self.out)

    def test_wk_rm_stops_it_before_it_destroys_the_workspace(self):
        s = _section(self.out, "rm-matching-pid")
        self.assertIn("server=gone", s, self.out)
        self.assertIn("workspace=gone", s, self.out)

    def test_neither_command_signals_a_pid_of_its_own(self):
        """One implementation: a second `t_exec <ws> kill <pid>` is a second
        place the pattern can be forgotten."""
        for rel in ("cmd/stop", "cmd/rm"):
            text = (REPO / rel).read_text()
            with self.subTest(cmd=rel):
                self.assertIn('rc_stop "$NAME"', text)
                self.assertNotIn('t_exec "$NAME" kill', text)
