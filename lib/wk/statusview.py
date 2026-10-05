"""`wk status` rendering: wk.status's record stream merged into one document, drawn as text, JSON, a static page
or a served one. `plan` names every job and its machine and `flush` ends one, so a machine is drawn when its
last job has flushed."""

import http.server
import json
import os
import sys
import threading
import time
import webbrowser

from wk import store
from wk.resources import workspace_marker_path

MARKERS = ("plan", "flush")


def default_mode(env, isatty):
    """The page at a terminal that has a browser to open, the table everywhere else."""
    if env.get("WK_STATUS_VIEW"):
        return env["WK_STATUS_VIEW"]
    if not isatty or env.get("CI") or env.get("NO_COLOR"):
        return "text"
    if os.path.isfile(workspace_marker_path(env)):
        return "text"
    if (env.get("SSH_CONNECTION") or env.get("SSH_TTY")) and not env.get("DISPLAY"):
        return "text"
    return "web"


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
    for r in records:
        if r.get("kind") not in MARKERS:
            yield r


class Merger:
    """Records -> one document, grouped machine / method / workspace, in arrival order."""

    LISTS = {"fact": "facts", "raw": "raw", "disk": "disk", "service": "services", "lock": "locks",
             "switch": "switches", "capacity": "capacity", "bench": "bench", "task": "tasks", "sdk": "sdk"}

    def __init__(self):
        self.doc = {"machines": [], "fleet": [], "bridges": [], "exit": 0}
        self.index = {}
        self.ws_index = {}

    def machine(self, name):
        if name not in self.index:
            m = {"name": name, "self": False, "methods": []}
            m.update((k, []) for k in self.LISTS.values())
            self.index[name] = m
            self.doc["machines"].append(m)
        return self.index[name]

    def method(self, m, name):
        for g in m["methods"]:
            if g["name"] == name:
                return g
        g = {"name": name, "workspaces": []}
        m["methods"].append(g)
        return g

    def merge_workspace(self, g, r):
        """One row per workspace name; a second view naming another state marks the row `disagree` and
        is exit 4, so a disagreement is never quieter than the state it hides."""
        key = (id(g), r.get("name", "?"))
        existing = self.ws_index.get(key)
        if existing is None:
            self.ws_index[key] = r
            g["workspaces"].append(r)
            return
        if existing.get("state") != r.get("state") or existing.get("ws") != r.get("ws"):
            existing["disagree"] = [existing.get("state", "?"), r.get("state", "?")]
            self.doc["exit"] = max(self.doc["exit"], 4)

    def feed(self, r):
        kind = r.get("kind")
        if kind == "machine":
            m = self.machine(r["name"])
            m["self"] = m["self"] or bool(r.get("self"))
            for k in ("tailnet", "direct", "conf"):
                if r.get(k) and not m.get(k):
                    m[k] = r[k]
        elif kind == "workspace":
            self.merge_workspace(self.method(self.machine(r.get("machine", "?")), r.get("method", "?")), r)
        elif kind in self.LISTS:
            self.machine(r.get("machine", "?"))[self.LISTS[kind]].append(r)
        elif kind == "fleet":
            self.doc["fleet"].append(r)
        elif kind == "bridge":
            self.doc["bridges"].append(r)
        elif kind == "exit":
            self.doc["exit"] = max(self.doc["exit"], int(r.get("code", 0)))


def merge(records):
    merger = Merger()
    for r in strip_markers(records):
        merger.feed(r)
    return merger.doc


GOOD = ("ok", "present", "running", "host mode", "up", "bench", "open", "complete")
BUSY = ("creating", "starting", "building", "fixing", "no", "empty", "held", "silent", "base", "role")
BAD = ("unhealthy", "incomplete", "died", "unanswered", "failed", "oom", "stalled", "broken",
       "unreachable", "gave-up", "error", "closed", "disagree", "desync", "unreadable")
IDLE = ("absent", "none", "stopped", "exited", "-", "clean", "finished", "off", "cancelled")


def severity(word):
    w = (word or "").split(" ")[0].lower()
    if w in GOOD:
        return "good"
    if w in BUSY:
        return "busy"
    if w in BAD:
        return "bad"
    if w in IDLE:
        return "idle"
    return ""


