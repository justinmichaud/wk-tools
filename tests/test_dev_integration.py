"""DevIntegrationTest: one workspace per target, taken through what a developer does with one -- the credentials,
`wk new`, git, both agents, the push switch, a build, git-webkit's credentials, `wk sync` and Zed -- against the
real machines. Each step converges from evidence, so a re-run joins what is already there; the workspace persists
between runs, and only the last step removes it, once every step before it passed in the same run.

A target (container, tart, moose, bb4):   wk selftest --live DevIntegrationContainer
From step N on:                         WK_INTEG_FROM=7 wk selftest --live DevIntegrationContainer
One step:                               wk selftest --live '*DevIntegrationContainer.test_07*'

While the target's own build runs, `wk selftest --live` refuses beside it: add --force, or run
python3 tests/run.py --live -k DevIntegrationContainer. Push is turned on only under PushGuard, and off again
however the run ends.
"""
import functools
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from tests import support
from tests.support import REPO, WK

sys.path.insert(0, str(REPO / "lib"))
from wk.wall import CSI  # noqa: E402

PREFIX = "integ-"
STEP_BUDGET = 1800.0      # tests/run.py's live budget, per step
MARGIN = 90.0             # what a step keeps back from the budget for the calls after its wait
REPLY = "WK-INTEG-OK"
PROMPT = "Reply with exactly the text %s and nothing else." % REPLY
CREDENTIALS = ("claude", "litellm", "github-pat", "bugzilla-api-key")
PROBE_BRANCH = "refs/heads/wk-integ-probe"

# The first `git status` in the checkout, cold: measured once per target on 2026-09-27.
GIT_STATUS_BUDGET = {
    "container": 3.0,     # 0.84 s cold and 0.46 s warm in a new container: headroom for a loaded podman VM
    "tart": 3.0,          # 0.82 s cold in a guest sharing the host with the podman VM (2026-10-01)
    "moose": 3.0,         # 0.36 s cold in a container on moose (2026-10-04)
}

TARGETS = {
    "container": {"new": (), "machine": None, "push": True},
    "tart": {"new": ("--target", "vm"), "machine": None, "push": True},
    # A peer workstation: `wk new --target moose` is moose's own `wk new`, a container there, and every later
    # command (push included, as `--target moose`) is moose's own wk.
    "moose": {"new": ("--target", "moose"), "machine": "moose", "push": True,
              "remedy": "moose pulls its own checkout: push this commit to the branch moose's checkout tracks, "
                        "then 'wk sync --tools moose'"},
    "bb4": {"new": ("--target", "buildbox4"), "machine": "buildbox4", "push": False,
            "remedy": "'wk sync --tools buildbox4' from a clean tree here"},
}

# lib/wk/pushswitch.py's scan, and `ps` where there is no /proc (a macOS guest).
AGENT_SCAN = r'''if [ -d /proc/self ]; then
    for e in /proc/[0-9]*/exe; do
        case "$(readlink "$e" 2>/dev/null)" in
            */claude/versions/*|*/.local/bin/claude) p=${e#/proc/}; printf "%s\n" "${p%/exe}" ;;
        esac
    done
else
    ps -Ao pid=,comm= | awk '$2 ~ /(^|\/)claude$|\/claude\/versions\// {print $1}'
fi'''

GIT_PROBE = r'''import json, subprocess, time
t = time.time()
subprocess.run(["git", "status", "--porcelain"], stdout=subprocess.DEVNULL, check=True)
took = time.time() - t
f = subprocess.run(["git", "fetch", "--dry-run", "--all"], capture_output=True, text=True)
print("WK-GIT " + json.dumps({"status_seconds": took, "fetch_rc": f.returncode,
      "would_update": [l for l in (f.stdout + f.stderr).splitlines() if l.strip() and not l.startswith("Fetching ")]}))'''

