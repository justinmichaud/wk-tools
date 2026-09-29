"""The bench record, a task directory -- task.json, runs/<run>/{env,result}.json -- whose state is recomputed from its runs on every read ("running" is its lock, which the caller reads), where each lives, and `wk bench ls`'s listing of every one in the fleet."""

import json
import os
import re
import sys
import zipfile

from wk import fleetwalk
from wk.kv import kv_file
from wk.machine import Local, replace_file

# The axes a report groups variance by, filled in for every subfield a writer left alone, so an older record and an uncontrolled one read alike.
DEFAULT_CONFIGURATION = {"aslr": "unset", "path_len": 0, "shared_cache": None, "env_pad_bytes": 0}

UNKNOWN = None
UNMEASURED = "unmeasured "


def failed(rows, at=0):
    return [r for r in rows if r[at] is False]


def unmeasured(rows, at=0):
    return [r for r in rows if r[at] is UNKNOWN]


def not_measured(n):
    return " (%d not measured)" % n if n else ""


def preflight_notes(rows, notes):
    clean = lambda d: d.replace('"', "").replace("\\", "")
    return ("".join("%s: %s; " % (w, clean(d)) for _, w, d in failed(rows))
            + "".join("%s%s: %s; " % (UNMEASURED, w, clean(d)) for _, w, d in unmeasured(rows)) + "".join(n + "; " for n in notes))


def load(path):
    """{} where the file is absent -- an older run, or one still writing it; anything else (corrupt JSON, a permission error) raises, so it is not misreported as merely missing."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def get_nested(doc, dotted_key):
    node = doc
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_nested(doc, dotted_key, value):
    parts = dotted_key.split(".")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _pairs(fields, who):
    for field in fields:
        key, sep, value = field.partition("=")
        if not sep:
            sys.exit("%s: not a key=value: %s" % (who, field))
        yield key, value


def write_env(path, fields, bool_fields=(), update=False):
    """The one writer of a run's env.json; `update` merges onto the write before the run, since wall_time_s comes after it."""
    doc = load(path) if update else {}
    for key, value in _pairs(fields, "env-record"):
        set_nested(doc, key, value)
    for key, value in _pairs(bool_fields, "env-record"):
        set_nested(doc, key, bool(value))
    cfg = doc.setdefault("configuration", {})
    for key, value in DEFAULT_CONFIGURATION.items():
        cfg.setdefault(key, value)
    replace_file(path, json.dumps(doc, indent=2))


