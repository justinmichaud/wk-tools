#!/usr/bin/env python3
"""Render `wk status`.

    status-view.py <text|json|html|web> <records-file> [--port N] [--interval S] [--out FILE]

The input is the record stream `wk status --records` writes: one JSON object per
line, in the order the walk found things. Several processes can write to it --
on a macOS host a listing is assembled by this machine's own targets out here
and its containers inside the podman VM, and two JSON *documents* cannot be
concatenated where two streams of lines can. Merging by machine name here is
also what retired the flag those two halves used to pass between them to agree
on which of them had already printed a heading.

Every view is drawn from the same merged document, which is the point: a number
that appears in the terminal and not in the browser is impossible rather than
merely unlikely, and `--json` is that document rather than a fourth rendering of
the same facts.

No third-party library, deliberately. Aligning columns whose widths are not
known until the last machine has answered is a page of python; needing
`pip install` before `wk status` would run on a build box, a Pi or a podman VM
is not a trade this repository makes.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser

# --- the document -------------------------------------------------------------


def merge(lines):
    """Records -> one document, grouped machine / method / workspace.

    Order is the order the records arrived in, per group, because that is the
    order the walk found them and it is stable (order_targets in cmd/status
    puts this machine first). Machines are *merged* rather than appended: the
    same machine can be reported by two processes, and it is one machine.
    """
    doc = {"machines": [], "fleet": [], "bridges": [], "exit": 0}
    index = {}

    def machine(name):
        if name not in index:
            m = {"name": name, "self": False, "methods": [], "facts": [], "raw": [],
                 "disk": [], "services": [], "locks": [], "switches": [],
                 "capacity": [], "bench": None}
            index[name] = m
            doc["machines"].append(m)
        return index[name]

    def method(m, name):
        for g in m["methods"]:
            if g["name"] == name:
                return g
        g = {"name": name, "workspaces": []}
        m["methods"].append(g)
        return g

    for line in lines:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            # A record that cannot be read is worth saying so about: it means a
            # collector wrote something malformed, and silently dropping it is
            # how a workspace goes missing from a listing that looks complete.
            print("wk status: unreadable record: %s" % line[:120], file=sys.stderr)
            continue
        kind = r.get("kind")
        if kind == "machine":
            m = machine(r["name"])
            m["self"] = m["self"] or bool(r.get("self"))
            # Everything else the record carries -- how it is reached, which
            # conf declared it -- kept from whichever half knew it. Dropping
            # these was invisible: the machine still appeared, with the answer
            # to "how do I get there" silently missing.
            for k in ("tailnet", "direct", "conf"):
                if r.get(k) and not m.get(k):
                    m[k] = r[k]
        elif kind == "workspace":
            method(machine(r.get("machine", "?")), r.get("method", "?"))[
                "workspaces"
            ].append(r)
        elif kind == "fact":
            machine(r.get("machine", "?"))["facts"].append(r)
        elif kind == "raw":
            machine(r.get("machine", "?"))["raw"].append(r)
        elif kind == "disk":
            machine(r.get("machine", "?"))["disk"].append(r)
        elif kind == "service":
            machine(r.get("machine", "?"))["services"].append(r)
        elif kind == "lock":
            machine(r.get("machine", "?"))["locks"].append(r)
        elif kind == "switch":
            machine(r.get("machine", "?"))["switches"].append(r)
        elif kind == "capacity":
            # Kept per reporter, not overwritten: on a macOS host the VM and the
            # Mac each answer, and "9 cores" and "10 cores" are both true about
            # different things.
            m = machine(r.get("machine", "?"))
            if m.get("capacity") is None:
                m["capacity"] = []
            m["capacity"].append(r)
        elif kind == "bench":
            machine(r.get("machine", "?"))["bench"] = r
        elif kind == "fleet":
            doc["fleet"].append(r)
        elif kind == "bridge":
            doc["bridges"].append(r)
        elif kind == "exit":
            # The worst of them, not the last: two halves each report their own,
            # and this command's contract is that a script can branch on it.
            doc["exit"] = max(doc["exit"], int(r.get("code", 0)))
    return doc


def read_doc(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return merge(fh)


# --- shared vocabulary --------------------------------------------------------
#
# One place where a state word becomes a colour, because a state that is red in
# the terminal and grey on the page is two answers to one question.

GOOD = ("ok", "present", "running", "host mode", "up")
BUSY = ("creating", "starting", "building", "fixing", "no", "empty", "held")
BAD = (
    "unhealthy",
    "failed",
    "oom",
    "stalled",
    "broken",
    "unreachable",
    "gave-up",
    "error",
)
IDLE = ("absent", "none", "stopped", "exited", "-", "clean", "finished", "off")


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
    """A build/test/babysit line, in one string, for a table cell."""
    out = "%s=%s" % (sub.get("kind", "?"), sub.get("state", "?"))
    bits = [b for b in (sub.get("config"), sub.get("arch")) if b]
    if sub.get("kind") == "babysit" and sub.get("config"):
        bits = [sub["config"], sub.get("model", ""), "fix %s/%s" % (sub.get("attempt", "?"), sub.get("max", "?"))]
        bits = [b for b in bits if b]
    if bits:
        out += " (%s)" % ", ".join(bits)
    if sub.get("seconds") is not None:
        secs = int(sub["seconds"])
        out += " %dm%02ds" % (secs // 60, secs % 60)
    if sub.get("peak_mb") is not None:
        out += "  peak %sMB" % sub["peak_mb"]
    if sub.get("exit") and sub.get("state") == "failed":
        out += "  exit %s" % sub["exit"]
    return out


# --- the terminal view --------------------------------------------------------

RESET = "\033[0m"
ANSI = {
    "good": "\033[32m",
    "busy": "\033[33m",
    "bad": "\033[31m",
    "idle": "\033[2m",
    "": "",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "head": "\033[1;36m",
    "rule": "\033[2m",
}

# Six columns, and the one that was dropped is `ws`: it repeated the state word
# exactly when it was not `present` (report_ws sets both from ws_state) and said
# "present" the rest of the time. What took its place is what a person actually
# needs before touching a workspace -- whether there is work in it that exists
# nowhere else.
COLUMNS = ("workspace", "state", "branch", "work", "snap", "build")


def ws_work(ws):
    """What is in this workspace that a `wk rm` would take with it."""
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
    # Yellow, not red: unpushed work is not a fault, it is a thing to know
    # before remaking the workspace. Nothing to lose reads dim.
    if ws.get("unpushed") or ws.get("dirty"):
        return "busy"
    return "idle"


def ws_branch(ws):
    b = ws.get("branch") or "-"
    if ws.get("behind"):
        b += " \u2193%s" % ws["behind"]
    if ws.get("ahead"):
        b += " \u2191%s" % ws["ahead"]
    return b


def ws_snap(ws):
    """How far the layer *underneath* has been left behind.

    A workspace is pinned to the snapshot it was made on, and `wk sync` cannot
    change that -- which is the honest answer to "I synced and nothing moved".
    """
    n = ws.get("base_behind")
    return "-%s" % n if n else ""


def ws_cells(ws):
    subs = ws.get("subs") or []
    return [
        ws.get("name", "?"),
        ws.get("state", "?"),
        ws_branch(ws),
        ws_work(ws),
        ws_snap(ws),
        sub_text(subs[0]) if subs else "",
    ]


def ws_hues(ws):
    subs = ws.get("subs") or []
    return {
        1: severity(ws.get("state")),
        3: ws_work_hue(ws),
        4: "busy" if ws.get("base_behind") else "",
        5: severity(subs[0].get("state")) if subs else "",
    }


def gb(mb):
    """Megabytes as something a person reads at a glance."""
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
    # The thresholds are about what fits next, not about tidiness: a WebKit
    # build tree is tens of gigabytes, so 90% of a disk this size is already
    # "the next build may not finish".
    return "bad" if pct >= 90 else "busy" if pct >= 75 else "good"


def paint(text, key, colour):
    if not colour or not ANSI.get(key):
        return text
    return "%s%s%s" % (ANSI[key], text, RESET)


def rule(widths, left, mid, right, fill="─"):
    return left + mid.join(fill * (w + 2) for w in widths) + right


def render_text(doc, colour):
    """The listing as one table per method group, all to the same widths.

    One set of widths for the whole listing, and that is the point: per-group
    widths made every block line up with itself and with nothing else, so the
    eye had to find the columns again at every heading.
    """
    out = []

    w = [len(h) for h in COLUMNS]
    for m in doc["machines"]:
        for g in m["methods"]:
            for ws in g["workspaces"]:
                for i, c in enumerate(ws_cells(ws)):
                    w[i] = max(w[i], len(c))
    # The inside of a row: every column, two spaces of padding each, and the
    # separators between them.
    inner = sum(w) + 3 * len(w) - 1

    def row(cells, hues=None):
        hues = hues or {}
        parts = [paint(c.ljust(w[i]), hues.get(i, ""), colour) for i, c in enumerate(cells)]
        return "  │ " + " │ ".join(parts) + " │"

    def span(text, hue=""):
        """A line that crosses the whole table -- a note, an error, a hint."""
        return "  │ " + paint(text.ljust(inner - 2), hue, colour) + " │"

    def heading(name, tag=""):
        # Ruled to the table's own width, so a section reads as the width of
        # what is under it rather than as an arbitrary line.
        out.append("")
        out.append(paint(name, "head", colour) + ("   " + paint(tag, "dim", colour) if tag else ""))
        out.append(paint("═" * (inner + 4), "rule", colour))

    def meta(obj, indent="  "):
        """How a machine is reached, and which file said it exists.

        Both are calculated (lib/reach.sh) rather than read from anywhere, and
        both are printed dim: they are the answer to a question asked when
        something is wrong, and they must not compete with the states above.
        """
        if obj.get("tailnet"):
            out.append(indent + paint("reached    ", "dim", colour) + obj["tailnet"])
        if obj.get("direct"):
            out.append(
                indent + paint("or without tailscale  ", "dim", colour) + obj["direct"]
            )
        if obj.get("conf"):
            out.append(indent + paint("from       ", "dim", colour) + paint(obj["conf"], "dim", colour))

    for m in doc["machines"]:
        heading(m["name"], "this machine" if m.get("self") else "")
        meta(m)

        if not any(g["workspaces"] for g in m["methods"]) and not m["raw"]:
            out.append("  " + paint("(no workspaces on it)", "idle", colour))

        for g in m["methods"]:
            if not g["workspaces"]:
                continue
            out.append("")
            out.append("  " + paint(g["name"], "bold", colour))
            out.append("  " + paint(rule(w, "┌", "┬", "┐"), "rule", colour))
            out.append(row([h.upper() for h in COLUMNS]))
            out.append("  " + paint(rule(w, "├", "┼", "┤"), "rule", colour))
            for ws in g["workspaces"]:
                subs = ws.get("subs") or []
                out.append(row(ws_cells(ws), ws_hues(ws)))
                # Everything else this workspace has been asked to do, under its
                # own row and in its own column.
                for sub in subs[1:]:
                    out.append(
                        row(
                            ["", "", "", "", "", sub_text(sub)],
                            {5: severity(sub.get("state"))},
                        )
                    )
                for n in ws.get("notes", []):
                    hue = "busy" if n["level"] == "warn" else "dim"
                    mark = "! " if n["level"] == "warn" else "  "
                    for i, text in enumerate(n["text"].split("\n")):
                        out.append(span((mark if i == 0 else "  ") + text.strip(), hue))
            out.append("  " + paint(rule(w, "└", "┴", "┘"), "rule", colour))

        # What the machine is, apart from the workspaces on it. Every line here
        # is a thing that breaks a build or costs work, and the colour is the
        # point: this block is read at a glance or not at all.
        # One label column for the whole block, wide enough for the widest label
        # there is, so the values line up with each other and with nothing else.
        def hrow(label, value, hue=""):
            # Padded to the column, and never *into* the value: a label longer
            # than the column ("push (podman VM)") ran straight into its own
            # answer.
            lab = label.ljust(14) if len(label) < 14 else label + "  "
            out.append("  " + paint(lab, "dim", colour) + paint(value, hue, colour))

        def where_label(name, obj):
            # "in the podman VM" reads as a phrase in the middle of a sentence
            # (the disk line uses it that way) and as noise in a column heading.
            w = (obj.get("where") or "").replace("in the ", "").replace("the ", "")
            return "%s (%s)" % (name, w) if w else name

        for d in m.get("disk") or []:
            tail = ""
            if d.get("snapshots"):
                tail = "  ·  %s snapshot%s" % (d["snapshots"], "" if d["snapshots"] == "1" else "s")
                if d.get("reclaimable") and d["reclaimable"] != "0":
                    tail += paint(", %s reclaimable (wk gc)" % d["reclaimable"], "busy", colour)
            where = " (%s)" % d["where"] if d.get("where") else ""
            hrow(
                "disk",
                "%s used   %s free of %s%s"
                % (
                    paint(d.get("used_pct", "?") + "%", disk_hue(d.get("used_pct")), colour),
                    gb(d.get("free_mb")),
                    gb(d.get("total_mb")),
                    paint(where, "dim", colour),
                )
                + tail,
            )
        for sv in m.get("services") or []:
            hrow(sv.get("name", "?"), sv.get("state", "?"), severity(sv.get("state")))
            if sv.get("fix"):
                hrow("", sv["fix"], "busy")
        for sw in m.get("switches") or []:
            hrow(
                where_label(sw.get("name", "?"), sw),
                paint(sw.get("state", "?"), "good" if sw.get("state") == "on" else "busy", colour)
                + paint("   " + sw.get("detail", ""), "dim", colour),
            )
        for cap in m.get("capacity") or []:
            if not cap.get("cores"):
                continue
            load, cores = cap.get("load"), cap.get("cores")
            hue = ""
            try:
                hue = "bad" if float(load) > int(cores) else "busy" if float(load) > int(cores) / 2 else "good"
            except (TypeError, ValueError):
                pass
            hrow(
                where_label("load", cap),
                "%s of %s cores   %s free of %s"
                % (paint(load or "?", hue, colour), cores, gb(cap.get("free_mb")), gb(cap.get("mem_mb"))),
            )
        for lk in m.get("locks") or []:
            # A lock whose holder is gone is not a lock: the next taker breaks
            # it. Saying "alive" or "stale" is the difference between waiting and
            # not.
            state = "held" if lk.get("alive") else "stale"
            hrow(
                "lock",
                "%s  %s  %s"
                % (
                    lk.get("resource", "?"),
                    paint(state, "busy" if lk.get("alive") else "bad", colour),
                    paint("pid %s  %s" % (lk.get("pid", "?"), lk.get("cmd", "")), "dim", colour),
                ),
            )
        b = m.get("bench")
        if b:
            hrow(
                "bench",
                "%s  %s"
                % (b.get("run", "?"), paint(b.get("state", "?"), severity(b.get("state")), colour)),
            )

        # A machine that could not answer in records: its own listing, as it
        # sent it, rather than nothing.
        for r in m["raw"]:
            out.append("")
            for ln in r.get("text", "").split("\n"):
                out.append("  " + paint(ln, "dim", colour))
            for n in r.get("notes", []):
                for ln in n["text"].split("\n"):
                    out.append("  " + paint(ln.strip(), "busy", colour))

        if m["facts"]:
            out.append("")
        for f in m["facts"]:
            if f.get("type") == "wk-tools":
                what = "wk-tools" + (" (%s)" % f["copy"] if f.get("copy") else "")
                ver = (f.get("sha") or "-") + ("+dirty" if f.get("dirty") else "")
                tail = (
                    paint("in sync", "good", colour)
                    if f.get("insync")
                    else paint(
                        "DIFFERS from the workstation (%s)" % f.get("expect", "?"),
                        "bad",
                        colour,
                    )
                )
                out.append("  %-30s %-14s %s  %s" % (what, ver, f.get("tree", "?"), tail))
                # The command, not "push it there": which one it is depends on
                # what kind of copy this is, and none of the three is guessable.
                if f.get("fix"):
                    out.append("  %-30s %s" % ("", paint(f["fix"], "busy", colour)))
            elif f.get("type") == "key":
                out.append("  %-30s %s" % ("push key", f.get("text", "")))

    if doc["fleet"]:
        heading("fleet", "role, mode, and the media wk owns (wk help hardware)")
        fw = [
            max([len(f.get("machine", "")) for f in doc["fleet"]] + [7]),
            max([len(f.get("role", "")) for f in doc["fleet"]] + [4]),
            max([len(f.get("mode", "")) for f in doc["fleet"]] + [4]),
        ]
        for f in doc["fleet"]:
            out.append(
                "  %s  %s  %s  %s"
                % (
                    f.get("machine", "").ljust(fw[0]),
                    paint(f.get("role", "").ljust(fw[1]), "dim", colour),
                    paint(f.get("mode", "").ljust(fw[2]), severity(f.get("mode")), colour),
                    f.get("media", ""),
                )
            )
            if f.get("armed"):
                out.append(
                    "  %s  %s"
                    % (
                        " " * fw[0],
                        paint(
                            "** armed for %s -- wk boot %s --status **"
                            % (f["armed"], f.get("machine", "")),
                            "busy",
                            colour,
                        ),
                    )
                )
            meta(f, "  " + " " * fw[0] + "  ")

    if doc["bridges"]:
        heading("tailnet bridges", "probed: the segment, the role, and its own health check")
        bw = max(len(b.get("name", "")) for b in doc["bridges"])
        for b in doc["bridges"]:
            out.append(
                "  %s  %-10s %-14s %s"
                % (
                    b.get("name", "").ljust(bw),
                    b.get("device", "?"),
                    b.get("segment", "?"),
                    paint(b.get("state", "?"), severity(b.get("state")), colour),
                )
            )
            pad = "  " + " " * bw + "  "
            if b.get("health"):
                out.append(pad + paint("health     ", "dim", colour) + b["health"])
            if "role_insync" in b:
                out.append(
                    pad
                    + paint("role       ", "dim", colour)
                    + (
                        paint("this repository's", "good", colour)
                        if b["role_insync"]
                        else paint(
                            "older than this repository -- wk bridge setup %s" % b.get("name", ""),
                            "bad",
                            colour,
                        )
                    )
                )
            meta(b, pad)
            if b.get("note"):
                out.append(pad + paint(b["note"], "dim", colour))

    out.append("")
    return "\n".join(out)


# --- the page -----------------------------------------------------------------
#
# Self-contained: no CDN, no font host, no framework. A page served from a
# laptop to look at a fleet must not depend on the network the fleet is the
# reason you are worried about.

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>wk status</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fbfbfa; --fg: #1c1c1a; --dim: #6b6b66; --line: #dcdcd6;
    --card: #ffffff; --good: #1a7f37; --busy: #9a6700; --bad: #c02a2a; --idle: #86867e;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#16171a; --fg:#e6e6e3; --dim:#9a9a94; --line:#2c2e33;
            --card:#1c1e22; --good:#4ac26b; --busy:#d4a72c; --bad:#ff6b6b; --idle:#75757040; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:2rem 2.5rem 4rem; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  h1 { font-size:1.1rem; margin:0 0 .25rem; letter-spacing:.02em; }
  .sub { color:var(--dim); margin-bottom:2rem; }
  .machine { margin:0 0 2.5rem; }
  .machine > h2 { font-size:1rem; margin:0 0 .75rem; padding-bottom:.4rem;
                  border-bottom:2px solid var(--line); }
  .machine > h2 .tag { color:var(--dim); font-weight:400; margin-left:.75rem; font-size:.85em; }
  .method { margin:0 0 1.5rem; }
  .method > h3 { font-size:.8rem; text-transform:uppercase; letter-spacing:.08em;
                 color:var(--dim); margin:0 0 .5rem; font-weight:600; }
  table { border-collapse:collapse; width:100%; background:var(--card);
          border:1px solid var(--line); border-radius:6px; overflow:hidden; }
  th { text-align:left; font-size:.72rem; text-transform:uppercase; letter-spacing:.06em;
       color:var(--dim); font-weight:600; padding:.5rem .75rem; border-bottom:1px solid var(--line); }
  td { padding:.4rem .75rem; border-top:1px solid var(--line); vertical-align:top;
       white-space:nowrap; }
  tr.sub td, tr.note td { border-top:none; }
  td.wide { white-space:normal; }
  .good { color:var(--good); } .busy { color:var(--busy); }
  .bad  { color:var(--bad); }  .idle { color:var(--idle); }
  .note { color:var(--dim); white-space:pre-wrap; font-size:.92em; }
  .note.warn { color:var(--busy); }
  .meta { color:var(--dim); font-size:.88em; margin:.25rem 0 .75rem; }
  .meta .k { display:inline-block; min-width:11rem; opacity:.75; }
  .health { font-size:.95em; }
  .health .k { min-width:9rem; }
  code.fix { background:var(--bg); border:1px solid var(--line); border-radius:4px;
             padding:0 .35rem; color:var(--busy); }
  .facts { margin:.5rem 0 0; color:var(--dim); font-size:.92em; }
  .facts .bad { color:var(--bad); }
  pre.raw { background:var(--card); border:1px solid var(--line); border-radius:6px;
            padding:.75rem; overflow-x:auto; margin:.5rem 0 0; }
  .fleet td:first-child, .bridges td:first-child { font-weight:600; }
  footer { position:fixed; right:1rem; bottom:.75rem; color:var(--dim); font-size:.8rem;
           background:var(--bg); padding:.25rem .5rem; border-radius:4px; }
  footer .spin { color:var(--busy); }
  /* The server has gone away -- the page is still showing the last listing it
     had, and every word of it may be hours old. Grey and a red frame, because
     the danger here is reading a stale fleet as a current one. */
  body.gone { filter:grayscale(1) opacity(.55); }
  body.gone::after { content:""; position:fixed; inset:0; pointer-events:none;
                     border:6px solid var(--bad); }
  body.gone footer { filter:none; color:var(--bad); font-weight:600; }
</style>
<body>
<div id="app"></div>
<footer id="foot">loading…</footer>
<script>
const ESC = s => String(s == null ? "" : s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const GOOD = ["ok","present","running","host mode"], BUSY = ["creating","starting","building","fixing"],
      BAD = ["failed","oom","stalled","broken","unreachable","gave-up","error"],
      IDLE = ["absent","none","stopped","exited","-"];
function sev(w) {
  w = String(w || "").split(" ")[0].toLowerCase();
  if (GOOD.includes(w)) return "good";
  if (BUSY.includes(w)) return "busy";
  if (BAD.includes(w)) return "bad";
  if (IDLE.includes(w)) return "idle";
  return "";
}
function subText(s) {
  let out = `${s.kind}=${s.state}`;
  let bits = [s.config, s.arch].filter(Boolean);
  if (s.kind === "babysit" && s.config) bits = [s.config, s.model, `fix ${s.attempt}/${s.max}`].filter(Boolean);
  if (bits.length) out += ` (${bits.join(", ")})`;
  if (s.seconds != null) out += ` ${Math.floor(s.seconds/60)}m${String(s.seconds%60).padStart(2,"0")}s`;
  if (s.peak_mb != null) out += `  peak ${s.peak_mb}MB`;
  if (s.exit && s.state === "failed") out += `  exit ${s.exit}`;
  return out;
}
// How a machine is reached and where it was declared -- calculated, never
// stored (lib/reach.sh), and shown dim: it is what somebody wants when
// something above it is wrong.
function meta(o) {
  const rows = [];
  if (o.tailnet) rows.push(`<div><span class="k">reached</span> ${ESC(o.tailnet)}</div>`);
  if (o.direct)  rows.push(`<div><span class="k">or without tailscale</span> ${ESC(o.direct)}</div>`);
  if (o.conf)    rows.push(`<div><span class="k">from</span> ${ESC(o.conf)}</div>`);
  return rows.length ? `<div class="meta">${rows.join("")}</div>` : "";
}
function GB(mb) {
  const n = parseInt(mb, 10);
  if (isNaN(n)) return "?";
  return n >= 1024 ? Math.round(n/1024) + "G" : n + "M";
}
function diskHue(p) {
  const n = parseInt(p, 10);
  if (isNaN(n)) return "";
  return n >= 90 ? "bad" : n >= 75 ? "busy" : "good";
}
// What the machine is, apart from the workspaces on it: the things that break a
// build or cost work. Same facts and same colours as the terminal, from the same
// document.
function health(m) {
  const r = [];
  for (const d of m.disk || []) {
    let tail = "";
    if (d.snapshots) {
      tail = ` · ${ESC(d.snapshots)} snapshot${d.snapshots === "1" ? "" : "s"}`;
      if (d.reclaimable && d.reclaimable !== "0")
        tail += `<span class="busy">, ${ESC(d.reclaimable)} reclaimable (wk gc)</span>`;
    }
    r.push(`<div><span class="k">disk${d.where ? " (" + ESC(d.where) + ")" : ""}</span>
      <span class="${diskHue(d.used_pct)}">${ESC(d.used_pct)}% used</span>
      ${GB(d.free_mb)} free of ${GB(d.total_mb)}${tail}</div>`);
  }
  for (const sv of m.services || [])
    r.push(`<div><span class="k">${ESC(sv.name)}</span><span class="${sev(sv.state)}">${ESC(sv.state)}</span>`
      + (sv.fix ? ` <code class="fix">${ESC(sv.fix)}</code>` : "") + `</div>`);
  for (const sw of m.switches || [])
    r.push(`<div><span class="k">${ESC(sw.name)}${sw.where ? " (" + ESC(sw.where) + ")" : ""}</span>
      <span class="${sw.state === "on" ? "good" : "busy"}">${ESC(sw.state)}</span> ${ESC(sw.detail || "")}</div>`);
  for (const c of m.capacity || []) {
    if (!c.cores) continue;
    const load = parseFloat(c.load), cores = parseInt(c.cores, 10);
    const hue = isNaN(load) ? "" : load > cores ? "bad" : load > cores/2 ? "busy" : "good";
    r.push(`<div><span class="k">load${c.where ? " (" + ESC(c.where) + ")" : ""}</span>
      <span class="${hue}">${ESC(c.load || "?")}</span> of ${ESC(c.cores)} cores ·
      ${GB(c.free_mb)} free of ${GB(c.mem_mb)}</div>`);
  }
  for (const lk of m.locks || [])
    r.push(`<div><span class="k">lock</span>${ESC(lk.resource)}
      <span class="${lk.alive ? "busy" : "bad"}">${lk.alive ? "held" : "stale"}</span>
      pid ${ESC(lk.pid || "?")} ${ESC(lk.cmd || "")}</div>`);
  if (m.bench)
    r.push(`<div><span class="k">bench</span>${ESC(m.bench.run)}
      <span class="${sev(m.bench.state)}">${ESC(m.bench.state)}</span></div>`);
  return r.length ? `<div class="meta health">${r.join("")}</div>` : "";
}
function render(doc) {
  const parts = [];
  for (const m of doc.machines) {
    parts.push(`<section class="machine"><h2>${ESC(m.name)}${m.self ? '<span class="tag">this machine</span>' : ""}</h2>${meta(m)}`);
    for (const g of m.methods) {
      if (!g.workspaces.length) continue;
      parts.push(`<div class="method"><h3>${ESC(g.name)}</h3><table>
        <tr><th>workspace</th><th>state</th><th>branch</th><th>work</th><th>snap</th><th>build</th></tr>`);
      for (const w of g.workspaces) {
        const subs = w.subs || [];
        const work = [];
        if (w.unpushed) work.push(`${w.unpushed} unpushed`);
        if (w.dirty) work.push(`${w.dirty} dirty`);
        if (w.untracked) work.push(`+${w.untracked} new`);
        const workTxt = work.length ? work.join(", ") : (w.ws === "present" ? "clean" : "");
        let branch = w.branch || "-";
        if (w.behind) branch += ` ↓${w.behind}`;
        if (w.ahead) branch += ` ↑${w.ahead}`;
        parts.push(`<tr>
          <td>${ESC(w.name)}</td>
          <td class="${sev(w.state)}">${ESC(w.state)}</td>
          <td>${ESC(branch)}</td>
          <td class="${work.length ? "busy" : "idle"}">${ESC(workTxt)}</td>
          <td class="${w.base_behind ? "busy" : "idle"}" title="${ESC(w.base || "")}">${w.base_behind ? "-" + ESC(w.base_behind) : ""}</td>
          <td class="${subs[0] ? sev(subs[0].state) : ""}">${subs[0] ? ESC(subText(subs[0])) : ""}</td></tr>`);
        for (const s of subs.slice(1))
          parts.push(`<tr class="sub"><td colspan="5"></td><td class="${sev(s.state)}">${ESC(subText(s))}</td></tr>`);
        for (const n of (w.notes || []))
          parts.push(`<tr class="note"><td colspan="6" class="wide"><div class="note ${n.level === "warn" ? "warn" : ""}">${ESC(n.text)}</div></td></tr>`);
      }
      parts.push(`</table></div>`);
    }
    parts.push(health(m));
    const facts = (m.facts || []).map(f => {
      if (f.type === "wk-tools") {
        const what = "wk-tools" + (f.copy ? ` (${f.copy})` : "");
        const ver = (f.sha || "-") + (f.dirty ? "+dirty" : "");
        return `<div>${ESC(what)} ${ESC(ver)} ${ESC(f.tree)} ` + (f.insync
          ? `<span class="good">in sync</span>`
          : `<span class="bad">DIFFERS from the workstation (${ESC(f.expect)})</span>`
            + (f.fix ? ` <code class="fix">${ESC(f.fix)}</code>` : "")) + `</div>`;
      }
      return `<div>push key ${ESC(f.text)}</div>`;
    });
    if (facts.length) parts.push(`<div class="facts">${facts.join("")}</div>`);
    for (const r of (m.raw || []))
      parts.push(`<pre class="raw">${ESC(r.text)}</pre>` +
        (r.notes || []).map(n => `<div class="note warn">${ESC(n.text)}</div>`).join(""));
    parts.push(`</section>`);
  }
  if (doc.fleet && doc.fleet.length) {
    parts.push(`<section class="machine"><h2>fleet<span class="tag">role, mode, and the media wk owns</span></h2>
      <table class="fleet"><tr><th>machine</th><th>role</th><th>mode</th><th>media</th></tr>`);
    for (const f of doc.fleet) {
      parts.push(`<tr><td>${ESC(f.machine)}</td><td class="idle">${ESC(f.role)}</td>
        <td class="${sev(f.mode)}">${ESC(f.mode)}</td><td class="wide">${ESC(f.media || "")}</td></tr>`);
      if (f.armed) parts.push(`<tr class="note"><td colspan="4" class="wide"><div class="note warn">armed for ${ESC(f.armed)} — wk boot ${ESC(f.machine)} --status</div></td></tr>`);
      const fm = meta(f);
      if (fm) parts.push(`<tr class="note"><td colspan="4" class="wide">${fm}</td></tr>`);
    }
    parts.push(`</table></section>`);
  }
  if (doc.bridges && doc.bridges.length) {
    parts.push(`<section class="machine"><h2>tailnet bridges<span class="tag">probed: the segment, the role, and its own health check</span></h2>
      <table class="bridges"><tr><th>bridge</th><th>device</th><th>segment</th><th>state</th><th>role</th></tr>`);
    for (const b of doc.bridges) {
      const role = b.role_insync === undefined ? ""
        : b.role_insync ? `<span class="good">this repository's</span>`
        : `<span class="bad">older — wk bridge setup ${ESC(b.name)}</span>`;
      parts.push(`<tr><td>${ESC(b.name)}</td><td>${ESC(b.device)}</td><td>${ESC(b.segment)}</td>
        <td class="${sev(b.state)}">${ESC(b.state || "?")}</td><td class="wide">${role}</td></tr>`);
      const extra = [];
      if (b.health) extra.push(`<div><span class="k">health</span> ${ESC(b.health)}</div>`);
      if (b.note) extra.push(`<div class="note">${ESC(b.note)}</div>`);
      const bm = meta(b);
      if (extra.length || bm) parts.push(`<tr class="note"><td colspan="5" class="wide"><div class="meta">${extra.join("")}</div>${bm}</td></tr>`);
      for (const n of (b.notes || [])) parts.push(`<tr class="note"><td colspan="5" class="wide"><div class="note warn">${ESC(n.text)}</div></td></tr>`);
    }
    parts.push(`</table></section>`);
  }
  document.getElementById("app").innerHTML = parts.join("");
}
const LIVE = __LIVE__;
if (!LIVE) { render(__DOC__); document.getElementById("foot").textContent = "static page — wk status --web for a live one"; }
else {
  let last = 0, busy = false;
  async function poll() {
    try {
      const r = await fetch("status.json", { cache: "no-store" });
      const p = await r.json();
      if (p.stamp !== last) { last = p.stamp; render(p.doc); }
      busy = p.refreshing;
      document.body.classList.remove("gone");
      const age = Math.round((Date.now()/1000) - p.stamp);
      document.getElementById("foot").innerHTML = busy
        ? '<span class="spin">refreshing…</span>'
        : `updated ${age}s ago · every ${p.interval}s`;
    } catch (e) {
      // Not a footer note: a page that stopped updating looks exactly like one
      // that is up to date, and this listing is the thing people act on.
      document.body.classList.add("gone");
      document.getElementById("foot").textContent = "wk status --web has stopped — this listing is frozen";
    }
  }
  poll(); setInterval(poll, 1500);
}
</script>
"""


