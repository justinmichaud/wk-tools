"""A long-running job's watched run, the pid it announces and its adoption, and the one way a recorded
job is stopped (0 stopped, 1 left, 2 none)."""

import fnmatch
import os
import re
import shlex
import signal as sig
import subprocess
import sys
import threading

from wk import act
from wk.act import die, info, log, warn
from wk.record import progress_line

TOOLS = {"cc1", "cc1plus", "lto1", "clang", "clang++", "gcc", "g++", "cc", "c++", "ld", "ld-classic", "ld64",
         "ld64.lld", "ld.lld", "lld", "ninja", "xcodebuild", "swift-frontend"}
KILL_WAIT = 15
ABORT_SECONDS = 1800
EXIT_OF = {sig.SIGINT: 130, sig.SIGTERM: 143, sig.SIGHUP: 129}
# Depth first, children before parents: ninja's children reparent to init once it is gone.
TREE = '_d() { for k in $(pgrep -P "$1"); do _d "$k"; done; echo "$1"; }; _d "$1"'


def _seconds(env, name, default):
    return int(env.get(name) or default)


class Interrupted(KeyboardInterrupt):
    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


class Signals:
    """TERM and HUP arrive as Interrupted, as ^C does, so each converges its record the one way."""

    def __enter__(self):
        self.saved = {}
        for s in (sig.SIGTERM, sig.SIGHUP, sig.SIGINT):
            try:
                self.saved[s] = sig.signal(s, _raise)
            except ValueError:
                pass
        return self

    def __exit__(self, *exc):
        for s, h in self.saved.items():
            sig.signal(s, h)
        return False


def _raise(signum, frame):
    for s in (sig.SIGTERM, sig.SIGHUP, sig.SIGINT):
        sig.signal(s, sig.SIG_IGN)
    raise Interrupted(signum)


def descendants(run, pid):
    found = [int(p) for p in run(["sh", "-c", TREE, "wk", str(pid)]).out.split() if p.isdigit()]
    return [p for p in found if p != pid] + [pid]


def kill_tree(machine, pid, signum):
    for p in descendants(machine.run, pid):
        if p != os.getpid():
            machine.kill(p, signum)


def build_processes(machine):
    """Every compiler and linker on this machine, busiest first, as (pcpu, name); `-A` because Darwin's `-e` is this user's only."""
    rows = []
    for line in machine.run(["ps", "-A", "-o", "pcpu=,comm="]).out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) in TOOLS:
            try:
                rows.append((float(parts[0]), os.path.basename(parts[1].strip())))
            except ValueError:
                continue
    return sorted(rows, reverse=True)


