"""Job sizing: how many compile jobs a run may use, from one probe of the place it runs in.

    reading = measure(machine, driver.exec_argv(ws, PROBE))     # inside the workspace, so a container's cgroup is seen
    jobs, mb = size(reading, preset.mb_per_job, borrowed, inv.env)

A reading that cannot be taken is fatal: nothing is built from a guess."""

import dataclasses
from typing import Optional

from wk.core.act import die

RESERVE_MB = 2048
MB = 1024 * 1024
MIN_BUDGET_MB = 8192
POLITE = ["nice", "-n", "19", "ionice", "-c", "3"]

# macOS has no MemAvailable, so its reading is the total; `sysctl vm.loadavg` prints `{ 1.23 1.20 1.10 }`.
PROBE = ["sh", "-c", """if [ "$(uname -s)" = Darwin ]; then
    echo "cores $(sysctl -n hw.ncpu)"; echo "total_bytes $(sysctl -n hw.memsize)"; echo "load $(sysctl -n vm.loadavg)"
else
    echo "cores $(nproc)"; echo "available_kb $(sed -n 's/^MemAvailable: *//p' /proc/meminfo)"; echo "load $(cat /proc/loadavg)"
    for f in memory.max cpu.max; do [ ! -e /sys/fs/cgroup/$f ] || echo "$f $(cat /sys/fs/cgroup/$f)"; done
fi"""]


@dataclasses.dataclass(frozen=True)
class Reading:
    """`ceiling_mb` and `ceiling_cores` are the cgroup v2 memory.max and cpu.max the place runs under, None without one."""
    cores: int
    load: int
    mem_mb: int
    ceiling_mb: Optional[int] = None
    ceiling_cores: Optional[int] = None


def parse(text, who):
    got = dict((line.split(None, 1) + [""])[:2] for line in text.splitlines() if line.strip())

    def number(key, field=0):
        words = [w for w in got.get(key, "").split() if w not in ("{", "}")]
        try:
            return float(words[field])
        except (IndexError, ValueError):
            die("cannot read %s on %s (the probe printed `%s`): jobs are sized from it, and nothing is built from a guess\n"
                "    the probe is PROBE in lib/wk/tasks/sizing.py: run it in the workspace to see what it cannot read"
                % (key, who, got.get(key, "")))
    mem_mb = number("total_bytes") // MB if "total_bytes" in got else number("available_kb") // 1024
    ceiling_mb = None if got.get("memory.max", "max") == "max" else int(number("memory.max") // MB)
    ceiling_cores = None if got.get("cpu.max", "max").startswith("max") else max(1, int(number("cpu.max") / number("cpu.max", 1)))
    return Reading(int(number("cores")), int(number("load")), int(mem_mb), ceiling_mb, ceiling_cores)


def measure(machine, argv):
    r = machine.run(argv, timeout=30)
    if not r.ok:
        die("cannot measure %s (the sizing probe exited %d: %s): nothing is built from a guess\n    `wk doctor` says what the server lacks"
            % (machine.name, r.rc, r.err.strip()))
    return parse(r.out, machine.name)


def jobs(reading, mb_per_job, borrowed, cap=None):
    cores = reading.cores if reading.ceiling_cores is None else min(reading.cores, reading.ceiling_cores)
    if borrowed:
        cores = min(cores - reading.load, cores // 2)
    mem = reading.mem_mb if reading.ceiling_mb is None else min(reading.mem_mb, reading.ceiling_mb)
    return max(1, min(cores, cores if cap is None else cap, (mem - RESERVE_MB) // mb_per_job))


def size(reading, mb_per_job, borrowed, env):
    """(jobs, MB per job). WK_MB_PER_JOB and WK_MAX_JOBS, set on the server, override the preset's figure and cap the count."""
    mb = int(env.get("WK_MB_PER_JOB") or mb_per_job)
    return jobs(reading, mb, borrowed, int(env["WK_MAX_JOBS"]) if env.get("WK_MAX_JOBS") else None), mb
