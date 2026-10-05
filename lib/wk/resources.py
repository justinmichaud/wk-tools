"""The envelope a place is sized from and each build's budget, read through a `Machine`."""

import argparse
import os
import sys
import time

from wk import act
from wk import record
from wk.kv import kv
from wk.store import Store

RESERVE_CORES = 1
RESERVE_MB = 12288
HEADLESS_RESERVE_CORES = 0
HEADLESS_RESERVE_MB = 2048
MB_PER_JOB = 1536
MIN_ENVELOPE_MB = 2048
CGROUP_MEM_MAX = "/sys/fs/cgroup/memory.max"


def workspace_marker_path(env):
    """Where a workspace's own marker file lives; `Registry.marker_path` asks the same question."""
    return env.get("WK_MARKER") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".wk-workspace")


class Resources:
    def __init__(self, machine, env=None, os_name=None):
        self.machine = machine
        self.env = os.environ if env is None else env
        self.os_name = os_name or ("macos" if os.uname().sysname == "Darwin" else "linux")

    # Written by provisioning at the store's default path, before any wk command can derive $WK_STORE.
    def headless_marker(self):
        return os.path.join(Store(self.env).provisioned_store_dir(), ".headless")

    def workspace_marker(self):
        return workspace_marker_path(self.env)

    def is_headless(self):
        return self.machine.exists(self.headless_marker()) or self.machine.exists(self.workspace_marker())

    def _setting(self, name, default):
        v = self.env.get(name)
        return int(v) if v else default

    def reserve_cores(self):
        if self.is_headless():
            return self._setting("WK_HEADLESS_RESERVE_CORES", HEADLESS_RESERVE_CORES)
        return self._setting("WK_RESERVE_CORES", RESERVE_CORES)

    def reserve_mb(self):
        if self.is_headless():
            return self._setting("WK_HEADLESS_RESERVE_MB", HEADLESS_RESERVE_MB)
        return self._setting("WK_RESERVE_MB", RESERVE_MB)

    def mb_per_job(self):
        return mb_per_job_setting(self.env)

    def _reading(self, value, what):
        value = value.strip()
        if not value.isdigit():
            act.die("cannot read %s on this %s machine.\n    Every job count and memory envelope is sized from it, so there is no\n"
                    "    parallelism wk can defend; it builds nothing from a guess." % (what, self.os_name))
        return int(value)

    def _sysctl(self, key):
        return self.machine.run(["sysctl", "-n", key]).out

    def _meminfo(self, key):
        try:
            text = self.machine.read("/proc/meminfo")
        except OSError:
            return ""
        for line in text.splitlines():
            if line.startswith(key + ":"):
                return line.split()[1]
        return ""

    def host_cores(self):
        if self.os_name == "macos":
            return self._reading(self._sysctl("hw.ncpu"), "the core count (sysctl hw.ncpu)")
        return self._reading(self.machine.run(["nproc"]).out, "the core count (nproc)")

    def host_mem_mb(self):
        if self.os_name == "macos":
            return self._reading(self._sysctl("hw.memsize"), "total memory (sysctl hw.memsize)") // 1024 // 1024
        return self._reading(self._meminfo("MemTotal"), "total memory (/proc/meminfo MemTotal)") // 1024

    def host_load(self):
        """Whole cores; `{ 1.23 1.20 1.10 }` on a Mac, where the average is second."""
        if self.os_name == "macos":
            text, index, what = self._sysctl("vm.loadavg"), 1, "the load average (sysctl vm.loadavg)"
        else:
            try:
                text = self.machine.read("/proc/loadavg")
            except OSError:
                text = ""
            index, what = 0, "the load average (/proc/loadavg)"
        fields = text.split()
        try:
            value = str(int(float(fields[index])))
        except (IndexError, ValueError):
            value = ""
        return self._reading(value, what)

    def cores(self):
        return self._setting("WK_CGROUP_CORES", None) or self.host_cores()

    def load(self):
        """A remote place's, measured by whoever can reach it, else this machine's."""
        v = self.env.get("WK_LOAD")
        return int(v) if v else self.host_load()

    def describe_cores(self):
        if self.os_name == "macos":
            p = self._sysctl("hw.perflevel0.logicalcpu").strip()
            if p:
                e = self._reading(self._sysctl("hw.perflevel1.logicalcpu"),
                                  "the efficiency core count (sysctl hw.perflevel1.logicalcpu)")
                return "%s P + %d E" % (p, e)
        return "%d cores" % self.host_cores()

    def avail_override(self):
        return self._setting("WK_AVAIL_MB", None)

    def max_jobs(self):
        return self._setting("WK_MAX_JOBS", None) or None

    def avail_mem_mb(self, cgroup_mb=None):
        """What a build here may take: free memory under any cgroup limit, since MemAvailable inside a container is the whole machine's."""
        if self.avail_override() is not None:
            return self.avail_override()
        if self.os_name == "linux":
            avail = self._reading(self._meminfo("MemAvailable"), "free memory (/proc/meminfo MemAvailable)") // 1024
        else:
            avail = self.host_mem_mb() - self.reserve_mb()
        cg = cgroup_mb if cgroup_mb is not None else self._setting("WK_CGROUP_MB", None)
        if cg is not None and cg < avail:
            avail = cg
        if self.machine.exists(CGROUP_MEM_MAX):
            limit = self.machine.read(CGROUP_MEM_MAX).strip()
            if limit != "max":
                limit = self._reading(limit, "the cgroup memory limit (%s)" % CGROUP_MEM_MAX) // 1024 // 1024
                if limit < avail:
                    avail = limit
        return avail

    def envelope_cores(self):
        return max(1, self.host_cores() - self.reserve_cores())

    def envelope_mem_mb(self):
        mem = self.host_mem_mb()
        m = mem - self.reserve_mb()
        return m if m >= MIN_ENVELOPE_MB else mem // 2