def _list_field(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def task_write(taskdir, fields, commands):
    doc = {"commands": list(commands)}
    for key, value in _pairs(fields, "task-write"):
        if key == "devices":
            doc["devices"] = [{"device": dev, "profile": profile}
                              for dev, _, profile in (item.partition("=") for item in _list_field(value))]
        elif key in ("plans", "slots"):
            doc[key] = _list_field(value)
        elif key == "rounds":
            doc[key] = int(value)
        else:
            set_nested(doc, key, value)
    for key in ("task", "requested", "devices", "plans", "slots", "rounds"):
        if key not in doc:
            sys.exit("task-write: %s is required" % key)
    if not doc["devices"] or not doc["plans"] or not doc["slots"]:
        sys.exit("task-write: devices, plans and slots each need at least one entry")
    os.makedirs(os.path.join(taskdir, "runs"), exist_ok=True)
    replace_file(os.path.join(taskdir, "task.json"), json.dumps(doc, indent=2, sort_keys=True) + "\n")


def task_doc(taskdir):
    doc = load(os.path.join(taskdir, "task.json"))
    if not doc:
        sys.exit("%s is not a task: no task.json (wk bench ls lists the tasks)" % taskdir)
    return doc


def not_a_measurement(env):
    """Why a run's reading measures no machine, or "": a rehearsal (a driver whose `measures` fact is no) proves the path only."""
    if env.get("measures") is False:
        return "%s is a rehearsal, and its reading is not a measurement of any machine" % (env.get("machine") or "the machine it ran on")
    return ""


def run_state(rundir, env):
    """ok has a result.json; failed has none but the wall_time_s written when run-benchmark returned; running has neither."""
    result = os.path.join(rundir, "result.json")
    if os.path.isfile(result) and os.path.getsize(result) > 0:
        return "rehearsal" if not_a_measurement(env) else "ok"
    if "wall_time_s" in env:
        return "failed"
    return "running"


def task_runs(taskdir):
    runs = []
    root = os.path.join(taskdir, "runs")
    if not os.path.isdir(root):
        return runs
    for name in sorted(os.listdir(root)):
        rundir = os.path.join(root, name)
        env = load(os.path.join(rundir, "env.json"))
        if not env or env.get("warmup"):
            continue
        runs.append({"id": name, "dir": rundir, "env": env, "state": run_state(rundir, env)})
    return runs



def task_arms(doc):
    """(the two things a task compares, what to call them): a systems A/B is one slot in two images."""
    subj = doc.get("subject", {})
    if subj.get("kind") == "systems":
        spec = [x for x in subj.get("spec", "").split(",") if x]
        if len(spec) == 2:
            return spec, "system"
    return doc.get("slots", []), "slot"


def subject_line(doc):
    subj = doc.get("subject", {})
    kind = subj.get("kind", "")
    devices = ", ".join(d["device"] for d in doc.get("devices", []))
    plans = ", ".join(doc.get("plans", []))
    slots = doc.get("slots", [])
    if kind in ("pull", "commit"):
        what = "A/B %s: %s vs base %s" % (subj.get("spec", "?"), (subj.get("head") or "?")[:10], (subj.get("base") or "?")[:10])
    elif kind == "workspace":
        what = "%s %s" % (subj.get("spec", "?"), doc["devices"][0].get("profile", ""))
    elif kind == "systems":
        arms, _ = task_arms(doc)
        what = "%s vs %s" % (arms[0], arms[1]) if len(arms) == 2 else "systems"
    elif len(slots) == 2:
        what = "%s vs %s" % (slots[0], slots[1])
    else:
        what = "slot %s" % "/".join(slots)
    parts = [what]
    if kind != "workspace":
        parts.append(devices)
    parts.append(plans)
    rounds = doc.get("rounds", 1)
    if len(slots) == 2 or rounds > 1:
        parts.append("%d round%s" % (rounds, "" if rounds == 1 else "s"))
    return " · ".join(p for p in parts if p)


def task_rounds(doc, runs):
    """{(device, plan): {round: {arm: run}}}, paired by the ab.round each run recorded."""
    out = {}
    for d in doc.get("devices", []):
        for plan in doc.get("plans", []):
            out[(d["device"], plan)] = {}
    for r in runs:
        env = r["env"]
        key = (env.get("machine") or env.get("workspace") or "?", env.get("plan", "?"))
        ab = env.get("ab") or {}
        if "round" not in ab or "arm" not in ab:
            continue
        out.setdefault(key, {}).setdefault(int(ab["round"]), {})[ab["arm"]] = r
    return out


PINS = ("runner_sha", "local_copy")


def paired(byround, names):
    """(a runs, b runs, dropped): the rounds both arms finished on one payload pin -- the runner commit and the
    benchmark copy -- and why each other round is left out, since arms on two pins measure two benchmarks."""
    a_dirs, b_dirs, dropped = [], [], []
    for rnd in sorted(byround):
        arms = byround[rnd]
        unfinished = ["%s: %s" % (n, arms[x]["state"] if x in arms else "not run") for n, x in zip(names, "ab")
                      if arms.get(x, {}).get("state") != "ok"]
        pins = [tuple(arms[x]["env"].get(k) or "" for k in PINS) for x in "ab"] if not unfinished else []
        if unfinished:
            dropped.append("round %d (%s)" % (rnd, ", ".join(unfinished)))
        elif pins[0] != pins[1]:
            dropped.append("round %d (payload pins differ: %s vs %s)" % (rnd, "@".join(pins[0]), "@".join(pins[1])))
        else:
            a_dirs.append(arms["a"]["dir"])
            b_dirs.append(arms["b"]["dir"])
    return a_dirs, b_dirs, dropped


def progress_line(log):
    try:
        text = open(log, errors="replace").read()
    except OSError:
        return ""
    m = None
    for m in re.finditer(r"Start the iteration (\d+) of (\d+)", text):
        pass
    return "iteration %s/%s" % (m.group(1), m.group(2)) if m else ""


def task_state(taskdir, running):
    doc = task_doc(taskdir)
    runs = task_runs(taskdir)
    arm_names, _ = task_arms(doc)
    planned = len(doc.get("devices", [])) * len(doc.get("plans", [])) * doc.get("rounds", 1) * len(arm_names)
    ok = [r for r in runs if r["state"] == "ok"]
    failed = [r for r in runs if r["state"] == "failed"]
    rehearsed = [r for r in runs if r["state"] == "rehearsal"]
    live = [r for r in runs if r["state"] == "running"]
    ended = len(ok) + len(failed) + len(rehearsed)
    if running:
        state = "running"
    elif ended >= planned:
        state = "complete"
    else:
        state = "incomplete"
    usable = 0
    for byround in task_rounds(doc, runs).values():
        for byarm in byround.values():
            if len(arm_names) == 2 and all(byarm.get(a, {}).get("state") == "ok" for a in ("a", "b")):
                usable += 1
    status = kv_file(os.path.join(taskdir, "status"))
    current = live[0] if (running and live) else None
    summary = "%d/%d runs ended, %d ok, %d failed" % (ended, planned, len(ok), len(failed))
    if rehearsed:
        summary += ", %d rehearsed (no measurement)" % len(rehearsed)
    if len(arm_names) == 2:
        summary += ", %d round%s usable" % (usable, "" if usable == 1 else "s")
    if current:
        env = current["env"]
        summary += "; now %s %s %s" % (env.get("plan", "?"), env.get("machine", "?"), env.get("build_slot", "?"))
        progress = progress_line(os.path.join(current["dir"], "run.log"))
        if progress:
            summary += " (%s)" % progress
    elif running and status.get("stage"):
        summary += "; " + status["stage"]
    elif state == "incomplete" and live:
        summary += "; %d run(s) died with their driver" % len(live)
    return {"doc": doc, "runs": runs, "state": state, "planned": planned, "ended": ended,
            "ok": len(ok), "failed": len(failed), "usable": usable, "current": current,
            "stage": status.get("stage", ""), "summary": summary}


def tasks(bench_dir, machine=None):
    m = machine or Local()
    return sorted(d for d in m.listdir(bench_dir) if m.exists(os.path.join(bench_dir, d, "task.json"))) if m.isdir(bench_dir) else []


# What a task delivers: its request, each run's json (env, result, the PGO reading), the warmup round's evidence and profiles; then its report.
MEASURED = ("task.json", "runs/*/*.json", "warmup/*")
EXPORTED = MEASURED + ("report-*.html",)
LIVE = MEASURED + ("status", "runs/*/run.log")
EXPORT_RECORD = "exported"


def guarded(path):
    path = path.rstrip("/")
    return os.path.dirname(os.path.dirname(path)), os.path.join(os.path.basename(os.path.dirname(path)), os.path.basename(path))


def gather(machine, taskdir, patterns=MEASURED):
    return machine.read_tree(*guarded(taskdir), patterns)


def _held(name, measured, zip_path):
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile):
        return False
    members = set(z.namelist())
    return all(name + "/" + rel in members and z.read(name + "/" + rel) == data for rel, data in measured.items())


