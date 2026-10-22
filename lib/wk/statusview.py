"""`wk status` rendering: wk.status's record stream merged into one document, drawn as text or JSON. `plan` names every
job and its machine and `flush` ends one, so a machine is drawn when its last job has flushed."""

import json
import os
import sys

MARKERS = ("plan", "flush")
LISTS = {"raw": "raw", "task": "tasks"}


def colour_wanted(stdout=None, env=None):
    stdout = sys.stdout if stdout is None else stdout
    env = os.environ if env is None else env
    try:
        tty = stdout.isatty()
    except (AttributeError, ValueError):
        tty = False
    return bool(tty and not env.get("NO_COLOR"))


def records_from_lines(lines):
    """Records out of JSON lines; an unreadable line is reported and skipped."""
    for line in lines:
        line = line.strip()
        if line.startswith("{"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print("wk status: unreadable record: %s" % line[:120], file=sys.stderr)


def strip_markers(records):
    return (r for r in records if r.get("kind") not in MARKERS)


class Merger:
    """Records -> one document, grouped machine / method / workspace, in arrival order."""

    def __init__(self):
        self.doc = {"machines": [], "exit": 0}
        self.index, self.ws_index = {}, {}

    def machine(self, name):
        if name not in self.index:
            self.index[name] = dict({"name": name, "self": False, "methods": []}, **{k: [] for k in LISTS.values()})
            self.doc["machines"].append(self.index[name])
        return self.index[name]

    def method(self, m, name):
        for g in m["methods"]:
            if g["name"] == name:
                return g
        m["methods"].append({"name": name, "workspaces": []})
        return m["methods"][-1]

    def merge_workspace(self, g, r):
        """One row per workspace name; a second view naming another state marks the row `disagree` and is exit 4, so a
        disagreement is never quieter than the state it hides."""
        key = (id(g), r.get("name", "?"))
        existing = self.ws_index.get(key)
        if existing is None:
            self.ws_index[key] = r
            g["workspaces"].append(r)
        elif (existing.get("state"), existing.get("ws")) != (r.get("state"), r.get("ws")):
            existing["disagree"] = [existing.get("state", "?"), r.get("state", "?")]
            self.doc["exit"] = max(self.doc["exit"], 4)

    def feed(self, r):
        kind = r.get("kind")
        if kind == "machine":
            m = self.machine(r["name"])
            m["self"] = m["self"] or bool(r.get("self"))
        elif kind == "workspace":
            self.merge_workspace(self.method(self.machine(r.get("machine", "?")), r.get("method", "?")), r)
        elif kind in LISTS:
            self.machine(r.get("machine", "?"))[LISTS[kind]].append(r)
        elif kind == "exit":
            self.doc["exit"] = max(self.doc["exit"], int(r.get("code", 0)))


def merge(records):
    merger = Merger()
    for r in strip_markers(records):
        merger.feed(r)
    return merger.doc


HUES = {"good": ("ok", "present", "running", "done"),
        "busy": ("creating", "starting", "building", "silent", "pending"),
        "bad": ("died", "unanswered", "failed", "oom", "stalled", "broken", "unreachable", "gave-up", "error", "disagree", "unreadable"),
        "idle": ("absent", "stopped", "exited", "-", "clean", "cancelled")}
ANSI = {"good": "\033[32m", "busy": "\033[33m", "bad": "\033[31m", "idle": "\033[2m", "dim": "\033[2m", "bold": "\033[1m",
        "head": "\033[1;36m"}
COLUMNS = ("workspace", "state", "branch", "base", "work", "snap", "build")
STEP_MARKS = {"done": ("[x]", "good"), "failed": ("[!]", "bad"), "skipped": ("[-]", "dim"), "pending": ("[ ]", "dim")}


def severity(word):
    w = (word or "").split(" ")[0].lower()
    return next((hue for hue, words in HUES.items() if w in words), "")


def paint(text, key, colour):
    return "%s%s\033[0m" % (ANSI[key], text) if colour and ANSI.get(key) else text


def sub_text(sub):
    return "%s=%s%s" % (sub.get("kind", "?"), sub.get("state", "?"), " (%s)" % sub["preset"] if sub.get("preset") else "")


def ws_cells(ws):
    work = ", ".join(b for b in ("%s unpushed" % ws["unpushed"] if ws.get("unpushed") else "",
                                 "%s dirty" % ws["dirty"] if ws.get("dirty") else "",
                                 "+%s new" % ws["untracked"] if ws.get("untracked") else "") if b)
    branch = (ws.get("branch") or "-") + (" ↓%s" % ws["behind"] if ws.get("behind") else "") + (" ↑%s" % ws["ahead"] if ws.get("ahead") else "")
    subs = ws.get("subs") or []
    state = "disagree (%s vs %s)" % tuple(ws["disagree"]) if ws.get("disagree") else ws.get("state", "?")
    return [ws.get("name", "?"), state, branch, ws.get("base") or "?", work or ("clean" if ws.get("ws") == "present" else ""),
            "-%s" % ws["snap_behind"] if ws.get("snap_behind") else "", sub_text(subs[0]) if subs else ""]


def notes(items, indent, colour):
    out = []
    for n in items or []:
        hue = "busy" if n.get("level") == "warn" else "dim"
        for i, text in enumerate(n.get("text", "").split("\n")):
            out.append(indent + paint(("! " if i == 0 and hue == "busy" else "  ") + text.strip(), hue, colour))
    return out


def step_mark(step, task_state):
    if task_state == "ok":
        return STEP_MARKS["done"]
    if step == "running":
        return ("[>]", "busy") if task_state in ("running", "silent", "starting") else ("[!]", "bad")
    return STEP_MARKS.get(step, STEP_MARKS["pending"])


def task_lines(t, colour):
    state = t.get("state", "?")
    out = ["  %s %s  %s  %s" % (t.get("task_kind", "task"), t.get("name", "?"), paint(state, severity(state), colour),
                                paint("%s  since %s" % (t.get("machine", "?"), t.get("since", "?")), "dim", colour))]
    if t.get("subject"):
        out.append("      " + paint(t["subject"], "dim", colour))
    steps = t.get("steps") or []
    for i, line in enumerate(t.get("plan") or [], 1):
        mark, hue = step_mark(steps[i - 1] if i <= len(steps) else "pending", state)
        out.append("      " + paint("%s %s" % (mark, line), hue, colour))
    if state == "died":
        out.append("      " + paint("died -- %s" % ("exit %s" % t["exit"] if t.get("exit") else "no exit recorded"), "bad", colour))
    for key in ("holds", "kill", "log"):
        if t.get(key):
            out.append("      " + paint("%-5s %s" % (key + ":", t[key]), "dim", colour))
    return out + notes(t.get("notes"), "      ", colour)


def render_machine_block(m, colour):
    out = ["", paint(m["name"], "head", colour) + (paint("   this machine", "dim", colour) if m.get("self") else "")]
    if not any(g["workspaces"] for g in m["methods"]) and not m["raw"]:
        out.append("  " + paint("(no workspaces on it)", "idle", colour))
    for g in m["methods"]:
        rows = [ws_cells(ws) for ws in g["workspaces"]]
        if not rows:
            continue
        widths = [max(len(c) for c in col) for col in zip(COLUMNS, *rows)]
        out += ["", "  " + paint(g["name"], "bold", colour),
                "    " + paint("  ".join(h.upper().ljust(w) for h, w in zip(COLUMNS, widths)).rstrip(), "dim", colour)]
        for ws, cells in zip(g["workspaces"], rows):
            hues = {1: severity(cells[1]), 6: severity((ws.get("subs") or [{}])[0].get("state"))}
            out.append("    " + "  ".join(paint(c.ljust(w), hues.get(i, ""), colour) for i, (c, w) in enumerate(zip(cells, widths))).rstrip())
            out += ["      " + paint(sub_text(s), severity(s.get("state")), colour) for s in (ws.get("subs") or [])[1:]]
            out += notes(ws.get("notes"), "      ", colour)
    if m["tasks"]:
        out.append("")
    for t in m["tasks"]:
        out += task_lines(t, colour)
    for r in m["raw"]:
        out += [""] + ["  " + paint(ln, "dim", colour) for ln in r.get("text", "").split("\n")] + notes(r.get("notes"), "  ", colour)
    return out


def render_text_stream(records, out, colour):
    """Arrival order: a planned machine gets a placeholder at once and its block when the last job the plan gave it has flushed."""
    merger, owner, pending, drawn = Merger(), {}, {}, set()

    def draw(machine):
        for m in merger.doc["machines"]:
            if m["name"] == machine and machine not in drawn:
                drawn.add(machine)
                out.write("".join(line + "\n" for line in render_machine_block(m, colour)))
        out.flush()

    for r in records:
        kind = r.get("kind")
        if kind == "plan":
            for j in r.get("jobs", []):
                owner[j["job"]] = j.get("machine")
                pending.setdefault(j.get("machine"), set()).add(j["job"])
                out.write(paint("  probing %s…" % j["job"], "dim", colour) + "\n")
            out.flush()
        elif kind == "flush":
            job = r.get("job")
            if job not in owner:
                print("wk status: '%s' ended without being in the plan" % job, file=sys.stderr)
                continue
            pending[owner[job]].discard(job)
            if not pending[owner[job]]:
                draw(owner[job])
        else:
            merger.feed(r)
    for m in merger.doc["machines"]:
        draw(m["name"])
    out.write("\n")
    out.flush()
    return merger.doc
