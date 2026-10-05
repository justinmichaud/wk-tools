"""`wk bench`'s verbs that are not the pipeline -- ls, report, export, compare, precision, seed, deploy, ab, plans -- over one registry."""

import contextlib
import io
import os
import tempfile
import zipfile

from wk import act, record as wkrecord
from wk.bench import ab, board, record, report, seed
from wk.lock import Lock, holder_pid
from wk.machine import Local, Planted, matches

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

    def lock_alive(self, path):
        pid = holder_pid(path)
        return pid is not None and self.machine.alive(pid)

    def plans(self):
        """`plans` runs before any workspace exists to read a plan from, so it asks the mirror: one `git ls-tree`, no export."""
        mirror = self.reg.store.mirror()
        ref = board.runner_ref(self.reg.env)
        if not self.machine.isdir(mirror) or not self.machine.run(
                ["git", "-C", mirror, "rev-parse", "--verify", "--quiet", ref + "^{commit}"]).ok:
            print("no mirror at %s to read plans from; 'wk sync' fetches one, or read\n"
                  "them from a workspace's own checkout: Tools/Scripts/run-benchmark --list-plans" % mirror)
            return 1
        r = self.machine.run(["git", "-C", mirror, "ls-tree", "--name-only", ref, "Tools/Scripts/%s/" % seed.PLANS])
        print("\n".join(sorted(os.path.basename(p)[:-5] for p in r.out.splitlines() if p.endswith(".plan"))))
        return 0

    def listing(self, warn=act.warn):
        label = wkrecord.row_label(self.reg.env) or wkrecord.machine_name(self.reg.env)
        return record.Listing(self.reg, self.reg.store, self.lock_alive, label, warn)

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

    def runs(self, spec, which, stack):
        """A staged copy of each run directory, here or on the machine that holds it (the path `wk bench ls` printed there)."""
        out = []
        for one in (x for x in spec.split(",") if x):
            if "/bench/" in one and "/runs/" in one:
                task, rest = one.split("/bench/", 1)[1].split("/")[0], "runs/" + one.split("/runs/", 1)[1]
            else:
                task, _, rest = one.partition("/")
            m, d = Local(), one.rstrip("/")
            hit = None if os.path.isdir(one) else self.find(task)
            if hit:
                m, d = hit[0], os.path.join(hit[1], rest)
            if "/runs/" in d:
                taskdir = d.rpartition("/runs/")[0]
                anchor = os.path.dirname(os.path.dirname(taskdir))
            else:
                anchor = os.path.dirname(d)
            copy = os.path.join(stack.enter_context(tempfile.TemporaryDirectory(prefix="wk-run-")), os.path.basename(d))
            os.makedirs(copy)
            try:
                files = m.read_tree(anchor, os.path.relpath(d, anchor), ("*.json",), depth=1)
            except Planted as e:
                refuse_planted(e)
            except OSError:
                files = {}
            for rel, data in files.items():
                with open(os.path.join(copy, rel), "wb") as f:
                    f.write(data)
            if "result.json" not in files:
                act.die("no such run in %s: %s (a run directory, from 'wk bench ls')" % (which, one))
            out.append(copy)
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
        with contextlib.ExitStack() as stack:
            a, b = self.runs(positional[0], "-a", stack), self.runs(positional[1], "-b", stack)
            for r in (a[0], b[0]):
                if str(record.get_nested(record.load(os.path.join(r, "env.json")), "count")) == "1":
                    act.warn("run '%s' has count=1: no p-value can be computed" % os.path.basename(r))
                    act.log("  re-run with --count 2 or more for a comparison with statistics")
            report.two_runs(a, b, html=html, text=text, machine=self.machine)
        return 0

    def home(self, task):
        d = record.homes(self.reg.store).get(task)
        if not d:
            seen = [l for l in self.listing(warn=lambda _msg: None).rows() if task in l][:3]
            act.die("no such task '%s' on this machine.\n%s\n"
                    "    A task stays on the machine that took it ('wk bench ls' names it in [] at\n"
                    "    the end of each line); run this there. Two run directories compare any two\n"
                    "    runs without a task at all." % (task, "\n".join("    " + l for l in seen)))
        return d

    def running(self, task):
        return self.lock_alive(self.reg.store.lock_path("bench-task-" + task))

    def task_report(self, task, html, text):
        m, d, here = self.locate(task)
        if html and html is not True:
            act.die("a task's html reports are named for it (report-<device>-<plan>.html, in %s); "
                    "--html takes no file here" % d)
        running = here and self.running(task)
        with self.staged(m, d, task, record.LIVE if running else record.MEASURED) as copy:
            report.task_report(copy, running, html=bool(html), text=text, shown=d)
            for name in sorted(os.listdir(copy)) if html else ():
                if name.startswith("report-") and name.endswith(".html"):
                    with open(os.path.join(copy, name)) as f:
                        m.write(os.path.join(d, name), f.read())
        return 0

    @contextlib.contextmanager
    def staged(self, m, d, task, patterns=record.MEASURED):
        """A copy here of a task, read through its machine's own reads: nothing reads a workspace's tasks in place."""
        try:
            files = record.gather(m, d, patterns)
        except Planted as e:
            refuse_planted(e)
        with tempfile.TemporaryDirectory(prefix="wk-export-") as stage:
            copy = os.path.join(stage, task)
            os.makedirs(copy)
            for rel, data in files.items():
                os.makedirs(os.path.dirname(os.path.join(copy, rel)), exist_ok=True)
                with open(os.path.join(copy, rel), "wb") as f:
                    f.write(data)
            yield copy

    def locate(self, task):
        """(machine, directory, whether this machine holds its lock): this store first, then each target's store of its own."""
        hit = self.find(task)
        if hit:
            return hit[0], hit[1], hit[0] is self.machine
        return None, self.home(task), False

    def find(self, task):
        """(machine, directory) of the task, or None."""
        d = record.homes_at(self.machine, self.reg.store.record_dir()).get(task)
        if d:
            return self.machine, d
        for name in self.reg.walk():
            try:
                t = self.reg.load(name)
            except LookupError:
                continue
            if t.probe()[0] != "answering" or not t.task_store():
                continue
            m, root = t.task_store()
            far = record.homes_at(m, root).get(task)
            if far:
                return m, far
        return None

    def export(self, task, to):
        if not task:
            act.die("usage: wk bench export <task> [--to <dir>]; see wk bench -h")
        m, d, here = self.locate(task)
        with self.staged(m, d, task) as copy:
            running = here and self.running(task)
            st = record.task_state(copy, running)
            if st["state"] != "complete":
                act.barrier("task %s is %s, so its report is partial: %s" % (task, st["state"], st["summary"]))
            dest = os.path.join(to or os.path.join(self.reg.store.home(), "Downloads"), task + ".zip")
            if self.machine.exists(dest):
                if not act.confirm("replace %s?" % dest):
                    act.die("not exported")
            else:
                act.nothing_to_ask()
            self.machine.mkdir(os.path.dirname(dest))
            m.write(os.path.join(d, record.EXPORT_RECORD), dest + "\n")
            self.machine.write(dest, b"" if act.dry_run() else archive(copy, running, d))
        print(dest)
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
            target.wait_ready(ws, self.clock)

        def read(path):
            r = target.exec(ws, ["cat", "%s/Tools/Scripts/%s" % (target.src(ws), path)])
            return r.out.replace("\r", "") if r.ok else None

        text = seed.plan_json(read, plan)
        lock = Lock(self.reg.store, self.machine, self.clock)
        print(seed.Seeder(self.machine, lock, os.path.join(self.reg.store.artifact_dir(), "bench"), self.reg.store.mirror()).seed(plan, text))
        return 0

    def deploy(self, ws, board_name, slot_name, machine=None, driver=None):
        if not ws or not board_name:
            act.die("usage: wk bench deploy <workspace> <board> [--slot <name>]; see wk bench -h")
        if self.reg.in_workspace():
            return board.request(self.root, self.reg, "stage", ["machine=" + board_name, "workspace=" + ws, "slot=" + slot_name],
                                 "wk bench deploy %s %s --slot %s" % (ws, board_name, slot_name))
        board.require_board(self.root, self.reg.env, board_name)
        held, rc = board.claim(self.root, self.reg.env, board_name, "deploy %s:%s to %s" % (ws, slot_name, board_name)), 1
        try:
            board.for_board(self.root, self.reg, ws, self.clock, board_name, machine=machine, driver=driver).deploy_slot(slot_name)
            rc = 0
        except act.Refused as e:
            rc = e.status
            raise
        finally:
            if held is not None:
                held.end(rc)
        return 0

    def ab(self, spec, o, kill):
        return ab.run(self.root, self.reg, self.clock, spec, o, kill)

    def mac(self):
        act.die("'wk bench mac' is gone -- the Mac's round trip is:\n    wk bench ab --devices <mac> --systems <a>,<b> --workspace <ws>")

    def mac_ab(self):
        act.die("'wk bench mac-ab' is gone -- a Mac A/B is read back where it is planted:\n"
                "    wk bench ab --devices <mac> --preflight|--progress|--status|--collect")

    def ab_summary(self, runs, root, out):
        if not runs or not os.path.isfile(runs):
            act.die("no run map at '%s' -- the A/B recorded nothing (--runs <runs.tsv>)" % runs)
        return report.ab_summary(runs, root, self.clock.iso(), out, machine=self.machine)

    def mac_volume(self):
        act.die("'wk bench mac-volume' does not exist -- the benchmark install is an image, built on the Mac:\n"
                "    wk sysimage build perf-macos-tolken [--create|--fetch|--install|--provision|--repair|--build-pkg|--all]")


def refuse_planted(e):
    act.die("%s.\n    A workspace can write its own tasks, and a link there could hand this host's files to whoever the\n"
            "    report goes to. Remove it (rm %s), then re-run." % (e, e.path))


def archive(taskdir, running, shown=None):
    """The zip's bytes: the report, as text and as html, then EXPORTED, all under the task's name; `shown` is where the task is."""
    name, text = os.path.basename(taskdir), io.StringIO()
    report.task_report(taskdir, running, html=True, out=io.StringIO(), shown=shown)
    report.task_report(taskdir, running, text=True, out=text, shown=shown)
    try:
        tree = Local().read_tree(*record.guarded(taskdir), record.EXPORTED)
    except Planted as e:
        refuse_planted(e)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name + "/report.txt", text.getvalue())
        for pattern in record.EXPORTED:
            for rel in sorted(r for r in tree if matches(r, (pattern,))):
                z.writestr(os.path.join(name, rel), tree.pop(rel))
    return buf.getvalue()