def unexported(machine, bench, default_dir):
    """[(task, why)] under `bench` on `machine` that no zip here holds byte for byte: the recorded one, or <default_dir>/<task>.zip."""
    try:
        names = tasks(bench, machine)
    except OSError as e:
        return [(bench, "unreadable, so whether it was exported cannot be known (%s)" % e)]
    out = []
    for t in names:
        d = os.path.join(bench, t)
        try:
            recorded = machine.read(os.path.join(d, EXPORT_RECORD)).strip() if machine.exists(os.path.join(d, EXPORT_RECORD)) else ""
            measured = gather(machine, d)
            if not any(_held(t, measured, z) for z in dict.fromkeys(p for p in (recorded, os.path.join(default_dir, t + ".zip")) if p)):
                out.append((t, "no export readable here holds it as it is now"))
        except OSError as e:
            out.append((t, "unreadable, so whether it was exported cannot be known (%s)" % e))
    return out


def _ws_root(store):
    return os.path.join(store.record_dir(), "ws")


def home_for(store, ws, task):
    """A new task's directory: its workspace's where this store (record_dir, not the podman VM's root) holds it, else bench/."""
    wsdir = os.path.join(_ws_root(store), ws) if ws else ""
    return os.path.join(wsdir, "bench", task) if wsdir and os.path.isdir(wsdir) else os.path.join(store.bench_dir(), task)


def task_roots(machine, root):
    ws = os.path.join(root, "ws")
    dirs = [os.path.join(ws, w, "bench") for w in (sorted(machine.listdir(ws)) if machine.isdir(ws) else [])]
    return [d for d in dirs if machine.isdir(d)] + [os.path.join(root, "bench")]


def homes_at(machine, root):
    out = {}
    for d in task_roots(machine, root):
        for t in tasks(d, machine):
            out.setdefault(t, os.path.join(d, t))
    return dict(sorted(out.items()))


def homes(store):
    return homes_at(Local(), store.record_dir())


def running_tasks(found, lock_path, alive):
    return {t for t in found if alive(lock_path("bench-task-" + t))}


def ls_rows(found, running=(), where=""):
    """One store's rows (`found` is its homes()): each task, its state, its directory and each run's; `where` names the machine holding the store."""
    out = []
    for name, taskdir in found.items():
        st = task_state(taskdir, name in running)
        out.append("%s  %s%s" % (name, subject_line(st["doc"]), "  [%s]" % where if where else ""))
        out.append("    %s  %s" % (st["state"], st["summary"]))
        out.append("    %s" % taskdir)
        for r in st["runs"]:
            m = r["env"]
            axes = m.get("runner", "browser")
            if m.get("arch", "native") != "native":
                axes += "/" + m["arch"]
            if m.get("bench_host", "container") != "container":
                axes += "/" + m["bench_host"]
            out.append("      %s  %s %s %s %s %s%s" % (
                r["dir"], m.get("plan", "?"), m.get("config", "?"), axes,
                (m.get("webkit_sha") or "?")[:10], r["state"],
                "  [FORCED]" if m.get("forced") else ""))
    return out


class Listing:
    """This machine's store, then every target whose machine answers for a store of its own, through its own wk."""

    def __init__(self, reg, store, alive, label, warn):
        self.reg, self.store, self.label, self.warn, self.alive = reg, store, label, warn, alive

    def store_rows(self):
        found = homes(self.store)
        return ls_rows(found, running_tasks(found, self.store.lock_path, self.alive), self.label)

    def _label(self, target, name):
        return name if target.kind == "remote" and not getattr(target, "is_local", True) else self.label

    def fleet_rows(self):
        return fleetwalk.fleet_rows(self.reg, "bench", "tasks", self._label, self.warn)

    def rows(self):
        return self.store_rows() + self.fleet_rows()