def sub_text(sub):
    out = "%s=%s" % (sub.get("kind", "?"), sub.get("state", "?"))
    if sub.get("preset"):
        out += " (%s)" % sub["preset"]
    return out


RESET = "\033[0m"
ANSI = {"good": "\033[32m", "busy": "\033[33m", "bad": "\033[31m", "idle": "\033[2m", "": "",
        "bold": "\033[1m", "dim": "\033[2m", "head": "\033[1;36m"}

COLUMNS = ("workspace", "state", "branch", "base", "work", "snap", "build")


def ws_work(ws):
    bits = []
    if ws.get("unpushed"):
        bits.append("%s unpushed" % ws["unpushed"])
    if ws.get("dirty"):
        bits.append("%s dirty" % ws["dirty"])
    if ws.get("untracked"):
        bits.append("+%s new" % ws["untracked"])
    if bits:
        return ", ".join(bits)
    return "clean" if ws.get("ws") == "present" else ""


def ws_work_hue(ws):
    return "busy" if ws.get("unpushed") or ws.get("dirty") else "idle"


def ws_branch(ws, cap=40):
    b = ws.get("branch") or "-"
    if len(b) > cap:
        b = b[: cap - 1] + "…"
    if ws.get("behind"):
        b += " ↓%s" % ws["behind"]
    if ws.get("ahead"):
        b += " ↑%s" % ws["ahead"]
    return b


def ws_snap(ws):
    n = ws.get("snap_behind")
    return "-%s" % n if n else ""


def ws_state_cell(ws):
    d = ws.get("disagree")
    if d:
        return "disagree (%s vs %s)" % (d[0], d[1])
    return ws.get("state", "?")


def ws_cells(ws):
    subs = ws.get("subs") or []
    return [ws.get("name", "?"), ws_state_cell(ws), ws_branch(ws), ws.get("base") or "?",
            ws_work(ws), ws_snap(ws), sub_text(subs[0]) if subs else ""]


def ws_hues(ws):
    subs = ws.get("subs") or []
    return {1: severity(ws_state_cell(ws)), 4: ws_work_hue(ws),
            5: "busy" if ws.get("snap_behind") else "",
            6: severity(subs[0].get("state")) if subs else ""}


def gb(mb):
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        return "?"
    return "%.0fG" % (mb / 1024.0) if mb >= 1024 else "%dM" % mb


def disk_hue(pct):
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return ""
    return "bad" if pct >= 90 else "busy" if pct >= 75 else "good"


def load_hue(load, cores):
    try:
        load, cores = float(load), int(cores)
    except (TypeError, ValueError):
        return ""
    if not cores:
        return ""
    return "bad" if load > cores else "busy" if load > cores / 2.0 else "good"


def sdk_verdict(s):
    upstream = s.get("upstream")
    if upstream:
        if upstream == s.get("tag"):
            return "current", "good"
        return "behind (%s)" % upstream, "busy"
    return "unknown -- %s" % (s.get("unknown") or "registry did not answer"), ""


def sdk_line(s, colour):
    return "%s pulled %s; upstream %s" % (s.get("tag") or "?", s.get("pulled") or "?",
                                          paint(*sdk_verdict(s), colour))


def where_tag(obj):
    w = (obj.get("where") or "").replace("in the ", "").replace("the ", "")
    return " (%s)" % w if w else ""


def paint(text, key, colour):
    if not colour or not ANSI.get(key):
        return text
    return "%s%s%s" % (ANSI[key], text, RESET)


class Kv(tuple):
    __slots__ = ()


def row(cells, widths, hues, colour):
    hues = hues or {}
    last = len(cells) - 1
    return "    " + "  ".join(paint(c if i == last else c.ljust(widths[i]), hues.get(i, ""), colour)
                             for i, c in enumerate(cells))


