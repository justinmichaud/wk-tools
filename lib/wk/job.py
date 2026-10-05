"""A job's watched run, the pid it announces and its adoption, and how it is stopped (0 stopped, 1 left, 2 none)."""

import fnmatch
import os
import re
import shlex
import signal as sig
import sys
import threading

from wk import act, record
from wk.act import die, info, log, warn
from wk.machine import far_side_start
from wk.record import progress_line

TOOLS = {"cc1", "cc1plus", "lto1", "clang", "clang++", "gcc", "g++", "cc", "c++", "ld", "ld-classic", "ld64",
         "ld64.lld", "ld.lld", "lld", "ninja", "xcodebuild", "swift-frontend"}
KILL_WAIT = 15
ABORT_SECONDS = 1800
EXCLUSIVE = ("build", "babysit", "yocto", "buildroot")   # jobs that hold a checkout: two at once corrupt it
EXIT_OF = {sig.SIGINT: 130, sig.SIGTERM: 143, sig.SIGHUP: 129}
# Depth first, children before parents: ninja's children reparent to init once it is gone.
TREE = '_d() { for k in $(pgrep -P "$1"); do _d "$k"; done; echo "$1"; }; _d "$1"'


def _seconds(env, name, default):
    return int(env.get(name) or default)


def heartbeat_seconds(env):
    return _seconds(env, "WK_HEARTBEAT_SECONDS", 300)


def pid_tries(env):
    return int(env.get("WK_JOB_PID_TRIES") or 900)


def kill_wait(env, default=KILL_WAIT):
    return int(env.get("WK_KILL_WAIT") or default)


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


def stall_report(machine, path, idle, verdict="silent", named=""):
    procs = build_processes(machine)
    if verdict == "wedged":
        warn("wedged: the log has named %s for %ds -- giving up and killing the job" % (named, idle))
    elif procs:
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


def watch(argv, path, machine, clock, env=None, cwd=None, abort=None, wedge=None):
    """The job's status, or 124 once its watchdog killed it (watch_pid)."""
    if act.dry_run():
        sys.stderr.write("would run: %s\n" % " ".join(shlex.quote(a) for a in argv))
        return 0
    with open(path, "wb") as out:
        p = machine.start(argv, out, cwd)
    try:
        if watch_pid(p.poll, p.pid, path, machine, clock, env, abort, wedge):
            p.wait()
            return 124
        return p.returncode
    except KeyboardInterrupt:
        terminate(lambda s: kill_tree(machine, p.pid, s), lambda: p.poll() is not None, clock, 2)
        p.wait()
        raise


