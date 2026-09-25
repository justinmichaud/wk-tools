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
from wk.machine import far_side_start
from wk.record import progress_line

TOOLS = {"cc1", "cc1plus", "lto1", "clang", "clang++", "gcc", "g++", "cc", "c++", "ld", "ld-classic", "ld64",
         "ld64.lld", "ld.lld", "lld", "ninja", "xcodebuild", "swift-frontend"}
KILL_WAIT = 15
ABORT_SECONDS = 1800
EXIT_OF = {sig.SIGINT: 130, sig.SIGTERM: 143, sig.SIGHUP: 129}
# Depth first, children before parents: ninja's children reparent to init once it is gone.
TREE = '_d() { for k in $(pgrep -P "$1"); do _d "$k"; done; echo "$1"; }; _d "$1"'
REFUSED = 3   # a bash caller's shim exits on it, as a die in the caller did


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


def watch(argv, path, machine, clock, env=None, cwd=None, popen=subprocess.Popen, abort=None, wedge=None):
    """The job's status, or 124 once its watchdog killed it (watch_pid)."""
    if act.dry_run():
        sys.stderr.write("would run: %s\n" % " ".join(shlex.quote(a) for a in argv))
        return 0
    with open(path, "wb") as out:
        p = popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=cwd)
    try:
        if watch_pid(p.poll, p.pid, path, machine, clock, env, abort, wedge):
            p.wait()
            return 124
        return p.returncode
    except KeyboardInterrupt:
        kill_tree(machine, p.pid, sig.SIGTERM)
        clock.sleep(2)
        kill_tree(machine, p.pid, sig.SIGKILL)
        p.wait()
        raise


def watch_pid(ended, pid, path, machine, clock, env=None, abort=None, wedge=None):
    """Until `ended()`; the verdict it killed on: "silent" past `abort` s (0 never), or "wedged" once `wedge`'s
    (beats, names) saw `names(path)` give one task for that many heartbeats in which the log grew."""
    env = os.environ if env is None else env
    poll, stall = _seconds(env, "WK_POLL_SECONDS", 15), _seconds(env, "WK_STALL_SECONDS", 300)
    beat = _seconds(env, "WK_HEARTBEAT_SECONDS", 300)
    abort = _seconds(env, "WK_ABORT_SECONDS", ABORT_SECONDS) if abort is None else abort
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
            return _give_up(machine, clock, pid, "silent")
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
                    return _give_up(machine, clock, pid, "wedged")
            last_beat = now
    return False


def _give_up(machine, clock, pid, verdict):
    kill_tree(machine, pid, sig.SIGTERM)
    clock.sleep(5)
    kill_tree(machine, pid, sig.SIGKILL)
    return verdict


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
    stall, beat = _seconds(env, "WK_STALL_SECONDS", 300), _seconds(env, "WK_HEARTBEAT_SECONDS", 300)
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
            "    job.adopt (lib/wk/job.py; job_pid_adopt from bash), which is a bug." % (t.id, pid, ws))
    args = pid_args(target, ws, pid)
    if not args:
        return
    if not match_any(args, want):
        die("refusing to send %s to pid %s inside '%s': it is running\n    '%s', not %s. The pid is what the workspace "
            "announced, and this one\n    is another process -- in a shared PID namespace it could be another\n"
            "    workspace's build. Stop the job where it runs:  wk enter %s" % (signal_name(signum), pid, ws, args, want, ws))
    kill_tree_in(target, ws, int(pid), signum)


def kill_tree_in(target, ws, pid, signum):
    """Descendants first, inside the workspace, for a pid whose command line the caller has already checked."""
    pids = descendants(lambda argv: target.exec(ws, argv), pid)
    target.act_exec(ws, ["kill", "-" + signal_name(signum)] + [str(p) for p in pids])


def _signal(target, ws, task, pid, machine, signum):
    if task.field("where") == "target":
        signal(target, ws, task, pid, signum)
    else:
        kill_tree(machine, pid, signum)


def signal_name(signum):
    return sig.Signals(signum).name[3:]


def kill(target, ws, task, word, machine, clock, env=None, me=None):
    """TERM, KILL after WK_KILL_WAIT, and the record ended `word`; True when it is gone. `stopping` goes on
    the record first, so the job's own driver, seeing its child die of the TERM, ends it `word` too."""
    env = os.environ if env is None else env
    pid, wait = task.field("pid"), _seconds(env, "WK_KILL_WAIT", KILL_WAIT)
    if act.dry_run():
        log("dry run -- would TERM pid %s, KILL it after %ds, and record it %s" % (pid or "(none yet)", wait, word))
        return True
    if not pid or int(pid) == (os.getpid() if me is None else me):
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


def main(argv, env=None):
    """The job for a bash caller (lib/watchdog.sh, lib/detach.sh): `python3 -m wk.job <verb> ...`."""
    from wk.clock import Clock
    from wk.machine import here
    from wk.record import Records, Task
    from wk.shell import caller_shell
    env = os.environ if env is None else env
    verb, a = argv[0], argv[1:]
    machine, clock, shell = here(), Clock(), caller_shell(env)
    tail = a[a.index("--") + 1:] if "--" in a else []
    a = a[:a.index("--")] if "--" in a else a

    def target():
        if shell is None:
            die("%s: this runs inside a workspace and no target is loaded" % verb)
        return shell

    def signum(name):
        return sig.Signals["SIG" + name]

    try:
        if verb == "watch":
            pid = int(a[0])
            return 124 if watch_pid(lambda: None if machine.alive(pid) else 0, pid, a[1], machine, clock, env) else 0
        if verb == "kill-tree":
            kill_tree(machine, int(a[0]), signum(a[1]))
        elif verb == "kill-tree-in":
            kill_tree_in(target(), a[0], int(a[1]), signum(a[2]))
        elif verb == "pid-args":
            sys.stdout.write(pid_args(target(), a[0], a[1]))
        elif verb == "adopt":
            return 0 if adopt(target(), a[0], Task(a[1], clock, machine=machine), int(a[2]), a[3]) else 1
        elif verb == "kill":
            t = Task(a[1], clock, target().ask if shell else None, machine)
            return 0 if kill(shell, a[0], t, a[2], machine, clock, env, me=int(a[3])) else 1
        elif verb == "stop":
            records = Records(env=env, clock=clock, ask_target=target().ask, machine=machine)
            return stop(shell, records, a[0], a[1], machine, clock, env)
        elif verb == "detach":
            if not tail:
                die("detach: nothing to run")
            sys.stdout.write(str(detach(machine, tail, a[0])))
        elif verb == "remote":
            if not tail:
                die("detach_remote: nothing to run")
            if not target().call(a[0], [remote_line(tail, a[1], a[2])]).ok:
                die("detach_remote: could not start the job")
        elif verb == "wait-remote":
            fn, opt = a[0], a[3:] + [""] * 4
            word, ok = wait_remote(lambda line: target().call(fn, [line], quiet=True), a[1], a[2], clock,
                                   int(opt[0] or 30), opt[1] == "1", int(opt[2] or 0), opt[3], env)
            sys.stdout.write(word)
            return 0 if ok else 1
        elif verb == "abort-seconds":
            sys.stdout.write(str(ABORT_SECONDS))
        else:
            die("wk.job: no verb '%s'" % verb, 2)
    except act.Refused:
        return REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
