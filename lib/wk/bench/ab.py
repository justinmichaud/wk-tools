"""An interleaved A/B on one system, each arm (system, slot, args), its legs one `Run` each, played by the one rounds
engine: a board's, a workspace's argument sets, the Mac's planted autorun; `wk bench ab <change>` runs one per board."""

import os
import re
import shlex
import statistics

from wk import act, images, job, project
from wk.act import Refused, die, info, log, warn
from wk.bench import plans, record, scores
from wk.bench.pipeline import Run, task_held
from wk.boot import cli as bootcli
from wk.lock import Lock

# A planted A/B has nobody in the room to extend a run: it goes on until it resolves a third of a per cent.
DEFAULTS = {"rounds": "5", "max_rounds": "40", "detect": "0", "lost": 3}
PLANTED = dict(DEFAULTS, detect="0.3", count="2", timeout="1800", settle="90")
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
EXCLUSIONS = os.path.join("bench", "subtest-exclusions.conf")
MEASURED = ("count", "timeout", "subtests", "cores", "browser", "browser_args")
INTERRUPTED = set(job.EXIT_OF.values())


def pair(spec, what):
    a, _, b = (spec or "").partition(",")
    if not a or not b or "," in b:
        die("--%s takes two names separated by a comma, e.g. --%s base,pr" % (what, what))
    if a == b:
        die("--%s needs two different arms (got '%s' twice). Two runs of one arm measure it twice,\n"
            "    which is a repeatability check rather than an A/B -- '--count N' asks for that." % (what, a))
    return a, b


def flags(o, keys):
    return [w for k in keys if o.get(k) for w in ("--" + k.replace("_", "-"), o[k])]


def refuse(o, keys, why):
    given = flags(o, keys)
    if given:
        die(why % given[0])


def check_plan(o, default_plans, defaults=DEFAULTS):
    """(rounds, max_rounds, detect, plans); --detect 0 runs --rounds exactly."""
    rounds = o.get("rounds") or defaults["rounds"]
    if not rounds.isdigit() or int(rounds) < 1:
        die("--rounds takes a number of at least 1 (got '%s')" % rounds)
    for key, what in (("count", "a number"), ("timeout", "seconds"), ("settle", "a number")):
        if o.get(key) and not o[key].isdigit():
            die("--%s takes %s (got '%s')" % (key, what, o[key]))
    pct = o.get("detect") or defaults["detect"]
    try:
        goal = float(pct)
    except ValueError:
        goal = -1
    if goal < 0:
        die("--detect '%s' is not a percentage (0.3 is a third of one per cent; 0 runs --rounds exactly)" % pct)
    top = o.get("max_rounds") or (defaults["max_rounds"] if goal else rounds)
    if not top.isdigit():
        die("--max-rounds takes a number (got '%s')" % top)
    if int(top) < int(rounds):
        die("--max-rounds %s is below --rounds %s: --rounds is the floor it alternates from" % (top, rounds))
    names = o.get("plans") or list(default_plans)
    for p in names:
        if not NAME.match(p):
            die("--plan '%s' is not a plan name (letters, digits, '.', '_' and '-')" % p)
    return int(rounds), int(top), goal, names


def duration(seconds):
    m = int(round(seconds / 60.0))
    return "%dh%02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % max(m, 1)


def leg_seconds(reg, homes, device, plan, count):
    """Seconds at `count` of each measured leg in `homes`' tasks: another count scales, the default count stands only for itself."""
    out = []
    for m, bench in (record.ws_home(reg, *h) for h in dict.fromkeys(homes)):
        for r in (r for t in record.tasks(bench, m) for r in record.task_runs(os.path.join(bench, t), m)):
            e = r["env"]
            if e.get("machine") != device or e.get("plan") != plan or r["state"] != "ok" or "wall_time_s" not in e:
                continue
            have = str(e.get("count") or "")
            if have == count:
                out.append(float(e["wall_time_s"]))
            elif have and count:
                out.append(float(e["wall_time_s"]) * int(count) / int(have))
    return out


