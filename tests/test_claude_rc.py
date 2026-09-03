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
cat "$(rc_status_file "$WS")"

rc_stop "$WS"
sleep 0.3

echo "MARK:status-stopped"
cat "$(rc_status_file "$WS")"

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

    def test_status_file_records_pid_and_log_while_running(self):
        """the status file (lib/detach.sh's schema) carries state=running, pid= and log="""
        out = self._run_probe()
        sf = _section(out, "status-running")
        self.assertIn("state=running", sf, out)
        self.assertRegex(sf, r"(?m)^pid=\d+$")
        self.assertRegex(sf, r"(?m)^log=.+")

    def test_stop_writes_state_stopped(self):
        """rc_stop writes state=stopped, and rc_alive is false afterwards"""
        out = self._run_probe()
        sf = _section(out, "status-stopped")
        self.assertIn("state=stopped", sf, out)
        alive = _section(out, "alive-after-stop")
        self.assertEqual(alive.strip(), "no", out)


if __name__ == "__main__":
    unittest.main()


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
echo "MARK:status"; cat "$(rc_status_file "$WS")"
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

    def test_the_record_says_stopped(self):
        status = self.cp.stdout.split("MARK:status", 1)[1]
        self.assertIn("state=stopped", status)
        self.assertNotIn("pid=", status)


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
