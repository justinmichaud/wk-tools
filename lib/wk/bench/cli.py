"""`wk bench`'s verbs -- run, ls, report, export, compare, precision, seed, deploy, ab, plans -- over one registry."""

import contextlib
import io
import os
import tempfile
import zipfile

from wk import act, record as wkrecord
from wk.act import die, info
from wk.bench import ab, board, board_ab, mac_ab, pipeline, plans, record, report, seed, systems
from wk.lock import Lock, holder_pid
from wk.machine import Local, Planted, matches
from wk.store import ws_name

REPORT_USAGE = ("usage: wk bench report <task> [--html] [--text]\n"
                "       wk bench report <run-a> <run-b> [--html out.html] [--text]; see wk bench -h")


def options(args, names, **given):
    """Each named option under its name without dashes: a value as given, True for a flag given, None for either absent."""
    return dict({n[2:].replace("-", "_"): args.flag(n) or args.value(n) for n in names}, **given)


RUN = ("--count", "--browser", "--subtests", "--cores", "--browser-args", "--software", "--system", "--slot", "--ab", "--ab-systems",
       "--rounds", "--task", "--timeout", "--exclude-subtests", "--collect", "--max-rounds", "--detect", "--a-args", "--b-args",
       "--no-warmup-profile", "--jit-tiers")
STAGED = ("--id", "--plan", "--count", "--subtests", "--payload", "--profile", "--timeout", "--browser-args", "--expect-display", "--ls", "--gates")
AB = ("--devices", "--release", "--builder", "--bits", "--base", "--build-on", "--rounds", "--count", "--timeout", "--task", "--systems",
      "--slot", "--detach", "--patch", "--workspace", "--max-rounds", "--detect", "--settle", "--a-args", "--b-args", "--plant", "--rehearse",
      "--allow-network-fetch", "--preflight", "--progress", "--status", "--collect")


def where(reg, args):
    """seed runs where its workspace's store is; ls walks from where it was typed, and answers another machine's walk (--continued) from this machine's store."""
    verb = args[0] if args else ""
    if verb == "seed":
        try:
            t = reg.ws_place(args[1]) if len(args) > 1 and args[1] else ""
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
        mirror = self.reg.store.mirror_dir()
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
            unpack(files, copy)
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
            unpack(files, copy)
            yield copy

    def locate(self, task):
        """(machine, directory, whether this machine holds its lock): this store first, then each place's store of its own."""
        hit = self.find(task)
        if hit:
            return hit[0], hit[1], hit[0] is self.machine
        return None, self.home(task), False

    def find(self, task):
        """(machine, directory) of the task, or None."""
        d = record.homes_at(self.machine, self.reg.store.records_dir()).get(task)
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
            pct = float(detect)
        except ValueError:
            act.die("--detect '%s' is not a percentage (0.3 is a third of one per cent)" % detect)
        report.precision(sides[0], sides[1], pct)
        return 0

    def seed(self, ws, plan, resolved):
        """`resolved`: the dispatcher already found the workspace and waited for it (WK_NAME)."""
        if not ws or not plan:
            act.die("usage: wk bench seed <workspace> <plan>; see wk bench -h")
        try:
            tname = self.reg.ws_place(ws)
            driver = self.reg.load(tname)
        except LookupError as e:
            act.die(str(e))
        if not resolved:
            driver.wait_ready(ws, self.clock)
        print(seed.pin(self.machine, Lock(self.reg.store, self.machine, self.clock), self.reg.store, seed.ws_reader(driver, ws), plan)[1])
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
        if not kill and o.get("devices") and ab.machine_kind(self.root, self.reg.env, o["devices"]) in ("mac", "guest"):
            m = mac_ab.MacAB(self.root, self.reg, self.clock, spec, o, self)
            return m.back() if any(o.get(k) for k in mac_ab.READS) else m.go()
        return ab.run(self.root, self.reg, self.clock, spec, o, kill)

    def ab_summary(self, runs, root, out):
        if not runs or not os.path.isfile(runs):
            act.die("no run map at '%s' -- the A/B recorded nothing (--runs <runs.tsv>)" % runs)
        return report.ab_summary(runs, root, self.clock.iso(), out, machine=self.machine)


