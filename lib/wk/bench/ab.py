"""`wk bench ab`: an interleaved A/B across the fleet's boards. A change's arms are two slots, base and the
patched head, each built against every device's image and deployed to its board; `--systems A,B` names two
system images on one board instead. The plan is a graph (lib/wk/sched.py) run in this process: each build,
deploy and collection is the `wk` command a person types, and each board's rounds are one board A/B
(lib/wk/bench/board_ab.py) recorded into this task."""

import concurrent.futures as futures
import os
import re
import statistics
import subprocess
import sys
from fnmatch import fnmatchcase

from wk import act, fleet, git, images, job, pgo, pr, record as progress, sched
from wk.act import Refused, die, info, log, warn
from wk.bench import board, board_ab, record, report
from wk.lock import Lock

SHA = re.compile(r"^[0-9a-f]{40}$")
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
RELEASE_OF = re.compile(r"^(wpe-|webkitglib/)([0-9]+\.[0-9]+)$")
ROUNDS, PLAN = "5", "speedometer3"
SLOTS = 'for s in /var/wk/slots/*/slot.json; do [ -f "$s" ] && basename "$(dirname "$s")"; done 2>/dev/null; true'
USAGE = ("usage: wk bench ab <pr-spec|branch|sha> --devices <a,b> [--release X.Y] [--builder B] [--bits N] [--base <sha>]\n"
         "           [--build-on <a[,b]>] [--plan P]... [--rounds N] [--count N] [--timeout S] [--detach]\n"
         "       wk bench ab --systems A,B --devices <board> [--slot S] [--plan P]... [--rounds N]\n"
         "       wk bench ab --devices <mac> --systems A,B | --patch <ref|diff> --workspace <ws> [--base <ref>] ...\n"
         "       wk bench ab <task> --kill; see wk bench -h")
BUILD_ONLY = ("release", "builder", "bits", "base", "build_on")
MAC_ONLY = ("patch", "workspace", "config", "settle", "a_args", "b_args", "plant", "rehearse", "allow_network_fetch",
            "preflight", "progress", "status", "collect")


def _or(words):
    return " or ".join(words)


def legs_per_plan(rounds, systems):
    """A warmup leg per arm, then each round one leg per arm; a system arm settles on each fresh boot first."""
    return 2 + rounds * (4 if systems else 2)


def leg_seconds(bench_dir, device, plan, count):
    """Every measured leg of `plan` on `device` in this store, as seconds at `count` iterations: a leg at another
    count scales by the ratio, and one at the plan's own default count stands only for that default."""
    out = []
    for t in record.tasks(bench_dir):
        for r in record.task_runs(os.path.join(bench_dir, t)):
            e = r["env"]
            if e.get("machine") != device or e.get("plan") != plan or r["state"] != "ok" or "wall_time_s" not in e:
                continue
            have = str(e.get("count") or "")
            if have == count:
                out.append(float(e["wall_time_s"]))
            elif have and count:
                out.append(float(e["wall_time_s"]) * int(count) / int(have))
    return out


def check_plan(o, default_plans):
    """(rounds, plans): the refusals every A/B's plan shares, a board's and a Mac's."""
    rounds = o.get("rounds") or ROUNDS
    if not rounds.isdigit() or int(rounds) < 1:
        die("--rounds takes a number of at least 1 (got '%s')" % rounds)
    for key, what in (("count", "a number"), ("timeout", "seconds")):
        if o.get(key) and not o[key].isdigit():
            die("--%s takes %s (got '%s')" % (key, what, o[key]))
    plans = o.get("plans") or list(default_plans)
    for p in plans:
        if not NAME.match(p):
            die("--plan '%s' is not a plan name (letters, digits, '.', '_' and '-')" % p)
    return int(rounds), plans


def duration(seconds):
    m = int(round(seconds / 60.0))
    return "%dh%02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % max(m, 1)


class Device:
    def __init__(self, name, profile, p):
        self.name, self.profile, self.p = name, profile, p
        self.lanes, self.mode, self.slots = [], "unreachable", []

    @property
    def pgo(self):
        return images.pgo_wanted(self.p["IMG_BUILDER"], self.p["CFG_RELEASE"])

    @property
    def sdk(self):
        return self.p["IMG_BUILDER"] == "yocto"

    def booted(self):
        return self.mode.startswith("bench %s-" % self.profile)