def cost_line(reg, homes, device, plan, count, legs):
    seen = leg_seconds(reg, homes, device, plan, count)
    if not seen:
        return "%s %s: %d legs; no leg of it at this --count measured on %s yet, so its time is unknown" % (device, plan, legs, device), None
    each = statistics.median(seen)
    return "%s %s: %d legs x ~%s (median of %d measured) = ~%s" % (device, plan, legs, duration(each), len(seen), duration(legs * each)), legs * each


class Rounds:
    """Counterbalanced rounds (a fixed order puts the machine's drift on one arm) to `rounds`, or with `detect` until
    `resolved(i)`, up to `top`; a round not every arm finished is lost, and `lost` of them stop it."""

    def __init__(self, rounds, top, detect, lost=DEFAULTS["lost"]):
        self.rounds, self.top, self.detect, self.lost = rounds, top, detect, lost

    @staticmethod
    def order(i, n=2):
        return list(range(n)) if i % 2 else list(reversed(range(n)))

    def of(self):
        return "%d-%d" % (self.rounds, self.top) if self.detect else str(self.rounds)

    def play(self, leg, resolved, have=(), n=2, plans=(None,)):
        """(outcome, kept, lost); `leg(i, arm, first, plan)` is one leg's success, each plan's arms in turn; `have` the
        rounds recorded already."""
        kept = lost = 0
        for i in range(1, (self.top if self.detect else self.rounds) + 1):
            if i in have:
                log("round %d/%s -- recorded already" % (i, self.of()))
                done = True
            else:
                arms = self.order(i, n)
                done = all([leg(i, a, a == arms[0], p) for p in plans for a in arms])
            if done:
                kept += 1
            else:
                warn("round %d did not finish on every arm -- the report drops it" % i)
                lost += 1
                if lost >= self.lost:
                    warn("%d rounds lost; stopping early" % lost)
                    return "lost-at-round-%d" % i, kept, lost
            if self.detect and i >= self.rounds and resolved(i):
                log("  resolved    %g%% at round %d" % (self.detect, i))
                return "resolved-at-round-%d" % i, kept, lost
        if self.detect:
            warn("--max-rounds %d reached without resolving %g%%; the report says what these rounds do resolve" % (self.top, self.detect))
            return "hit-max-rounds", kept, lost
        return "rounds-done", kept, lost


class Task:

    def __init__(self, root, reg, clock, o):
        self.root, self.reg, self.clock, self.o = str(root), reg, clock, dict(o)
        self.here, self.env = reg.machine, reg.env
        self.lock = Lock(reg.store, reg.machine, clock)
        self.task = self.o.get("task") or ""

    def shape(self, devices, names, slots):
        return {"devices": devices, "plans": ",".join(names), "rounds": "%d" % self.rounds, "slots": ",".join(dict.fromkeys(slots))}

    def new_task(self, home, task, fields, command):
        self.task = task
        return record.new_task(*home, task, self.lock, self.clock.iso(), fields, command)

    def guarded(self, body, end):
        rc = 1
        try:
            with job.Signals():
                rc = body()
            return rc
        except job.Interrupted as e:
            rc = "cancelled"
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        except Refused as e:
            rc = e.status
            raise
        finally:
            end(rc)
            self.lock.release_all()

    def reported(self, report, task):
        try:
            report()
        except (Refused, SystemExit, OSError, ValueError) as e:
            warn("the report did not complete (%s); the runs are recorded:  wk bench report %s" % (e, task))