def unpack(files, into):
    for rel, data in files.items():
        os.makedirs(os.path.dirname(os.path.join(into, rel)), exist_ok=True)
        with open(os.path.join(into, rel), "wb") as f:
            f.write(data)


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


BOARD_AB_ONLY = ("exclude_subtests", "no_warmup_profile", "jit_tiers")
AB_ONLY = ("rounds",) + BOARD_AB_ONLY


def nothing_left(reg, ws, plan, task):
    """A one-run task restarted with --task: whether it already holds its run ok. An A/B restarts through its own command."""
    d = os.path.join(record.leg_home(reg, ws, task)[1], task)
    doc = record.task_doc(d)
    if len(record.task_arms(doc)[0]) != 1:
        die("task %s is an A/B; restart it with its own command:\n    %s" % (task, doc.get("restart") or doc.get("commands", ["?"])[-1]))
    if plan not in doc.get("plans", []):
        die("task %s measures %s, not %s" % (task, ", ".join(doc.get("plans", [])), plan))
    st = record.task_state(d, False)
    if st["ok"] < st["planned"]:
        return False
    info("task %s already holds its run ok (%s); nothing is left to run" % (task, st["summary"]))
    return True


def ab_report(taskdir):
    report.task_report(taskdir, False, html=True, text=True)


def run(root, reg, words, o, kill, clock):
    """`wk bench run <ws> <plan>`: the dispatcher resolved the workspace (WK_NAME) and dropped it from `words`."""
    ws, plan = ws_name(reg.env), (words[0] if words else "")
    if not ws or not (plan or kill):
        die("usage: wk bench run <workspace> <plan> [options]; see wk bench -h")
    ab = not kill and (o.get("ab") or o.get("ab_systems"))
    options = not kill and (o.get("a_args") is not None or o.get("b_args") is not None)
    if options and (ab or o.get("system")):
        die("--a-args and --b-args are an A/B of one build in this workspace; on a board the arms are\n"
            "    slots (--ab) or systems (--ab-systems)")
    alone = [k for k in (BOARD_AB_ONLY if options else () if ab else AB_ONLY) if o.get(k)]
    if alone:
        die("--%s belongs to an A/B on a board (--ab or --ab-systems)" % alone[0].replace("_", "-"))
    if options:
        return board_ab.ArgsAB(root, reg, ws, plan, o, clock, ab_report).go()
    if o.get("system") and reg.in_workspace() and (ab or o.get("collect")):
        die("an A/B or a collection on a board is not a request a workspace can make; run it on the workstation:\n"
            "    wk bench run %s %s --system %s ..." % (ws, plan, o["system"]))
    if ab:
        return board_ab.run(root, reg, ws, plan, o, clock, ab_report)
    if o.get("system") and reg.in_workspace():
        return board.request(root, reg, "run", ["machine=" + o["system"], "workspace=" + ws, "plan=" + plan, "slot=" + (o.get("slot") or ""),
                                                "count=" + (o.get("count") or "")], "wk bench run %s %s --system %s" % (ws, plan, o["system"]))
    if o.get("task") and not kill and nothing_left(reg, ws, plan, o["task"]):
        return 0
    system = systems.for_workspace(root, reg, ws, clock, o.get("system") or "")
    if o.get("collect") and system.kind != "board":
        die("--collect takes a PGO profile from a board's instrumented slot: --system <board> --slot <name>-instr")
    r = pipeline.run_class(system)(root, reg, system, clock, plans, reg.env)
    if kill:
        return r.stop()
    if o.get("task") and not act.dry_run():
        r.lock.hold("bench-task-" + o["task"], timeout=5)
    return r.go(plan, o)
