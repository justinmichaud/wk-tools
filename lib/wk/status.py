"""The `wk status` walk: one job per place, each answering in records -- the workspaces it holds and the tasks its
store records -- read here, or asked of the place's own wk (fleet.ask). Every fact is read as the walk runs."""

import os
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from wk import fleet, places, record, statusview
from wk.clock import Clock
from wk.kv import ANSI, kv
from wk.store import Snapshots, dispatch_place

ENDED_AS_ASKED = ("ok", "cancelled", "stopped", "refused")
TASK_EXIT = {"running": 2, "starting": 2, "silent": 2, "died": 4, "unreadable": 4, "unanswered": 4,
             "failed": 1, "gave-up": 1, "error": 1, "stalled": 3, "oom": 3}
METHOD = {"container": "container", "vm": "macOS guest"}
RANK = {"container": 0, "vm": 1, "local": 2}
WAIT_SECONDS = 5   # between two walks of `--wait`: a build's state changes in minutes

WS_PROBE = r'''cd @SRC@ 2>/dev/null || exit 0
printf 'origin=%s\n' "$(git config --get remote.origin.url 2>/dev/null)"
printf 'dirty=%s\n' "$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')"
printf 'untracked=%s\n' "$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ')"
printf 'unpushed=%s\n' "$(git rev-list --count HEAD --not --remotes 2>/dev/null)"
_u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || _u=origin/main
printf 'upstream=%s\n' "$_u"
set -- $(git rev-list --left-right --count "$_u...HEAD" 2>/dev/null)
printf 'behind=%s\nahead=%s\n' "${1:-}" "${2:-}"
@BASE@
printf 'wsbase=%s\n' "${_b:-?}"
'''


def clean(text):
    return ANSI.sub("", str(text)).replace("\r", "")


def bump(worst, code):
    try:
        code = int(str(code).strip())
    except ValueError:
        code = 4
    return max(worst, code if 0 <= code <= 4 else 4)


def rec(kind, **fields):
    return dict(kind=kind, **{k: clean(v) if isinstance(v, str) else v for k, v in fields.items() if v not in (None, "")})


def note(r, text, level="info"):
    r.setdefault("notes", []).append({"level": level, "text": clean(text)})


# A verdict's note: (level, text) with {kind} {name} {log} {age} {abort} {pid} {ask} {exit} filled in.
NOTES = {"running": ("info", "alive: {progress} (last output {age}s ago)"),
         "silent": ("warn", "no log output for {age}s -- busy, since nothing here says it stopped. Follow it:  tail -f {log}"),
         "died": ("warn", "{kind} '{name}' died without recording an exit -- its log is {log}"),
         "unreadable": ("warn", "this record cannot be read ({bad}), so nothing says whether its job runs: {path}"),
         "unanswered": ("warn", "'{name}' did not say within {ask}s whether pid {pid} lives:  wk enter {name}"),
         "failed": ("warn", "exit {exit}"),
         "stalled": ("warn", "killed after no output; see {log}"),
         "gave-up": ("warn", "{kind} gave up -- see {log}"),
         "error": ("warn", "{kind} gave up -- see {log}"),
         "oom": ("warn", "killed for memory; see {log}")}