def subtests(root, plan, plan_doc, bits, asked, exclude):
    """(subtests, excluded): a subtest one arm's width cannot run is dropped from both (bench/subtest-exclusions.conf);
    --subtests overrides."""
    rows = []
    with open(os.path.join(root, EXCLUSIONS)) as f:
        for line in f:
            w = line.split(None, 3)
            if len(w) >= 3 and not w[0].startswith("#") and w[0] == plan and w[1] == str(bits):
                rows.append((w[2], w[3].strip() if len(w) > 3 else ""))
    for name, why in rows:
        log("  excluded    %s -- %s" % (name, why))
    dropped = [n for n, _ in rows] + [x for x in exclude.split(",") if x]
    if asked:
        return " ".join(asked.replace(",", " ").split()), ",".join(dropped)
    if not dropped:
        return "", ""
    listed = [s for group in (plan_doc.get("subtests") or {}).values() for s in group]
    unknown = sorted(set(dropped) - set(listed))
    if unknown:
        die("%s names no subtest of this plan (bench/subtest-exclusions.conf or --exclude-subtests)" % ", ".join(unknown))
    keep = [s for s in listed if s not in dropped]
    if not keep:
        die("every subtest of this plan is excluded")
    log("  subtests    %d of the plan's own list; %s dropped" % (len(keep), ",".join(dropped)))
    return " ".join(keep), ",".join(dropped)


class Arm:
    def __init__(self, label, system, slot="", args="", ident=""):
        self.label, self.system, self.slot, self.args, self.ident = label, system, slot, args, ident