def stall_report(machine, path, idle):
    procs = build_processes(machine)
    if procs:
        warn("no output for %ds, and this machine is running %d compiler/linker process(es) -- a full-LTO link is silent for minutes at a time"
             % (idle, len(procs)))
        log("  busiest:       %s at %s%% CPU" % (procs[0][1], ("%g" % procs[0][0])))
    else:
        warn("no output for %ds, and nothing here is compiling or linking" % idle)
    log("  last progress: %s" % (progress_line(path) or "unknown"))
    try:
        for line in machine.read("/proc/meminfo").splitlines():
            if line.startswith("MemAvailable:"):
                log("  memory:        %d MB available" % (int(line.split()[1]) // 1024))
    except OSError:
        pass
    try:
        for line in machine.read("/sys/fs/cgroup/memory.events").splitlines():
            if line.startswith("oom_kill ") and line.split()[1] != "0":
                warn("  cgroup has OOM-killed %s process(es) -- lower the job count" % line.split()[1])
    except OSError:
        pass
    try:
        with open(path, errors="replace") as f:
            tail = [l for l in f.read().replace("\r", "\n").split("\n") if l]
    except OSError:
        tail = []
    log("  tail: %s" % (tail[-1][:100] if tail else ""))


def watch(argv, path, machine, clock, env=None, cwd=None, popen=subprocess.Popen):
    """The job's status, or 124 once it was silent past WK_ABORT_SECONDS and killed. A hang is read from the log's growth."""
    env = os.environ if env is None else env
    if act.dry_run():
        sys.stderr.write("would run: %s\n" % " ".join(shlex.quote(a) for a in argv))
        return 0
    poll, stall = _seconds(env, "WK_POLL_SECONDS", 15), _seconds(env, "WK_STALL_SECONDS", 300)
    abort, beat = _seconds(env, "WK_ABORT_SECONDS", ABORT_SECONDS), _seconds(env, "WK_HEARTBEAT_SECONDS", 300)
    with open(path, "wb") as out:
        p = popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=cwd)
    try:
        start = last_change = last_beat = clock.now()
        last_size, warned = 0, False
        while p.poll() is None:
            for _ in range(poll):
                clock.sleep(1)
                if p.poll() is not None:
                    break
            size, now = os.path.getsize(path) if os.path.exists(path) else 0, clock.now()
            if size != last_size:
                last_size, last_change, warned = size, now, False
            idle = int(now - last_change)
            if idle >= abort:
                warn("no output for %ds -- giving up and killing the job" % idle)
                stall_report(machine, path, idle)
                kill_tree(machine, p.pid, sig.SIGTERM)
                clock.sleep(5)
                kill_tree(machine, p.pid, sig.SIGKILL)
                p.wait()
                return 124
            if idle >= stall and not warned:
                stall_report(machine, path, idle)
                log("  will abort if still silent at %ds" % abort)
                warned = True
            if now - last_beat >= beat:
                log("  ... %s (%dm elapsed)" % (progress_line(path) or "running", (now - start) // 60))
                last_beat = now
        return p.returncode
    except KeyboardInterrupt:
        kill_tree(machine, p.pid, sig.SIGTERM)
        clock.sleep(2)
        kill_tree(machine, p.pid, sig.SIGKILL)
        p.wait()
        raise


def announced_pid(path, label):
    """The pid a job announces down its log (`wk: <label> pid <n>`), the one channel back from every target kind."""
    try:
        with open(path, errors="replace") as f:
            m = re.search(r"^wk: %s pid ([0-9]+)" % re.escape(label), f.read(), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


class PidWatch(threading.Thread):
    def __init__(self, target, ws, task, path, label, patterns, tries):
        super().__init__(daemon=True)
        self.args_ = (target, ws, task, path, label, patterns)
        self.tries = tries
        self.done = threading.Event()
        self.adopted = None

    def run(self):
        target, ws, task, path, label, patterns = self.args_
        for _ in range(self.tries):
            pid = announced_pid(path, label)
            if pid is not None:
                self.adopted = adopt(target, ws, task, pid, patterns)
                return
            if self.done.wait(1):
                return

    def stop(self):
        self.done.set()


def match_any(text, patterns):
    return any(fnmatch.fnmatchcase(text, p) for p in patterns.split())


def pid_args(target, ws, pid):
    return target.exec(ws, ["ps", "-o", "args=", "-p", str(pid)]).out.replace("\r", "").replace("\n", " ").strip()


def adopt(target, ws, t, pid, want):
    """A pid out of a workspace is its own claim, and a wkdev container shares the host's PID namespace
    (--pid host): it is adopted, and later signalled, only while its command line there matches `want`."""
    args = pid_args(target, ws, pid)
    if match_any(args, want):
        t.set("pid_match", want)
        t.pid(pid)
        t.set("where", "target")
        return True
    warn("'%s' names pid %s as its job, and that pid inside '%s' is running\n  '%s', not %s. It is not adopted, so\n"
         "  nothing here will signal it; stop the job where it runs:  wk enter %s"
         % (ws, pid, ws, args or "nothing -- it is already gone", want, ws))
    return False


def signal(target, ws, t, pid, signum):
    want = t.field("pid_match")
    if not want:
        die("the record %s holds pid %s inside '%s' and no pattern its\n    command line must match, so nothing can tell it "
            "from any other pid in a\n    shared PID namespace. Whatever adopted that pid did not go through\n"
            "    job.adopt (lib/wk/job.py), which is a bug." % (t.id, pid, ws))
    args = pid_args(target, ws, pid)
    if not args:
        return
    if not match_any(args, want):
        die("refusing to send %s to pid %s inside '%s': it is running\n    '%s', not %s. The pid is what the workspace "
            "announced, and this one\n    is another process -- in a shared PID namespace it could be another\n"
            "    workspace's build. Stop the job where it runs:  wk enter %s" % (signal_name(signum), pid, ws, args, want, ws))
    pids = descendants(lambda argv: target.exec(ws, argv), int(pid))
    target.act_exec(ws, ["kill", "-" + signal_name(signum)] + [str(p) for p in pids])


def _signal(target, ws, task, pid, machine, signum):
    if task.field("where") == "target":
        signal(target, ws, task, pid, signum)
    else:
        kill_tree(machine, pid, signum)


def signal_name(signum):
    return sig.Signals(signum).name[3:]


def kill(target, ws, task, word, machine, clock, env=None):
    """TERM, KILL after WK_KILL_WAIT, and the record ended `word`; True when it is gone. `stopping` goes on
    the record first, so the job's own driver, seeing its child die of the TERM, ends it `word` too."""
    env = os.environ if env is None else env
    pid, wait = task.field("pid"), _seconds(env, "WK_KILL_WAIT", KILL_WAIT)
    if act.dry_run():
        log("dry run -- would TERM pid %s, KILL it after %ds, and record it %s" % (pid or "(none yet)", wait, word))
        return True
    if not pid or int(pid) == os.getpid():
        task.end(word)
        return True
    task.set("stopping", word)
    _signal(target, ws, task, int(pid), machine, sig.SIGTERM)
    waited = 0
    while waited < wait and task.alive(None):
        clock.sleep(1)
        waited += 1
    if task.alive(None):
        warn("pid %s did not stop on TERM after %ds -- killing it" % (pid, waited))
        _signal(target, ws, task, int(pid), machine, sig.SIGKILL)
        for _ in range(5):
            if not task.alive(None):
                break
            clock.sleep(1)
    left = task.alive(None)
    task.end(word)
    return not left


def stop(target, records, ws, kind, machine, clock, env=None):
    t = records.find(kind, ws)
    if t is None or not t.alive(None):
        log("no %s is running in '%s' -- 'wk status %s' says what it last did" % (kind, ws, ws))
        return 2
    info("stopping the %s in '%s' (pid %s on %s)" % (kind, ws, t.field("pid"), t.field("machine")))
    ok = kill(target, ws, t, "cancelled", machine, clock, env)
    if act.dry_run():
        return 2
    target.task_put(ws, t)
    if ok:
        info("stopped '%s's %s and recorded it as cancelled" % (ws, kind))
    return 0 if ok else 1