def page(doc, live):
    return PAGE.replace("__LIVE__", "true" if live else "false").replace(
        "__DOC__", json.dumps(doc) if not live else "null"
    )


# --- the server ---------------------------------------------------------------


class Live:
    """The document, kept current by re-running `wk status` in the background.

    Refreshed on a timer rather than on every request, because a bare
    `wk status` walks the fleet over ssh -- a page that re-ran it per poll would
    put a browser's refresh rate onto a phone-linked Pi. The page polls this
    process, which is cheap; this process asks the fleet, which is not.
    """

    def __init__(self, interval):
        self.interval = max(5, int(interval))
        self.lock = threading.Lock()
        self.doc = {"machines": [], "fleet": [], "bridges": [], "exit": 0}
        self.stamp = 0
        self.refreshing = False

    def payload(self):
        with self.lock:
            return json.dumps(
                {
                    "doc": self.doc,
                    "stamp": self.stamp,
                    "interval": self.interval,
                    "refreshing": self.refreshing,
                }
            ).encode()

    def refresh_once(self):
        with self.lock:
            self.refreshing = True
        wk = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wk")
        try:
            out = subprocess.run(
                [wk, "status", "--json"],
                capture_output=True,
                text=True,
                timeout=300,
            ).stdout
            doc = json.loads(out) if out.strip().startswith("{") else None
        except Exception as exc:  # a refresh that fails must not stop the loop
            print("wk status --web: refresh failed: %s" % exc, file=sys.stderr)
            doc = None
        with self.lock:
            if doc is not None:
                self.doc = doc
                self.stamp = int(time.time())
            self.refreshing = False

    def loop(self):
        while True:
            self.refresh_once()
            time.sleep(self.interval)