class Writer:

    def __init__(self, colour):
        self.colour = colour
        self.out = []

    def heading(self, name, tag=""):
        self.out.append("")
        self.out.append(paint(name, "head", self.colour) + (paint("   " + tag, "dim", self.colour) if tag else ""))

    def kv(self, label, value, indent="  "):
        self.out.append(Kv((indent, label, value)))

    def align(self, start):
        labels = [k[1] for k in self.out[start:] if isinstance(k, Kv)]
        if not labels:
            return
        w = max([len(l) for l in labels] + [14])
        for i in range(start, len(self.out)):
            if not isinstance(self.out[i], Kv):
                continue
            indent, label, value = self.out[i]
            lab = label.ljust(w) if label else " " * w
            self.out[i] = indent + (paint(lab, "dim", self.colour) if label else lab) + " " + value

    def meta(self, obj, indent="  "):
        if obj.get("tailnet"):
            self.kv("reached", obj["tailnet"], indent)
        if obj.get("direct"):
            self.kv("without tailscale", obj["direct"], indent)
        if obj.get("conf"):
            self.kv("from", paint(obj["conf"], "dim", self.colour), indent)

    def notes(self, items, indent="      "):
        for n in items or []:
            hue = "busy" if n.get("level") == "warn" else "dim"
            for i, text in enumerate(n.get("text", "").split("\n")):
                self.out.append(indent + paint(("! " if i == 0 and hue == "busy" else "  ") + text.strip(), hue, self.colour))


LIVE = ("running", "silent", "starting")
STEP_MARKS = {"done": ("[x]", "good"), "failed": ("[!]", "bad"), "skipped": ("[-]", "dim"), "pending": ("[ ]", "dim")}


def step_mark(step_state, task_state):
    if task_state == "ok":
        return STEP_MARKS["done"]
    if step_state == "running":
        return ("[>]", "busy") if task_state in LIVE else ("[!]", "bad")
    return STEP_MARKS.get(step_state, STEP_MARKS["pending"])


def render_task(wr, t, colour):
    state = t.get("state", "?")
    head = "%s  %s" % (paint(state, severity(state), colour),
                       paint("%s  since %s" % (t.get("machine", "?"), t.get("since", "?")), "dim", colour))
    wr.kv("%s %s" % (t.get("task_kind", "task"), t.get("name", "?")), head)
    if t.get("subject"):
        wr.out.append("      " + paint(t["subject"], "dim", colour))
    plan = t.get("plan") or []
    steps = t.get("steps") or []
    for i, line in enumerate(plan, 1):
        mark, hue = step_mark(steps[i - 1] if i <= len(steps) else "pending", state)
        wr.out.append("      " + paint("%s %s" % (mark, line), hue, colour))
    if state == "died":
        rc = t.get("exit")
        wr.out.append("      " + paint("died -- %s" % ("exit %s" % rc if rc else "no exit recorded"), "bad", colour))
    if t.get("holds"):
        wr.out.append("      " + paint("holds: %s" % t["holds"], "dim", colour))
    wr.out.append("      " + paint("kill: %s" % t.get("kill", "?"), "dim", colour))
    if t.get("log"):
        wr.out.append("      " + paint("log:  %s" % t["log"], "dim", colour))


def column_widths(m):
    w = [len(h) for h in COLUMNS]
    for g in m["methods"]:
        for ws in g["workspaces"]:
            for i, c in enumerate(ws_cells(ws)):
                w[i] = max(w[i], len(c))
    return w


