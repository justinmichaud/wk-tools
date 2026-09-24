"""`wk bench`'s verbs that are Python -- ls, report, compare, precision, seed -- over one registry; the rest are lib/bench-arms.sh."""

import os

from wk import act, record as wkrecord, shell
from wk.bench import record, report, seed
from wk.lock import Lock, holder_pid

REPORT_USAGE = ("usage: wk bench report <task> [--html] [--text]\n"
                "       wk bench report <run-a> <run-b> [--html out.html] [--text]; see wk bench -h")


def where(reg, args):
    """seed runs where its workspace's store is; ls walks from where it was typed, and answers another machine's walk (--continued) from this machine's store."""
    verb = args[0] if args else ""
    if verb == "seed":
        try:
            t = reg.ws_target(args[1]) if len(args) > 1 and args[1] else ""
        except LookupError:
            t = ""
        return "workspace" if t == "container" else "host"
    if verb == "ls":
        return "store" if "--continued" in args[1:] else "local"
    return "host"


class Bench:
    def __init__(self, root, reg, clock):
        self.root, self.reg, self.clock, self.machine = str(root), reg, clock, reg.machine
        self.bench_dir = reg.store.bench_dir()

    def lock_alive(self, path):
        pid = holder_pid(path)
        return pid is not None and self.machine.alive(pid)

    def listing(self, warn=act.warn):
        label = self.reg.env.get("WK_ROW_LABEL") or wkrecord.machine_name(self.reg.env)
        return record.Listing(self.reg, self.bench_dir, self.reg.store.lock_path, self.lock_alive, label, warn)

    def ls(self, continued):
        rows = self.listing().rows()
        if rows:
            print("\n".join(rows), flush=True)
        if continued:
            return 0
        if not rows:
            act.log("(no tasks on any machine this one knows)")
            return 0
        act.log("")
        act.log("  each task is on the machine that took it, named after WHERE; nothing is")
        act.log("  copied between machines, so what you see here is what that machine has")
        act.log("  now.  wk bench report <task>  reports one.")
        return 0

    def runs(self, spec, which):
        out = []
        for one in (x for x in spec.split(",") if x):
            d = one if os.path.isdir(one) else os.path.join(self.bench_dir, one)
            if not os.path.isfile(os.path.join(d, "result.json")):
                act.die("no such run in %s: %s (a run directory, from 'wk bench ls')" % (which, one))
            out.append(d)
        if not out:
            act.die("no runs given for %s" % which)
        return out

    def report(self, positional, html, text):
        """`html` is True for a bare --html, else the file it names."""
        if not positional:
            act.die(REPORT_USAGE)
        if len(positional) == 1:
            return self.task_report(positional[0], html, text)
        if len(positional) < 2 or not positional[1]:
            act.die(REPORT_USAGE)
        if html is True:
            act.die("the two-run form writes where it is told: --html out.html")
        a, b = self.runs(positional[0], "-a"), self.runs(positional[1], "-b")
        for r in (a[0], b[0]):
            if str(record.get_nested(record.load(os.path.join(r, "env.json")), "count")) == "1":
                act.warn("run '%s' has count=1: no p-value can be computed" % os.path.basename(r))
                act.log("  re-run with --count 2 or more for a comparison with statistics")
        report.two_runs(a, b, html=html, text=text)
        return 0

    def task_report(self, task, html, text):
        d = os.path.join(self.bench_dir, task)
        if not os.path.isfile(os.path.join(d, "task.json")):
            seen = [l for l in self.listing(warn=lambda _msg: None).rows() if task in l][:3]
            act.die("no such task '%s' in this machine's store (%s has no task.json).\n%s\n"
                    "    A task stays on the machine that took it ('wk bench ls' names it in [] at\n"
                    "    the end of each line); run this there. Two run directories compare any two\n"
                    "    runs without a task at all." % (task, d, "\n".join("    " + l for l in seen)))
        if html and html is not True:
            act.die("a task's html reports are named for it (report-<device>-<plan>.html, in %s); "
                    "--html takes no file here" % d)
        running = self.lock_alive(self.reg.store.lock_path("bench-task-" + task))
        report.task_report(d, running, html=bool(html), text=text)
        return 0

    def compare(self, positional, html):
        if len(positional) < 2:
            act.die("usage: wk bench compare <run-a> <run-b> [ws]; see wk bench -h")
        return self.report(positional, html, True)

    def precision(self, sides, detect):
        if len(sides) < 2:
            act.die("usage: wk bench precision <run-a> <run-b> [--detect PCT]\n"
                    "    Either side may be comma-separated run directories, pooled.")
        try:
            target = float(detect)
        except ValueError:
            act.die("--detect '%s' is not a percentage (0.3 is a third of one per cent)" % detect)
        report.precision(sides[0], sides[1], target)
        return 0

    def seed(self, ws, plan, resolved):
        """`resolved`: the dispatcher already found the workspace and waited for it (WK_NAME)."""
        if not ws or not plan:
            act.die("usage: wk bench seed <workspace> <plan>; see wk bench -h")
        try:
            tname = self.reg.ws_target(ws)
            target = self.reg.load(tname)
        except LookupError as e:
            act.die(str(e))
        if not resolved:
            rc = shell.run(self.root, "load_target %s >/dev/null 2>&1; wait_ready" % shell.sh_quote(tname), ws)
            if rc != 0:
                raise act.Refused(rc)

        def read(path):
            r = target.exec(ws, ["cat", "%s/Tools/Scripts/%s" % (target.src(ws), path)])
            return r.out.replace("\r", "") if r.ok else None

        text = seed.plan_json(read, plan)
        lock = Lock(self.reg.store, self.machine, self.clock)
        print(seed.Seeder(self.machine, lock, os.path.join(self.reg.store.artifact_dir(), "bench")).seed(plan, text))
        return 0
