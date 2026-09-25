"""`wk bench run <ws> <plan> --system <board> --ab A,B | --ab-systems A,B`: an interleaved A/B on one board, each leg a
BoardRun. An arm is (system, slot): a slot A/B holds the system fixed, a system A/B the slot, booting each leg's system."""

import json
import os
import re

from wk import act, images, job, record as progress
from wk.act import Refused, die, info, log, warn
from wk.bench import board, pipeline, record, report
from wk.boot import cli as bootcli
from wk.lock import Lock

EXCLUSIONS = os.path.join("bench", "subtest-exclusions.conf")
NARROW = ("armhf", "armv7l", "i686")
SYSTEM_TRIES = 3
WAIT, POLL, ARM_RETRY = 420, 12, 15
LOST_ROUNDS = 3
MAX_ROUNDS = 40
INTERRUPTED = set(job.EXIT_OF.values())


def width(ident, env):
    """A system id is <profile>-<hash>, and the profile declares the image's word size."""
    name = ident.rsplit("-", 1)[0] if re.search(r"-[0-9a-f]{8}", ident) else ident
    return 32 if (images.quiet_load(name, env) or {}).get("IMG_ARCH") in NARROW else 64


def exclusions(root, plan, bits):
    rows = []
    with open(os.path.join(root, EXCLUSIONS)) as f:
        for line in f:
            w = line.split(None, 3)
            if len(w) >= 3 and not w[0].startswith("#") and w[0] == plan and w[1] == str(bits):
                rows.append((w[2], w[3].strip() if len(w) > 3 else ""))
    return rows


def kept(plan_doc, drop):
    """The plan's own subtests minus `drop`, so both arms cover the same set."""
    listed = [s for group in (plan_doc.get("subtests") or {}).values() for s in group]
    unknown = sorted(set(drop) - set(listed))
    if unknown:
        die("%s names no subtest of this plan (bench/subtest-exclusions.conf or --exclude-subtests)" % ", ".join(unknown))
    keep = [s for s in listed if s not in drop]
    if not keep:
        die("every subtest of this plan is excluded")
    return keep


def subtests(root, env, plan, plan_doc, idents, asked, exclude):
    """(subtests, excluded): a subtest one arm's width cannot run is dropped from both; --subtests overrides."""
    bits = 32 if any(width(i, env) == 32 for i in idents if i) else 64
    rows = exclusions(root, plan, bits)
    for name, why in rows:
        log("  excluded    %s -- %s" % (name, why))
    dropped = [n for n, _ in rows] + [x for x in exclude.split(",") if x]
    if asked:
        return " ".join(asked.replace(",", " ").split()), ",".join(dropped)
    if not dropped:
        return "", ""
    keep = kept(plan_doc, dropped)
    log("  subtests    %d of the plan's own list; %s dropped" % (len(keep), ",".join(dropped)))
    return " ".join(keep), ",".join(dropped)


def pair(spec, what):
    a, _, b = (spec or "").partition(",")
    if not a or not b or "," in b:
        die("--%s takes two names separated by a comma, e.g. --%s base,pr" % (what, what))
    if a == b:
        die("--%s needs two different arms (got '%s' twice). Two runs of one arm measure it twice,\n"
            "    which is a repeatability check rather than an A/B -- '--count N' asks for that." % (what, a))
    return a, b


def stopping(o, rounds, detect="0"):
    """(max_rounds, detect): past --rounds the rounds go on until report.resolved says they resolve --detect percent,
    up to --max-rounds; --detect 0 runs --rounds exactly."""
    pct = o.get("detect") or detect
    try:
        target = float(pct)
    except ValueError:
        target = -1
    if target < 0:
        die("--detect '%s' is not a percentage (0.3 is a third of one per cent; 0 runs --rounds exactly)" % pct)
    top = o.get("max_rounds") or str(MAX_ROUNDS if target else rounds)
    if not top.isdigit():
        die("--max-rounds takes a number (got '%s')" % top)
    if int(top) < rounds:
        die("--max-rounds %s is below --rounds %d: --rounds is the floor it alternates from" % (top, rounds))
    return int(top), target