def watch_pid(ended, pid, path, machine, clock, env=None, abort=None, wedge=None):
    """Until `ended()`; the verdict it killed on: "silent" past `abort` s (0 never), or "wedged" once `wedge`'s
    (beats, names) saw `names(path)` give one task for that many heartbeats in which the log grew."""
    env = os.environ if env is None else env
    poll, stall = _seconds(env, "WK_POLL_SECONDS", 15), record.watchdog_stall(env)
    beat = heartbeat_seconds(env)
    abort = record.watchdog_abort(env, ABORT_SECONDS) if abort is None else abort
    start = last_change = last_beat = clock.now()
    last_size, warned, named, same = 0, False, "", 0
    while ended() is None:
        for _ in range(poll):
            clock.sleep(1)
            if ended() is not None:
                break
        size, now = os.path.getsize(path) if os.path.exists(path) else 0, clock.now()
        if size != last_size:
            last_size, last_change, warned = size, now, False
        idle = int(now - last_change)
        if abort and idle >= abort:
            warn("no output for %ds -- giving up and killing the job" % idle)
            stall_report(machine, path, idle)
            terminate(lambda s: kill_tree(machine, pid, s), lambda: ended() is not None, clock, 5)
            return "silent"
        if idle >= stall and not warned:
            stall_report(machine, path, idle)
            log("  will abort if still silent at %ds" % abort if abort else "  not stopping it: silence is not a failure here")
            warned = True
        if now - last_beat >= beat:
            log("  ... %s (%dm elapsed)" % (progress_line(path) or "running", (now - start) // 60))
            if wedge and last_change > last_beat:
                now_named = wedge[1](path)
                same = same + 1 if now_named and now_named == named else 0
                named = now_named
                if same >= wedge[0]:
                    stall_report(machine, path, int(same * beat), "wedged", named)
                    terminate(lambda s: kill_tree(machine, pid, s), lambda: ended() is not None, clock, 5)
                    return "wedged"
            last_beat = now
    return False


def terminate(send, gone, clock, grace, kill_grace=0):
    """`send(SIGTERM)`, then `send(SIGKILL)` once `grace` seconds pass without `gone()`; whether it went."""
    send(sig.SIGTERM)
    if clock.wait_until(gone, grace, 1):
        return True
    send(sig.SIGKILL)
    return clock.wait_until(gone, kill_grace, 1)


def detach(machine, argv, log_path):
    machine.mkdir(os.path.dirname(log_path))
    machine.write(log_path, "")
    return machine.spawn(argv, log_path)


def remote_line(argv, log_path, rc):
    """nohup outlives the closing session's SIGHUP and disown its job table; not every far side has setsid."""
    inner = "( %s ); echo $? > %s" % (" ".join(shlex.quote(a) for a in argv), shlex.quote(rc))
    return "rm -f %s; %s" % (shlex.quote(rc), far_side_start("bash -c %s" % shlex.quote(inner), log_path, "disown"))


def wait_remote(ask, log_path, rc, clock, interval=30, stream=False, timeout=0, abort_re="", env=None):
    """(status, True), or ("timeout"|"aborted", False); silence is reported, and a wedged browser's traceback aborts."""
    env = os.environ if env is None else env
    stall, beat = record.watchdog_stall(env), heartbeat_seconds(env)
    q = shlex.quote
    start = last_change = last_beat = clock.now()
    last_size, warned = 0, False

    def size():
        digits = re.sub(r"[^0-9]", "", ask("wc -c < %s 2>/dev/null" % q(log_path)).out)
        return int(digits) if digits else None

    def more(n):
        if n is not None and n > last_size:
            sys.stderr.write(ask("tail -c +%d %s" % (last_size + 1, q(log_path))).out)

    while True:
        clock.sleep(interval)
        if timeout and clock.now() - start >= timeout:
            return "timeout", False
        if abort_re and ask("grep -qiE %s %s" % (q(abort_re), q(log_path))).ok:
            return "aborted", False
        n, now = size(), clock.now()
        if n is not None and n > last_size:
            if stream:
                more(n)
            last_size, last_change, warned = n, now, False
        status = re.sub(r"[^0-9]", "", ask("cat %s 2>/dev/null" % q(rc)).out)
        if status:
            if stream:
                more(size())
            return status, True
        idle = int(now - last_change)
        if idle >= stall and not warned:
            warn("no output for %ds -- not stopping it; a detached job can be\n  silent for a long time. Look on the far side:  tail -f %s"
                 % (idle, log_path))
            warned = True
        if not stream and now - last_beat >= beat:
            log("  ... still running (%dm)" % ((now - start) // 60))
            last_beat = now


def announced_pid(path, label):
    """The pid a job announces down its log (`wk: <label> pid <n>`), the one channel back from every driver."""
    try:
        with open(path, errors="replace") as f:
            m = re.search(r"^wk: %s pid ([0-9]+)" % re.escape(label), f.read(), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


class PidWatch(threading.Thread):
    def __init__(self, driver, ws, task, path, label, patterns, tries):
        super().__init__(daemon=True)
        self.args_ = (driver, ws, task, path, label, patterns)
        self.tries = tries
        self.done = threading.Event()
        self.adopted = None

    def run(self):
        driver, ws, task, path, label, patterns = self.args_
        for _ in range(self.tries):
            pid = announced_pid(path, label)
            if pid is not None:
                self.adopted = adopt(driver, ws, task, pid, patterns)
                return
            if self.done.wait(1):
                return

    def stop(self):
        self.done.set()


def match_any(text, patterns):
    return any(fnmatch.fnmatchcase(text, p) for p in patterns.split())


def pid_args(driver, ws, pid):
    return driver.exec(ws, ["ps", "-o", "args=", "-p", str(pid)]).out.replace("\r", "").replace("\n", " ").strip()


def adopt(driver, ws, t, pid, want):
    """A pid out of a workspace is its own claim, and a wkdev container shares the host's PID namespace
    (--pid host): it is adopted, and later signalled, only while its command line there matches `want`."""
    args = pid_args(driver, ws, pid)
    if match_any(args, want):
        t.set("pid_match", want)
        t.pid(pid)
        t.set("where", "place")
        return True
    warn("'%s' names pid %s as its job, and that pid inside '%s' is running\n  '%s', not %s. It is not adopted, so\n"
         "  nothing here will signal it; stop the job where it runs:  wk enter %s"
         % (ws, pid, ws, args or "nothing -- it is already gone", want, ws))
    return False


def signal(driver, ws, t, pid, signum):
    want = t.field("pid_match")
    if not want:
        die("the record %s holds pid %s inside '%s' and no pattern its\n    command line must match, so nothing can tell it "
            "from any other pid in a\n    shared PID namespace. Whatever adopted that pid did not go through\n"
            "    job.adopt (lib/wk/job.py), which is a bug." % (t.id, pid, ws))
    args = pid_args(driver, ws, pid)
    if not args:
        return
    if not match_any(args, want):
        die("refusing to send %s to pid %s inside '%s': it is running\n    '%s', not %s. The pid is what the workspace "
            "announced, and this one\n    is another process -- in a shared PID namespace it could be another\n"
            "    workspace's build. Stop the job where it runs:  wk enter %s" % (signal_name(signum), pid, ws, args, want, ws))
    kill_tree_in(driver, ws, int(pid), signum)


def kill_tree_in(driver, ws, pid, signum):
    pids = descendants(lambda argv: driver.exec(ws, argv), pid)
    driver.act_exec(ws, ["kill", "-" + signal_name(signum)] + [str(p) for p in pids])


def _signal(driver, ws, task, pid, machine, signum):
    if task.field("where") == "place":
        signal(driver, ws, task, pid, signum)
    else:
        kill_tree(machine, pid, signum)


def signal_name(signum):
    return sig.Signals(signum).name[3:]


def kill(driver, ws, task, word, machine, clock, env=None, me=None):
    """TERM, KILL after WK_KILL_WAIT, the record ended `word`; `stopping` first, so the detached run ends it `word` too."""
    env = os.environ if env is None else env
    pid, wait = task.field("pid"), kill_wait(env)
    if act.dry_run():
        log("dry run -- would TERM pid %s, KILL it after %ds, and record it %s" % (pid or "(none yet)", wait, word))
        return True
    if not pid or int(pid) == (os.getpid() if me is None else me):
        task.end(word)
        return True
    task.set("stopping", word)

    def send(signum):
        if signum == sig.SIGKILL:
            warn("pid %s did not stop on TERM after %ds -- killing it" % (pid, wait))
        _signal(driver, ws, task, int(pid), machine, signum)
    gone = terminate(send, lambda: not task.alive(None), clock, wait, 5)
    task.end(word)
    return gone


def stop(driver, records, ws, kind, machine, clock, env=None):
    t = records.find(kind, ws)
    if t is None or not t.alive(None):
        log("no %s is running in '%s' -- 'wk status %s' says what it last did" % (kind, ws, ws))
        return 2
    info("stopping the %s in '%s' (pid %s on %s)" % (kind, ws, t.field("pid"), t.field("machine")))
    ok = kill(driver, ws, t, "cancelled", machine, clock, env)
    if act.dry_run():
        return 2
    if ok:
        info("stopped '%s's %s and recorded it as cancelled" % (ws, kind))
    return 0 if ok else 1


def records_of(driver, clock, machine):
    env = dict(driver.env)
    record.default_watchdog(env, abort=ABORT_SECONDS)
    return record.of_driver(driver, clock, machine, env)


def busy_reason(driver, records, name, skip=""):
    """What holds the checkout: a live record of an EXCLUSIVE kind, else a live pid file in the workspace's home no record names."""
    recorded = set()
    for t in records.list():
        if skip and os.path.realpath(str(t.path)) == os.path.realpath(skip):
            continue
        if t.field("name") != name:
            continue
        if t.field("where") == "place":
            recorded.add(t.field("pid"))
        if t.field("kind") in EXCLUSIVE and t.alive(None):
            return "%s (pid %s, %s)  stop it: %s" % (t.field("kind"), t.field("pid"), t.field("machine"), t.field("kill"))
    home = os.path.join(driver.store.ws_dir(name), "home")
    here = records.machine
    if not here.isdir(home):
        return None
    for n in here.listdir(home):
        if not n.endswith(".pid"):
            continue
        try:
            pid = re.sub(r"[^0-9]", "", here.read(os.path.join(home, n)))
        except OSError:
            continue
        if pid and pid not in recorded and driver.exec(name, ["kill", "-0", pid]).ok:
            return "%s (pid %s in the workspace)" % (n[:-4], pid)
    return None


def holder_alive(reg):
    """A budget record's holder, `pid:<n>` on this machine (Budget.record's callers write no other kind)."""
    return lambda h: h.startswith("pid:") and h[4:].isdigit() and reg.machine.alive(int(h[4:]))


def detached(here, recs, clock, kind, name, argv, path, what):
    since = clock.stamp()
    pid = detach(here, argv, path)
    offset = [0]

    def pump():
        try:
            data = here.read_bytes(path, offset[0])
        except OSError:
            return
        offset[0] += len(data)
        sys.stderr.write(data.decode(errors="replace"))

    while True:
        pump()
        t = recs.find(kind, name, floor=since)
        if t is not None and t.id.endswith("-%d" % pid):
            return pid
        if not here.alive(pid):
            pump()
            die("the detached %s of '%s' ended before it started -- what it said is\n    above, in full in %s" % (what, name, path))
        clock.sleep(1)
