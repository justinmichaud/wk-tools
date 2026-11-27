"""What `wk status` reports: one row per task that is live or is the last under its label, and one exit code.

Silence is the time since a running task's log last grew, read when asked: nothing watches in the background."""

from wk.core import seams
from wk.core.errors import BUSY, FAILED, OK, STALLED

WARN, STALL = 300, 1800
STALLED_STATE = "stalled"
LIVE = (seams.QUEUED, seams.RUNNING, STALLED_STATE)
CODES = {seams.QUEUED: BUSY, seams.RUNNING: BUSY, STALLED_STATE: STALLED, seams.FAILED: FAILED, seams.KILLED: FAILED,
         seams.DONE: OK}


def rows(tasks, quiet, now):
    """`quiet` is {id: seconds since its log grew}, asked for only when a task is running."""
    last = {t.label: t.id for t in tasks}
    out = []
    for t in tasks:
        if t.ended and last[t.label] != t.id:
            continue
        silent = int(quiet()[t.id]) if t.state == seams.RUNNING else None
        state = STALLED_STATE if silent is not None and silent >= STALL else t.state
        out.append({"id": t.id, "label": t.label, "group": t.group, "state": state, "rc": t.rc, "age": int(now - t.at), "silent": silent})
    return out


def code(rows):
    """3 stalled, 2 busy, 1 failed, 0."""
    codes = [CODES[row["state"]] for row in rows]
    live = [c for c, row in zip(codes, rows) if row["state"] in LIVE]
    return max(live or codes or [OK])


def span(seconds):
    return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60) if seconds >= 3600 else "%dm%02ds" % (seconds // 60, seconds % 60)


def line(row):
    words = [span(row["age"]) + ("" if row["state"] in LIVE else " ago")]
    if row["rc"]:
        words.append("exit %d" % row["rc"])
    if row["silent"] is not None and row["silent"] >= WARN:
        words.append("silent for %ds" % row["silent"])
    return ("%-8s %-30s %s" % (row["state"], row["label"], ", ".join(words))).rstrip()


def normalise(text):
    """A log as a terminal would leave it: ninja and Xcode redraw progress with carriage returns."""
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
