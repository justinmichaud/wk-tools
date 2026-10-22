"""`wk bench ab --devices <mac>`: no session on this side survives the reboot into a Mac's bench install, so the A/B is
planted on it -- wk-tools, the job, the task record it fills and a LaunchAgent -- and run there at autologin by
lib/wk/bench/autorun.py as one `AB` over the volume's staged builds; its gates, its progress and its result are read back
here over whichever install answers."""

import base64
import json
import os
import plistlib
import shlex

from wk import act, fleet, presets, record as wkrecord, samply, sched
from wk.act import die, info, log, warn
from wk.bench import record
from wk.bench.ab import PLANTED, Task, check_plan, cost_line, flags, pair, refuse
from wk.bench.mac import BENCH_ROOT, PUT_SKIP, WKMAC, side
from wk.bench.pipeline import check, variance
from wk.boot import open_driver
from wk.boot.mac import STARTUP_DISK
from wk.kv import kv
from wk.machine import Ssh, lib_argv
from wk.quiet import DESKTOP
from wk.workspace import require_name

AB_PLANS = ("jetstream3", "speedometer3", "motionmark")
AB_PRESET = "mac-release-pgo"
READS = tuple(o[2:] for o in ("--preflight", "--progress", "--status", "--collect"))
USAGE = ("usage: wk bench ab --devices <mac> --systems <staged-a>,<staged-b> --workspace <ws> [--plan P]... [--rounds N]\n"
         "           [--max-rounds N] [--detect PCT] [--count N] [--timeout S] [--settle S] [--a-args ...] [--b-args ...] [--plant] [--rehearse]\n"
         "       wk bench ab --devices <mac> --patch <ref|diff> --workspace <ws> [--base <ref>] [--preset P] ...")
BOARD_ONLY = ("release", "builder", "bits", "build_on", "slot", "detach", "task")
SITE = "Library/Python/3.9/lib/python/site-packages"
AGENT = "com.wk.bench-ab"
AUTORUN = BENCH_ROOT + "/wk-tools/lib/wk/bench/autorun.py"
# RunAtLoad alone: KeepAlive would restart a finished benchmark, and the agent removes itself when its job is done.
PLIST = plistlib.dumps({"Label": AGENT, "ProgramArguments": ["/usr/bin/python3", AUTORUN], "RunAtLoad": True, "ProcessType": "Interactive",
                        "StandardOutPath": BENCH_ROOT + "/autorun.agent.log", "StandardErrorPath": BENCH_ROOT + "/autorun.agent.log",
                        "EnvironmentVariables": {"WK_BENCH_ROOT": BENCH_ROOT}}).decode()
DOWN_WAIT, DOWN_POLL = 150, 5
BOOT_SETTLE, BOOT_POLL, BOOT_WAIT = 45, 20, 600   # for its first seconds a restarting machine still answers
PGO_INSTR = "-instr"
PGO_STATE = ".local/state/wk/pgo"
CHECKOUT = "git -C %s checkout -q %s"


def q(*words):
    return shlex.join(str(w) for w in words)


def display_verdict(text, want):
    """(ok, detail): one ONLINE display, and it the declared kind; a second panel changes what MotionMark draws."""
    try:
        doc = json.loads(text)
    except ValueError:
        return record.UNKNOWN, ("wk/mac.py displays did not print JSON" if text
                                else "'wk/mac.py displays' answered nothing -- CoreGraphics could not be asked")
    kind = lambda d: "builtin" if d.get("builtin") else "external"   # noqa: E731
    on = [d for d in doc.get("displays") or [] if d.get("online")]
    shown = ", ".join("%s %sx%s" % ((kind(d),) + tuple(((d.get("points") or []) + ["?", "?"])[:2])) for d in on) or "none"
    if len(on) != 1:
        return False, "%d online display(s): %s" % (len(on), shown)
    if kind(on[0]) != want:
        return False, "the one online display is not the %s panel this machine declares (%s)" % (want, shown)
    return True, "%s alone, as the install that answers here reads it" % shown