def task_records(records, only=None, clock=None):
    """One record per task that is running or ended badly; one that ended as asked left its product as the report."""
    clock = clock or records.clock
    out, worst = [], 0
    for t in records.list():
        if only and t.field("name") != only:
            continue
        st = t.verdict("capped")
        if st in ENDED_AS_ASKED:
            continue
        kind, name, log = t.field("kind"), t.field("name"), t.field("log")
        age, abort = record.log_age(log, clock, t.machine), t.field("abort_after")
        r = rec("task", machine=t.field("machine"), task=t.id, task_kind=kind, name=name, state=st, since=t.field("started"),
                steps=[s for _, s in t.steps()], plan=t.plan(), kill=t.field("kill"), subject=t.field("subject"), log=log,
                holds="(unreadable)" if t.holds() is None else " ".join(t.holds()), exit=t.field("exit"),
                log_age=str(age) if st == "silent" and age is not None else None)
        code = TASK_EXIT.get(st, 0)
        if st == "silent" and age is not None and abort.isdigit() and age > int(abort):
            note(r, "silent for %ss, past the %ss its watchdog was to kill it at: the watchdog is gone too. Re-run the %s"
                 % (age, abort, kind), "warn")
            code = 4
        elif st in NOTES:
            level, text = NOTES[st]
            note(r, text.format(kind=kind, name=name, log=log, age="?" if age is None else age, pid=t.field("pid"),
                                ask=record.ASK_SECONDS, exit=t.field("exit"), bad=", ".join(t.unreadable()), path=t.path,
                                progress=record.progress_line(log, t.machine) if st == "running" else ""), level)
        if st == "failed":
            for line in record.first_error(log, t.machine):
                note(r, "  " + line)
        worst = bump(worst, code)
        out.append(r)
    return out, worst


def wait(poll, timeout, clock, label, info, warn):
    """Polls while `poll` says busy (2); another answer ends the wait only once the next poll repeats it, since one walk
    whose probe failed reads 4 of a build still running. `timeout` seconds of elapsed time (0: none) end it unanswered."""
    seen = {"said": False, "rc": None, "pending": None}

    def idle():
        rc = seen["rc"] = poll()
        if rc != 2 and (not seen["said"] or rc == seen["pending"]):
            return True
        seen["pending"] = None if rc == 2 else rc
        if not seen["said"]:
            info("waiting for%s to finish (wk status says busy)" % (" '%s'" % label if label else ""))
            seen["said"] = True
        return False

    if not clock.wait_until(idle, timeout or None, WAIT_SECONDS) and seen["rc"] == 2:
        warn("still busy after %ds -- the work continues; this only stopped waiting" % timeout)
    return seen["rc"]