class AB(Task):
    """`kind` names what the arms differ in: slot, system or options. A system arm boots its system before each leg and
    discards a settle run after the boot; a system with a warmup round runs one first, on the first plan, and judges it.
    Each round runs every plan's legs."""

    def __init__(self, root, reg, clock, ws, names, o, kind, arms, reporter, defaults=DEFAULTS):
        super().__init__(root, reg, clock, o)
        self.ws, self.kind, self.arms, self.reporter = ws, kind, arms, reporter
        self.rounds, top, detect, self.plans = check_plan(o, names, defaults)
        self.engine, self.outcome = Rounds(self.rounds, top, detect), ""
        self.system = arms[0].system
        self.labels = tuple(a.label for a in arms)
        m, bench = self.system.tasks()
        if self.task and not m.exists(os.path.join(bench, self.task, "task.json")):
            die("no such task '%s' in %s; 'wk bench ls' lists the tasks and where each is" % (self.task, bench))
        self.taskdir, self.owned = (os.path.join(bench, self.task) if self.task else ""), False
        self.env = dict(reg.env, WK_DEVICE_HELD="device:%s" % self.system.machine_name) if self.system.machine_name else dict(reg.env)

    def leg(self, plan, o):
        try:
            return Run(self.system, plans, dict(self.env, WK_TASK_HELD=self.task)).go(plan, dict(self.base, subtests=self.subtests[plan], **o)) == 0
        except Refused as e:
            if e.status in INTERRUPTED:
                raise
            return False

    def booter(self):
        return bootcli.Boot(self.root, self.system.driver.conf, self.system.driver, self.env)

    def arm_leg(self, i, plan, o):
        arm = self.arms[i]
        o = dict(o, slot=arm.slot, arm_args=arm.args, arm="ab"[i])
        if not arm.ident:
            return self.leg(plan, o)
        if not self.booter().enter(arm.ident, self.clock):
            if act.dry_run():
                log("dry run -- the legs on %s would run once it answers" % arm.ident)
            else:
                warn("%s would not come up as %s in %d attempts; the leg is lost" % (self.system.machine_name, arm.ident, bootcli.TRIES))
            return False
        # every leg is a fresh boot, and a first run after one is not like the rest
        if not o.get("warmup") and not self.leg(plan, dict(o, expect=arm.ident, settle="1")):
            warn("the settle run for this leg did not complete")
        return self.leg(plan, dict(o, expect=arm.ident))

    def begin(self):
        s, (a, b) = self.system, self.labels
        bits = s.width([arm.ident for arm in self.arms if arm.ident] or [""])
        asked, exclude = self.o.get("subtests") or "", self.o.get("exclude_subtests") or ""
        if exclude and not bits:
            die("--exclude-subtests belongs to an A/B on a board, whose width decides what is dropped; here --subtests names what runs")
        self.subtests, dropped = {}, ""
        for p in self.plans:
            self.subtests[p], dropped = subtests(self.root, p, s.plan_doc(p, plans, self.lock), bits, asked, exclude) if bits else (asked, "")
        self.base = {k: self.o.get(k) or "" for k in ("count", "timeout", "cores", "no_warmup_profile", "jit_tiers", "software",
                                                       "browser", "browser_args", "preset")}
        self.base.update(excluded=dropped, slot_a=a, slot_b=b, task=self.task)
        if self.task or act.dry_run():
            if self.task and not act.dry_run() and not task_held(self.env, self.task):
                self.lock.hold("bench-task-" + self.task, timeout=5)
            return
        given = flags(self.o, MEASURED) + (["--software"] if self.o.get("software") else [])
        preset = self.base["preset"] or project.get("BENCH_PRESET")
        what = {"slot": "--ab %s,%s" % (a, b), "system": "--ab-systems %s,%s --slot %s" % (a, b, self.arms[0].slot),
                "options": "--preset %s --a-args %s --b-args %s" % (preset, shlex.quote(self.arms[0].args), shlex.quote(self.arms[1].args))}[self.kind]
        command = "wk bench run %s %s%s %s --rounds %d%s%s" % (
            self.ws, self.plans[0], " --system " + s.machine_name if s.machine_name else "", what, self.rounds,
            " --max-rounds %d --detect %g" % (self.engine.top, self.engine.detect) if self.engine.detect else "",
            "".join(" " + shlex.quote(w) for w in given))
        name = {"slot": "%s-%s-vs-%s" % (s.machine_name, a, b), "system": "%s-systems" % s.machine_name, "options": "%s-options" % self.ws}[self.kind]
        subject = {"subject.kind": "options", "subject.a": self.arms[0].args, "subject.b": self.arms[1].args} if self.kind == "options" else \
            {"subject.kind": self.kind + "s", "subject.spec": "%s,%s" % (a, b)}
        device = s.device(self.arms[0], self.kind) or "%s=%s" % (self.ws, preset)
        fields = dict(subject, **self.shape(device, self.plans, [arm.slot or self.ws for arm in self.arms]))
        fields.update({k: self.o[k] for k in ("count", "timeout") if self.o.get(k)})
        self.taskdir = self.new_task(s.tasks(), "%s-%s" % (self.clock.stamp(), name), fields, command)
        self.base["task"], self.owned = self.task, True

    def warmup(self):
        """Round 0, thrown away: what the rounds cannot read -- the GL driver, the width, the JIT tier -- and the profile."""
        log("")
        log("warmup round -- not measured; it establishes what the arms are and profiles them")
        for i in (0, 1):
            if not self.arm_leg(i, self.plans[0], {"warmup": "1", "round": "0"}):
                warn("the warmup leg for %s did not complete" % "AB"[i])
        if act.dry_run():
            return
        problems = scores.warmup_check(*scores.evidence_paths(self.taskdir, self.system.machine_name), self.kind != "system") \
            if self.system.evidence else []
        if problems:
            act.barrier("the warmup round says these two arms are not what the A/B claims:\n" + "\n".join("    " + p for p in problems))
            return
        log("  warmup      both arms confirmed; evidence in %s" % os.path.join(self.taskdir, "warmup"))

    def byround(self, plan):
        doc = record.task_doc(self.taskdir)
        return record.task_rounds(doc, record.task_runs(self.taskdir)).get((self.system.machine_name or self.ws, plan), {})

    def resolved(self, i):
        return all(scores.resolved(*record.paired(self.byround(p), self.labels)[:2], self.engine.detect) for p in self.plans)

    def measured(self):
        have = set.intersection(*[{rnd for rnd, arms in self.byround(p).items() if all(arms.get(x, {}).get("state") == "ok" for x in "ab")}
                                  for p in self.plans])
        self.outcome, kept, lost = self.engine.play(self.round_leg, self.resolved, have, plans=self.plans)
        return kept, lost

    def round_leg(self, i, a, first, plan):
        if first:
            log("")
        log("round %d/%s -- %s%s %s%s" % (i, self.engine.of(), plan + ", " if len(self.plans) > 1 else "", self.kind, self.labels[a],
                                          " (first)" if first else ""))
        return self.arm_leg(a, plan, {"round": str(i)})

    def go(self):
        held = self.system.claim(self.env, self.ws, "wk bench run %s --kill%s" % (self.ws, " --system " + self.system.machine_name
                                                                                   if self.system.machine_name else ""),
                                 ["%s on %s, %s" % (" ".join(self.plans), self.system.machine_name or self.ws, " vs ".join(self.labels))])
        return self.guarded(self.body, lambda rc: held is not None and held.end(rc))

    def body(self):
        for arm in self.arms:
            if not arm.ident and arm.slot:
                self.system.manifest(arm.slot)
        self.begin()
        if self.task:
            log("  task        %s (%s)" % (self.task, self.taskdir))
        n, warm = self.rounds, self.system.warmup
        log("interleaving %d round(s) of %s vs %s%s" % (n, self.labels[0], self.labels[1], " after a warmup round" if warm else ""))
        if warm:
            self.warmup()
        if act.dry_run():
            for i in ([] if warm else Rounds.order(1)):
                self.round_leg(1, i, i == Rounds.order(1)[0], self.plans[0])
            info("dry run -- then %d round(s), each arm once per round, the lead alternating" % n)
            return 0
        kept, lost = self.measured()
        if self.kind == "system":
            self.booter().release(self.clock)
        log("")
        log("%d round(s) usable, %d dropped; the runs are in %s/runs" % (kept, lost, self.taskdir))
        self.lock.release_all()
        if not self.owned:
            log("  report:  wk bench report %s" % self.task)
        else:
            self.reported(lambda: self.reporter(self.taskdir), self.task)
        if not kept:
            die("no round produced both halves on %s; nothing to compare." % (self.system.machine_name or self.ws))
        return 0