class AB:
    def __init__(self, root, reg, ws, plan, o, clock, popen, driver=None, machine=None):
        self.root, self.reg, self.ws, self.plan, self.o, self.clock, self.popen = str(root), reg, ws, plan, o, clock, popen
        self.name, self.here = o["system"], reg.machine
        self.systems = bool(o.get("ab_systems"))
        a, b = pair(o.get("ab_systems") or o.get("ab"), "ab-systems" if self.systems else "ab")
        slot = o.get("slot") or "a"
        self.arms = [(a, slot), (b, slot)] if self.systems else [("", a), ("", b)]
        for _, s in self.arms:
            images.check_slot_name(s)
        self.labels = (a, b)
        rounds = o.get("rounds") or "3"
        if not rounds.isdigit() or int(rounds) < 1:
            die("--rounds takes a number of at least 1 (got '%s')" % rounds)
        self.rounds = int(rounds)
        self.max_rounds, self.detect = stopping(o, self.rounds)
        self.env = dict(reg.env, WK_DEVICE_HELD="device:" + self.name)
        self.task, self.taskdir, self.owned, self.held = o.get("task") or "", "", False, None
        if self.task:
            self.taskdir = os.path.join(reg.store.bench_dir(), self.task)
            if not os.path.isfile(os.path.join(self.taskdir, "task.json")):
                die("no such task '%s' (%s has no task.json); 'wk bench ls' lists the tasks" % (self.task, self.taskdir))
        self.system = board.for_board(self.root, reg, ws, clock, self.name, machine=machine, driver=driver)
        self.recs = progress.Records(reg.store.record_dir(), clock=clock, env=reg.env, machine=self.here)
        self.lock = Lock(reg.store, self.here, clock)

    def run(self):
        return board.BoardRun(self.root, self.reg, self.system, self.clock, self.env, self.popen, name=self.ws + "-leg")

    def leg(self, o):
        """One leg; False when it produced nothing, which loses its round but not the A/B."""
        try:
            return self.run().go(self.plan, dict(self.base, **o)) == 0
        except Refused as e:
            if e.status in INTERRUPTED:
                raise
            return False

    def mode(self):
        return self.system.driver.probe()

    def answered(self, want_host=False):
        d = self.system.driver
        return self.clock.wait_until(lambda: d.probe() != "unreachable" and (d.ch.channel == "host" or not want_host), WAIT, POLL)

    def transition(self, verb, want=""):
        b = bootcli.Boot(self.root, self.system.driver.conf, self.system.driver, self.env)
        try:
            return (b.arm(want) if verb == "arm" else b.disarm() if verb == "disarm" else b.back()) == 0
        except Refused:
            return False

    def boot(self, want):
        """The board running `want`, read from the marker the running system serves; False when it will not come up so."""
        d, name = self.system.driver, self.name
        tries = 0
        while tries <= SYSTEM_TRIES:
            tries += 1
            if self.mode() == "unreachable":
                info("%s is not answering yet; waiting for it before deciding" % name)
                self.answered()
            kind, _, ident = d.mode.partition(" ")
            if kind == "bench" and ident == want:
                info("%s is running %s (verified from its own marker)" % (name, want))
                return True
            if ident:
                info("%s answers as %s (%s), not %s" % (name, ident, kind, want))
            if tries > SYSTEM_TRIES or act.dry_run():
                break
            info("%s: arming and booting system %s (attempt %d of %d)" % (name, want, tries, SYSTEM_TRIES))
            if kind == "bench" and not d.arm_from_bench:
                info("%s: it arms only from its rescue; going back to it first" % name)
                self.transition("back")
                self.answered(want_host=True)
                continue
            if not self.transition("arm", want):
                warn("could not arm %s on %s (attempt %d)" % (want, name, tries))
                self.clock.sleep(ARM_RETRY)
                continue
            self.answered()
        if act.dry_run():
            if d.mode.startswith("bench") and not d.arm_from_bench:
                self.transition("back")
                log("  then arm %s from its rescue and boot it" % want)
            else:
                self.transition("arm", want)
            log("dry run -- the legs on %s would run once it answers" % want)
            return False
        warn("%s would not come up as %s in %d attempts; the leg is lost" % (name, want, SYSTEM_TRIES))
        return False

    def arm_leg(self, arm, o):
        ident, slot = self.arms[arm]
        if not ident:
            return self.leg(dict(o, slot=slot))
        if not self.boot(ident):
            return False
        if not o.get("warmup") and not self.leg(dict(o, slot=slot, expect=ident, settle="1")):
            warn("the settle run for this leg did not complete")   # every leg is a fresh boot, and a first run after one is not like the rest
        return self.leg(dict(o, slot=slot, expect=ident))

    # -- the task, the warmup round and the rounds
    def begin(self, plan_doc):
        a, b = self.labels
        idents = [a, b] if self.systems else [self.system.driver.probe().partition(" ")[2]]
        excluded = subtests(self.root, self.reg.env, self.plan, plan_doc, idents, self.o.get("subtests") or "", self.o.get("exclude_subtests") or "")
        self.base = {k: self.o.get(k) or "" for k in ("count", "timeout", "cores", "no_warmup_profile", "jit_tiers")}
        self.base.update(subtests=excluded[0], excluded=excluded[1], slot_a=a, slot_b=b, task=self.task)
        if self.task or act.dry_run():
            return
        stamp, flag = self.clock.stamp(), "--ab-systems" if self.systems else "--ab"
        self.task = "%s-%s-systems" % (stamp, self.name) if self.systems else "%s-%s-%s-vs-%s" % (stamp, self.name, a, b)
        self.taskdir = os.path.join(self.reg.store.bench_dir(), self.task)
        if os.path.exists(self.taskdir):
            die("task %s already exists (%s); a task is one request, made once" % (self.task, self.taskdir))
        self.lock.hold("bench-task-" + self.task, timeout=5)
        device = self.name if self.systems else "%s=%s" % (self.name, self.system.manifest(a).get("profile", ""))
        extra = ["%s=%s" % (k, self.base[k]) for k in ("count", "timeout") if self.base[k]]
        command = "wk bench run %s %s --system %s %s %s%s --rounds %d%s%s" % (
            self.ws, self.plan, self.name, flag, "%s,%s" % (a, b), " --slot " + self.arms[0][1] if self.systems else "", self.rounds,
            " --max-rounds %d --detect %g" % (self.max_rounds, self.detect) if self.detect else "",
            "".join(" --%s %s" % (k, self.base[k]) for k in ("count", "timeout") if self.base[k]))
        record.task_write(self.taskdir, ["task=" + self.task, "requested=" + self.clock.iso(), "subject.kind=" + ("systems" if self.systems else "slots"),
                                         "subject.spec=%s,%s" % (a, b), "devices=" + device, "plans=" + self.plan, "rounds=%d" % self.rounds,
                                         "slots=" + ",".join(dict.fromkeys(s for _, s in self.arms))] + extra, [command])
        self.base["task"], self.owned = self.task, True

    def warmup(self):
        """Round 0, thrown away: what the rounds cannot read -- the GL driver, the width, the JIT tier -- and the profile."""
        log("")
        log("warmup round -- not measured; it establishes what the arms are and profiles them")
        for arm in (0, 1):
            if not self.arm_leg(arm, {"warmup": "1", "round": "0", "arm": "ab"[arm]}):
                warn("the warmup leg for %s did not complete" % "AB"[arm])
        if act.dry_run():
            return
        d = os.path.join(self.taskdir, "warmup")
        problems = report.warmup_check(*[os.path.join(d, "%s-%s.evidence.json" % (self.name, x)) for x in "ab"], not self.systems)
        if problems:
            act.barrier("the warmup round says these two arms are not what the A/B claims:\n" + "\n".join("    " + p for p in problems))
            return
        log("  warmup      both arms confirmed; evidence in %s" % d)

    def resolved(self):
        """The rounds this task holds for this board and plan, paired as the report pairs them."""
        doc = record.task_doc(self.taskdir)
        byround = record.task_rounds(doc, record.task_runs(self.taskdir)).get((self.name, self.plan), {})
        a_dirs, b_dirs, _ = record.paired(byround, self.labels)
        return report.resolved(a_dirs, b_dirs, self.detect)

    def measured(self):
        kept = lost = 0
        of = "%d-%d" % (self.rounds, self.max_rounds) if self.detect else str(self.rounds)
        for i in range(1, self.max_rounds + 1):
            log("")
            done = {}
            for arm in ((0, 1) if i % 2 else (1, 0)):   # counterbalanced: a round that always led with A would put every round's drift on B
                log("round %d/%s -- %s %s%s" % (i, of, "system" if self.systems else "slot", self.labels[arm],
                                                 " (first)" if arm == (0 if i % 2 else 1) else ""))
                done[arm] = self.arm_leg(arm, {"round": str(i), "arm": "ab"[arm]})
            if done[0] and done[1]:
                kept += 1
            else:
                lost += 1
                warn("round %d produced %sonly -- the report drops it" % (i, "".join(self.labels[a] + " " for a in (0, 1) if done[a])))
                if lost >= LOST_ROUNDS:
                    warn("%d rounds lost; stopping early" % LOST_ROUNDS)
                    break
            if i >= self.rounds and not self.detect:
                break
            if i >= self.rounds and self.resolved():
                log("  resolved    %g%% at round %d" % (self.detect, i))
                break
        else:
            warn("--max-rounds %d reached without resolving %g%%; the report says what these rounds do resolve" % (self.max_rounds, self.detect))
        return kept, lost

    def go(self):
        self.held = progress.hold(self.recs, lambda r: board.fleet_holders(self.root, self.reg.env, self.recs, r), self.name, "bench",
                                  self.ws, "wk bench run %s --kill --system %s" % (self.ws, self.name), "",
                                  ["%s on %s, %s" % (self.plan, self.name, " vs ".join(self.labels))], os.getpid(), self.reg.env)
        rc = 1
        try:
            with job.Signals():
                rc = self.body()
        except job.Interrupted as e:
            rc = "cancelled"
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        except Refused as e:
            rc = e.status
            raise
        finally:
            if self.held is not None:
                self.held.end(rc)
            self.lock.release_all()
        return rc

    def body(self):
        if not self.systems:
            for _, s in self.arms:
                self.system.manifest(s)
        r = self.run()
        text = r.pin(self.plan)
        self.begin(json.loads(text))
        if self.task:
            log("  task        %s (%s)" % (self.task, self.taskdir))
        n = self.rounds
        log("interleaving %d round(s) of %s vs %s after a warmup round -- %d %s" % (
            n, self.labels[0], self.labels[1], n * 2 + 2, "boots and runs" if self.systems else "runs"))
        self.warmup()
        if act.dry_run():
            info("dry run -- then %d round(s), each arm once per round, the lead alternating" % n)
            return 0
        kept, lost = self.measured()
        if self.systems:
            self.release()
        log("")
        log("%d round(s) usable, %d dropped; the runs are in %s/runs" % (kept, lost, self.taskdir))
        self.report()
        if not kept:
            die("no round produced both halves on %s; nothing to compare." % self.name)
        return 0

    def release(self):
        """The board back on its rescue, its arming record cleared there: the A/B claimed each system it booted."""
        if not (self.transition("back") and self.answered(want_host=True) and self.transition("disarm")):
            warn("%s was not handed back; 'wk boot %s --status' says where it is, --disarm and --back return it" % (self.name, self.name))

    def report(self):
        self.lock.release_all()
        if not self.owned:
            log("  report:  wk bench report %s" % self.task)
            return
        try:
            report.task_report(self.taskdir, False, html=True, text=True)
        except (Refused, SystemExit, OSError, ValueError) as e:
            warn("the report did not complete (%s); the runs are recorded:  wk bench report %s" % (e, self.task))


def run(root, reg, ws, plan, o, clock, popen, driver=None, machine=None):
    if o.get("ab") and o.get("ab_systems"):
        die("--ab compares two slots in one booted system, --ab-systems two systems -- they are different comparisons; pick one.")
    if not o.get("system"):
        die("--%s is an A/B on one board: name it with --system <board>" % ("ab-systems" if o.get("ab_systems") else "ab"))
    if o.get("collect"):
        die("--collect takes a profile from one instrumented slot, so it is neither arm of a comparison")
    if o.get("slot") and not o.get("ab_systems"):
        die("--slot names the one slot a system A/B holds fixed; a slot A/B names its two slots in --ab")
    if o.get("cores") and not pipeline.cores_valid(o["cores"]):
        die("--cores '%s' is not a valid Linux cpu list (e.g. 0-3, 2,3, 0-1,4, 7)" % o["cores"])
    return AB(root, reg, ws, plan, o, clock, popen, driver=driver, machine=machine).go()
