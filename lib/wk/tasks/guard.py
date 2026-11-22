"""The memory guard: the parent of a build or test run, on the server, inside the workspace.

    python3 -I <ws>/build/guard.py <budget MB> <1 when the cgroup is the workspace's own, else 0> <command>...

It runs the command in a child cgroup v2 whose memory.max is the budget, where one can be made and read back; where
it cannot, it samples the command's process tree every 30 s and kills it over budget, or when the machine has under
2048 MB available. It prints which of the two it is doing. A run it or the kernel killed for memory exits 99.
Standard library only, and no import of wk: `wk build` copies this one file into the workspace."""

import os
import signal
import subprocess
import sys

INTERVAL, FLOOR_MB, GRACE, KILLED = 30, 2048, 10, 99
MB = 1024 * 1024


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def own_cgroup(text):
    """The cgroup v2 path in /proc/self/cgroup, or None."""
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return None


def tree(ps, root):
    """(MB resident, pids) of `root` and everything under it, from `ps -eo pid=,ppid=,rss=`."""
    rows = [tuple(int(word) for word in line.split()) for line in ps.splitlines() if line.strip()]
    pids, grew = {root}, True
    while grew:
        more = {pid for pid, ppid, _ in rows if ppid in pids} - pids
        pids, grew = pids | more, bool(more)
    return sum(rss for pid, _, rss in rows if pid in pids) // 1024, sorted(pids)


def held_mb(stat):
    """A cgroup's anonymous and shmem pages from memory.stat: the page cache in memory.current is the kernel's to reclaim."""
    return sum(int(line.split()[1]) for line in stat.splitlines() if line.split()[0] in ("anon", "shmem")) // MB


def available_mb(meminfo):
    """MemAvailable, or None where there is none (macOS)."""
    for line in (meminfo or "").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return None


def verdict(used, budget, available):
    """Why the run must be killed now, or None."""
    if used > budget:
        return "this run is using %d MB, over its %d MB budget" % (used, budget)
    if available is not None and available < FLOOR_MB:
        return "the machine has %d MB available, under the %d MB floor, and this run is using %d MB of it" % (available, FLOOR_MB, used)
    return None


def oom_kills(events):
    """The oom_kill count in a cgroup's memory.events."""
    return sum(int(line.split()[1]) for line in (events or "").splitlines() if line.startswith("oom_kill "))


def make_cgroup(base, budget):
    """A child of the cgroup at `base` with memory.max = `budget` MB, read back; None where it cannot be made.
    The empty ones a killed guard left are removed on the way."""
    path = "%s/wk-guard-%d" % (base, os.getpid())
    try:
        for name in os.listdir(base):
            if name.startswith("wk-guard-"):
                remove(base + "/" + name)
        os.mkdir(path)
        with open(path + "/memory.max", "w") as f:
            f.write(str(budget * MB))
    except OSError:
        remove(path)
        return None
    if read(path + "/memory.max").strip() != str(budget * MB):
        remove(path)
        return None
    return path


def remove(cgroup):
    try:
        os.rmdir(cgroup)
    except OSError:
        pass


def enter(path):
    with open(path + "/cgroup.procs", "w") as f:
        f.write(str(os.getpid()))


def say(text):
    print("wk: " + text, flush=True)


def stop(child, pids):
    """TERM before KILL: a half-written object file fails the next build, not this one."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        try:
            child.wait(GRACE)
            return
        except subprocess.TimeoutExpired:
            pass


def watch(child, budget, stat, meminfo, ps=("ps", "-eo", "pid=,ppid=,rss=")):
    """Sample until the command ends; its exit status, or KILLED."""
    while child.poll() is None:
        table = subprocess.run(ps, stdout=subprocess.PIPE, universal_newlines=True).stdout
        used, pids = tree(table, child.pid)
        if stat:
            used = max(used, held_mb(read(stat) or ""))
        why = verdict(used, budget, available_mb(read(meminfo))) if table else "its memory cannot be read: ps printed nothing"
        if why:
            say("MEMORY LIMIT: %s; killing it rather than leaving the OOM killer to choose" % why)
            stop(child, pids)
            return KILLED
        try:
            child.wait(INTERVAL)
        except subprocess.TimeoutExpired:
            pass
    return child.returncode


def main(argv, root="/"):
    budget, own, command = int(argv[0]), argv[1] == "1", argv[2:]
    here = own_cgroup(read(root + "proc/self/cgroup"))
    base = None if here is None else "%ssys/fs/cgroup%s" % (root, here.rstrip("/"))
    cgroup = make_cgroup(base, budget) if base else None
    if cgroup:
        say("memory guard: a cgroup with memory.max %d MB" % budget)
        child = subprocess.Popen(command, preexec_fn=lambda: enter(cgroup))
        rc = child.wait()
        if oom_kills(read(cgroup + "/memory.events")):
            say("MEMORY LIMIT: the kernel killed part of this run at its %d MB cgroup ceiling" % budget)
            rc = KILLED
        remove(cgroup)
    else:
        stat = base + "/memory.stat" if own and base else None
        say("memory guard: no child cgroup can be made here, so watching the %s every %d s: budget %d MB, floor %d MB"
            % ("process tree and the workspace's cgroup" if stat else "process tree", INTERVAL, budget, FLOOR_MB))
        rc = watch(subprocess.Popen(command), budget, stat, root + "proc/meminfo")
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
