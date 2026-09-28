"""What is on a Mac's screen and what came back to life, asked once (`blocker`) or for as long as a run lasts (`Watch`)."""

import os
import threading

from wk.machine import lib_argv

WINDOWS = "bench/mac-window-probe.sh"
DESKTOP = "bench/mac-quiet-desktop.sh"
WATCH_SECONDS = 10
ASK_SECONDS = 20   # one reading, a first probe build included
UNASKED = ("the window server could not be asked: %s answered `?` (not macOS, no `cc` to build the probe with, "
           "or a probe that would not build)" % WINDOWS)


def blocker(machine, root):
    said = machine.run(lib_argv(root, WINDOWS, "wk_window_probe"), timeout=ASK_SECONDS).out
    reading = next((l[len("windows="):] for l in said.splitlines() if l.startswith("windows=")), "")
    if not reading or reading == "?":
        return "?"
    uninvited = machine.run(lib_argv(root, WINDOWS, "wk_window_unexpected", reading), timeout=ASK_SECONDS).out
    return ",".join(sorted({e.split(":", 1)[0] for e in uninvited.split(";") if e}))


def restarted(machine, root):
    rows = machine.run(lib_argv(root, DESKTOP, "wk_quiet_desktop_stopped"), timeout=ASK_SECONDS).out.splitlines()
    skip = set(machine.run(lib_argv(root, DESKTOP, "wk_quiet_desktop_unstoppable"), timeout=ASK_SECONDS).out.split())
    want = {r.split()[1] for r in rows if len(r.split()) > 1} - skip
    seen = set()
    for line in machine.run(["ps", "-Ao", "stat=,comm="], timeout=ASK_SECONDS).out.splitlines():
        state, _, comm = line.strip().partition(" ")
        comm = comm.strip().rsplit("/", 1)[-1]
        if comm in want and not state.startswith("T"):
            seen.add(comm)
    return sorted(seen)


class Watch(threading.Thread):
    """A sighting, a reading that could not be taken, and a watch that failed are each a finding: none reads clean."""

    def __init__(self, machine, root, clock, env=None):
        super().__init__(daemon=True)
        self.machine, self.root, self.clock = machine, str(root), clock
        env = os.environ if env is None else env
        self.every = float(env.get("WK_SCREEN_WATCH_SECONDS") or WATCH_SECONDS)
        self.seen, self.done = [], threading.Event()

    def note(self, what):
        self.seen.append("%s\t%s" % (self.clock.iso(), what))

    def run(self):
        try:
            if self.machine.run(["uname", "-s"], timeout=ASK_SECONDS).out.strip() != "Darwin":
                return
            while True:
                front = blocker(self.machine, self.root)
                if front:
                    self.note(UNASKED if front == "?" else front)
                back = restarted(self.machine, self.root)
                if back:
                    self.note("running again: " + ",".join(back))
                if self.done.wait(self.every):
                    return
        except Exception as e:   # noqa: BLE001 -- a watch that died unseen would read as a clean run
            self.note("the screen watch stopped: %s: %s" % (type(e).__name__, e))

    def stop(self, wait=None):
        wait = self.every + 4 * ASK_SECONDS if wait is None else wait
        self.done.set()
        if self.is_alive():
            self.join(wait)
        if self.is_alive():
            self.note("the screen watch did not answer within %gs of the run ending" % wait)
        return sorted({l.split("\t", 1)[1]: l for l in self.seen}.values(), key=lambda l: l.split("\t", 1)[1])