def serve(doc, port, interval):
    live = Live(interval)
    with live.lock:
        live.doc = doc
        live.stamp = int(time.time())
    body = page(None, live=True).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 -- http.server's spelling
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
            pass  # the terminal that started this is not a web server log

    # 127.0.0.1, never 0.0.0.0: this page says what every machine in the fleet is
    # doing and which keys it holds, and it is nobody else's business.
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    except OSError as exc:
        # A refusal that names the remedy, rather than a traceback: the usual
        # cause is the last `wk status --web` still running in another terminal,
        # and the page it is already serving is the one being asked for.
        print(
            "wk status: cannot serve on 127.0.0.1:%s (%s).\n"
            "    Another 'wk status --web' is probably still running -- its page is\n"
            "    already live. Otherwise pick a port: wk status --web --port 0"
            % (port, exc.strerror or exc),
            file=sys.stderr,
        )
        return 1
    url = "http://127.0.0.1:%d/" % httpd.server_port
    threading.Thread(target=live.loop, daemon=True).start()
    print("wk status: serving %s (refreshing every %ds, ctrl-c to stop)" % (url, live.interval),
          file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass  # a browser that will not open is not a reason to stop serving
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    return 0


# --- entry --------------------------------------------------------------------


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    mode, recs = argv[1], argv[2]
    port, interval, out = 0, 20, ""
    rest = argv[3:]
    while rest:
        flag = rest.pop(0)
        value = rest.pop(0) if rest else ""
        if flag == "--port":
            port = value or 0
        elif flag == "--interval":
            interval = value or 20
        elif flag == "--out":
            out = value

    doc = read_doc(recs)

    if mode == "json":
        json.dump(doc, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    elif mode == "html":
        target = out or os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "wk-status.html"
        )
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(page(doc, live=False))
        print(target)
    elif mode == "web":
        return serve(doc, port or 0, interval)
    else:
        colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        sys.stdout.write(render_text(doc, colour) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
