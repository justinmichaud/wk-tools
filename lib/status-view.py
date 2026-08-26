#!/usr/bin/env python3
"""Render `wk status`.

    status-view.py <text|json|html|web> <records-file> [--port N] [--interval S] [--out FILE]

The input is the record stream `wk status --records` writes: one JSON object per
line, in the order the walk found things. Several processes can write to it --
on a macOS host a listing is assembled by this machine's own targets out here
and its containers inside the podman VM, and two JSON *documents* cannot be
concatenated where two streams of lines can. Merging by machine name here is
also what saves the two halves from having to tell each other which of them has
already printed a heading.

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

# Matched on the *first word*, because most of these states are phrases: "bench
# mode", "base image -- not a bench system", "role installed", "no answer within
# 20s". The page is handed these four lists rather than carrying a second copy
# (page(), below) -- when it did carry one the two had already drifted, and `up`,
# `clean`, `finished` and `held` were coloured in the terminal and plain on the
# page.
GOOD = ("ok", "present", "running", "host mode", "up", "bench", "open")
BUSY = ("creating", "starting", "building", "fixing", "no", "empty", "held",
        # A board that fell back to its base image is not a bench system, and
        # reading it as one is how an unarmed board came to be benchmarked; a
        # bridge with the role installed but no way to run its health check
        # from here is likewise an answer that is not yet "good".
        "base", "role")
BAD = (
    "unhealthy",
    "failed",
    "oom",
    "stalled",
    "broken",
    "unreachable",
    "gave-up",
    "error", "closed",
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


def ws_branch(ws, cap=40):
    """The branch, and how far it has drifted from its upstream.

    Capped for the table, because a column is as wide as the widest thing in it
    and a WebKit bug branch is a sentence: one 78-character name pushed BUILD --
    the column somebody is actually reading -- off the side of the terminal.
    The page shows the whole thing; this is the view where width is scarce.
    """
    b = ws.get("branch") or "-"
    if len(b) > cap:
        b = b[: cap - 1] + "\u2026"
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


def load_hue(load, cores):
    """Load against cores, which is the only reading of a load average that
    means anything: 8 is idle on a 64-core build box and desperate on a Pi."""
    try:
        load, cores = float(load), int(cores)
    except (TypeError, ValueError):
        return ""
    if not cores:
        return ""
    return "bad" if load > cores else "busy" if load > cores / 2.0 else "good"


def where_word(obj):
    """Which of a machine's two of something this is, as a column-heading word.

    "in the podman VM" reads as a phrase in the middle of a sentence and as
    noise next to a label; the record carries the sentence form because the
    collector is the one that knows.
    """
    return (obj.get("where") or "").replace("in the ", "").replace("the ", "")


def paint(text, key, colour):
    if not colour or not ANSI.get(key):
        return text
    return "%s%s%s" % (ANSI[key], text, RESET)


def render_text(doc, colour):
    """The listing as plain aligned columns.

    Deliberately the plainest thing that carries the facts. The page is what
    this fleet is looked at with now -- `wk status` at a terminal opens it
    (status_default_mode, lib/common.sh) -- and this is what a pipe, a redirect,
    `wk selftest` and an agent get. That reader wants the columns to line up and
    wants nothing else. A box-drawn grid -- rules, corner glyphs, blank-celled
    continuation rows -- is a second showpiece to keep correct for a reader who
    already has a better one.

    The one piece of machinery kept is a single set of column widths for the
    whole listing. Per-group widths made every block line up with itself and
    with nothing else, so the eye had to find the columns again at every heading
    -- and it is four lines, not machinery.
    """
    out = []

    w = [len(h) for h in COLUMNS]
    for m in doc["machines"]:
        for g in m["methods"]:
            for ws in g["workspaces"]:
                for i, c in enumerate(ws_cells(ws)):
                    w[i] = max(w[i], len(c))

    def row(cells, hues=None):
        # Every column padded but the last, so a row does not end in a field of
        # spaces -- with colour on, the reset sequence sits after them and no
        # amount of stripping afterwards can find them.
        hues = hues or {}
        last = len(cells) - 1
        return "    " + "  ".join(
            paint(c if i == last else c.ljust(w[i]), hues.get(i, ""), colour)
            for i, c in enumerate(cells)
        )

    def heading(name, tag=""):
        out.append("")
        out.append(paint(name, "head", colour) + (paint("   " + tag, "dim", colour) if tag else ""))

    # A label/value line is buffered rather than formatted, because its column
    # width is not known until every label in the section has been seen.
    #
    # Measured rather than a constant. At a fixed 14 a machine's block reads as
    # three ragged tables: every label wider than that -- "without
    # tailscale", "push (podman VM)", "wk-tools (in the podman VM)" -- pushed
    # its own value out to a column of its own, and the wk-tools and push-key
    # rows were formatted by a *separate* pair of hardcoded widths (30 and 14)
    # that lined up with nothing above them. One section, one column.
    class Kv(tuple):
        __slots__ = ()

    def kv(label, value, indent="  "):
        out.append(Kv((indent, label, value)))

    def align(start):
        """Give every buffered line in out[start:] one label column.

        Called at the end of a section. The width is the widest label actually
        present and never less than 14, so a section of short labels does not
        pull its values left of where every other section puts them.
        """
        labels = [k[1] for k in out[start:] if isinstance(k, Kv)]
        if not labels:
            return
        w = max([len(l) for l in labels] + [14])
        for i in range(start, len(out)):
            if not isinstance(out[i], Kv):
                continue
            indent, label, value = out[i]
            # An empty label is a continuation of the line above -- a fix, a
            # remedy -- so it is spaces, not a painted blank.
            lab = label.ljust(w) if label else " " * w
            out[i] = indent + (paint(lab, "dim", colour) if label else lab) + " " + value

    def meta(obj, indent="  "):
        """How a machine is reached, and which file said it exists. Both are
        calculated at read time (lib/reach.sh) and both are dim: they answer a
        question asked when something above them is wrong."""
        if obj.get("tailnet"):
            kv("reached", obj["tailnet"], indent)
        if obj.get("direct"):
            kv("without tailscale", obj["direct"], indent)
        if obj.get("conf"):
            kv("from", paint(obj["conf"], "dim", colour), indent)

    def notes(items, indent="      "):
        for n in items or []:
            hue = "busy" if n.get("level") == "warn" else "dim"
            for i, text in enumerate(n.get("text", "").split("\n")):
                out.append(indent + paint(("! " if i == 0 and hue == "busy" else "  ")
                                          + text.strip(), hue, colour))

    for m in doc["machines"]:
        heading(m["name"], "this machine" if m.get("self") else "")
        _machine_start = len(out)
        meta(m)

        if not any(g["workspaces"] for g in m["methods"]) and not m["raw"]:
            out.append("  " + paint("(no workspaces on it)", "idle", colour))

        for g in m["methods"]:
            if not g["workspaces"]:
                continue
            out.append("")
            out.append("  " + paint(g["name"], "bold", colour))
            out.append(row([h.upper() for h in COLUMNS], {i: "dim" for i in range(len(COLUMNS))}))
            for ws in g["workspaces"]:
                subs = ws.get("subs") or []
                out.append(row(ws_cells(ws), ws_hues(ws)))
                # Everything else this workspace was asked to do, one per line
                # under its row rather than as a row with the identity blanked.
                for sub in subs[1:]:
                    out.append("      " + paint(sub_text(sub), severity(sub.get("state")), colour))
                notes(ws.get("notes"))

        # What the machine is, apart from the workspaces on it. Every line is a
        # thing that breaks a build or costs work.
        if any(m.get(k) for k in ("disk", "services", "switches", "capacity", "locks")) or m.get("bench"):
            out.append("")
        for d in m.get("disk") or []:
            tail = ""
            if d.get("snapshots"):
                tail = "  ·  %s snapshot%s" % (d["snapshots"], "" if d["snapshots"] == "1" else "s")
                if d.get("reclaimable") and d["reclaimable"] != "0":
                    tail += paint(", %s reclaimable (wk gc)" % d["reclaimable"], "busy", colour)
            kv("disk" + (" (%s)" % where_word(d) if where_word(d) else ""),
               "%s used   %s free of %s%s"
               % (paint((d.get("used_pct", "?") or "?") + "%", disk_hue(d.get("used_pct")), colour),
                  gb(d.get("free_mb")), gb(d.get("total_mb")), tail))
        for sv in m.get("services") or []:
            kv(sv.get("name", "?"), paint(sv.get("state", "?"), severity(sv.get("state")), colour))
            if sv.get("fix"):
                kv("", paint(sv["fix"], "busy", colour))
        for sw in m.get("switches") or []:
            kv(sw.get("name", "?") + (" (%s)" % where_word(sw) if where_word(sw) else ""),
               paint(sw.get("state", "?"), "good" if sw.get("state") == "on" else "busy", colour)
               + paint("   " + sw.get("detail", ""), "dim", colour))
        for cap in m.get("capacity") or []:
            label = "load" + (" (%s)" % where_word(cap) if where_word(cap) else "")
            if not cap.get("cores"):
                # A probe that did not answer says so here rather than the
                # line vanishing, which reads as "this machine has no load" --
                # the one thing that is never true.
                if cap.get("note"):
                    kv(label, paint(cap["note"], "bad", colour))
                continue
            # A remote target's free memory is measured with no total beside
            # it (t_mem_mb there is MemAvailable, not a size, targets/remote.sh)
            # -- so the total is only ever printed when there is one.
            free = "%s free" % gb(cap.get("free_mb"))
            if cap.get("mem_mb"):
                free += " of %s" % gb(cap.get("mem_mb"))
            kv(label, "%s of %s cores   %s"
               % (paint(cap.get("load") or "?", load_hue(cap.get("load"), cap.get("cores")), colour),
                  cap.get("cores"), free))
        for lk in m.get("locks") or []:
            # A lock whose holder is gone is not a lock: the next taker breaks
            # it. "held" against "stale" is the difference between waiting and
            # not.
            kv("lock", "%s  %s  %s"
               % (lk.get("resource", "?"),
                  paint("held" if lk.get("alive") else "stale",
                        "busy" if lk.get("alive") else "bad", colour),
                  paint("pid %s  %s" % (lk.get("pid", "?"), lk.get("cmd", "")), "dim", colour)))
        b = m.get("bench")
        if b:
            kv("bench", "%s  %s" % (b.get("run", "?"),
                                    paint(b.get("state", "?"), severity(b.get("state")), colour)))

        # A machine that could not answer in records: its own listing, as it
        # sent it, rather than nothing.
        for r in m["raw"]:
            out.append("")
            for ln in r.get("text", "").split("\n"):
                out.append("  " + paint(ln, "dim", colour))
            notes(r.get("notes"), "  ")

        if m["facts"]:
            out.append("")
        for f in m["facts"]:
            if f.get("type") == "wk-tools":
                what = "wk-tools" + (" (%s)" % f["copy"] if f.get("copy") else "")
                # The tree hash is the answer; the commit is context, and only
                # when there is one. Every rsynced copy reports `-` for it --
                # they are copied with `--exclude .git` -- so it was a column of
                # dashes with no heading in the common case, sitting between the
                # label and the thing the reader came for.
                verdict = (paint("in sync", "good", colour) if f.get("insync")
                           else paint("DIFFERS from the workstation (%s)" % f.get("expect", "?"),
                                      "bad", colour))
                extra = ""
                if f.get("sha") and f["sha"] != "-":
                    extra = paint("  at %s%s" % (f["sha"], "+dirty" if f.get("dirty") else ""),
                                  "dim", colour)
                elif f.get("dirty"):
                    extra = paint("  +dirty", "dim", colour)
                kv(what, "%s  %s%s" % (f.get("tree", "?"), verdict, extra))
                # The command, not "push it there": which one it is depends on
                # what kind of copy this is, and none of the three is guessable.
                if f.get("fix"):
                    kv("", paint(f["fix"], "busy", colour))
            elif f.get("type") == "key":
                kv("push key", f.get("text", ""))

        align(_machine_start)

    if doc["fleet"]:
        heading("fleet", "role, mode, and the media wk owns (wk help hardware)")
        fw = max([len(f.get("machine", "")) for f in doc["fleet"]] + [7])
        rw = max([len(f.get("role", "")) for f in doc["fleet"]] + [4])
        mw = max([len(f.get("mode", "")) for f in doc["fleet"]] + [4])
        _fleet_start = len(out)
        for f in doc["fleet"]:
            out.append("  %s  %s  %s  %s"
                       % (f.get("machine", "").ljust(fw),
                          paint(f.get("role", "").ljust(rw), "dim", colour),
                          paint(f.get("mode", "").ljust(mw), severity(f.get("mode")), colour),
                          f.get("media", "")))
            if f.get("armed"):
                out.append("  %s  %s" % (" " * fw,
                           paint("** armed for %s -- wk boot %s --status **"
                                 % (f["armed"], f.get("machine", "")), "busy", colour)))
            meta(f, "  " + " " * fw + "  ")
        align(_fleet_start)

        # How each machine is made again from nothing.
        #
        # Composed by the machine's own boot driver from the fields its conf
        # declares (b_reprovision), so nothing here is a second copy of a fact
        # and a lane that changes shape changes this with it. Printed after the
        # table rather than inside it: it is several lines per machine, and the
        # table's job is the one-line answer.
        recipes = [f for f in doc["fleet"] if f.get("reprovision")]
        if recipes:
            heading("re-provisioning",
                    "each machine from nothing -- 'wk help hardware' for why the lanes differ")
            for f in recipes:
                out.append("  " + paint(f.get("machine", ""), "dim", colour)
                           + "  " + paint(f.get("role", ""), "dim", colour))
                for line in f["reprovision"].split("\n"):
                    if not line.strip():
                        continue
                    # A line that is indented in the driver is a note about the
                    # command above it, not a command -- so it stays dim and the
                    # commands stay copyable.
                    if line.startswith(" "):
                        out.append("          " + paint(line.strip(), "dim", colour))
                    else:
                        out.append("      " + line)
                out.append("")

            # And the one command each *role* is provisioned by, which is the
            # question the per-machine recipes above do not answer: those say
            # how a particular board is rebuilt, and this says which verb to
            # reach for when the thing in front of you is a kind of thing.
            #
            # Derived from what the fleet actually holds -- a role nothing here
            # plays contributes no line, so this cannot drift into advertising a
            # lane that was removed.
            roles = []
            if any(f.get("role") == "bench-device" for f in doc["fleet"]):
                roles.append(("a rescue system",
                              "wk sysimage write <id> --disk <machine>:<device> --rescue"))
                roles.append(("a bench system",
                              "wk sysimage write <id> --disk <machine>:<device>"))
            if doc.get("bridges"):
                roles.append(("a tailnet bridge", "wk bridge provision <name>"))
            if any(f.get("role") == "workstation" for f in doc["fleet"]):
                roles.append(("a workstation", "./setup"))
            if roles:
                out.append("  " + paint("by role", "dim", colour)
                           + "  " + paint("one image serves both board roles; the marker on the "
                                          "card is the only difference", "dim", colour))
                lw = max(len(r[0]) for r in roles)
                for what, cmd in roles:
                    out.append("      " + paint(what.ljust(lw), "dim", colour) + "   " + cmd)
                out.append("")

    if doc["bridges"]:
        heading("tailnet bridges", "probed: the segment, the role, and its own health check")
        bw = max(len(b.get("name", "")) for b in doc["bridges"])
        _bridge_start = len(out)
        for b in doc["bridges"]:
            out.append("  %s  %-10s %-14s %s"
                       % (b.get("name", "").ljust(bw), b.get("device", "?"), b.get("segment", "?"),
                          paint(b.get("state", "?"), severity(b.get("state")), colour)))
            pad = "  " + " " * bw + "  "
            if b.get("health"):
                kv("health", b["health"], pad)
            if "role_insync" in b:
                kv("role",
                   paint("this repository's", "good", colour) if b["role_insync"]
                   else paint("older than this repository -- wk bridge setup %s" % b.get("name", ""),
                              "bad", colour),
                   pad)
            meta(b, pad)
            if b.get("note"):
                out.append(pad + paint(b["note"], "dim", colour))
            notes(b.get("notes"), pad)
        align(_bridge_start)

    out.append("")
    return "\n".join(out)


# --- the page -----------------------------------------------------------------
#
# Self-contained: no CDN, no font host, no framework. A page served from a
# laptop to look at a fleet must not depend on the network the fleet is the
# reason you are worried about -- and a status page that cannot render because
# a stylesheet host is unreachable is the exact failure it exists to report on.
#
# This is the primary view now (status_default_mode, lib/common.sh): a bare
# `wk status` at a terminal opens it. So it is built to be read at a glance and
# not merely to contain the facts -- the same document underneath, arranged so
# that the things that cost time or work are the things the eye lands on:
#
#   the verdict     the exit code, in the words cmd/status's header gives it,
#                   coloured, at the top. That code is the command's contract
#                   and it was previously visible only to `echo $?`.
#   what is wrong   a strip of counts, and only of things that are non-zero: a
#                   listing with nothing wrong shows nothing there rather than
#                   six zeroes to read past.
#   pressure        disk and load as bars against their own ceiling, because
#                   "92%" and "1.32" are numbers whose meaning is the ratio.
#   work at risk    unpushed commits and a dirty tree, in the column consulted
#                   before `wk rm`.
#   a stale lock    one whose holder is gone is not a lock at all, and it reads
#                   exactly like one that is held.
#
# The state vocabulary is injected rather than written twice (__SEV__ below).
# A second copy in JavaScript drifts from this one: `up`, `clean`, `finished`
# and `held` end up coloured in the terminal and plain on the page, which is a
# listing that answers a question two ways.

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>wk status</title>
<style>
  /* Light is the base and dark redefines only the tokens, so no colour has its
     single definition inside a media query. */
  :root {
    color-scheme: light dark;
    --bg:#f7f7f5; --fg:#1b1b19; --dim:#6d6d67; --faint:#8e8e87;
    --line:#e0e0d9; --card:#ffffff; --sunk:#f0f0ec;
    --good:#127a33; --busy:#8a5b00; --bad:#b3261e; --idle:#8a8a82; --accent:#2d5fa8;
    --good-bg:#e4f4e8; --busy-bg:#fdf1d6; --bad-bg:#fbe6e4; --idle-bg:#eeeeea;
    --accent-bg:#e6eefb;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#131417; --fg:#e7e7e3; --dim:#9b9b95; --faint:#7c7c76;
      --line:#2a2c31; --card:#1a1c20; --sunk:#212429;
      --good:#5fd07f; --busy:#e0b23c; --bad:#ff7b70; --idle:#84847d; --accent:#7fa9ee;
      --good-bg:#16301f; --busy-bg:#332a12; --bad-bg:#37201e; --idle-bg:#23252a;
      --accent-bg:#1b2536;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:0 0 4rem; background:var(--bg); color:var(--fg);
         font:13.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  .wrap { max-width:1400px; margin:0 auto; padding:1.5rem 1.75rem; }

  /* --- the top: the verdict, then only what is wrong --- */
  .top { border-bottom:1px solid var(--line); background:var(--card); }
  .top .wrap { padding-bottom:1rem; }
  .title { display:flex; align-items:baseline; gap:.9rem; flex-wrap:wrap; }
  h1 { font-size:1.05rem; margin:0; letter-spacing:.04em; }
  .verdict { font-weight:700; }
  .attn { display:flex; gap:.45rem; flex-wrap:wrap; margin-top:.7rem; }
  .attn:empty { display:none; }
  .allclear { margin-top:.7rem; color:var(--good); }

  /* --- chips: one shape for every state word in the listing --- */
  .chip { display:inline-block; padding:.08rem .5rem; border-radius:999px;
          font-size:.8rem; font-weight:600; white-space:nowrap;
          background:var(--idle-bg); color:var(--idle); }
  .chip.good{background:var(--good-bg);color:var(--good)}
  .chip.busy{background:var(--busy-bg);color:var(--busy)}
  .chip.bad {background:var(--bad-bg); color:var(--bad)}
  .chip.idle{background:var(--idle-bg);color:var(--idle)}
  .chip.accent{background:var(--accent-bg);color:var(--accent)}
  .chip .n { font-variant-numeric:tabular-nums; }

  h2 { font-size:1rem; margin:0; letter-spacing:.02em; }
  .tag { color:var(--dim); font-weight:400; font-size:.82em; margin-left:.6rem; }
  section.machine { margin:1.75rem 0 0; }
  .mhead { display:flex; align-items:baseline; gap:.5rem; flex-wrap:wrap;
           border-bottom:2px solid var(--line); padding-bottom:.45rem; margin-bottom:.75rem; }
  .mhead .spacer { flex:1; }

  /* --- the tiles: what a machine is, apart from the workspaces on it --- */
  .tiles { display:grid; gap:.55rem; margin:0 0 1rem;
           grid-template-columns:repeat(auto-fill,minmax(15rem,1fr)); }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:7px;
          padding:.5rem .65rem; }
  .tile .tk { color:var(--dim); font-size:.75rem; text-transform:uppercase;
              letter-spacing:.07em; margin-bottom:.2rem; }
  .tile .tv { font-size:.92rem; }
  .tile .tv b { font-variant-numeric:tabular-nums; font-weight:700; }
  .tile .sub { color:var(--faint); font-size:.8rem; }
  /* A ratio, drawn as a ratio: "92%" and "1.32" are numbers whose whole
     meaning is what they are a fraction of. */
  .meter { height:5px; border-radius:3px; background:var(--sunk); margin-top:.4rem;
           overflow:hidden; }
  .meter i { display:block; height:100%; background:var(--idle); }
  .meter.good i{background:var(--good)} .meter.busy i{background:var(--busy)}
  .meter.bad  i{background:var(--bad)}

  /* --- the workspace tables --- */
  .method { margin:0 0 1.1rem; }
  .method > h3 { font-size:.75rem; text-transform:uppercase; letter-spacing:.09em;
                 color:var(--dim); margin:0 0 .35rem; font-weight:700; }
  table { border-collapse:collapse; width:100%; background:var(--card);
          border:1px solid var(--line); border-radius:7px; overflow:hidden; }
  th { text-align:left; font-size:.7rem; text-transform:uppercase; letter-spacing:.07em;
       color:var(--dim); font-weight:700; padding:.45rem .7rem;
       border-bottom:1px solid var(--line); background:var(--sunk); }
  td { padding:.38rem .7rem; border-top:1px solid var(--line); vertical-align:top;
       white-space:nowrap; }
  tr.sub td, tr.note td { border-top:none; padding-top:0; }
  td.wide { white-space:normal; }
  td.name { font-weight:700; }
  /* The row of a workspace that needs a person, marked on the row itself: a
     coloured word in one cell is a thing to find, and this is a thing to see. */
  tr.attention td { background:var(--bad-bg); }
  tr.attention td.name { box-shadow:inset 3px 0 0 var(--bad); }
  tr.working td { background:var(--busy-bg); }
  tr.working td.name { box-shadow:inset 3px 0 0 var(--busy); }
  .good{color:var(--good)} .busy{color:var(--busy)} .bad{color:var(--bad)}
  .idle{color:var(--idle)} .dim{color:var(--dim)}
  .note { color:var(--dim); white-space:pre-wrap; font-size:.88em; }
  .note.warn { color:var(--busy); }
  .meta { color:var(--faint); font-size:.84em; margin:.2rem 0 .8rem; }
  .meta .k { display:inline-block; min-width:11rem; }
  code.fix { background:var(--sunk); border:1px solid var(--line); border-radius:4px;
             padding:0 .35rem; color:var(--busy); font-weight:600; }
  .facts { margin:.4rem 0 0; color:var(--dim); font-size:.88em; }
  pre.raw { background:var(--card); border:1px solid var(--line); border-radius:7px;
            padding:.7rem; overflow-x:auto; margin:.4rem 0 0; font-size:.85em; }

  /* --- the fleet board --- */
  .board { display:grid; gap:.6rem; grid-template-columns:repeat(auto-fill,minmax(17rem,1fr)); }
  .dev { background:var(--card); border:1px solid var(--line); border-left:4px solid var(--idle);
         border-radius:7px; padding:.6rem .75rem; }
  .dev.good{border-left-color:var(--good)} .dev.busy{border-left-color:var(--busy)}
  .dev.bad {border-left-color:var(--bad)}
  /* A board actually running a bench system is the one state on this board
     that changes what you may do with the machine, so it is the loud one. */
  .dev.bench { border-left-color:var(--accent); background:var(--accent-bg); }
  .dev .dn { font-weight:700; font-size:.95rem; }
  .dev .dr { color:var(--dim); font-size:.8rem; }
  .dev .dm { margin:.3rem 0; }
  .dev .media { color:var(--faint); font-size:.82em; white-space:normal; }
  .armed { margin-top:.4rem; color:var(--busy); font-weight:600; font-size:.85em; }

  footer { position:fixed; right:1rem; bottom:.75rem; color:var(--dim); font-size:.8rem;
           background:var(--card); border:1px solid var(--line);
           padding:.3rem .6rem; border-radius:999px; }
  footer .spin { color:var(--busy); font-weight:600; }
  /* The server has gone away -- the page is still showing the last listing it
     had, and every word of it may be hours old. Grey and a red frame, because
     the danger here is reading a stale fleet as a current one. */
  body.gone { filter:grayscale(1) opacity(.55); }
  body.gone::after { content:""; position:fixed; inset:0; pointer-events:none;
                     border:6px solid var(--bad); }
  body.gone footer { filter:none; color:var(--bad); font-weight:700; }
</style>
<body>
<div class="top"><div class="wrap">
  <div class="title"><h1>wk status</h1><span id="verdict" class="chip verdict">…</span>
    <span id="counts" class="dim"></span></div>
  <div id="attn" class="attn"></div>
</div></div>
<div class="wrap"><div id="app"></div></div>
<footer id="foot">loading…</footer>
<script>
const ESC = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// The state vocabulary, from lib/status-view.py -- one list, not a second copy
// that drifts from it.
const SEV = __SEV__;
function sev(w) {
  w = String(w || "").split(" ")[0].toLowerCase();
  for (const k of ["good","busy","bad","idle"]) if (SEV[k].includes(w)) return k;
  return "";
}
const chip = (w, k) => `<span class="chip ${k === undefined ? sev(w) : k}">${ESC(w)}</span>`;
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
// stored (lib/reach.sh), and shown faint: it is what somebody wants when
// something above it is wrong.
function meta(o) {
  const rows = [];
  if (o.tailnet) rows.push(`<div><span class="k">reached</span>${ESC(o.tailnet)}</div>`);
  if (o.direct)  rows.push(`<div><span class="k">without tailscale</span>${ESC(o.direct)}</div>`);
  if (o.conf)    rows.push(`<div><span class="k">from</span>${ESC(o.conf)}</div>`);
  return rows.length ? `<div class="meta">${rows.join("")}</div>` : "";
}
const GB = mb => { const n = parseInt(mb, 10); return isNaN(n) ? "?" : (n >= 1024 ? Math.round(n/1024) + "G" : n + "M"); };
// The thresholds are "will the next build fit", not tidiness: a WebKit build
// tree is tens of gigabytes, so 90% of a disk this size is already a build that
// may not finish.
function diskHue(p) { const n = parseInt(p,10); return isNaN(n) ? "" : n >= 90 ? "bad" : n >= 75 ? "busy" : "good"; }
function loadHue(l, c) { l = parseFloat(l); c = parseInt(c,10);
  return (isNaN(l) || !c) ? "" : l > c ? "bad" : l > c/2 ? "busy" : "good"; }
const where = o => (o.where || "").replace("in the ","").replace("the ","");
function meter(pct, hue) {
  const p = Math.max(0, Math.min(100, pct));
  return `<div class="meter ${hue}"><i style="width:${p}%"></i></div>`;
}
function tile(k, v, m) { return `<div class="tile"><div class="tk">${ESC(k)}</div>
  <div class="tv">${v}</div>${m || ""}</div>`; }

// What the machine is, apart from the workspaces on it: the things that break a
// build or cost work, each as one tile, each with its number against the
// ceiling that number means anything relative to.
function tiles(m) {
  const t = [];
  for (const d of m.disk || []) {
    const pct = parseInt(d.used_pct, 10);
    let sub = `${GB(d.free_mb)} free of ${GB(d.total_mb)}`;
    if (d.snapshots) sub += ` · ${ESC(d.snapshots)} snapshot${d.snapshots === "1" ? "" : "s"}`;
    if (d.reclaimable && d.reclaimable !== "0")
      sub += ` · <span class="busy">${ESC(d.reclaimable)} reclaimable (wk gc)</span>`;
    t.push(tile("disk" + (where(d) ? " · " + where(d) : ""),
      `<b class="${diskHue(d.used_pct)}">${ESC(d.used_pct || "?")}%</b> used
       <span class="sub">${sub}</span>`, meter(pct, diskHue(d.used_pct))));
  }
  for (const c of m.capacity || []) {
    const label = "load" + (where(c) ? " · " + where(c) : "");
    if (!c.cores) {
      // A probe that did not answer says so on the tile rather than the
      // tile vanishing, which reads as "nothing is using this machine".
      if (c.note) t.push(tile(label, chip(c.note, "bad")));
      continue;
    }
    const hue = loadHue(c.load, c.cores), pct = (parseFloat(c.load) / parseInt(c.cores,10)) * 100;
    // A remote target's free memory has no total beside it (t_mem_mb there
    // is MemAvailable, not a size, targets/remote.sh) -- print one only when
    // there is one.
    const free = c.mem_mb ? `${GB(c.free_mb)} free of ${GB(c.mem_mb)}` : `${GB(c.free_mb)} free`;
    t.push(tile(label,
      `<b class="${hue}">${ESC(c.load || "?")}</b> of ${ESC(c.cores)} cores
       <span class="sub">${free}</span>`, meter(pct, hue)));
  }
  for (const sv of m.services || [])
    t.push(tile(sv.name, chip(sv.state) + (sv.fix ? ` <code class="fix">${ESC(sv.fix)}</code>` : "")));
  for (const sw of m.switches || [])
    t.push(tile(sw.name + (where(sw) ? " · " + where(sw) : ""),
      chip(sw.state, sw.state === "on" ? "good" : "busy") +
      ` <span class="sub">${ESC(sw.detail || "")}</span>`));
  for (const lk of m.locks || [])
    // A lock whose holder is gone is not a lock: the next taker breaks it, and
    // it looks exactly like one that is held.
    t.push(tile("lock · " + ESC(lk.resource),
      chip(lk.alive ? "held" : "stale", lk.alive ? "busy" : "bad") +
      ` <span class="sub">pid ${ESC(lk.pid || "?")} ${ESC(lk.cmd || "")}</span>`));
  if (m.bench)
    t.push(tile("bench", chip(m.bench.state) + ` <span class="sub">${ESC(m.bench.run)}</span>`));
  return t.length ? `<div class="tiles">${t.join("")}</div>` : "";
}

// The exit code, in cmd/status's own words. It is this command's whole contract
// and on a page it was previously invisible.
const VERDICT = {
  0: ["good", "idle, or the last build succeeded"],
  1: ["bad",  "a build failed"],
  2: ["busy", "a build is running, or a workspace is being created"],
  3: ["bad",  "a build stalled and was killed"],
  4: ["bad",  "a workspace needs a person"],
};
// Only what is wrong, and only when it is: a listing with nothing to say shows
// nothing here rather than a row of zeroes to read past.
function summary(doc) {
  let ws = 0, attn = 0, running = 0, failed = 0, stale = 0, full = 0, drift = 0,
      unpushed = 0, away = 0, oldrole = 0, bench = 0;
  for (const m of doc.machines) {
    for (const g of m.methods) for (const w of g.workspaces) {
      ws++;
      if (["creating","broken","unreachable"].includes(w.ws)) attn++;
      if (w.unpushed) unpushed += parseInt(w.unpushed, 10) || 0;
      for (const s of w.subs || []) {
        if (s.state === "running" || s.state === "building") running++;
        if (["failed","oom","stalled","gave-up","error"].includes(s.state)) failed++;
      }
    }
    for (const lk of m.locks || []) if (!lk.alive) stale++;
    for (const d of m.disk || []) if (parseInt(d.used_pct,10) >= 90) full++;
    for (const f of m.facts || []) if (f.type === "wk-tools" && !f.insync) drift++;
  }
  for (const f of doc.fleet || []) {
    if (String(f.mode || "").startsWith("bench")) bench++;
    if (String(f.mode || "").startsWith("unreachable") || String(f.mode || "").startsWith("no answer")) away++;
  }
  for (const b of doc.bridges || []) if (b.role_insync === false) oldrole++;
  const bits = [
    [attn,     "bad",    n => `${n} workspace${n>1?"s":""} needing a person`],
    [failed,   "bad",    n => `${n} failed`],
    [stale,    "bad",    n => `${n} stale lock${n>1?"s":""}`],
    [full,     "bad",    n => `${n} disk${n>1?"s":""} over 90%`],
    [running,  "busy",   n => `${n} running`],
    [unpushed, "busy",   n => `${n} unpushed commit${n>1?"s":""}`],
    [drift,    "busy",   n => `${n} wk-tools ${n>1?"copies":"copy"} behind`],
    [oldrole,  "busy",   n => `${n} bridge role${n>1?"s":""} older than the repo`],
    [away,     "idle",   n => `${n} board${n>1?"s":""} not answering`],
    [bench,    "accent", n => `${n} board${n>1?"s":""} in bench mode`],
  ];
  return { ws, chips: bits.filter(b => b[0] > 0).map(b => chip(b[2](b[0]), b[1])) };
}

function render(doc) {
  const s = summary(doc);
  const [vk, vt] = VERDICT[doc.exit] || ["", "exit " + doc.exit];
  const v = document.getElementById("verdict");
  v.className = "chip verdict " + vk;
  v.textContent = doc.exit + " · " + vt;
  document.getElementById("counts").textContent =
    `${doc.machines.length} machine${doc.machines.length===1?"":"s"} · ${s.ws} workspace${s.ws===1?"":"s"}`;
  document.getElementById("attn").innerHTML =
    s.chips.length ? s.chips.join("") : '<span class="allclear">nothing wants attention</span>';

  const parts = [];
  for (const m of doc.machines) {
    parts.push(`<section class="machine"><div class="mhead"><h2>${ESC(m.name)}</h2>` +
      (m.self ? '<span class="tag">this machine</span>' : "") + `</div>${meta(m)}${tiles(m)}`);
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
        // The row itself carries the verdict, not only one cell of it.
        const needs = ["creating","broken","unreachable"].includes(w.ws);
        const busy = subs.some(x => x.state === "running" || x.state === "building");
        const cls = needs ? "attention" : busy ? "working" : "";
        parts.push(`<tr class="${cls}">
          <td class="name">${ESC(w.name)}</td>
          <td>${chip(w.state)}</td>
          <td title="${ESC(w.branch || "")}">${ESC(branch)}</td>
          <td class="${work.length ? "busy" : "idle"}">${ESC(workTxt)}</td>
          <td class="${w.base_behind ? "busy" : "idle"}" title="${ESC(w.base || "")}">${w.base_behind ? "-" + ESC(w.base_behind) : ""}</td>
          <td class="${subs[0] ? sev(subs[0].state) : ""}">${subs[0] ? ESC(subText(subs[0])) : ""}</td></tr>`);
        for (const x of subs.slice(1))
          parts.push(`<tr class="sub"><td colspan="5"></td><td class="${sev(x.state)}">${ESC(subText(x))}</td></tr>`);
        for (const n of (w.notes || []))
          parts.push(`<tr class="note"><td colspan="6" class="wide"><div class="note ${n.level === "warn" ? "warn" : ""}">${ESC(n.text)}</div></td></tr>`);
      }
      parts.push(`</table></div>`);
    }
    const facts = (m.facts || []).map(f => {
      if (f.type === "wk-tools") {
        const what = "wk-tools" + (f.copy ? ` (${f.copy})` : "");
        const ver = (f.sha || "-") + (f.dirty ? "+dirty" : "");
        return `<div>${ESC(what)} ${ESC(ver)} ${ESC(f.tree)} ` + (f.insync
          ? chip("in sync", "good")
          : chip("differs from the workstation (" + (f.expect || "?") + ")", "bad")
            + (f.fix ? ` <code class="fix">${ESC(f.fix)}</code>` : "")) + `</div>`;
      }
      return `<div>push key ${ESC(f.text)}</div>`;
    });
    if (facts.length) parts.push(`<div class="facts">${facts.join("")}</div>`);
    for (const r of (m.raw || []))
      parts.push(`<pre class="raw">${ESC(r.text)}</pre>` +
        (r.notes || []).map(n => `<div class="note warn">${ESC(n.text)}</div>`).join(""));
    if (!m.methods.some(g => g.workspaces.length) && !(m.raw || []).length)
      parts.push(`<div class="idle">(no workspaces on it)</div>`);
    parts.push(`</section>`);
  }

  if (doc.fleet && doc.fleet.length) {
    parts.push(`<section class="machine"><div class="mhead"><h2>fleet</h2>
      <span class="tag">role, mode, and the media wk owns — wk help hardware</span></div>
      <div class="board">`);
    for (const f of doc.fleet) {
      const bench = String(f.mode || "").startsWith("bench");
      parts.push(`<div class="dev ${bench ? "bench" : sev(f.mode)}">
        <div class="dn">${ESC(f.machine)} <span class="dr">${ESC(f.role || "")}</span></div>
        <div class="dm">${chip(f.mode)}</div>
        <div class="media">${ESC(f.media || "")}</div>` +
        (f.armed ? `<div class="armed">armed for ${ESC(f.armed)} — wk boot ${ESC(f.machine)} --status</div>` : "") +
        meta(f) + `</div>`);
    }
    parts.push(`</div></section>`);
  }

  if (doc.bridges && doc.bridges.length) {
    parts.push(`<section class="machine"><div class="mhead"><h2>tailnet bridges</h2>
      <span class="tag">probed: the segment, the role, and its own health check</span></div>
      <table><tr><th>bridge</th><th>device</th><th>segment</th><th>state</th><th>role</th><th>health</th></tr>`);
    for (const b of doc.bridges) {
      // A role older than this repository's is the one thing here that has a
      // command attached to it, so it is a chip and not a sentence to find.
      const role = b.role_insync === undefined ? ""
        : b.role_insync ? chip("this repository's", "good")
        : chip("older — wk bridge setup " + b.name, "bad");
      parts.push(`<tr><td class="name">${ESC(b.name)}</td><td>${ESC(b.device)}</td><td>${ESC(b.segment)}</td>
        <td>${chip(b.state || "?")}</td><td class="wide">${role}</td>
        <td class="wide dim">${ESC(b.health || "")}</td></tr>`);
      const extra = [];
      if (b.note) extra.push(`<div class="note">${ESC(b.note)}</div>`);
      const bm = meta(b);
      if (extra.length || bm) parts.push(`<tr class="note"><td colspan="6" class="wide">${extra.join("")}${bm}</td></tr>`);
      for (const n of (b.notes || [])) parts.push(`<tr class="note"><td colspan="6" class="wide"><div class="note warn">${ESC(n.text)}</div></td></tr>`);
    }
    parts.push(`</table></section>`);
  }
  document.getElementById("app").innerHTML = parts.join("");
}
const LIVE = __LIVE__;
if (!LIVE) { render(__DOC__); document.getElementById("foot").textContent = "static page — wk status --web for a live one"; }
else {
  let last = 0;
  async function poll() {
    try {
      const r = await fetch("status.json", { cache: "no-store" });
      const p = await r.json();
      if (p.stamp !== last) { last = p.stamp; render(p.doc); }
      document.body.classList.remove("gone");
      const age = Math.round((Date.now()/1000) - p.stamp);
      document.getElementById("foot").innerHTML = p.refreshing
        ? '<span class="spin">walking the fleet…</span>'
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
    return (
        PAGE.replace("__LIVE__", "true" if live else "false")
        .replace("__SEV__", json.dumps({"good": GOOD, "busy": BUSY, "bad": BAD, "idle": IDLE}))
        .replace("__DOC__", json.dumps(doc) if not live else "null")
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
        # Two locks with two jobs: `lock` guards the document while it is being
        # swapped under a request, and `busy` guards the *right to walk*. One
        # lock could not do both -- holding it for the whole walk would block
        # every page poll for the walk's entire duration, which is the thing the
        # timer exists to keep off the request path.
        self.lock = threading.Lock()
        self.busy = threading.Lock()
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
        # One walk at a time, ever. `wk status` on this fleet takes tens of
        # seconds and can take minutes when the tailnet is down -- longer than
        # any sensible interval -- so a timer that fired regardless would have
        # two walks probing the same boards over the same phone at once. Not
        # merely wasteful: two fleet blocks running together disagree with each
        # other about which boards are up, and neither answer is a fleet that
        # has changed.
        #
        # A refusal rather than a queue, because the answer a second walk would
        # give is the answer the first one is already about to give. Non-blocking
        # for the same reason: a caller told "no" here has lost nothing.
        if not self.busy.acquire(blocking=False):
            return
        try:
            with self.lock:
                self.refreshing = True
            wk = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wk"
            )
            doc = None
            try:
                # --json, and not --records: this asks for the same document
                # every other view is drawn from, through the same dispatcher a
                # person would type -- so the page cannot be showing a listing
                # assembled differently from the one the terminal gets.
                #
                # WK_STATUS_VIEW so the walk cannot decide it is a person at a
                # terminal and try to serve a second page from inside this one.
                env = dict(os.environ, WK_STATUS_VIEW="json")
                out = subprocess.run(
                    [wk, "status", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env,
                ).stdout
                doc = json.loads(out) if out.strip().startswith("{") else None
            except Exception as exc:  # a refresh that fails must not stop the loop
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
            # The interval is a gap between walks, not a period to fire on: a
            # walk that overran its interval must not be followed immediately by
            # another, and one that was quick must not wait the whole interval
            # again on top of the time it already took. A second of floor so a
            # fleet that answers instantly cannot become a spin.
            time.sleep(max(1.0, self.interval - (time.time() - started)))


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