class Plant(Task):
    boot_wait = BOOT_WAIT

    def __init__(self, root, reg, clock, spec, o, bench, driver=open_driver):
        super().__init__(root, reg, clock, o)
        self.spec, self.bench, self.make_driver = spec or "", bench, driver
        self.name = self.o.get("devices") or ""
        self.a = self.b = self.task = self.taskdir = self.logs = ""
        self._mgr = self._tools = None

    def check(self):
        o = self.o
        if self.spec:
            die("a Mac's arms are staged builds, not a change resolved in the mirror: drop '%s'.\n%s" % (self.spec, USAGE))
        refuse(o, BOARD_ONLY, "%s is a board A/B's; a Mac A/B is planted on its bench install and runs by itself")
        self.rounds, top, detect, self.plans = check_plan(o, AB_PLANS, PLANTED)
        o.update(max_rounds=str(top), detect="%g" % detect, **{k: o.get(k) or PLANTED[k] for k in ("count", "timeout", "settle")})
        self.preset_name = o.get("preset") or AB_PRESET
        if o.get("systems"):
            if o.get("patch") or o.get("base"):
                die("--systems names two builds already staged; --patch and --base build them. One or the other.")
            self.a, self.b = pair(o["systems"], "systems")
        elif o.get("patch"):
            self.a, self.b = "base", "patched"
        else:
            die(USAGE)
        if not o.get("workspace"):
            die("the task lives in the workspace that builds the arms (--patch) or staged arm A (--systems): --workspace <ws> ('wk ls')")
        require_name(o["workspace"])
        self.ws = o["workspace"]

    def resolve(self):
        conf = fleet.Fleet(self.root, self.env).load(self.name)
        if not conf:
            die("unknown machine '%s' (wk boot --list)" % self.name)
        self.d = self.make_driver(self.root, dict(conf, name=self.name))
        self.d.probe()
        self.mac = side(self.root, self.d)

    def staging_root(self):
        root = self.mac.root_dir()
        if not root:
            die("nothing on %s is readable right now: it answers on neither node, or it is in host mode with its bench\n"
                "    volume not attached. 'wk boot %s --status' says which." % (self.name, self.name))
        return root

    def firmware(self):
        """(ok, detail): the firmware's own default has to be the bench volume, or the restart below needs a human."""
        if not self.mac.measures:
            return True, "starting the guest is the transition"
        grp = self.mac.py(WKMAC, "boot-volume").rsplit(":", 1)[-1]
        if not grp:
            return record.UNKNOWN, "the firmware publishes no boot-volume, so what a restart enters cannot be read"
        bench, host = self.mac.py(WKMAC, "volume-group", self.d.volume()), self.mac.py(WKMAC, "volume-group", "/")
        if bench and grp == bench:
            return True, "%s = '%s', so the restart below needs no human" % (grp, self.d.c("volume"))
        return False, ("%s = the host install, so a restart comes back here and the A/B never runs" % grp if host and grp == host
                       else "%s matches neither install on this disk" % grp)

    def display(self):
        return display_verdict(self.mac.py(WKMAC, "displays"), (self.d.display() or "builtin").split()[0])

    def staged(self, root):
        """(id, sha, gated) per staged build: gated is whether it carries the readings its collection was judged by."""
        out = []
        for ident in self.mac.ls(root + "/staged"):
            d = "%s/staged/%s" % (root, ident)
            stage = self.read_json(d + "/stage.json")
            out.append((ident, (stage or {}).get("webkit_sha") or "", self.mac.run(["sh", "-c", 'ls "$1"/WebKitBuild/*/wk-profile-check.json', "sh", d]).ok))
        return out

    def gates(self):
        """[(ok, what, detail, remedy)] in order; a gate the rest stand on ends the list when it fails."""
        n, mode, rows = self.name, self.d.mode, []
        if mode == "unreachable":
            return [(False, "reachable", "%s does not answer ssh with a key, on its host node or its bench install's" % n,
                     "everything below needs the machine, so nothing else was checked")]
        bench = mode.startswith("bench")
        rows.append((not bench, "host mode", "%s answers and carries no bench marker" % n if not bench else
                     "%s is in BENCH mode (%s) -- the arms and the tools are on the host install, so a plant needs it" % (n, mode[6:]), "")
                    if self.mac.measures else
                    (bench, "a bench install", "%s answers and is marked (%s)" % (n, mode[6:]) if bench else
                     "%s answers but carries no /etc/wk-image, so every leg would be refused" % n, "wk boot %s" % n))
        root, bh = self.mac.root_dir(), self.mac.home_dir() or ""
        rows.append((bool(root), "staging root", root or "%s's driver can see no staging root" % n, ""))
        if not root:
            return rows
        if self.mac.measures:
            done = "provisioning complete" in self.mac.read(os.path.dirname(root) + "/log/wk-bench-firstboot.log")
            rows.append((done, "provisioned", "'%s' has finished a first boot" % self.d.c("volume") if done else "no 'provisioning complete' in its first-boot log",
                         "every leg is refused on an unquieted desktop; on the Mac: wk sysimage build %s --repair, then boot it once"
                         % (self.d.c("image_preset") or "<image-preset>")))
        rows.append((self.mac.test("-w", root), "writable", "%s takes a plant without sudo" % root, ""))
        rows.append((bool(bh) and self.mac.test("-d", bh) and self.mac.test("-w", bh + "/Library"), "bench home",
                     "%s (LaunchAgents installable without sudo)" % bh if bh else "the driver names no bench home", ""))
        if not bh:
            return rows
        alu = self.mac.out(["defaults", "read", bh + "/../../Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
        rows.append((alu == "bench", "autologin", "the bench account logs in at the console" if alu == "bench" else
                     "autoLoginUser is '%s' -- the run would have no session" % (alu or "unset"), ""))
        rows.append((self.mac.test("-d", "%s/%s/objc" % (bh, SITE)), "pyobjc over there", "run-benchmark's prepare_env does a bare 'import objc'", ""))
        rows.append((True, "scipy over there", "installed" if self.mac.test("-d", "%s/%s/scipy" % (bh, SITE)) else
                     "not yet; the plant installs it with this machine's network", ""))
        arms = self.staged(root)
        missing = [] if self.o.get("patch") else [a for a in (self.a, self.b) if a not in [s[0] for s in arms]]
        rows.append((not missing and bool(arms or self.o.get("patch")), "staged builds",
                     "; ".join("%s (%s) gated=%s" % (i, sha[:12], "yes" if g else "no") for i, sha, g in arms) or
                     ("none yet; --patch stages both arms" if self.o.get("patch") else "nothing on the volume"),
                     "not staged: %s -- wk bench deploy <ws> %s --slot <name>" % (", ".join(missing), n) if missing else ""))
        ok, said = self.display()
        rows.append((ok, "one display", said, "Disconnect it: a second panel changes what MotionMark draws. No --force crosses it."))
        ok, said = self.firmware()
        rows.append((ok, "firmware default", said, STARTUP_DISK % self.d.c("volume")))
        ready = self.mac.restart_ready()
        rows.append((ready, "restartable", "this restarts %s itself" % n if ready else self.mac.restart_detail(),
                     "any application can refuse a graceful restart, so an unattended one uses the helper: wk machine setup %s" % n))
        return rows

    def preflight(self):
        info("preflight for an unattended A/B on %s" % self.name)
        rows = self.gates()
        for ok, what, detail, remedy in rows:
            check(ok, what, detail)
            if not ok and remedy:
                log("       " + remedy)
        fails = record.failed(rows)
        log("")
        if fails:
            warn("%d preflight check(s) failed" % len(fails))
        else:
            info("preflight clean" + record.not_measured(len(record.unmeasured(rows))))
        return len(fails)

    def progress(self):
        """The gates as steps, then the planted job's: is it there for these arms, how far it got, whether it was read back."""
        info("the A/B on %s, step by step" % self.name)
        if self.d.mode.startswith("bench"):
            log("  [x] the machine is in bench mode: %s -- the A/B is on it now;  wk bench ab --devices %s --status" % (self.d.mode[6:], self.name))
            return 0
        rows = self.gates()
        for ok, what, detail, remedy in rows:
            log("  %s %s: %s" % ("[x]" if ok else "[?]" if ok is record.UNKNOWN else "[ ]", what, detail))
            if not ok and remedy:
                log("        do: " + remedy)
        root = self.mac.root_dir() if self.d.mode != "unreachable" else ""
        job = self.read_json(root + "/job.json") if root else None
        planted = bool(job) and {a.get("id") for a in job.get("arms") or []} <= {s[0] for s in self.staged(root)}
        log("  %s a job is planted for the staged arms: %s" % ("[x]" if planted else "[ ]", " ".join(a.get("id", "") for a in (job or {}).get("arms") or [])
                                                                 or "none"))
        st = kv(self.mac.read(root + "/autorun.state")) if root else {}
        log("  %s the rounds are done: %s" % ("[x]" if st.get("outcome") else "[ ]", st.get("outcome") or st.get("phase") or "not started"))
        log("  [ ] the result is read back:  wk bench ab --devices %s --collect" % self.name)
        return 0

    def read_json(self, path):
        try:
            return json.loads(self.mac.read(path) or "null")
        except ValueError:
            return None

    def manager(self):
        if self._mgr is None:
            self._mgr = self.mac.manager()
            self._tools = self.mac.tools(self._mgr)
            if not self._tools:
                die("no wk-tools on %s's host install, so nothing there can build or stage an arm. One command puts\n"
                    "    it there, with the privileged helpers:  wk machine setup %s" % (self.name, self.name))
        return self._mgr

    def rwk(self, *words, logged=""):
        m = self.manager()
        argv = ["sh", "-c", "cd %s && ./wk %s" % (shlex.quote(self._tools), q(*words))]
        if not logged:
            return m.run(argv)
        local = argv if m is self.here or not isinstance(m, Ssh) else m.argv(q(*argv))
        log("  %s   (log: %s)" % (" ".join(("wk",) + words), logged))
        return self.here.act_run(["sh", "-c", sched.LOGGED, "sh", logged] + local)

    def guest_sh(self, script, mutates=False):
        """Written to a file and then run: on stdin a script is truncated by the first thing in it that reads stdin."""
        inner = "printf %%s %s | base64 -d > /tmp/wk-guest.sh && bash /tmp/wk-guest.sh" % base64.b64encode(script.encode()).decode()
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "wk-" + self.ws, inner]
        m = self.manager()
        r = m.act_run(argv) if mutates else m.run(argv)
        return r.__class__(r.rc, r.out.replace("\r", ""), r.err)

    def guest_src(self):
        src = (self.guest_sh('for p in ~/WebKit ~/webkit; do [ -d "$p/.git" ] && { echo "$p"; exit 0; }; done').out.split() or [""])[0]
        if not src:
            die("no WebKit checkout found in the build guest '%s'" % self.ws)
        return src

    def build_and_stage(self, label, slot):
        info("  building %s" % label)
        path = os.path.join(self.logs, "build-%s.log" % slot)
        if not self.rwk("build", self.ws, self.preset_name, logged=path).ok:
            die("the %s build failed; its log is %s" % (label, path))
        info("  staging %s as '%s'" % (label, slot))
        if not self.rwk("bench", "deploy", self.ws, self.name, "--slot", slot, "--preset", self.preset_name, *[w for p in self.plans for w in ("--plan", p)],
                        logged=os.path.join(self.logs, "stage-%s.log" % slot)).ok:
            die("staging %s failed" % label)
        measured = presets.resolve(self.preset_name, "macos", "vm", self.env).build_dir(self.guest_src())
        dirs = q(measured, measured + PGO_INSTR)   # a profile-guided arm leaves ~100 GB of products in the guest, spent once staged
        said = self.guest_sh("du -sk %s 2>/dev/null | awk '{s+=$1} END {print int(s/1048576)}'\nrm -rf %s\n"
                             "df -g / | awk 'NR==2 {print $4}'" % (dirs, dirs), mutates=True).out.split() + ["?", "?"]
        log("  reclaimed %s's products (%s GB); %s GB free in the guest now" % (label, said[0], said[1]))
        return slot

    def build_ab(self):
        base, patch = self.o.get("base") or "", self.o["patch"]
        if act.dry_run():
            log("  would build the baseline (%s) in '%s' and stage it as '%s'" % (base or "current HEAD", self.ws, self.a))
            log("  would then apply '%s' and stage that as '%s'" % (patch, self.b))
            return
        self.rwk("start", self.ws)
        src = self.guest_src()
        orig = (self.guest_sh("git -C %s symbolic-ref --quiet --short HEAD 2>/dev/null || git -C %s rev-parse HEAD" % (q(src), q(src)))
                .out.split() or [""])[0]
        if not orig:
            die("could not read the guest checkout's current ref")
        log("  checkout %s; '%s' is restored when done" % (src, orig))
        base = base or orig
        if not self.guest_sh("set -e; " + CHECKOUT % (q(src), q(base)), mutates=True).ok:
            die("could not check out the baseline '%s' in the guest" % base)
        self.build_and_stage("baseline (%s)" % base, self.a)
        if os.path.isfile(patch):
            with open(patch, "rb") as f:
                diff = base64.b64encode(f.read()).decode()
            step = "set -e; printf %%s %s | base64 -d > /tmp/wk-ab.patch; git -C %s apply --index /tmp/wk-ab.patch" % (diff, q(src))
            why = "the patch did not apply cleanly to '%s'" % base
        else:
            step, why = "set -e; " + CHECKOUT % (q(src), q(patch)), "no such ref '%s' in the guest checkout" % patch
        restore = CHECKOUT % (q(src), "-f " + q(orig))
        if not self.guest_sh(step, mutates=True).ok:
            self.guest_sh(restore, mutates=True)
            die(why + "; the tree has been put back")
        self.build_and_stage("patched (%s)" % patch, self.b)
        if not self.guest_sh(restore, mutates=True).ok:
            warn("  could not restore '%s' in the guest -- the tree is left on the patched ref" % orig)
        info("  stopping the build guest")   # a running macOS VM competes for CPU with what runs next
        self.rwk("stop", self.ws)

    def fields(self):
        return dict(self.shape("%s=%s" % (self.name, self.preset_name), self.plans, [self.a, self.b]),
                    **{"subject.kind": "slots", "subject.spec": "%s,%s" % (self.a, self.b), "count": self.o["count"], "timeout": self.o["timeout"]})

    def command(self):
        words = ["wk", "bench", "ab", "--devices", self.name, "--systems", "%s,%s" % (self.a, self.b), "--workspace", self.ws, "--rounds", str(self.rounds)]
        words += flags(self.o, ("max_rounds", "detect", "count", "timeout", "settle"))
        return " ".join(words + [w for p in self.plans for w in ("--plan", p)])

    def job(self, declared):
        o = self.o
        return dict({"task": self.task, "workspace": self.ws, "machine": self.name, "plans": list(self.plans), "rounds": str(self.rounds),
                     "max_rounds": o["max_rounds"], "detect": o["detect"], "timeout": o["timeout"], "count": o["count"], "display": declared,
                     "settle": o["settle"], "rehearsal": "1" if o.get("rehearse") else "",
                     "arms": [{"label": "A", "id": self.a, "browser_args": o.get("a_args") or ""},
                              {"label": "B", "id": self.b, "browser_args": o.get("b_args") or ""}],
                     "created_at": self.clock.iso(), "created_by": wkrecord.host_name(self.here)}, **variance(self.env))

    def create_task(self, stamp):
        self.task = "%s-%s-mac-ab" % (stamp, self.name)
        self.home, bench = record.ws_home(self.reg, self.ws)
        self.taskdir, self.logs = os.path.join(bench, self.task), record.driver_logs(self.reg.store, self.task)
        if act.dry_run():
            return
        record.held((self.home, bench), self.ws)
        self.new_task((self.home, bench), self.task, self.fields(), self.command())
        self.here.mkdir_now(self.logs)

    def put_file(self, src, dest, what):
        """The channel delivers and what landed is judged here: a transport that wrote nothing still exits 0."""
        want = self.here.run(["sh", "-c", 'wc -c < "$1"', "sh", src]).out.strip()
        got = "" if self.mac.put_file(src, dest) else self.mac.out(["sh", "-c", 'wc -c < "$1"', "sh", dest]).strip()
        if not want or want != got:
            die("could not %s: %s is %s bytes, expected %s" % (what, dest, got or "unreadable", want or "unreadable"))

    def deliver(self, name, text, dest, what):
        local = os.path.join(self.logs, name)
        self.here.write(local, text)
        self.put_file(local, dest, what)

    def put_tree(self, src, dest, what):
        """Every file verified, not a sentinel: a tree stale in one file behaves as an older build, after the reboot."""
        ex = [w for x in PUT_SKIP for w in ("--exclude", x)]
        want = self.here.run(["python3", os.path.join(self.root, "lib", "treehash.py"), src] + ex).out.strip()
        got = "" if self.mac.put(src, dest, *PUT_SKIP) else (self.mac.py("lib/treehash.py", dest, *ex).splitlines() or [""])[-1]
        if not want or want != got:
            die("could not %s: %s hashes %s, this tree %s" % (what, dest, got or "unreadable", want or "unreadable"))
        log("  verified: %s is this tree file for file (%s)" % (dest, want[:16]))

    def show(self):
        o = self.o
        info("plan: %s, %s round(s), interleaved%s" % (" ".join(self.plans), self.rounds if float(o["detect"]) == 0 else "%d-%s" % (self.rounds, o["max_rounds"]),
                                                     "; no precision goal" if float(o["detect"]) == 0 else ", until it resolves %s%%" % o["detect"]))
        if o.get("rehearse"):
            warn("  --rehearse: every leg is forced past its preflight and recorded as forced; it measures the path, not the machine")
        for label, arm, args in (("A", self.a or "built from %s" % (o.get("base") or "HEAD"), o.get("a_args")),
                                 ("B", self.b or "built with %s" % o.get("patch"), o.get("b_args"))):
            log("  arm %s: %s%s" % (label, arm, "  args: " + args if args else ""))
        for i, p in enumerate(self.plans):
            log("  cost  " + cost_line(self.reg, [(self.ws, "")], self.name, p, o["count"], (2 if i == 0 else 0) + 2 * self.rounds)[0])

    def plant(self):
        declared = self.d.display()
        if not declared:
            die("%s declares no display: add its mode to machines/%s.conf, in points, kind first, e.g. display=\"builtin 1470x956\"\n"
                "    ('python3 lib/wk/mac.py displays' on that install prints both)" % (self.name, self.name))
        root, bh = self.staging_root(), self.mac.home_dir()
        if not bh:
            die("%s's driver names no bench home, so nothing can be planted for its account" % self.name)
        staged = self.mac.ls(root + "/staged")
        for arm in () if self.o.get("patch") else (self.a, self.b):
            if arm not in staged:
                die("no staged build '%s' on %s. There is:\n%s" % (arm, self.name, "\n".join("    " + s for s in staged) or "    nothing"))
        if act.dry_run():
            for line in ("sync wk-tools to %s/wk-tools" % root, "install scipy for the bench account if it lacks it", "plant samply and the tailnet payload",
                         "write the job and the task record %s to %s" % (self.task, root), "reset %s/autorun.state" % root,
                         "turn the screensaver off and Do Not Disturb on for the bench account, each read back",
                         "install %s/Library/LaunchAgents/%s.plist" % (bh, AGENT)):
                log("  would " + line)
            return
        info("  syncing wk-tools onto the volume")   # /var/wk: the first-boot daemon rsyncs over ~bench's own checkout
        self.put_tree(self.root, root + "/wk-tools", "sync wk-tools onto the bench volume")
        site = "%s/%s" % (bh, SITE)
        if not self.mac.test("-d", site + "/scipy"):
            info("  installing scipy into the bench account's site-packages")
            if not self.mac.run(["/usr/bin/python3", "-m"] + "pip install --quiet --target".split() + [site, "scipy"], mutates=True).ok:
                die("scipy did not install into %s on %s, and run-benchmark compares the rounds with it; this machine needs a network" % (site, self.name))
        self.quiet_account(bh, root)
        arch = self.mac.out(["uname", "-m"])
        binary = samply.fetch(self.here, self.reg.store.cache_dir(), arch, "Darwin")
        if not binary:
            die("no samply for %s here, and the warmup round profiles each arm with it" % (arch or "that machine"))
        dest = samply.store_dir(root + "/cache", samply.triple(arch, "Darwin")) + "/samply"
        self.put_file(binary, dest, "plant samply for the warmup round")
        self.mac.run(["chmod", "0755", dest], mutates=True)
        from wk.sysimage.mactailnet import Tailnet
        info("  collecting the tailnet payload")   # the half that needs a network, Go and this machine's auth key
        got = Tailnet(self.here, self.env).collect(self.name, os.path.join(self.reg.store.state_dir(), "mac-tailnet", "collected"))
        self.put_tree(got, root + "/tailnet", "put the tailnet payload on the volume")
        info("  writing the job and its task record")
        self.deliver("job.json", json.dumps(self.job(declared), indent=2) + "\n", root + "/job.json", "write the job onto the volume")
        task = os.path.join(root, "results", self.task)
        self.mac.run(["mkdir", "-p", task + "/runs"], mutates=True)
        self.deliver("task.json", self.home.read(os.path.join(self.taskdir, "task.json")), task + "/task.json", "write the task record onto the volume")
        # reset here and nowhere else: the autorun only advances it
        self.deliver("planted.state", "phase=planted\ntask=%s\nattempts=0\nplanted_at=%s\n" % (self.task, self.clock.iso()),
                     root + "/autorun.state", "reset the autorun's state on the volume")
        self.deliver(AGENT + ".plist", PLIST, "%s/Library/LaunchAgents/%s.plist" % (bh, AGENT), "install the launch agent")
        info("planted: %s" % self.task)
        log("  task   %s   ('wk status' lists it; 'wk bench report %s' reads it)" % (self.taskdir, self.task))
        log("  log    %s/autorun.log   ('wk bench ab --devices %s --status' reads it, in either mode)" % (root, self.name))

    def quiet_account(self, bh, root):
        """A lock mid-run and a banner over the browser are both invisible to every later gate, so both are refused here."""
        uuid = next((l.split('"')[3] for l in self.mac.out(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]).splitlines()
                     if "IOPlatformUUID" in l and l.count('"') >= 4), "")
        saver = "%s/Library/Preferences/ByHost/com.apple.screensaver.%s" % (bh, uuid)
        for argv in (["mkdir", "-p", bh + "/Library/Preferences/ByHost"], ["defaults", "write", saver, "idleTime", "-int", "0"],
                     ["defaults", "write", bh + "/Library/Preferences/com.apple.screensaver", "askForPassword", "-int", "0"]):
            self.mac.run(argv, mutates=True)
        idle = self.mac.out(["defaults", "read", saver, "idleTime"])
        if idle != "0":
            act.barrier("could not disable the screensaver on %s -- idleTime reads '%s', and a benchmark makes no input, so the\n"
                        "    screen lock would end a run in silence. Nothing has been rebooted." % (self.name, idle or "unreadable"), env=self.env)
        log("  screen lock: screensaver disabled on the volume (idleTime=0, verified)")
        dnd = (self.mac.run(lib_argv(root + "/wk-tools", DESKTOP, "wk_quiet_dnd_on", bh), mutates=True).out.split() or [""])[-1]
        if dnd != "on":
            act.barrier("could not turn Do Not Disturb on for the bench account -- it reads '%s', and no later gate sees a banner.\n"
                        "    Nothing has been rebooted." % (dnd or "unreadable"), env=self.env)
        log("  notifications: Do Not Disturb on for the bench account (verified)")

    def restart(self):
        """The display and the firmware default are asked again seconds before the transition; a reading that is wrong or unread stops it."""
        for what, ask in (("the display", self.display), ("the firmware default", self.firmware)):
            ok, said = ask()
            if not ok:
                die("%s on %s %s: %s\n    Nothing has been rebooted, and the job stays planted. Fix it and re-run, or reboot\n"
                    "    %s by hand once it reads right -- the planted job runs by itself either way." %
                    (what, self.name, "could not be read" if ok is record.UNKNOWN else "is not right", said, self.name))
        self.boot_before = self.d.boot_id()
        info("go: reboot %s now (boot before: %s)" % (self.name, self.boot_before or "unknown"))
        self.d.reboot()
        if not self.clock.wait_until(lambda: not self.mac.test("-d", "/"), DOWN_WAIT, DOWN_POLL):
            die("could not reboot %s -- it is still answering (a refused reboot exits 0). Reboot it by hand or from the\n"
                "    startup manager and the planted job runs." % self.name)
        info("  %s is going down" % self.name)
        if not self.mac.measures:
            self.d.arm()   # a guest's reboot is its stop, and starting it again is the transition

    def wait(self):
        info("wait: up to %d minutes for %s to answer on either node" % (self.boot_wait // 60, self.name))
        self.clock.sleep(BOOT_SETTLE)
        start, said = self.clock.monotonic(), False
        while True:
            mode = self.d.probe()
            if mode.startswith("bench"):
                info("  %s answers in BENCH mode (%s)" % (self.name, mode[6:]))
                return "bench"
            if mode == "host":
                if self.boot_before and self.d.boot_id() == self.boot_before:   # asked only here: in bench mode it is another install's
                    warn("  %s is answering on the SAME boot -- it never rebooted" % self.name)
                    return "noreboot"
                info("  %s is back in HOST mode" % self.name)
                return "host"
            if self.clock.monotonic() - start >= self.boot_wait:
                warn("  %s has answered on neither node in %ds" % (self.name, self.boot_wait))
                return "silent"
            if not said:
                log("  no answer on either node yet -- this is the reboot itself")
                said = True
            self.clock.sleep(BOOT_POLL)

    def outcome(self, came):
        n, vol = self.name, self.d.c("volume") or "the bench install"
        if came == "bench":
            return info("%s answers in BENCH mode -- the A/B is running there. 'wk bench ab --devices %s --status' follows it;\n"
                        "  when the job ends it holds a while and powers off, and --collect reads it from the host install." % (n, n))
        if came == "silent":
            return info("%s answers on neither node: still restarting, halted, or its join did not come up. Each leg is written\n"
                        "  to the volume as it ends:  wk bench ab --devices %s --status   once one answers." % (n, n))
        why, fix = {"host": ("came back to host mode", "the reboot did not enter '%s'; %s" % (vol, STARTUP_DISK % vol)),
                    "noreboot": ("never rebooted", "reboot %s by any means, the startup manager too, and it runs by itself" % n)}[came]
        warn("%s %s, so the A/B has not run. The job is planted and still valid: %s." % (n, why, fix))

    def go(self):
        self.check()
        self.resolve()
        if self.mac.measures and self.d.ch.here():
            die("this reboots %s, so it cannot be driven from %s -- the reboot would take the driver with it.\n"
                "    Run it from another machine." % (self.name, self.name))
        if self.preflight():
            if act.dry_run():
                warn("preflight failed; showing the plan anyway because this is --dry-run")
            else:
                act.barrier("the preflight on %s failed, and nothing there has been changed yet. Each failure is something\n"
                            "    a run discovers after the reboot, in bench mode, where nothing can report it." % self.name)
        self.show()
        plant_only = bool(self.o.get("plant"))
        if not act.confirm("plant this A/B on %s%s?" % (self.name, "" if plant_only else " and restart it into its bench install")):
            die("not run")
        self.create_task(self.clock.stamp())
        try:
            if self.o.get("patch"):
                self.build_ab()
            self.plant()
            if act.dry_run():
                info("dry run -- nothing on %s was changed and nothing was rebooted" % self.name)
                log("  not checked here: whether a leg would pass on the bench install. That gate reads the running system;\n"
                    "  in bench mode, ask it:  wk bench run %s %s --system %s --slot %s --dry-run" % (self.ws, self.plans[0], self.name, self.a))
                return 0
            if plant_only:
                info("planted and not started. The A/B runs the next time the bench install boots.")
                return 0
            self.restart()
            self.outcome(self.wait())
            return 0
        finally:
            self.lock.release_all()

    def back(self):
        verbs = [k for k in READS if self.o.get(k)]
        if len(verbs) > 1:
            die("--%s: one reading at a time" % " and --".join(verbs))
        extra = [k for k, v in self.o.items() if v and k not in READS + ("devices",)]
        if self.spec or extra:
            die("--%s reads a planted job and takes only --devices <mac> (got %s)" % (verbs[0], self.spec or flags(self.o, extra)[0]))
        self.resolve()
        return {"preflight": lambda: 1 if self.preflight() else 0, "progress": self.progress,
                "status": lambda: self.read(False), "collect": lambda: self.read(True)}[verbs[0]]()

    def read(self, report):
        root, mode = self.staging_root(), self.d.mode
        info("%s is in bench mode (%s) -- this is the run itself, read over its own node" % (self.name, mode[6:]) if mode.startswith("bench")
             else "%s is in host mode, and this is read off the volume it mounts" % self.name)
        st = self.mac.read(root + "/autorun.state")
        log("\n".join("  " + l for l in st.splitlines()) or "  no autorun state -- the agent never ran")
        log("  last 20 lines of the autorun log:\n" + "\n".join("    " + l for l in self.mac.out(["tail", "-20", root + "/autorun.log"]).splitlines()))
        task = kv(st).get("task", "")
        hit = self.bench.find(task) if task else None
        if not hit:
            warn("no task planted on %s has a record this machine reaches; its results stay on the volume" % self.name)
            return 1
        m, taskdir = hit
        m.write_own(os.path.join(taskdir, "autorun.state"), st)
        got = self.mac.run(["sh", "-c", 'cd "$1" && tar -cf - runs warmup 2>/dev/null | base64', "sh", "%s/results/%s" % (root, task)])
        packed = os.path.join(taskdir, "volume.tar.b64")
        if got.ok and got.out.strip():
            m.write(packed, got.out)
            if not m.act_run(["sh", "-c", 'base64 -d < "$1" | tar -xf - -C "$2"', "sh", packed, taskdir]).ok:
                warn("the volume's runs did not unpack onto %s" % taskdir)
            m.remove(packed)
        info("%s: %s" % (task, record.task_state(taskdir, False)["summary"] if not isinstance(m, Ssh) else "read onto %s" % taskdir))
        if report:
            self.reported(lambda: self.bench.task_report(task, False, True), task)
        return 0