class Budget:
    """Each build's memory and jobs, one record per build under <state>/builds."""

    def __init__(self, machine, env=None, clock=None):
        self.machine = machine
        self.env = os.environ if env is None else env
        self.clock = clock

    def dir(self):
        return os.path.join(Store(self.env).state_dir(), "builds")

    def machine_label(self):
        return self.env.get("WK_BUILD_MACHINE") or record.host_name(self.machine)

    def record(self, label, jobs, budget_mb, holder):
        now = self.clock.now() if self.clock else time.time()
        self.machine.mkdir(self.dir())
        path = os.path.join(self.dir(), "%d-%d-%s" % (now, os.getpid(), os.urandom(3).hex()))
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        self.machine.write(path, "label=%s\nmachine=%s\njobs=%s\nbudget_mb=%s\nholder=%s\nstarted=%s\n"
                           % (label, self.machine_label(), jobs, budget_mb, holder, started))
        return path

    def running(self, holder_alive):
        """(label, jobs, budget_mb) per live build on this machine; a dead one's record goes."""
        try:
            names = self.machine.listdir(self.dir())
        except OSError:
            return []
        me, out = self.machine_label(), []
        for n in names:
            path = os.path.join(self.dir(), n)
            try:
                rec = kv(self.machine.read(path))
            except OSError:
                continue
            if rec.get("machine") != me:
                continue
            if holder_alive(rec.get("holder", "")):
                out.append((rec.get("label", ""), int(rec.get("jobs") or 0), int(rec.get("budget_mb") or 0)))
            else:
                self.machine.remove(path)
        return out

    def jobs(self, cores, avail_mb, mb_per_job, load=None, max_jobs=None, running=()):
        cores = max(1, cores - sum(r[1] for r in running))
        avail = max(0, avail_mb - sum(r[2] for r in running))
        by_mem = avail // mb_per_job
        by_cpu = cores
        if load is not None:
            if by_mem >= cores and load > cores // 2:
                load //= 2   # memory-idle under a high load average is a killed build's decaying average
            by_cpu = min(cores - load, cores // 2)
        jobs = min(by_mem, by_cpu)
        if max_jobs and jobs > max_jobs:
            jobs = max_jobs
        return max(1, jobs)

    def explain(self, cores, avail_mb, mb_per_job, load=None, max_jobs=None, running=()):
        jobs = self.jobs(cores, avail_mb, mb_per_job, load, max_jobs, running)
        reserved = sum(r[2] for r in running)
        act.log("resources: %d jobs (cores=%d avail=%dMB%s @ %dMB/job%s%s)"
                % (jobs, cores, avail_mb, " minus %dMB other builds" % reserved if reserved else "", mb_per_job,
                   ", polite, load=%d" % load if load is not None else "", ", max %s" % max_jobs if max_jobs else ""))
        if not max_jobs and jobs < cores // 2:
            by_mem = avail_mb // mb_per_job
            if by_mem <= jobs:
                act.warn("parallelism: %d jobs is under half of %d cores -- the memory\n  envelope only fits %d at %dMB/job (%dMB available)."
                         % (jobs, cores, by_mem, mb_per_job, avail_mb))
            elif load is not None:
                act.warn("parallelism: %d jobs is under half of %d cores -- load average\n  %d is treated as that many cores already spoken for on this shared machine."
                         % (jobs, cores, load))
            else:
                act.warn("parallelism: %d jobs is under half of %d cores -- %d is\n  this place's own ceiling (a reserve held back for the host, or a fixed vCPU/cgroup count)."
                         % (jobs, cores, cores))
        return jobs

    def free_gb(self, path):
        """`df -Pk` is the one spelling both dfs have."""
        return parse_df(self.machine.run(["df", "-Pk", path]).out)

    def disk_admit(self, what, need, free, where):
        if free is None:
            act.warn("cannot tell how much is free on %s; %s wants about %d GB, so this is not checked." % (where, what, need))
            return
        if free >= need:
            return
        act.barrier("%d GB free on %s; %s wants about %d GB.\n    It would halt part-built rather than fill the disk. 'wk gc' reclaims what\n"
                    "    nothing references, 'wk gc --purge-builds' the build trees images come out\n    of, and 'wk doctor' says where the rest went."
                    % (free, where, what, need))

    def admit(self, what, jobs, running):
        """One machine builds one thing at a time, whatever would be left over."""
        if not running:
            return
        rows = "".join("      %s (%s jobs, %s MB)\n" % r for r in running)
        act.barrier("%s is already building:\n%s    %s wants %d job(s) of it. A machine builds one thing at a time: two\n"
                    "    builds sharing one take longer together than in turn, and each reports a\n"
                    "    number the other moved. Wait for it ('wk status' shows a workspace's\n"
                    "    build), or --force to build beside it anyway.\n    A scheduled step comes back to this once it ends."
                    % (self.machine_label(), rows, what, jobs), retry=True)


def mb_per_job_setting(env, default=MB_PER_JOB):
    return int(env.get("WK_MB_PER_JOB") or default)


def parse_df(out):
    lines = out.replace("\r", "").splitlines()
    fields = lines[1].split() if len(lines) > 1 else []
    return (int(fields[3]) + 1048575) // 1048576 if len(fields) > 3 and fields[3].isdigit() else None


def build_jobs(res, budget, running):
    """From the memory not already spoken for, since a link out of RAM hangs a machine; clamped by cores."""
    return budget.jobs(res.cores(), res.avail_mem_mb(), res.mb_per_job(), None, res.max_jobs(), running)


def defaults():
    from wk.presets import DISK_GB
    return "".join(': "${%s:=%s}"\n' % pair for pair in (
        ("WK_RESERVE_CORES", RESERVE_CORES), ("WK_RESERVE_MB", RESERVE_MB),
        ("WK_HEADLESS_RESERVE_CORES", HEADLESS_RESERVE_CORES), ("WK_HEADLESS_RESERVE_MB", HEADLESS_RESERVE_MB),
        ("WK_MB_PER_JOB", MB_PER_JOB), ("WK_BUILD_DISK_GB", DISK_GB)))


READINGS = {"host-mem-mb": "host_mem_mb", "envelope-cores": "envelope_cores", "envelope-mem-mb": "envelope_mem_mb",
            "describe-cores": "describe_cores", "headless-marker": "headless_marker", "defaults": None}


def main(argv, env=None):
    from wk.machine import here
    p = argparse.ArgumentParser(prog="python3 -m wk.resources")
    p.add_argument("--os", choices=("linux", "macos"))
    p.add_argument("reading", choices=sorted(READINGS))
    a = p.parse_args(argv)
    res = Resources(here(), os.environ if env is None else env, a.os)
    out = str(getattr(res, READINGS[a.reading])() if READINGS[a.reading] else defaults())
    sys.stdout.write(out if a.reading in ("describe-cores", "headless-marker", "defaults") else out + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except act.Refused as e:
        sys.exit(e.status)