class Walk:
    """One `wk status`: a job per place this machine lists."""

    def __init__(self, root, name=None, env=None, clock=None, reg=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.clock = clock or Clock()
        self.reg = reg or places.Registry(self.root, env=self.env)
        self.name = name
        self.this_machine = record.row_label(self.env) or record.machine_name(self.env)
        self.is_self = record.host_self(self.env) or not record.row_label(self.env)
        self.lock = threading.Lock()
        self.tasks_said, self.bases, self._loaded = set(), {}, {}
        self.worst = 0

    def places(self):
        return sorted(self.reg.walk(), key=lambda t: (RANK.get(self.reg.kind(t), 3), t))

    def driver(self, name):
        """The one driver object per place for this walk, so a machine is probed at most once."""
        with self.lock:
            if name not in self._loaded:
                self._loaded[name] = self.reg.load(name)
            return self._loaded[name]

    def machine_of(self, driver):
        return driver.name if driver.kind == "remote" and not driver.is_local and driver.host else self.this_machine

    def _job(self, tname):
        def run():
            try:
                driver = self.driver(tname)
            except LookupError as e:
                return [rec("raw", machine=self.this_machine, text=str(e))], 4
            try:
                return self.report(driver)
            except Exception as e:
                return [rec("raw", machine=self.machine_of(driver), text="%s: %s: %s" % (tname, type(e).__name__, e))], 4
        return run

    def records(self, markers=True):
        """The stream: a plan, each job's records as it ends and a flush for it, then the exit; `markers=False` is the batch in start order."""
        names = [dispatch_place(self.env) or self.reg.ws_place(self.name)] if self.name else self.places()
        jobs = [(t, self._job(t)) for t in names]
        if markers:
            plan = []
            for t, _ in jobs:
                try:
                    plan.append({"job": t, "machine": self.machine_of(self.driver(t))})
                except LookupError:
                    plan.append({"job": t, "machine": self.this_machine})
            yield {"kind": "plan", "jobs": plan}
        worst = 0
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            job_of = {pool.submit(fn): job for job, fn in jobs}
            for f in as_completed(job_of) if markers else job_of:
                recs, w = f.result()
                worst = bump(worst, w)
                yield from recs
                if markers:
                    yield {"kind": "flush", "job": job_of[f]}
        self.worst = worst
        yield {"kind": "exit", "code": worst}

    def worst_only(self):
        for _ in self.records(markers=False):
            pass
        return self.worst

    def report(self, driver):
        gm = self.machine_of(driver)
        out = [rec("machine", name=gm, self=gm == self.this_machine and self.is_self or None)]
        a = fleet.ask(driver, ["status", "--records"] + ([self.name] if self.name else []), dict(os.environ, WK_ROW_LABEL=gm))
        if a.rc is not None:
            return out + list(statusview.strip_markers(statusview.records_from_lines(clean(a.out).splitlines()))), bump(0, a.rc)
        if a.why and a.side != "no-wk":   # a box with no wk of its own is read from here
            return out + [rec("raw", machine=gm, text="%s: %s" % (driver.name, a.why))], 4 if a.side == "unreachable" else 0
        records, worst = driver.records(self.clock), 0
        names = [self.name] if self.name else driver.workspaces()
        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda ws: self.workspace(driver, gm, ws, records), names))
        for r, w in rows:
            out.append(r)
            worst = bump(worst, w)
        recs, w = self.tasks(records)
        return out + recs, bump(worst, w)

    def current_base(self, driver):
        with self.lock:
            if driver.name not in self.bases:
                self.bases[driver.name] = Snapshots(driver.store, driver.store_machine).current()
            return self.bases[driver.name]

    def workspace(self, driver, gm, ws, records):
        info = driver.info(ws)
        st = driver.state(ws, info=info)
        r = rec("workspace", machine=gm, method=METHOD.get(driver.kind, "native"), name=ws, state=info if st == "present" else st,
                ws=st, branch=driver.branch(ws))
        worst, probe, repo = 0, {}, driver.repo(ws)
        if st == "present":
            script = WS_PROBE.replace("@SRC@", shlex.quote(driver.src(ws))).replace("@BASE@", places.upstream_line_body())
            probe = kv(driver.exec(ws, ["sh", "-c", script]).out)
            origin = probe.get("origin", "")
            if origin and origin != repo.origin(driver.here, self.root):
                note(r, "origin is %s, not upstream -- 'wk sync %s --fix'" % (origin, ws), "warn")
            r.update((f, probe[f]) for f in ("dirty", "untracked", "unpushed", "upstream", "behind", "ahead") if probe.get(f, "0") != "0")
        base = (places.image_base(self.root, ws) or (repo.name if not repo.snapshot else None)
                or (probe.get("wsbase") if probe.get("wsbase") != "?" else None))
        if base:
            r["base"] = base
        snap = driver.store.ws_snapshot_id(ws)
        if snap:
            r["snap"] = snap
            cur = self.current_base(driver)
            if cur and cur != snap:
                r["snap_behind"] = len([b for b in Snapshots(driver.store, driver.store_machine).ids() if b > snap])
        new = records.find("new", ws) if st == "creating" else None
        if st == "creating" and new and new.alive(None):
            note(r, "being created right now -- its 'new' task says how far it is")
            worst = 2
        elif st == "creating":
            note(r, "creation never finished, and nothing is creating it now: %s  (or --force on a complete checkout)"
                 % driver.remake_hint(ws), "warn")
            worst = 4
        elif st == "broken":
            note(r, "the record says a %s workspace and the machine has none: something outside wk removed it.  wk rm %s"
                 % (driver.name, ws), "warn")
            if driver.store_machine.isdir(driver.store.ws_dir(ws)):
                note(r, "what is left of it here: %s" % driver.store.ws_dir(ws))
            worst = 4
        elif st == "unreachable":
            note(r, "the machine behind '%s' did not answer, so nothing about the workspace was checked" % driver.name, "warn")
            worst = 4
        build = records.find("build", ws)
        r["subs"] = [{"kind": "build", "state": build.verdict("capped"), "preset": build.field("preset")}] if build else []
        return r, worst

    def tasks(self, records):
        with self.lock:
            if str(records.root) in self.tasks_said:
                return [], 0
            self.tasks_said.add(str(records.root))
        return task_records(records, self.name, self.clock)
