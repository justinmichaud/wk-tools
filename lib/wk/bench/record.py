"""The bench record, a task directory -- task.json, runs/<run>/{env,result}.json -- whose state is recomputed from its runs
on every read ("running" is its lock, which the caller reads), and `wk bench ls`'s listing of every one in the fleet."""

import json
import os
import re
import sys

from wk import fleetwalk
from wk.kv import kv_file

# The axes a report groups variance by, filled in for every subfield a writer left alone, so an older record and an uncontrolled one read alike.
DEFAULT_CONFIGURATION = {"aslr": "unset", "path_len": 0, "shared_cache": None, "env_pad_bytes": 0}


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
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
    with open(path, "w") as f:
        json.dump(doc, f, indent=2)


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
    out = os.path.join(taskdir, "task.json")
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, out)


def task_doc(taskdir):
    doc = load(os.path.join(taskdir, "task.json"))
    if not doc:
        sys.exit("%s is not a task: no task.json (wk bench ls lists the tasks)" % taskdir)
    return doc


def not_a_measurement(env):
    """Why a run's reading measures no machine, or "": a rehearsal (a driver whose B_MEASURES is no) proves the path only."""
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


def status_lines(st):
    out = ["%s=%s" % (key, st[key]) for key in ("state", "planned", "ended", "ok", "failed", "usable", "summary")]
    out.append("subject=%s" % subject_line(st["doc"]))
    out.append("current=%s" % (st["current"]["id"] if st["current"] else ""))
    return out


def tasks(bench_dir):
    if not os.path.isdir(bench_dir):
        return []
    return sorted(d for d in os.listdir(bench_dir) if os.path.isfile(os.path.join(bench_dir, d, "task.json")))


def running_tasks(bench_dir, lock_path, alive):
    return {t for t in tasks(bench_dir) if alive(lock_path("bench-task-" + t))}


def ls_rows(bench_dir, running=(), where=""):
    """One store's rows: each task, its state, its directory and each run's; `where` names the machine holding the store."""
    out = []
    for name in tasks(bench_dir):
        taskdir = os.path.join(bench_dir, name)
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

    def __init__(self, reg, bench_dir, lock_path, alive, label, warn):
        self.reg, self.bench_dir, self.label, self.warn = reg, bench_dir, label, warn
        self.lock_path, self.alive = lock_path, alive

    def store_rows(self):
        return ls_rows(self.bench_dir, running_tasks(self.bench_dir, self.lock_path, self.alive), self.label)

    def _label(self, target, name):
        return name if target.kind == "remote" and not getattr(target, "is_local", True) else self.label

    def fleet_rows(self):
        return fleetwalk.fleet_rows(self.reg, "bench", "tasks", self._label, self.warn)

    def rows(self):
        return self.store_rows() + self.fleet_rows()