def render_machine_block(m, colour):
    w = column_widths(m)
    wr = Writer(colour)
    out = wr.out
    wr.heading(m["name"], "this machine" if m.get("self") else "")
    start = len(out)
    wr.meta(m)

    if not any(g["workspaces"] for g in m["methods"]) and not m["raw"]:
        out.append("  " + paint("(no workspaces on it)", "idle", colour))

    for g in m["methods"]:
        if not g["workspaces"]:
            continue
        out.append("")
        out.append("  " + paint(g["name"], "bold", colour))
        out.append(row([h.upper() for h in COLUMNS], w, {i: "dim" for i in range(len(COLUMNS))}, colour))
        for ws in g["workspaces"]:
            subs = ws.get("subs") or []
            out.append(row(ws_cells(ws), w, ws_hues(ws), colour))
            for sub in subs[1:]:
                out.append("      " + paint(sub_text(sub), severity(sub.get("state")), colour))
            wr.notes(ws.get("notes"))

    if any(m.get(k) for k in ("disk", "sdk", "services", "switches", "capacity", "locks", "tasks", "bench")):
        out.append("")
    for d in m.get("disk") or []:
        tail = ""
        if d.get("snapshots"):
            tail = "  ·  %s snapshot%s" % (d["snapshots"], "" if d["snapshots"] == "1" else "s")
            if d.get("reclaimable") and d["reclaimable"] != "0":
                tail += paint(", %s reclaimable (wk gc)" % d["reclaimable"], "busy", colour)
        wr.kv("disk" + where_tag(d),
              "%s used   %s free of %s%s" % (paint((d.get("used_pct", "?") or "?") + "%", disk_hue(d.get("used_pct")), colour),
                                            gb(d.get("free_mb")), gb(d.get("total_mb")), tail))
    for s in m.get("sdk") or []:
        wr.kv("sdk image", sdk_line(s, colour))
    for sv in m.get("services") or []:
        wr.kv(sv.get("name", "?"), paint(sv.get("state", "?"), severity(sv.get("state")), colour))
        if sv.get("fix"):
            wr.kv("", paint(sv["fix"], "busy", colour))
    for sw in m.get("switches") or []:
        wr.kv(sw.get("name", "?") + where_tag(sw),
              paint(sw.get("state", "?"), "good" if sw.get("state") == "on" else "busy", colour)
              + paint("   " + sw.get("detail", ""), "dim", colour))
    for cap in m.get("capacity") or []:
        label = "load" + where_tag(cap)
        if not cap.get("cores"):
            if cap.get("note"):
                wr.kv(label, paint(cap["note"], "bad", colour))
            continue
        free = "%s free" % gb(cap.get("free_mb"))
        if cap.get("mem_mb"):
            free += " of %s" % gb(cap.get("mem_mb"))
        wr.kv(label, "%s of %s cores   %s" % (paint(cap.get("load") or "?", load_hue(cap.get("load"), cap.get("cores")), colour),
                                              cap.get("cores"), free))
    for lk in m.get("locks") or []:
        wr.kv("lock", "%s  %s  %s" % (lk.get("resource", "?"),
                                      paint("held" if lk.get("alive") else "stale", "busy" if lk.get("alive") else "bad", colour),
                                      paint("pid %s  %s" % (lk.get("pid", "?"), lk.get("cmd", "")), "dim", colour)))
    for b in m.get("bench") or []:
        wr.kv("bench", "%s  %s  %s" % (b.get("task", "?"), paint(b.get("state", "?"), severity(b.get("state")), colour),
                                       paint(b.get("summary", ""), "dim", colour)))
        wr.kv("", paint("%s  %s" % (b.get("subject", ""), b.get("path", "")), "dim", colour))
    for t in m.get("tasks") or []:
        render_task(wr, t, colour)

    for r in m["raw"]:
        out.append("")
        for ln in r.get("text", "").split("\n"):
            out.append("  " + paint(ln, "dim", colour))
        wr.notes(r.get("notes"), "  ")

    if m["facts"]:
        out.append("")
    for f in m["facts"]:
        if f.get("type") == "wk-tools":
            what = "wk-tools" + (" (%s)" % f["copy"] if f.get("copy") else "")
            verdict = (paint("in sync", "good", colour) if f.get("insync")
                       else paint("DIFFERS from the workstation (%s)" % f.get("expect", "?"), "bad", colour))
            ident = (f.get("sha") or "?") + ("+dirty" if f.get("dirty") else "")
            wr.kv(what, "%s  %s" % (paint(ident, "dim", colour), verdict))
            if f.get("fix"):
                wr.kv("", paint(f["fix"], "busy", colour))
        elif f.get("type") == "key":
            wr.kv("push key", f.get("text", ""))

    wr.align(start)
    return out