def run(root, reg, clock, ws, plan, o, system, reporter):
    if o.get("collect"):
        die("--collect takes a profile from one instrumented slot, so it is neither arm of a comparison")
    if sum(1 for k in ("ab", "ab_systems", "a_args") if o.get(k) is not None and o.get(k) is not False) > 1 or (
            o.get("b_args") is not None and (o.get("ab") or o.get("ab_systems"))):
        die("--ab compares two slots in one booted system, --ab-systems two systems, --a-args/--b-args two argument sets of one\n"
            "    build: different comparisons; pick one.")
    if o.get("a_args") is not None or o.get("b_args") is not None:
        a, b = o.get("a_args") or "", o.get("b_args") or ""
        if not system.local:
            die("--a-args and --b-args are an A/B of one build in this workspace or on a Mac's bench volume; on a board the arms\n"
                "    are slots (--ab) or systems (--ab-systems)")
        if a == b:
            die("--a-args and --b-args are the same ('%s'). Two runs of one arm measure it twice,\n"
                "    which is a repeatability check rather than an A/B -- '--count N' asks for that." % a)
        arms = [Arm(a or "(none)", system, slot=o.get("slot") or "", args=a), Arm(b or "(none)", system, slot=o.get("slot") or "", args=b)]
        return AB(root, reg, clock, ws, [plan], o, "options", arms, reporter).go()
    if not system.machine_name:
        die("--%s is an A/B on one board: name it with --system <board>" % ("ab-systems" if o.get("ab_systems") else "ab"))
    if o.get("ab_systems"):
        a, b = pair(o["ab_systems"], "ab-systems")
        slot = o.get("slot") or "a"
        images.check_slot_name(slot)
        return AB(root, reg, clock, ws, [plan], o, "system", [Arm(a, system, slot, ident=a), Arm(b, system, slot, ident=b)], reporter).go()
    if o.get("slot"):
        die("--slot names the one slot a system A/B holds fixed; a slot A/B names its two slots in --ab")
    a, b = pair(o["ab"], "ab")
    for s in (a, b):
        images.check_slot_name(s)
    return AB(root, reg, clock, ws, [plan], o, "slot", [Arm(a, system, a), Arm(b, system, b)], reporter).go()