# webkitscmpy's and webkitbugspy's own credential lookups: the GitHub user, an empty pull request (422 when the
# write is authenticated, the injector's 412 when it is not; nothing is created either way), and the Bugzilla
# user record, which carries `email` only for a caller logged in as that user.
CRED_PROBE = r'''import contextlib, io, json, os, re, subprocess, sys
sys.path.insert(0, "Tools/Scripts")
import webkitpy  # noqa: F401 -- its autoinstall provides what webkitscmpy imports
from webkitscmpy import remote
from webkitbugspy import bugzilla
out = {}
url = subprocess.run(["git", "config", "--get", "remote.fork.url"], capture_output=True, text=True).stdout.strip()
fork = remote.GitHub(url[:-len(".git")] if url.endswith(".git") else url)
me = fork.request(endpoint_url=fork.api_url + "/user", authenticated=True, paginate=False)
out["github_user"] = (me or {}).get("login")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    fork.request(path="pulls", method="POST", json={}, authenticated=True, paginate=False)
m = re.search(r"returned status code '(\d+)'", err.getvalue())
out["github_write"] = int(m.group(1)) if m else None
bz = bugzilla.Tracker("https://bugs.webkit.org")
user, _ = bz.credentials(required=True)
r = bz.session.get("https://bugs.webkit.org/rest/user" + bz._login_arguments(required=True, query="names=" + user), timeout=60)
rows = r.json().get("users") or []
out["bugzilla_user"] = user
out["bugzilla_logged_in"] = r.status_code == 200 and bool(rows) and "email" in rows[0]
print("WK-CRED " + json.dumps(out))'''

GUARD = "import subprocess, sys\nsys.stdin.buffer.read()\nsys.exit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL))\n"


# -- the evidence, read from what wk prints

def clean(text):
    return CSI.sub("", text or "").replace("\r", "")


def credential_rows(doctor_text):
    """{name: (mark, line)} for each credential `wk doctor` rates; `fork push key` stands for the deploy keys."""
    rows = {}
    for line in clean(doctor_text).splitlines():
        m = re.match(r"^  (ok|--|\?\?)\s{2,}(.*)$", line)
        if not m:
            continue
        mark, what = m.groups()
        for name in CREDENTIALS + ("fork push key",):
            if re.match(r"%s( --|:| reaches further|\s{2,}->|$)" % re.escape(name), what):
                rows.setdefault(name, (mark, line.strip()))
    return rows


def credential_problems(rows, names):
    """The rows that stop a target: absent, or anything but ok and "reaches further than wk spends it"."""
    out = []
    for name in names:
        mark, line = rows.get(name, ("", "%s: not in wk doctor's output at all" % name))
        if not (mark == "ok" or (mark == "??" and " reaches further " in line)):
            out.append(line)
    return out


def records(text):
    out = []
    for line in clean(text).splitlines():
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def workspace_record(text, ws):
    return next((r for r in records(text) if r.get("kind") == "workspace" and r.get("name") == ws), None)


def workspace_action(rec):
    """none | new | start | broken, from the workspace's `wk status --records` row (None when there is none)."""
    if rec is None or rec.get("ws") == "creating" or rec.get("state") == "creating":
        return "new"
    if rec.get("ws") == "broken" or rec.get("state") == "broken":
        return "broken"
    return "none" if rec.get("state") in ("running", "present") else "start"   # a build box's workspace has no running state


def last_build(rec):
    """(state, config) of the build the workspace's record names, or (None, None)."""
    for sub in (rec or {}).get("subs") or []:
        if sub.get("kind") == "build":
            return sub.get("state"), sub.get("config")
    return None, None


def tagged(text, tag):
    for line in clean(text).splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    return None


def ls_rows(text):
    """(name, target, state) for each `wk ls` row."""
    rows = []
    for line in clean(text).splitlines()[1:]:
        f = line.split()
        if len(f) >= 3:
            rows.append((f[0], f[1], f[2]))
    return rows


def start_step():
    return int(os.environ.get("WK_INTEG_FROM") or 1)


def real_env():
    """This machine's environment as a person's shell has it: tests/support.py points the credential APIs, the
    tailnet keys and the dispatcher's variables away from the real ones for every other test."""
    env = {k: v for k, v in os.environ.items() if k not in support.DISPATCH_VARS}
    for k, v in list(env.items()):
        if v == support.NO_GITHUB or v.startswith(support.NO_SECRETS):
            del env[k]
    return env


class Ran:
    def __init__(self, rc, out):
        self.rc, self.out = rc, clean(out)