def render_fleet_and_bridges(doc, colour):
    wr = Writer(colour)
    out = wr.out
    fleet = [f for f in doc["fleet"] if f.get("machine") != self_machine_name(doc)]

    if fleet:
        wr.heading("fleet", "role, mode, and the media wk owns")
        fw = max([len(f.get("machine", "")) for f in fleet] + [7])
        rw = max([len(f.get("role", "")) for f in fleet] + [4])
        mw = max([len(f.get("mode", "")) for f in fleet] + [4])
        start = len(out)
        for f in fleet:
            out.append("  %s  %s  %s  %s" % (f.get("machine", "").ljust(fw), paint(f.get("role", "").ljust(rw), "dim", colour),
                                             paint(f.get("mode", "").ljust(mw), severity(f.get("mode")), colour), f.get("media", "")))
            if f.get("armed"):
                detail = "armed for %s" % f["armed"]
                if f.get("armed_by"):
                    detail += " by %s" % f["armed_by"]
                if f.get("armed_at"):
                    detail += " since %s" % f["armed_at"]
                hue = "busy"
                if f.get("armed_desync"):
                    detail = "desync -- %s, and the record was never cleared" % detail
                    hue = "bad"
                out.append("  %s  %s" % (" " * fw, paint("** %s -- wk boot %s --status **"
                                                        % (detail, f.get("machine", "")), hue, colour)))
            wr.meta(f, "  " + " " * fw + "  ")
        wr.align(start)

        recipes = [f for f in fleet if f.get("reprovision")]
        if recipes:
            wr.heading("re-provisioning", "each machine from nothing")
            for f in recipes:
                out.append("  " + paint(f.get("machine", ""), "dim", colour) + "  " + paint(f.get("role", ""), "dim", colour))
                for line in f["reprovision"].split("\n"):
                    if not line.strip():
                        continue
                    if line.startswith(" "):
                        out.append("          " + paint(line.strip(), "dim", colour))
                    else:
                        out.append("      " + line)
                out.append("")

            roles = []
            if any(f.get("role") == "bench-device" for f in fleet):
                roles.append(("a rescue system", "wk sysimage write <id> --disk <machine>:<device> --rescue"))
                roles.append(("a bench system", "wk sysimage write <id> --disk <machine>:<device>"))
            if doc.get("bridges"):
                roles.append(("a tailnet bridge", "wk machine setup <name> --disk <machine>:<device>"))
            if any(f.get("role") == "workstation" for f in fleet):
                roles.append(("a workstation", "./setup"))
            if roles:
                out.append("  " + paint("by role", "dim", colour) + "  "
                           + paint("one image serves both board roles; the marker on the card is the only difference", "dim", colour))
                lw = max(len(r[0]) for r in roles)
                for what, cmd in roles:
                    out.append("      " + paint(what.ljust(lw), "dim", colour) + "   " + cmd)
                out.append("")

    if doc["bridges"]:
        wr.heading("tailnet bridges", "probed: the segment, the role, and its own health check")
        bw = max(len(b.get("name", "")) for b in doc["bridges"])
        start = len(out)
        for b in doc["bridges"]:
            out.append("  %s  %-10s %-14s %s" % (b.get("name", "").ljust(bw), b.get("device", "?"), b.get("segment", "?"),
                                                 paint(b.get("state", "?"), severity(b.get("state")), colour)))
            pad = "  " + " " * bw + "  "
            if b.get("health"):
                wr.kv("health", b["health"], pad)
            if "role_insync" in b:
                wr.kv("role", paint("this repository's", "good", colour) if b["role_insync"]
                      else paint("older than this repository -- wk machine setup %s" % b.get("name", ""), "bad", colour), pad)
            wr.meta(b, pad)
            if b.get("note"):
                out.append(pad + paint(b["note"], "dim", colour))
            wr.notes(b.get("notes"), pad)
        wr.align(start)

    return out


def self_machine_name(doc):
    return next((m["name"] for m in doc["machines"] if m.get("self")), None)


def self_line_text(machine, role, mode, colour):
    return "%s -- %s, %s" % (machine, paint(role, "dim", colour), paint(mode, severity(mode), colour))