class AB:
    """`boards(name)` answers (mode, slots) for a board and `bench(ws, plan, o)` runs one board A/B; both default
    to the real board, and `pool` is the executor steps run on."""

    def __init__(self, root, reg, clock, spec, o, boards=None, bench=None, pool=futures.ThreadPoolExecutor, popen=subprocess.Popen):
        self.root, self.reg, self.clock, self.spec, self.o = str(root), reg, clock, spec or "", dict(o)
        self.here, self.env, self.store = reg.machine, reg.env, reg.store
        self.boards, self.bench, self.pool, self.popen = boards or self.board_state, bench or self.board_ab, pool, popen
        self.lock = Lock(self.store, self.here, clock)
        self.wk = os.path.join(self.root, "wk")
        self.bench_dir = self.store.bench_dir()
        self.systems = bool(self.o.get("systems"))
        self.task = self.o.get("task") or ""
        self.taskdir = os.path.join(self.bench_dir, self.task) if self.task else ""
        self.me = progress.machine_name(self.env, self.here)
        self.devices, self.arms, self.pr, self._base_branch = [], [], {}, None
        self.head = self.base = self.head_desc = self.base_how = self.branch = ""
        self.ahead = self.behind = 0
        self.logged = set()   # the steps that are a `wk` command, each logging beside its resource

    def check(self):
        o = self.o
        if not o.get("devices"):
            die(USAGE + "\n    --devices names the boards (wk boot --list)")
        given = [k for k in MAC_ONLY if o.get(k)]
        if given:
            die("--%s is a Mac A/B's: --devices names a board here, and a board's arms are slots" % given[0].replace("_", "-"))
        self.rounds, self.plans = check_plan(o, [PLAN])
        self.max_rounds, self.detect = board_ab.stopping(o, self.rounds)
        if o.get("detach") and act.dry_run():
            die("--detach and --dry-run: a dry run has nothing to detach")
        if self.task and not os.path.isfile(os.path.join(self.taskdir, "task.json")):
            die("no such task '%s' (%s has no task.json); 'wk bench ls' lists the tasks" % (self.task, self.taskdir))
        if o.get("bits") not in (None, "", "32", "64"):
            die("--bits takes 32 or 64 (got '%s')" % o["bits"])
        if self.systems:
            self.check_systems()
        elif not self.spec or self.spec.startswith("-"):
            die(USAGE)
        elif o.get("slot"):
            die("--slot names the one slot a --systems A/B holds fixed; a change's arms are the slots base and pr<n>")

    def check_systems(self):
        o = self.o
        if self.spec:
            die("--systems compares two system images, so there is no change to build: drop '%s'" % self.spec)
        given = [k for k in BUILD_ONLY if o.get(k)]
        if given:
            die("--%s says how to build a change's slots, and a --systems A/B builds nothing" % given[0].replace("_", "-"))
        if "," in o["devices"]:
            die("--systems names two system ids, and a system id names one board's image: one --devices board")
        a, b = board_ab.pair(o["systems"], "systems")
        slot = o.get("slot") or "a"
        images.check_slot_name(slot)
        self.arms = [(a, slot), (b, slot)]

    def git(self, *args):
        return self.here.run(["git", "-C", self.mirror] + list(args))

    def rev(self, ref):
        r = self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        return r.out.strip() if r.ok else ""

    def _resolved_or_planned(self, dest, what, fetch):
        return pr.resolved_or_planned(self.here, self.mirror, dest, what, fetch)

    def resolve_head(self):
        """Which of the three the spec is settles before the mirror is required, so a malformed spec is refused as the argument error it is."""
        s = self.spec
        self.pr = dict(kind="commit", n="", remote="") if SHA.match(s) else pr.parse_spec(s)
        if not self.o.get("release") and self.pr["kind"] != "pull":
            die("--release is required for a commit or a branch: only a pull request\n"
                "    has a base branch saying which image it is for ('wk sysimage --list' has\n"
                "    every release; --release 2.52).")
        self.mirror = self.store.mirror()
        if not self.here.isdir(self.mirror):
            die("no mirror at %s; 'wk sync' makes one. Both commits are resolved in it." % self.mirror)
        if self.pr["kind"] == "commit":
            if not self.git("cat-file", "-e", s + "^{commit}").ok:
                die("the mirror does not have commit %s ('wk sync' fetches the upstream branches)" % s)
            self.head, self.head_desc = s, "commit"
        elif self.pr["kind"] == "user":
            self.head_branch()
        else:
            remote, n = self.pr["remote"], self.pr["n"]
            dest = "refs/remotes/pr/" + pr.pull_refname(remote, n)
            self.head = self._resolved_or_planned(dest, "pull request %s" % n,
                                                  lambda: pr.mirror_fetch_pull(self.here, self.store, self.lock, remote, n))
            if not self.head:
                die("the mirror has no head for pull request %s after fetching it" % n)
            self.head_desc = "%s (pull request %s on %s)" % (s, n, remote)

    def head_branch(self):
        """A fork's branch is a head like a pull request's: found in whichever of that fork's repositories carries it, fetched into the mirror."""
        user, branch = self.pr["user"], self.pr["branch"]
        found = pr.branch_repos(self.here, user, branch)
        if not found:
            die("no branch '%s' under '%s'.\n    Checked: %s\n    A pull request is named as wpe:<n> or <n>, a commit as its "
                "40-digit sha." % (branch, user, " ".join(pr.branch_repo_urls(user))))
        if len(found) > 1:
            die("'%s' exists in more than one of %s's repositories:\n%s\n    They are different projects; pass the head sha of "
                "the one you mean." % (branch, user, "\n".join("    %s %s %s" % f for f in found)))
        repo, url, _ = found[0]
        ref = pr.pr_refname(user, repo, branch)
        dest = "refs/remotes/pr/" + ref
        self.head = self._resolved_or_planned(dest, "%s:%s from %s" % (user, branch, url),
                                              lambda: pr.mirror_fetch(self.here, self.store, self.lock, url, "refs/heads/" + branch, dest))
        if not self.head:
            die("the mirror has no head for '%s' after fetching it from %s" % (branch, url))
        self.head_desc = "%s:%s (branch of %s/%s)" % (user, branch, user, repo)

    def pr_base_branch(self):
        """The branch a pull request was written against, which its head ref does not carry: asked of GitHub, once."""
        if self._base_branch is None:
            url = dict(git.REMOTES)[self.pr["remote"]]
            repo = re.sub(r"\.git$", "", url).replace("https://github.com/", "")
            r = self.here.run(["gh", "pr", "view", self.pr["n"], "--repo", repo, "--json", "baseRefName", "--jq", ".baseRefName"])
            if not r.ok or not r.out.strip():
                die("could not read pull request %s's base branch from %s (gh auth login signs gh in):\n    %s\n"
                    "    Say both of the things it answers yourself:\n"
                    "        --release <x.y>   which image to measure on (wk sysimage --list)\n"
                    "        --base <sha>      the unpatched side, usually the head's own parent"
                    % (self.pr["n"], repo, (r.err or r.out).strip()))
            self._base_branch = r.out.strip()
        return self._base_branch

    def release(self):
        if self.o.get("release"):
            return self.o["release"]
        m = RELEASE_OF.match(self.pr_base_branch())
        if not m:
            die("pull request %s targets '%s', which names no release.\n    Say which image to measure on: --release 2.38 "
                "(wk sysimage --list)" % (self.pr["n"], self.pr_base_branch()))
        return m.group(2)

    def narrow(self, dev, matches, release):
        """What tells the matches apart is what to ask for: naming a width narrows nothing between two builders of it."""
        builders, widths = [], []
        for c in matches:
            b, w = c.split("-%s-" % release, 1)[1].split("-", 1)[0], c.rsplit("-", 1)[1]
            builders += [b] if b not in builders else []
            widths += ["%s-%s" % (dev, w)] if "%s-%s" % (dev, w) not in widths else []
        said = (["--builder " + _or(builders)] if len(builders) > 1 else []) + (["--devices " + _or(widths)] if len(widths) > 1 else [])
        return ", and ".join(said) or ("nothing -- they differ in neither builder nor width, so no option here tells them apart,\n"
                                       "    and two such configurations at one release is a bug in image/configs")

    def profile_for(self, dev, bits, release):
        pattern = "*-%s-%s-%s-%s" % (release, self.o.get("builder") or "*", dev, bits or "*")
        matches = [c for c in images.names(self.env) if fnmatchcase(c, pattern)]
        if not matches:
            die("no image configuration for %s at release %s%s%s.\n    'wk sysimage --list' has every configuration this checkout "
                "defines." % (dev, release, " (%s)" % self.o["builder"] if self.o.get("builder") else "", ", %s-bit" % bits if bits else ""))
        if len(matches) > 1:
            die("%s has more than one image at release %s:\n%s\n    Say which: %s"
                % (dev, release, "\n".join("    " + m for m in matches), self.narrow(dev, matches, release)))
        return matches[0]

    def resolve_devices(self, release):
        for tok in self.o["devices"].split(","):
            dev, bits = tok, self.o.get("bits") or ""
            if tok[-3:] in ("-32", "-64"):
                dev, bits = tok[:-3], tok[-2:]
            board.require_board(self.root, self.env, dev)
            profile = self.profile_for(dev, bits, release)
            p = images.load(profile, self.env)
            if p["CFG_NEEDS"]:
                die("%s cannot be built yet: %s\n    (%s)" % (profile, p["CFG_NEEDS"], images.conf_path(profile, self.env)))
            if p["IMG_BUILDER"] not in images.WS_BUILDERS:
                die("%s is built by %s, which has no WebKit slots" % (profile, p["IMG_BUILDER"] or "nothing"))
            self.devices.append(Device(dev, profile, p))
        first = self.devices[0].p
        for d in self.devices[1:]:
            if (d.p["CFG_BRANCH"], d.p["CFG_REMOTE"]) != (first["CFG_BRANCH"], first["CFG_REMOTE"]):
                die("%s tracks %s/%s while %s tracks %s/%s;\n    one A/B measures one branch. Run them as two."
                    % (d.profile, d.p["CFG_REMOTE"], d.p["CFG_BRANCH"], self.devices[0].profile, first["CFG_REMOTE"], first["CFG_BRANCH"]))
        if not first["CFG_BRANCH"] or not first["CFG_REMOTE"]:
            die("%s declares no CFG_BRANCH/CFG_REMOTE, so there is no branch to take a base from" % self.devices[0].profile)

    def resolve_base(self):
        """Off the branch the change was written against -- a pull request's own base branch -- not the image's, which
        says only which image to measure on. Refreshed first: a stale mirror would pick an older base."""
        p = self.devices[0].p
        remote, branch = p["CFG_REMOTE"], p["CFG_BRANCH"]
        if not self.o.get("base") and self.pr["kind"] == "pull":
            remote, branch = self.pr["remote"], self.pr_base_branch()
        url = dict(git.REMOTES).get(remote)
        if not url:
            die("no upstream remote '%s' (lib/wk/git.py REMOTES) to fetch %s from" % (remote, branch))
        dest = "refs/remotes/%s/%s" % (remote, branch)
        tip = self._resolved_or_planned(dest, "%s/%s" % (remote, branch),
                                        lambda: pr.mirror_fetch(self.here, self.store, self.lock, url, "refs/heads/" + branch, dest))
        if not tip:
            die("the mirror has no %s/%s after fetching it" % (remote, branch))
        self.branch = "%s/%s" % (remote, branch)
        if self.o.get("base"):
            self.base = self.rev(self.o["base"])
            if not self.base:
                die("--base '%s' is not a commit the mirror knows" % self.o["base"])
            self.base_how = "given"
        else:
            r = self.git("merge-base", self.head, tip)
            if not r.ok or not r.out.strip():
                die("%s and %s share no history; is this change against that branch?\n    Say the base yourself: --base <sha>"
                    % (self.head, self.branch))
            self.base, self.base_how = r.out.strip(), "merge-base of the head and %s (guessed)" % self.branch
        self.ahead, self.behind = self.count("%s..%s" % (self.base, self.head)), self.count("%s..%s" % (self.head, tip))

    def count(self, spec):
        out = self.git("rev-list", "--count", spec).out.strip()
        return int(out) if out.isdigit() else 0

    def build_on(self):
        names = [m for m in (self.o.get("build_on") or "").split(",") if m]
        if len(names) > 2:
            die("--build-on names the machine each of the two arms builds on, base first, so it takes one name or two "
                "(got '%s')." % self.o["build_on"])
        known = self.reg.known()
        for m in names:
            if m not in known and m != self.me:
                die("--build-on names a machine this checkout knows (machines/<name>.conf) or this one, and '%s' is neither.\n"
                    "    The machines here: %s" % (m, " ".join(known) or "none"))
        return (names + names)[:2] if names else ["", ""]

    def resolve_lanes(self):
        on = self.build_on()
        pr_slot = "pr" + self.pr["n"] if self.pr.get("n") else "pr"
        self.arms = [("", "base"), ("", pr_slot)]
        for d in self.devices:
            lane = images.image_ws(d.profile, self.env)
            for named in on:
                try:
                    where = images.ws_machine(named, "" if named else self.reg.ws_target(lane), self.me)
                except LookupError as e:
                    die(str(e))
                target = named if named and named != self.me else ""
                d.lanes.append((lane, "%s@%s" % (d.profile, named) if named else d.profile, where, target))

    def holds(self, spec, lane, *rest):
        return sched.wk_yes(self.here, [self.wk, "sysimage", "holds", spec, "--workspace", lane] + list(rest))

    def wk_step(self, sid, on, needs, holds, done, words, target=""):
        self.logged.add(sid)
        return sched.wk_step(self.here, self.wk, lambda s: os.path.join(self.taskdir, sched.log_name(s)), sid, on, needs, holds,
                             done, words, target)

    def pgo_steps(self, d, lane, spec, on, target, commit, slot, need):
        return pgo.steps(self.wk_step, self.holds, d.name, lane, spec, on, target, commit, slot, (need,))

    def build_steps(self, d, imaged):
        out = []
        for a, (_, slot) in enumerate(self.arms):
            commit = self.head if a else self.base
            lane, spec, on, target = d.lanes[a]
            key, res = "%s@%s" % (lane, on), images.build_resource(on)
            built = ("toolchain:" if d.sdk else "image:") + key
            if key not in imaged:
                imaged.add(key)
                out.append(self.wk_step("image:" + key, on, (), (res,), self.holds(spec, lane),
                                        ["sysimage", "build", spec, "--workspace", lane]))
                if d.sdk:   # the nativesdk stack does not fit the webkit stage's booking, so the SDK is machine-sized and its own step
                    out.append(self.wk_step("toolchain:" + key, on, ("image:" + key,), (res,), self.holds(spec, lane, "--toolchain"),
                                            ["sysimage", "build", spec, "--workspace", lane, "--stage", "toolchain"]))
            if d.pgo:
                out += self.pgo_steps(d, lane, spec, on, target, commit, slot, built)
            else:
                out.append(self.wk_step("slot:%s:%s" % (lane, slot), on, (built,), (res,), self.holds(spec, lane, "--slot", slot, "--commit", commit),
                                        ["sysimage", "webkit", spec, "--workspace", lane, "--commit", commit, "--slot", slot]))
            out.append(self.wk_step("deploy:%s:%s" % (d.name, slot), on, ("slot:%s:%s" % (lane, slot),), ("device:" + d.name,), None,
                                    ["bench", "deploy", lane, d.name, "--slot", slot], target))
        return out

    def steps(self):
        out, imaged, benches = [], set(), []
        for d in self.devices:
            needs = ()
            if not self.systems:
                out += self.build_steps(d, imaged)
                needs = tuple("deploy:%s:%s" % (d.name, s) for _, s in self.arms)
            for plan in self.plans:
                benches.append("bench:%s:%s" % (d.name, plan))
                out.append(sched.Step(benches[-1], self.me, needs, ("device:" + d.name,), None,
                                      (lambda d, plan: lambda: self.run_bench(d, plan))(d, plan), command=self.bench_command(d, plan)))
        out.append(sched.Step("report", self.me, benches, (), None, self.run_report,
                              command="wk bench report %s --html --text" % (self.task or "<task>")))
        return sched.validate(out)

    def bench_options(self, d):
        o = {"system": d.name, "rounds": str(self.rounds), "task": self.task, "count": self.o.get("count") or "",
             "timeout": self.o.get("timeout") or "", "max_rounds": str(self.max_rounds) if self.detect else "",
             "detect": "%g" % self.detect if self.detect else ""}
        a, b = self.arms
        if self.systems:
            return d.name, dict(o, ab_systems="%s,%s" % (a[0], b[0]), slot=a[1])
        return d.lanes[0][0], dict(o, ab="%s,%s" % (a[1], b[1]))

    def bench_command(self, d, plan):
        ws, o = self.bench_options(d)
        flag = "--ab-systems " + o["ab_systems"] + " --slot " + o["slot"] if self.systems else "--ab " + o["ab"]
        return "wk bench run %s %s --system %s %s --rounds %s%s --task %s" % (
            ws, plan, d.name, flag, o["rounds"], "".join(" --%s %s" % (k.replace("_", "-"), o[k]) for k in ("max_rounds", "detect", "count", "timeout")
                                                      if o[k]), self.task or "<task>")

    def run_bench(self, d, plan):
        ws, o = self.bench_options(d)
        try:
            return self.bench(ws, plan, o) or 0
        except Refused as e:
            return e.status

    def board_ab(self, ws, plan, o):
        return board_ab.run(self.root, self.reg, ws, plan, o, self.clock, self.popen)

    def run_report(self):
        try:
            report.task_report(self.taskdir, False, html=True, text=True)
        except (Refused, SystemExit, OSError, ValueError) as e:
            warn("the report did not complete (%s); the runs are recorded:  wk bench report %s" % (e, self.task))
            return 1
        return 0

    def board_state(self, name):
        """(mode, slots): what the board answers it is running now, and the slots its bench system holds."""
        try:
            s = board.for_board(self.root, self.reg, "", self.clock, name)
            mode = s.driver.probe()
            if not mode.startswith("bench "):
                return mode, []
            return mode, s.bench().run(["sh", "-c", SLOTS]).out.replace("\r", "").split()
        except Refused:
            return "unreachable", []

    def cost(self):
        """{(device, plan): (legs, seconds or None, legs measured)}: the plan's cost from this store's measured legs."""
        out = {}
        for d in self.devices:
            for plan in self.plans:
                legs = legs_per_plan(self.rounds, self.systems)
                seen = leg_seconds(self.bench_dir, d.name, plan, self.o.get("count") or "")
                out[(d.name, plan)] = (legs, legs * statistics.median(seen) if seen else None, len(seen))
        return out

    def cost_lines(self):
        cost, lines = self.cost(), []
        for (dev, plan), (legs, secs, n) in sorted(cost.items()):
            if secs is None:
                lines.append("%s %s: %d legs; no leg of it at this --count measured on %s yet, so its time is unknown"
                             % (dev, plan, legs, dev))
            else:
                lines.append("%s %s: %d legs x ~%s (median of %d measured) = ~%s" % (dev, plan, legs, duration(secs / legs), n, duration(secs)))
        per_board = {}
        for (dev, _), (_, secs, _) in cost.items():
            per_board.setdefault(dev, []).append(secs)
        if per_board and all(None not in v for v in per_board.values()):
            lines.append("about %s on the boards, which run at once; the builds are not in it" % duration(max(sum(v) for v in per_board.values())))
        return lines

    def subject(self):
        if self.systems:
            return "%s vs %s on %s" % (self.arms[0][0], self.arms[1][0], self.devices[0].name)
        return "%s on %s: %s vs %s, %s" % (self.head_desc, ", ".join(d.name for d in self.devices), self.base[:12], self.head[:12],
                                           self.devices[0].p["CFG_RELEASE"])

    def subject_line(self, sha):
        r = self.git("log", "-1", "--format=%s", sha)
        return r.out.strip()[:70] if r.ok else ""

    def show(self, steps, done):
        if self.systems:
            log("A/B of system %s vs %s on %s" % (self.arms[0][0], self.arms[1][0], self.devices[0].name))
        else:
            p = self.devices[0].p
            log("A/B of %s on %s\n" % (self.head_desc, ", ".join(d.name for d in self.devices)))
            log("  patched   %s  %s" % (self.head[:12], self.subject_line(self.head)))
            log("  base      %s  %s" % (self.base[:12], self.subject_line(self.base)))
            log("            %s; the patched side is %d commit(s) ahead of it" % (self.base_how, self.ahead))
            if self.behind:
                log("            %s has moved %d commit(s) past the base since" % (self.branch, self.behind))
            log("  release   %s (%s %s/%s)" % (p["CFG_RELEASE"], p["CFG_PROJECT"] or "WebKit", p["CFG_REMOTE"], p["CFG_BRANCH"]))
        log("  plans     %s" % " ".join(self.plans))
        log("  rounds    %s per plan, the lead alternating%s%s" % (
            self.rounds, "; up to %d until they resolve %g%%" % (self.max_rounds, self.detect) if self.detect else "",
            "; --count %s iterations per run" % self.o["count"] if self.o.get("count") else ""))
        log("  arms      %s" % ", ".join(s for _, s in self.arms) if not self.systems else "  slot      %s in both systems" % self.arms[0][1])
        log("  task      %s" % (self.task or "(named when it is created)"))
        for line in self.cost_lines():
            log("  cost      " + line)
        log("")
        for d in self.devices:
            log("  %s: %s" % (d.name, "board %s" % d.mode if self.systems else "image %s" % d.profile))
            if not self.systems:
                for (_, slot), (lane, _, on, _) in zip(self.arms, d.lanes):
                    log("        lane %-7s %s on %s" % (slot, lane, on))
                log("        board      %s%s" % (d.mode, "  (slots there: %s)" % (" ".join(d.slots) or "none") if d.booted()
                                                  else "  <-- not booted into %s" % d.profile))
                log("        build      %s" % ("profile-guided: each slot is instrument, collect on %s, rebuild -- so %s has to be in this "
                                               "image before the builds" % (d.name, d.name) if d.pgo
                                               else "plain: %s predates upstream cmake PGO support" % d.p["CFG_RELEASE"]))
        log("\nthe plan as a graph -- a step runs when its needs are done and what it holds is free:\n")
        sched.render(steps, done, sys.stderr)
        for d in self.devices:
            if not self.systems and not d.booted():
                log("\n  %s is not booted into %s -- by hand, before the steps above:\n    wk sysimage write --from %s --disk %s:<device>\n"
                    "    wk boot %s" % (d.name, d.profile, d.profile, d.name, d.name))

    def argv(self):
        """This A/B as a command, for the process --detach hands it to."""
        words = [self.spec] if self.spec else []
        for key in ("devices", "release", "builder", "bits", "base", "build_on", "systems", "slot", "count", "timeout", "max_rounds", "detect"):
            if self.o.get(key):
                words += ["--" + key.replace("_", "-"), self.o[key]]
        for p in self.plans:
            words += ["--plan", p]
        return [self.wk, "bench", "ab"] + words + ["--rounds", str(self.rounds)]

    def create_task(self):
        """Created, and its lock taken, before any work; a given --task is a --detach parent's, whose lock this process takes over."""
        if self.task:
            self.lock.hold("bench-task-" + self.task, timeout=30)
            return
        stamp = self.clock.stamp()
        self.task = ("%s-%s-systems" % (stamp, self.devices[0].name) if self.systems
                     else "%s-%s-pr%s" % (stamp, self.pr["remote"], self.pr["n"]) if self.pr.get("n") else "%s-%s" % (stamp, self.head[:12]))
        self.taskdir = os.path.join(self.bench_dir, self.task)
        if os.path.exists(self.taskdir):
            die("task %s already exists (%s); a task is one request, made once" % (self.task, self.taskdir))
        self.lock.hold("bench-task-" + self.task, timeout=5)
        a, b = self.arms
        if self.systems:
            subj = ["subject.kind=systems", "subject.spec=%s,%s" % (a[0], b[0])]
            devices = self.devices[0].name
        else:
            subj = ["subject.kind=" + ("pull" if self.pr.get("n") else "commit"), "subject.spec=" + self.spec, "subject.remote=" + self.pr["remote"],
                    "subject.number=" + self.pr["n"], "subject.head=" + self.head, "subject.base=" + self.base, "subject.base_how=" + self.base_how,
                    "subject.release=" + self.devices[0].p["CFG_RELEASE"], "subject.branch=%s/%s" % (self.devices[0].p["CFG_REMOTE"], self.devices[0].p["CFG_BRANCH"])]
            devices = ",".join("%s=%s" % (d.name, d.profile) for d in self.devices)
        record.task_write(self.taskdir, ["task=" + self.task, "requested=" + self.clock.iso(), "devices=" + devices, "plans=" + ",".join(self.plans),
                                         "rounds=%d" % self.rounds, "slots=" + ",".join(dict.fromkeys(s for _, s in self.arms))] + subj
                          + ["%s=%s" % (k, self.o[k]) for k in ("count", "timeout") if self.o.get(k)], [" ".join(["wk"] + self.argv()[1:])])

    def resolve(self):
        self.check()
        if not self.systems:
            self.resolve_head()
            self.resolve_devices(self.release())
            self.resolve_base()
            self.resolve_lanes()
        else:
            board.require_board(self.root, self.env, self.o["devices"])
            self.devices.append(Device(self.o["devices"], "", images.FIELDS))
        for d in self.devices:
            d.mode, d.slots = self.boards(d.name)

    def go(self):
        self.resolve()
        steps = self.steps()
        done = sched.done_ids(steps, self.pool)
        if self.ahead > 1:
            act.barrier("the patched side is %d commits ahead of the base, so the two arms differ by more than the change under\n"
                        "    measurement and no number says which commit moved it. Name the commit's own parent:\n"
                        "        --base %s" % (self.ahead, self.git("rev-parse", "--short=12", self.head + "^").out.strip() or "<sha>"))
        self.show(steps, done)
        if act.dry_run():
            log("\ndry run -- nothing was built or run.")
            return 0
        log("")
        if not self.systems:
            warn("this replaces slots %s on the boards named, and moves each image workspace's checkout to the commits above."
                 % " and ".join("'%s'" % s for _, s in self.arms))
        if not act.confirm("run these %d step(s)%s?" % (len(steps), "" if self.systems else ", replacing those slots")):
            die("not run")
        self.create_task()
        info("task %s  (%s)" % (self.task, self.taskdir))
        if self.o.get("detach"):
            pid = job.detach(self.here, self.argv() + ["--yes", "--task", self.task], os.path.join(self.taskdir, "ab.log"))
            info("detached as pid %d -- this end can go away" % pid)
            log("  follow:  tail -f %s/ab.log\n  state:   wk status;  wk bench ls\n  report:  wk bench report %s   (partial while it runs)"
                % (self.taskdir, self.task))
            return 0
        return self.run(self.steps())

    def run(self, steps):
        order = sched.plan_order(steps)
        recs = progress.Records(self.store.record_dir(), clock=self.clock, env=self.env, machine=self.here)
        t = recs.begin("ab", "here", self.task, "wk bench ab %s --kill" % self.task, os.path.join(self.taskdir, "ab.log"),
                       [s.command for s in order])
        t.set("subject", self.subject())

        def announce(event, step, rc=0):
            t.step_event(order.index(step) + 1, event)
            log(sched.say_event(order, event, step, rc, os.path.join(self.taskdir, sched.log_name(step)) if step.id in self.logged else ""))

        rc = 1
        try:
            with job.Signals():
                s = sched.Scheduler(order, announce, pool=self.pool)
                rc = s.run_all()
                for line in sched.summary(s):
                    log(line)
                if rc:
                    die("A/B incomplete: the steps above say which did not run, and each one's log is beside its own resource in\n"
                        "    %s. Every round that finished is recorded ('wk bench report %s'), and re-running\n"
                        "    'wk bench ab ... --task %s' takes up what is left." % (self.taskdir, self.task, self.task))
                self.verify()
        except job.Interrupted as e:
            rc = "cancelled"
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        except Refused as e:
            rc = e.status
            raise
        finally:
            t.end(rc)
            self.lock.release_all()
        info("A/B complete: %s  (wk bench ls; wk bench report %s)" % (self.taskdir, self.task))
        return 0

    def verify(self):
        """Both slots are what the A/B names after the builds, asked of the machine holding each lane."""
        if self.systems:
            return
        for d in self.devices:
            for a, (_, slot) in enumerate(self.arms):
                lane, spec = d.lanes[a][:2]
                if not self.holds(spec, lane, "--slot", slot, "--commit", self.head if a else self.base)():
                    die("%s does not hold slot '%s' at %s after the builds (wk sysimage ls), so what ran was not the pair\n"
                        "    this A/B names. Do not take the report as it stands." % (lane, slot, (self.head if a else self.base)[:12]))