def wk(*args, timeout=600, env=None):
    try:
        cp = subprocess.run([str(WK), *args], cwd=str(REPO), env=env or real_env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return Ran(124, out + "\n[timed out after %ds: wk %s]" % (timeout, " ".join(args)))
    return Ran(cp.returncode, cp.stdout)


def tail(text, n=40):
    return "\n".join(text.splitlines()[-n:])


class PushGuard:
    """Push stays on while this is held: a child in a session of its own reads a pipe only this process writes, and
    runs `wk key push off` when it closes -- at release(), or when this process ends however it ends."""

    def __init__(self, off_argv):
        self.log = tempfile.NamedTemporaryFile(prefix="wk-integ-push-guard-", suffix=".log", delete=False)
        r, self.w = os.pipe()
        self.proc = subprocess.Popen([sys.executable, "-c", GUARD, *off_argv], stdin=r, stdout=self.log,
                                     stderr=subprocess.STDOUT, env=real_env(), cwd=str(REPO), start_new_session=True)
        os.close(r)

    def release(self):
        if self.w is not None:
            os.close(self.w)
            self.w = None
        rc = self.proc.wait(timeout=300)
        with open(self.log.name) as f:
            text = f.read()
        os.unlink(self.log.name)
        return rc, clean(text)


def step(n, needs_workspace=True):
    def deco(fn):
        @functools.wraps(fn)
        def run(self):
            cls = type(self)
            if n < start_step():
                self.skipTest("WK_INTEG_FROM=%d starts after step %d" % (start_step(), n))
            if needs_workspace and cls.outcome.get(2) in ("failed", "skipped"):
                self.skipTest("step 2 %s in this run, so there is no workspace to run step %d in" % (cls.outcome[2], n))
            self.t0 = time.monotonic()
            try:
                fn(self)
            except unittest.SkipTest:
                cls.outcome[n] = "skipped"
                raise
            except BaseException:
                cls.outcome[n] = "failed"
                raise
            cls.outcome[n] = "passed"
        run.wk_step = n
        return run
    return deco


class TargetSteps:
    """The steps, in order; a target class names its target."""

    target = ""

    @classmethod
    def setUpClass(cls):
        cls.ws = PREFIX + cls.target
        cls.conf = TARGETS[cls.target]
        cls.outcome = {}
        cls.guard = None
        cls.background = None

    @classmethod
    def tearDownClass(cls):
        cls.stop_background()
        if cls.guard:
            rc, text = cls.guard.release()
            cls.guard = None
            if rc != 0:
                sys.stderr.write("[%s] push guard: 'wk key push off' exited %d:\n%s\n" % (cls.ws, rc, text))

    # -- helpers

    def left(self):
        return STEP_BUDGET - MARGIN - (time.monotonic() - self.t0)

    def wk_ok(self, *args, timeout=600, why=""):
        r = wk(*args, timeout=timeout)
        self.assertEqual(0, r.rc, "%s'wk %s' exited %d:\n%s" % (why and why + "\n", " ".join(args), r.rc, tail(r.out)))
        return r

    def push_args(self, verb, *more):
        machine = self.conf["machine"]
        return ("key", "push", verb) + (("--target", machine) if machine else ()) + more

    def record(self):
        return workspace_record(wk("status", self.ws, "--records", timeout=300).out, self.ws)

    def src(self):
        from wk import targets
        reg = targets.Registry(str(REPO), env=real_env())
        return reg.load(reg.ws_target(self.ws)).src(self.ws)

    def inside(self, script, timeout=300):
        return wk("enter", self.ws, "--", "bash", "-lc", script, timeout=timeout)

    def in_checkout(self, code, tag, timeout=300):
        r = wk("enter", self.ws, "--", "bash", "-lc", 'cd "$1" && exec python3 -c "$2" </dev/null', "wk-integ",
               self.src(), code, timeout=timeout)
        found = tagged(r.out, tag)
        self.assertIsNotNone(found, "the probe in '%s' printed no %s line (exit %d):\n%s" % (self.ws, tag, r.rc, tail(r.out)))
        return found

    def agent_pids(self, ws):
        return [p for p in wk("enter", ws, "--", "sh", "-c", AGENT_SCAN, timeout=120).out.split() if p.isdigit()]

    def foreign_sessions(self):
        """Workspaces on the switch's machine the test did not make, with a claude process in them: `wk key push on` ends or arms it."""
        host = self.conf["machine"] or support.THIS_HOST
        return [n for n, t, st in ls_rows(wk("ls", timeout=300).out)
                if not n.startswith(PREFIX) and st == "running" and t.split(":")[0].lower() == host and self.agent_pids(n)]

    def need_push_target(self):
        if not self.conf["push"]:
            self.skipTest("'%s' is a build machine, which has no push switch ('wk key push' exits 5 there): "
                          "its keys are live wherever they sit" % self.conf["machine"])
        foreign = self.foreign_sessions()
        if foreign:
            self.skipTest("a claude session runs in %s, which this test did not make, and 'wk key push on' "
                          "would end it (or arm it, with --force)" % " ".join(foreign))

    def ensure_push_on(self):
        cls = type(self)
        if cls.guard is None:
            cls.guard = PushGuard([str(WK), *self.push_args("off")])
        if wk(*self.push_args("status"), timeout=300).rc != 0:
            self.wk_ok(*self.push_args("on", "--yes"), timeout=600)
        self.assertEqual(0, wk(*self.push_args("status"), timeout=300).rc, "push is not on after 'wk key push on'")

    def push_off(self):
        cls = type(self)
        if cls.guard is not None:
            rc, text = cls.guard.release()
            cls.guard = None
            self.assertEqual(0, rc, "'wk key push off' (the guard's) exited %d:\n%s" % (rc, tail(text)))
        else:
            self.wk_ok(*self.push_args("off"), timeout=600)
        self.assertEqual(1, wk(*self.push_args("status"), timeout=300).rc, "push is not off after 'wk key push off'")

    @classmethod
    def stop_background(cls):
        p = cls.background
        cls.background = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()

    def start_background_claude(self):
        """A claude session that outlives the calls made about it, the way a person's does. Not `sleep`: Claude Code
        moves a long one to the background and the session ends in seconds (measured 2026-09-27)."""
        type(self).background = subprocess.Popen(
            [str(WK), "ai", "claude", self.ws, "-p", 'Use your Bash tool, in the foreground with a 600000 ms timeout, to run exactly: '
             'python3 -c "import time; time.sleep(420)". Then reply DONE.'],
            cwd=str(REPO), env=real_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            pids = self.agent_pids(self.ws)
            if pids:
                return pids
            self.assertIsNone(type(self).background.poll(), "the background 'wk ai claude' ended before its session ran")
            time.sleep(5)
        self.fail("no claude process appeared in '%s' within 240s of 'wk ai claude'" % self.ws)

    def remotes_current(self, measure_status):
        got = self.in_checkout(GIT_PROBE, "WK-GIT")
        self.assertEqual(0, got["fetch_rc"], "git fetch --dry-run --all failed in '%s'" % self.ws)
        self.assertEqual([], got["would_update"], "a remote in '%s' is behind what it fetches from" % self.ws)
        if measure_status:
            budget = GIT_STATUS_BUDGET.get(self.target)
            self.assertIsNotNone(budget, "no git status budget is measured for the %s target yet: this run took %.2fs "
                                 "-- put a budget in GIT_STATUS_BUDGET with a one-line why" % (self.target, got["status_seconds"]))
            self.assertLess(got["status_seconds"], budget, "git status took %.2fs in '%s'" % (got["status_seconds"], self.ws))

    def agent_replies(self, agent, *extra):
        r = wk("ai", agent, self.ws, *(extra + ("-p", PROMPT)), timeout=900)
        self.assertEqual(0, r.rc, "'wk ai %s %s' exited %d:\n%s" % (agent, self.ws, r.rc, tail(r.out)))
        self.assertIn(REPLY, [l.strip() for l in r.out.splitlines()], "%s did not answer:\n%s" % (agent, tail(r.out)))
        return r

    def dry_push(self):
        return self.inside('cd "%s" && git push --dry-run fork HEAD:%s 2>&1' % (self.src(), PROBE_BRANCH), timeout=180)

    # -- the steps

    @step(1, needs_workspace=False)
    def test_01_doctor_has_every_credential(self):
        r = wk("doctor", timeout=600)
        problems = credential_problems(credential_rows(r.out), CREDENTIALS + ("fork push key",))
        self.assertEqual([], problems, "wk doctor says:\n  " + "\n  ".join(problems))

    @step(2, needs_workspace=False)
    def test_02_workspace_is_ready(self):
        action = workspace_action(self.record())
        made = action == "new"
        if action == "broken":
            self.fail("'%s' is broken; wk says:\n%s" % (self.ws, tail(wk("status", self.ws, "--text", timeout=300).out)))
        if action == "new":
            self.wk_ok("new", self.ws, *self.conf["new"], timeout=int(self.left()))
            action = workspace_action(self.record())
        if action == "start":   # a guest is made stopped
            r = wk("start", self.ws, timeout=int(self.left()))
            if "not enough memory to start" in r.out:
                self.skipTest("'wk start %s' needs a person:\n%s" % (self.ws, tail(r.out, 15)))
            self.assertEqual(0, r.rc, "'wk start %s' exited %d:\n%s" % (self.ws, r.rc, tail(r.out)))
        wk("status", self.ws, "--wait", "--timeout", str(int(self.left())), timeout=int(self.left()) + 30)
        rec = self.record()
        self.assertEqual("none", workspace_action(rec), "'%s' is not running and ready: %r" % (self.ws, rec))
        if not made:    # a workspace this run joined fetches what the mirror gained since, as a returning developer does
            self.wk_ok("sync", self.ws, timeout=900)
        claude = self.inside('"$HOME/.local/bin/claude" --version || claude --version', timeout=120)
        self.assertEqual(0, claude.rc, "'%s' was made without a Claude CLI that runs:\n%s" % (self.ws, tail(claude.out)))

    @step(3)
    def test_03_git_is_fast_and_current(self):
        self.remotes_current(measure_status=True)

    @step(4)
    def test_04_claude_starts_after_the_sandbox_check(self):
        r = self.agent_replies("claude", *(("--force",) if self.conf["machine"] == "buildbox4" else ()))
        evidence = "is not run for" if self.conf["machine"] == "buildbox4" else "sandbox intact"
        self.assertIn(evidence, r.out, "no sign the sandbox check ran:\n%s" % tail(r.out))

    @step(5)
    def test_05_push_on_never_coexists_with_claude(self):
        self.need_push_target()
        self.push_off()
        try:
            pids = self.start_background_claude()
            cls = type(self)
            cls.guard = PushGuard([str(WK), *self.push_args("off")])
            self.wk_ok(*self.push_args("on", "--force", "--yes"), why="--force keeps the session and loads the keys")
            self.assertEqual(0, wk(*self.push_args("status")).rc, "push is not on after 'wk key push on --force'")
            self.assertTrue(set(pids) & set(self.agent_pids(self.ws)), "--force ended the claude session it was to keep")
            inside = wk("enter", self.ws, "--", "bash", "-lc", "wk key push on --force", timeout=120)
            self.assertNotEqual(0, inside.rc, "'wk key push on --force' worked inside '%s':\n%s" % (self.ws, tail(inside.out)))
            self.assertIn("throws the credential switch", inside.out)
            self.push_off()
            self.assertTrue(self.agent_pids(self.ws), "the claude session ended before 'wk key push on' was asked about it")
            cls.guard = PushGuard([str(WK), *self.push_args("off")])
            on = self.wk_ok(*self.push_args("on", "--yes"))
            self.assertIn("ending the claude session", on.out)
            self.assertEqual([], self.agent_pids(self.ws), "push is on and claude still runs in '%s'" % self.ws)
        finally:
            self.stop_background()
            self.push_off()

    @step(6)
    def test_06_pi_answers(self):
        self.agent_replies("pi", *(("--force",) if self.conf["machine"] == "buildbox4" else ()))

    @step(7)
    def test_07_jsc_debug_builds(self):
        state, config = last_build(self.record())
        if (state, config) == ("ok", "jsc-debug"):
            return
        if wk("status", self.ws, timeout=300).rc != 2:
            self.wk_ok("build", self.ws, "jsc-debug", "--detach", timeout=600)
        waited = wk("status", self.ws, "--wait", "--timeout", str(int(self.left())), timeout=int(self.left()) + 60)
        if waited.rc == 2:
            self.fail("jsc-debug is still building in '%s' after this step's budget; re-run the step to join it" % self.ws)
        state, config = last_build(self.record())
        if (state, config) != ("ok", "jsc-debug"):
            self.fail("the jsc-debug build in '%s' ended %s (%s):\n%s"
                      % (self.ws, state, config, tail(wk("status", self.ws, "--log", timeout=300).out, 60)))

    @step(8)
    def test_08_push_on(self):
        self.need_push_target()
        self.ensure_push_on()

    @step(9)
    def test_09_git_webkit_reads_with_the_credentials(self):
        self.need_push_target()
        self.ensure_push_on()
        r = self.dry_push()
        self.assertEqual(0, r.rc, "git push --dry-run to the fork did not authenticate:\n%s" % tail(r.out))
        got = self.in_checkout(CRED_PROBE, "WK-CRED")
        self.assertTrue(got["github_user"], "webkitscmpy's authenticated GET /user answered nothing: %r" % got)
        self.assertEqual(422, got["github_write"], "an empty pull request should be refused by GitHub itself: %r" % got)
        self.assertTrue(got["bugzilla_logged_in"], "webkitbugspy's user lookup was not logged in: %r" % got)

    @step(10)
    def test_10_claude_never_runs_with_push_on(self):
        """A Mac's container session is started in the podman machine, which holds half the switch and cannot throw the
        host's: it refuses until `wk key push off` here. Where the whole switch is in reach, it is thrown first."""
        self.need_push_target()
        self.ensure_push_on()
        if self.target == "container" and sys.platform == "darwin":
            r = wk("ai", "claude", self.ws, "-p", PROMPT, timeout=900)
            self.assertNotEqual(0, r.rc, "claude started with push on:\n%s" % tail(r.out))
            self.assertIn("could not hold back the push keys", r.out)
            self.assertNotIn(REPLY, [l.strip() for l in r.out.splitlines()])
            self.push_off()
            self.agent_replies("claude")
            return
        r = self.agent_replies("claude")
        self.assertIn("turning it off while the agent runs", r.out)
        self.assertLess(r.out.index("turning it off"), r.out.index("starting Claude"))
        self.assertIn("no identity reaches this workspace", r.out, "the session's own wall check saw a key")
        self.assertIn("git push stays off", r.out)

    @step(11)
    def test_11_push_off_reaches_nothing(self):
        self.need_push_target()
        self.push_off()
        self.wk_ok("doctor", self.ws, timeout=600, why="the wall around '%s' with push off" % self.ws)
        r = self.dry_push()
        self.assertNotEqual(0, r.rc, "git push --dry-run to the fork authenticated with push off:\n%s" % tail(r.out))
        self.assertIn("publickey", r.out)
        got = self.in_checkout(CRED_PROBE, "WK-CRED")
        self.assertEqual(412, got["github_write"], "a write with push off should be the injector's 412: %r" % got)
        self.assertFalse(got["bugzilla_logged_in"], "webkitbugspy is logged in to Bugzilla with push off: %r" % got)

    @step(12)
    def test_12_sync_leaves_every_remote_current(self):
        self.wk_ok("sync", self.ws, timeout=900)
        self.remotes_current(measure_status=False)

    @step(13)
    def test_13_zed_connects(self):
        if sys.platform != "darwin":
            self.skipTest("Zed is driven on the Mac this suite runs on")
        if under_zed():
            self.skipTest("this test runs inside Zed's terminal, and quitting Zed would kill the test itself")
        quit_zed()
        self.assertEqual([], zed_pids(), "the person's Zed did not quit, so the test cannot tell its own window apart")
        try:
            self.wk_ok("zed", self.ws, timeout=300)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not self.zed_servers():
                time.sleep(5)
            self.assertTrue(zed_pids(), "no Zed process after 'wk zed %s'" % self.ws)
            self.assertTrue(self.zed_servers(), "Zed never connected to '%s' (no zed-remote-server in it)" % self.ws)
        finally:
            quit_zed()
            self.inside("pkill -f '[z]ed-remote-server'; true", timeout=120)
        self.assertEqual([], zed_pids(), "Zed did not quit")

    def zed_servers(self):
        return [p for p in self.inside("pgrep -f '[z]ed-remote-server' || true", timeout=120).out.split() if p.isdigit()]

    @step(14)
    def test_14_remove_the_workspace(self):
        passed = [n for n in range(1, 14) if type(self).outcome.get(n) == "passed"]
        if len(passed) != 13:
            self.skipTest("the workspace stays until steps 1-13 all pass in one run (passed: %s)" % passed)
        self.wk_ok("rm", self.ws, "--yes", timeout=900)
        self.assertIsNone(self.record(), "'%s' is still there after 'wk rm'" % self.ws)


def under_zed():
    pid = os.getppid()
    while pid > 1:
        r = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True)
        ppid, _, comm = r.stdout.strip().partition(" ")
        if not ppid.isdigit():
            return False
        if comm.strip().endswith("Zed.app/Contents/MacOS/zed"):
            return True
        pid = int(ppid)
    return False


def quit_zed():
    subprocess.run(["osascript", "-e", 'quit app "Zed"'], capture_output=True, timeout=60)
    deadline = time.monotonic() + 30
    while zed_pids() and time.monotonic() < deadline:
        time.sleep(1)


def zed_pids():
    r = subprocess.run(["pgrep", "-f", "Zed.app/Contents/MacOS/zed"], capture_output=True, text=True)
    return r.stdout.split()


# -- the gates: nothing here starts, boots or updates a machine

def own_build(label, target):
    return label.startswith("wk build %s%s " % (PREFIX, target))


def target_unready(target):
    if not support.live_selected():
        return "live tier not selected: the %s target needs real machines" % target
    conf = TARGETS[target]
    if target == "container":
        missing = support.container_target_missing()
        if missing:
            return missing
        busy = [b for b in support.builds_on_the_books() if not own_build(b, target)]
        if busy:
            return "a build is on this machine's books (%s): re-run the target on an idle machine" % ", ".join(busy)
    if target == "tart":
        return tart_unready()
    if conf["machine"]:
        from wk import targets
        t = targets.Registry(str(REPO), env=real_env()).load(conf["machine"])
        side, why = t.probe()
        if side != "answering":
            return "'%s' does not answer (%s %s)" % (conf["machine"], side, why)
        rc, out = t.wk("doctor", "--probe-tools")
        far = dict(l.partition("=")[::2] for l in out.splitlines() if "=" in l).get("sha", "")
        here = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        if far != here:
            return "%s runs wk-tools %s and this tree is at %s, and the target's commands run in its own wk: %s" % (
                conf["machine"], far[:12] or "(none)", here[:12], conf["remedy"])
    return None


def tart_unready():
    """A guest is cloned from the golden base, and rebuilding a stale one takes hours and a person."""
    if sys.platform != "darwin" or subprocess.run(["sh", "-c", "command -v tart"], capture_output=True).returncode:
        return "the tart target needs a Mac with tart installed"
    from wk import targets
    from wk.sysimage import guestbase
    vm = targets.Registry(str(REPO), env=real_env()).load("vm")
    if vm.vm_state(PREFIX + "tart") != "absent" or os.environ.get("WK_VM_FORCE"):
        return None
    base = guestbase.Base(vm)
    why = base.stale() if base.ready() else "it is not there, or its provisioning never finished"
    return why and ("'%s' cannot be cloned for the tart target: %s. Rebuild it (%s --rebuild, hours), "
                    "or WK_VM_FORCE=1 clones it anyway" % (base.name, why, guestbase.guest.BASE_BUILD))


def on_target(name):
    def make(cls):
        cls.target = name
        return support.requires(functools.lru_cache(maxsize=None)(target_unready), name)(cls)
    return make


@on_target("container")
class DevIntegrationContainer(TargetSteps, unittest.TestCase):
    pass


@on_target("tart")
class DevIntegrationTart(TargetSteps, unittest.TestCase):
    pass


@on_target("moose")
class DevIntegrationMoose(TargetSteps, unittest.TestCase):
    pass


@on_target("bb4")
class DevIntegrationBb4(TargetSteps, unittest.TestCase):
    pass


# -- the pure parts, unit tier

DOCTOR = """
credentials
  \x1b[33m??\x1b[0m    github-pat reaches further than wk spends it   -> a classic token (scopes: public_repo)
  \x1b[32mok\x1b[0m    bugzilla-api-key -- https://bugs.webkit.org accepts it
  \x1b[32mok\x1b[0m    claude -- a Claude Code OAuth token, which is inference-only
  \x1b[33m??\x1b[0m    litellm: nothing stored   -> wk key setup
workspaces store
  \x1b[31m--\x1b[0m    fork push key                                  -> wk key deploy  (needs gh auth)
"""


class TestTheEvidence(unittest.TestCase):
    def test_doctor_rows_are_read_by_name(self):
        rows = credential_rows(DOCTOR)
        self.assertEqual("ok", rows["claude"][0])
        self.assertEqual(["??    litellm: nothing stored   -> wk key setup",
                          "--    fork push key                                  -> wk key deploy  (needs gh auth)"],
                         credential_problems(rows, CREDENTIALS + ("fork push key",)))

    def test_a_credential_doctor_never_named_is_a_problem(self):
        self.assertEqual(["ntfy: not in wk doctor's output at all"], credential_problems({}, ("ntfy",)))

    def test_what_step_2_does_follows_the_record(self):
        for rec, want in ((None, "new"), ({"ws": "creating", "state": "creating"}, "new"),
                          ({"ws": "broken", "state": "exited"}, "broken"), ({"ws": "present", "state": "running"}, "none"),
                          ({"ws": "present", "state": "stopped"}, "start")):
            with self.subTest(rec=rec):
                self.assertEqual(want, workspace_action(rec))

    def test_the_record_and_its_build_are_read_from_status_records(self):
        text = ('\x1b[0m{"kind":"machine","name":"tolken"}\r\n'
                '{"kind":"workspace","name":"integ-x","state":"running","ws":"present",'
                '"subs":[{"kind":"build","state":"ok","config":"jsc-debug"}]}\n{"kind":"exit","code":0}\n')
        rec = workspace_record(text, "integ-x")
        self.assertEqual(("ok", "jsc-debug"), last_build(rec))
        self.assertIsNone(workspace_record(text, "integ-y"))
        self.assertEqual((None, None), last_build({"subs": []}))

    def test_a_probe_line_is_found_among_a_shells_noise(self):
        self.assertEqual({"a": 1}, tagged("motd\r\n\x1b[1mWK-GIT {\"a\": 1}\r\n", "WK-GIT"))
        self.assertIsNone(tagged("nothing", "WK-GIT"))

    def test_ls_rows_skip_the_header(self):
        text = "NAME   TARGET            STATE\ncorpse-tv  tolken:container  running  main\n"
        self.assertEqual([("corpse-tv", "tolken:container", "running")], ls_rows(text))

    def test_only_the_targets_own_build_is_joined_rather_than_waited_out(self):
        self.assertTrue(own_build("wk build integ-container (jsc-debug)", "container"))
        self.assertFalse(own_build("wk build corpse-tv (jsc-release)", "container"))
        self.assertFalse(own_build("wk build integ-container2 (jsc-debug)", "container"))


class TestTheSteps(unittest.TestCase):
    def steps(self):
        return sorted((getattr(TargetSteps, n).wk_step, n) for n in dir(TargetSteps) if n.startswith("test_"))

    def test_the_steps_run_in_their_numbered_order_and_removal_is_last(self):
        steps = self.steps()
        self.assertEqual(list(range(1, 15)), [n for n, _ in steps])
        self.assertEqual([name for _, name in steps], sorted(name for _, name in steps), "unittest runs methods by name")
        self.assertEqual("test_14_remove_the_workspace", steps[-1][1])

    def test_every_target_is_a_class_with_every_step(self):
        for cls in (DevIntegrationContainer, DevIntegrationTart, DevIntegrationMoose, DevIntegrationBb4):
            self.assertEqual("live", cls.wk_tier)
            self.assertIn(cls.target, TARGETS)

    def test_a_step_before_the_starting_one_skips_and_so_does_one_after_a_step_2_that_did_not_pass(self):
        class Target(TargetSteps, unittest.TestCase):
            target = "container"
            outcome = {}

            @step(3)
            def test_x(self):
                pass
        with unittest.mock.patch.dict(os.environ, {"WK_INTEG_FROM": "4"}):
            with self.assertRaises(unittest.SkipTest):
                Target("test_x").test_x()
        for outcome in ("failed", "skipped"):
            Target.outcome = {2: outcome}
            with self.assertRaises(unittest.SkipTest):
                Target("test_x").test_x()
        Target.outcome = {2: "passed"}
        Target("test_x").test_x()
        self.assertEqual("passed", Target.outcome[3])

    def test_the_guard_turns_push_off_when_its_holder_is_gone(self):
        with support.scratch_dir() as d:
            done = d / "off"
            g = PushGuard(["sh", "-c", 'echo off > "%s"' % done])
            self.assertFalse(done.exists())
            self.assertEqual((0, ""), g.release())
            self.assertEqual("off\n", done.read_text())

    def test_the_real_environment_drops_what_the_suite_points_away(self):
        env = real_env()
        self.assertNotIn("WK_GITHUB_API", env)
        self.assertNotIn("WK_TS_AUTHKEY", env)


if __name__ == "__main__":
    unittest.main()