def render_text_stream(records, out, colour):
    """Arrival order: a planned machine gets a placeholder at once and its
    block when the last job the plan gave it has flushed."""
    merger = Merger()
    owner = {}
    pending = {}
    drawn = set()
    lead_shown = False

    def draw(machine):
        for m in merger.doc["machines"]:
            if m["name"] == machine and machine not in drawn:
                drawn.add(machine)
                for line in render_machine_block(m, colour):
                    out.write(line + "\n")
        out.flush()

    for r in records:
        kind = r.get("kind")
        if kind == "fleet" and r.get("self"):
            merger.feed(r)
            if not lead_shown:
                out.write(self_line_text(r.get("machine", "?"), r.get("role", "?"), r.get("mode", "?"), colour) + "\n")
                out.flush()
                lead_shown = True
            continue
        if kind == "plan":
            for j in r.get("jobs", []):
                owner[j["job"]] = j.get("machine")
                if j.get("machine"):
                    pending.setdefault(j["machine"], set()).add(j["job"])
                    out.write(paint("  probing %s…" % j["job"], "dim", colour) + "\n")
            out.flush()
            continue
        if kind == "flush":
            job = r.get("job")
            if job not in owner:
                print("wk status: '%s' ended without being in the plan" % job, file=sys.stderr)
                continue
            if owner[job]:
                pending[owner[job]].discard(job)
                if not pending[owner[job]]:
                    draw(owner[job])
            continue
        merger.feed(r)

    for m in merger.doc["machines"]:
        draw(m["name"])
    for line in render_fleet_and_bridges(merger.doc, colour):
        out.write(line + "\n")
    out.write("\n")
    out.flush()
    return merger.doc


PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.html")


def page(doc, live):
    with open(PAGE, encoding="utf-8") as fh:
        html = fh.read()
    return (html.replace("__LIVE__", "true" if live else "false")
            .replace("__SEV__", json.dumps({"good": GOOD, "busy": BUSY, "bad": BAD, "idle": IDLE}))
            .replace("__DOC__", json.dumps(doc) if not live else "null"))


def write_page(doc, out):
    dest = out or os.path.join(os.environ.get("TMPDIR", "/tmp"), "wk-status.html")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(page(doc, live=False))
    return dest


class Live:
    """Re-walked on a timer; `lock` guards the document mid-swap and `busy` the right to walk."""

    def __init__(self, root, interval):
        self.root = root
        self.interval = max(5, int(interval))
        self.lock = threading.Lock()
        self.busy = threading.Lock()
        self.doc = {"machines": [], "fleet": [], "bridges": [], "exit": 0}
        self.stamp = 0
        self.refreshing = False

    def payload(self):
        with self.lock:
            return json.dumps({"doc": self.doc, "stamp": self.stamp, "interval": self.interval,
                               "refreshing": self.refreshing}).encode()

    def refresh_once(self):
        if not self.busy.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.refreshing = True
            doc = None
            try:
                from wk import status   # status imports this module
                doc = merge(status.Walk(self.root, name=store.ws_name() or None, fleet=True, devices=True).records())
            except Exception as exc:
                print("wk status --web: refresh failed: %s" % exc, file=sys.stderr)
            with self.lock:
                if doc is not None:
                    self.doc = doc
                    self.stamp = int(time.time())
                self.refreshing = False
        finally:
            self.busy.release()

    def loop(self):
        while True:
            started = time.time()
            self.refresh_once()
            time.sleep(max(1.0, self.interval - (time.time() - started)))


def serve(root, doc, port, interval):
    live = Live(root, interval)
    with live.lock:
        live.doc = doc
        live.stamp = int(time.time())
    body = page(None, live=True).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/status.json"):
                data, ctype = live.payload(), "application/json"
            elif self.path in ("/", "/index.html"):
                data, ctype = body, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    except OSError as exc:
        print("wk status: cannot serve on 127.0.0.1:%s (%s).\n"
              "    Another 'wk status --web' is probably still running -- its page is\n"
              "    already live. Otherwise pick a port: wk status --web --port 0" % (port, exc.strerror or exc),
              file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d/" % httpd.server_port
    threading.Thread(target=live.loop, daemon=True).start()
    print("wk status: serving %s (refreshing every %ds, ctrl-c to stop)" % (url, live.interval), file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    return 0