def kill(reg, clock, task):
    """`wk bench ab <task> --kill`: the A/B's process and every step it started, the record ended cancelled once they are gone."""
    recs = progress.Records(reg.store.record_dir(), clock=clock, env=reg.env, machine=reg.machine)
    t = recs.find("ab", task)
    if t is None:
        die("no A/B task '%s' was started from this machine.\n    'wk bench ls' names the tasks and 'wk status' the one running." % task)
    if not t.alive(None):
        die("A/B task '%s' is not running: it is %s.\n    What it measured is recorded:  wk bench report %s" % (task, t.verdict(), task))
    info("stopping A/B task '%s' (pid %s)" % (task, t.field("pid")))
    if not job.kill(None, task, t, "cancelled", reg.machine, clock, reg.env):
        die("pid %s outlived a TERM and a KILL (ps -p %s); the record says cancelled, the process does not" % (t.field("pid"), t.field("pid")))
    info("cancelled. The rounds it recorded stay in %s ('wk bench report %s');\n    re-running with --task %s takes up what is left."
         % (os.path.join(reg.store.bench_dir(), task), task, task))
    return 0


def machine_kind(root, env, name):
    try:
        return (fleet.Fleet(root, env).load(name) or {}).get("KIND", "")
    except fleet.ConfError:
        return ""


def run(root, reg, clock, spec, o, kill_it=False):
    if kill_it:
        if not spec:
            die(USAGE)
        return kill(reg, clock, spec)
    if o.get("devices") and machine_kind(root, reg.env, o["devices"]) in ("mac", "guest"):
        from wk.bench import mac   # it builds on this module
        m = mac.MacAB(root, reg, clock, spec, o)
        return m.back() if any(o.get(k) for k in mac.READS) else m.go()
    return AB(root, reg, clock, spec, o).go()
