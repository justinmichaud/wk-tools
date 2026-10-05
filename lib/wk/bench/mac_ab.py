"""`wk bench ab --devices <mac>`: the A/B planted on the Mac's benchmark install, run there at autologin, read back here."""

import base64
import json
import os
import plistlib
import shlex
import statistics

from wk import act, fleet, job, notify, presets, record as wkrecord, samply as wksamply, sched
from wk.act import Refused, die, info, log, warn
from wk.bench import ab, board_ab, pipeline, record
from wk.bench.systems import first_line
from wk.boot import open_driver
from wk.boot.mac import BENCH_ROOT, Script
from wk.kv import kv
from wk.lock import Lock
from wk.machine import Ssh
from wk.workspace import require_name
from wk.bench.mac import MARKER, PUT_SKIP, WKMAC


AB_PLANS = ("jetstream3", "speedometer3", "motionmark")
AB_PRESET = "mac-release-pgo"
AB_DEFAULTS = (("count", "2"), ("timeout", "1800"), ("settle", "90"))
DETECT = "0.3"   # nobody is in the room to extend a run, so a Mac's rounds go on until they resolve a third of a per cent
READS = ("preflight", "progress", "status", "collect")
MAC_USAGE = ("usage: wk bench ab --devices <mac> --systems <staged-a>,<staged-b> [--plan P]... [--rounds N] [--max-rounds N]\n"
             "           [--detect PCT] [--count N] [--timeout S] [--settle S] [--a-args ...] [--b-args ...] [--plant] [--rehearse]\n"
             "           --workspace <ws>\n"
             "       wk bench ab --devices <mac> --patch <ref|diff> --workspace <ws> [--base <ref>] [--preset P] ...")
BOARD_ONLY = ("release", "builder", "bits", "build_on", "slot", "detach", "task")
SITE = "Library/Python/3.9/lib/python/site-packages"
AGENT = "com.wk.bench-ab"
AUTORUN = BENCH_ROOT + "/wk-tools/lib/wk/bench/autorun.py"
# RunAtLoad alone: KeepAlive would restart a finished benchmark, and the agent removes itself when its job is done.
PLIST = plistlib.dumps({"Label": AGENT, "ProgramArguments": ["/usr/bin/python3", AUTORUN], "RunAtLoad": True, "ProcessType": "Interactive",
                        "StandardOutPath": BENCH_ROOT + "/autorun.agent.log", "StandardErrorPath": BENCH_ROOT + "/autorun.agent.log",
                        "EnvironmentVariables": {"WK_AB_ROOT": BENCH_ROOT}}).decode()
DOWN_WAIT, DOWN_POLL = 150, 5
BOOT_SETTLE, BOOT_POLL, BOOT_WAIT = 45, 20, 600   # for its first seconds a restarting machine still answers
PGO_INSTR = "-instr"
PGO_STATE = ".local/state/wk/pgo"
CHECKOUT = "git -C %s checkout -q %s"


def q(*words):
    return shlex.join(str(w) for w in words)


def stripped(r):
    return r.__class__(r.rc, r.out.replace("\r", ""), r.err)


class OnMac(Script):
    """A bench/onboard/mac-* file: the Mac A/B's side of the measured install, read verbatim."""

    where = os.path.join("bench", "onboard")


class Remote:
    """The measured install, over whichever channel its driver's probe answered on; each command a named file."""

    def __init__(self, root, driver):
        self.root, self.d = str(root), driver

    def call(self, script, mutates=False):
        return stripped(self.d.ch.call("r_ssh", script, mutates=mutates))

    def run(self, name, mutates=False, **params):
        return self.call(OnMac(self.root, name, **params), mutates=mutates)

    def out(self, name, **params):
        r = self.run(name, **params)
        return r.out.strip() if r.ok else ""

    def test(self, flag, path):
        return self.call(Script(self.root, "mac-test.sh", WK_TEST=flag, WK_PATH=path)).ok

    def read(self, path):
        return self.call(Script(self.root, "mac-read.sh", WK_PATH=path)).out

    def py(self, rel, *args):
        """A tree file run by the install's python3, as a parameter: a guest's channel carries no stdin."""
        with open(os.path.join(self.root, rel)) as f:
            params = dict(("WK_%d" % (i + 1), a) for i, a in enumerate(args))
            return self.out("mac-py.sh", WK_PY=f.read(), **params)


def display_verdict(text, want):
    """(ok, detail): one ONLINE display, and it the declared kind; a second panel changes what MotionMark draws."""
    try:
        doc = json.loads(text)
    except ValueError:
        return record.UNKNOWN, ("wk/mac.py displays did not print JSON" if text
                                else "'wk/mac.py displays' answered nothing -- CoreGraphics could not be asked")

    def kind(d):
        return "builtin" if d.get("builtin") else "external"

    def one(d):
        p = (d.get("points") or []) + ["?", "?"]
        return "%s %sx%s" % (kind(d), p[0], p[1])

    on = [d for d in doc.get("displays") or [] if d.get("online")]
    shown = ", ".join(one(d) for d in on) or "none"
    if len(on) != 1:
        return False, "%d online display(s): %s" % (len(on), shown)
    if kind(on[0]) != want:
        return False, "the one online display is not the %s panel this machine declares (%s)" % (want, shown)
    return True, "%s alone, as the install that answers here reads it" % shown


class MacAB:
    """`wk bench ab --devices <mac>`: no session this side survives the reboot into the benchmark install, so the job
    is planted on it while it is merely mounted, and a LaunchAgent starts it at autologin."""

    boot_wait = BOOT_WAIT

    def __init__(self, root, reg, clock, spec, o, bench, driver=open_driver):
        """`bench` finds a task in any store this machine reaches and reports it (lib/wk/bench/cli.py's Bench)."""
        self.root, self.reg, self.clock, self.spec, self.o, self.bench = str(root), reg, clock, spec or "", dict(o), bench
        self.here, self.env, self.make_driver = reg.machine, reg.env, driver
        self.name = self.o.get("devices") or ""
        self.lock = Lock(reg.store, self.here, clock)
        self.a = self.b = self.fw_detail = self.task = self.taskdir = self.logs = ""
        self._mgr = self._tools = None

    def check(self):
        o = self.o
        if self.spec:
            die("a Mac's arms are staged builds, not a change resolved in the mirror: drop '%s'.\n%s" % (self.spec, MAC_USAGE))
        given = [k for k in BOARD_ONLY if o.get(k)]
        if given:
            die("--%s is a board A/B's; a Mac A/B is planted on its benchmark install and runs by itself" % given[0].replace("_", "-"))
        self.rounds, self.plans = ab.check_plan(o, AB_PLANS)
        top, detect = board_ab.stopping(o, self.rounds, DETECT)
        o["max_rounds"], o["detect"] = str(top), "%g" % detect
        for key, default in AB_DEFAULTS:
            o[key] = o.get(key) or default
        for key in ("count", "settle"):
            if not o[key].isdigit():
                die("--%s takes a number (got '%s')" % (key, o[key]))
        self.preset_name = o.get("preset") or AB_PRESET
        if o.get("systems"):
            if o.get("patch") or o.get("base"):
                die("--systems names two builds already staged; --patch and --base build them. One or the other.")
            self.a, self.b = board_ab.pair(o["systems"], "systems")
        elif not o.get("patch"):
            die(MAC_USAGE)
        if not o.get("workspace"):
            die("the task lives in the workspace that builds the arms (--patch) or staged arm A (--systems): --workspace <ws> ('wk ls')")
        require_name(o["workspace"])
        self.ws = o["workspace"]

    def resolve(self):
        conf = fleet.Fleet(self.root, self.env).load(self.name)
        if not conf:
            die("unknown machine '%s' (wk boot --list)" % self.name)
        self.conf = dict(conf, name=self.name)
        self.d = self.make_driver(self.root, self.conf)
        self.d.probe()
        self.guest = self.d.arming == "guest"
        self.mac = Remote(self.root, self.d)

    # -- what preflight reads
    def firmware_is_bench(self):
        """The firmware's own default has to be the bench volume, or the restart below needs a human."""
        grp = self.mac.py(WKMAC, "boot-volume").rsplit(":", 1)[-1]
        if not grp:
            self.fw_detail = "the firmware publishes no boot-volume, so what a restart enters cannot be read"
            return record.UNKNOWN
        bench = self.mac.py(WKMAC, "volume-group", self.d.volume())
        host = self.mac.py(WKMAC, "volume-group", "/")
        if bench and grp == bench:
            self.fw_detail = "%s = '%s', so the restart below needs no human" % (grp, self.d.c("volume"))
            return True
        self.fw_detail = ("%s = the host install, so a restart comes back here and the A/B never runs" % grp if host and grp == host
                          else "%s matches neither install on this disk" % grp)
        return False

    def display_check(self):
        want = (self.d.display() or "builtin").split()[0]
        return display_verdict(self.mac.py(WKMAC, "displays"), want)

    def firstboot_log(self, root):
        return os.path.dirname(root) + "/log/wk-bench-firstboot.log"

    def provisioned(self, root):
        """The volume's first boot logs its completion line last and then deletes itself, so the log is the record."""
        return "provisioning complete" in self.mac.read(self.firstboot_log(root))

    def staged_ids(self, root):
        return sorted(self.mac.out("mac-ls.sh", WK_PATH=root + "/staged").split())

    def preflight(self):
        info("preflight for an unattended A/B on %s" % self.name)
        fails, unknown = [], []

        def ck(ok, what, detail, *remedy):
            pipeline.Run.check(ok, what, detail)
            if ok is record.UNKNOWN:
                unknown.append(what)
            elif not ok:
                fails.append(what)
            if not ok:
                for line in remedy:
                    log("       " + line)

        mode, n = self.d.mode, self.name
        if mode == "unreachable":
            ck(False, "reachable", "%s does not answer ssh with a key, on its host node or its benchmark install's" % n)
            log("  everything below needs the machine, so nothing else was checked.")
            return len(fails)
        bench = mode.startswith("bench")
        if self.guest:
            ck(bench, "a benchmark install", "%s answers and is marked (%s)" % (n, mode[6:]) if bench else
               "%s answers but carries no /etc/wk-image, so every leg would be refused" % n)
        else:
            ck(not bench, "host mode", "%s answers and carries no bench marker" % n if not bench else
               "%s is in BENCH mode (%s) -- the arms and the tools are on the host install, so a plant needs it" % (n, mode[6:]))
        root = self.d.bench_root()
        ck(bool(root), "staging root", root or "%s's driver can see no staging root" % n)
        if not root:
            return len(fails)
        if not self.guest:
            done = self.provisioned(root)
            ck(done, "provisioned", "'%s' has finished a first boot" % self.d.c("volume") if done
               else "no 'provisioning complete' in %s" % self.firstboot_log(root),
               "every leg is refused on an unquieted desktop; on the Mac: wk sysimage build %s --repair, then boot it once"
               % (self.d.c("image_preset") or "<image-preset>"))
        ck(self.mac.test("-w", root), "writable", "%s takes a plant without sudo" % root)
        bh = self.d.bench_home() or ""
        ck(self.mac.test("-d", bh) and self.mac.test("-w", bh + "/Library") if bh else False, "bench home",
           "%s (LaunchAgents installable without sudo)" % bh if bh else "the driver names no bench home")
        if not bh:
            log("  the checks below are relative to it, so nothing else was checked.")
            return len(fails)
        alu = self.mac.out("mac-defaults.sh", WK_PATH=bh + "/../../Library/Preferences/com.apple.loginwindow", WK_KEY="autoLoginUser")
        ck(alu == "bench", "autologin", "the bench account logs in at the console" if alu == "bench" else
           "autoLoginUser is '%s' -- the run would have no session" % (alu or "unset"))
        ck(self.mac.test("-d", "%s/%s/objc" % (bh, SITE)), "pyobjc over there", "run-benchmark's prepare_env does a bare 'import objc'")
        if not self.mac.test("-d", "%s/%s/scipy" % (bh, SITE)):
            log("  note scipy is not on the bench install; the plant installs it (needs this machine's network)")
        if self.mac.test("-f", bh + "/../../Library/LaunchDaemons/com.wk.bench-firstboot.plist"):
            log("  note the first-boot daemon is still installed; the autorun stands aside until it has provisioned")
        staged = self.staged_ids(root)
        ck(bool(staged or self.o.get("patch")), "staged builds", " ".join(staged) or
           ("none yet; --patch stages both arms" if self.o.get("patch") else "nothing on the volume, and no --patch to build from"))
        ok, said = self.display_check()
        ck(ok, "one display", said, "Disconnect it: a second panel changes what MotionMark draws. No --force crosses it.")
        if self.guest:
            ck(True, "enters bench mode", "starting the guest is the transition")
        else:
            ck(self.firmware_is_bench(), "firmware default", self.fw_detail,
               "wk boot %s arms it (or pick '%s' in the startup manager); --plant reboots nothing." % (n, self.d.c("volume")))
        ready = self.d.restart_ready()
        ck(ready, "restartable", "this restarts %s itself" % n if ready else self.d.restart_detail(),
           "any application can refuse a graceful restart, so an unattended one uses the helper: wk machine setup %s" % n)
        log("")
        if fails:
            warn("%d preflight check(s) failed" % len(fails))
        else:
            info("preflight clean" + record.not_measured(len(unknown)))
        return len(fails)

    # -- the machine that builds and stages the arms
    def manager(self):
        if self._mgr is None:
            self._mgr = self.d.manager()
            self._tools = self.d.manager_tools(self._mgr)
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
        return stripped(m.act_run(argv) if mutates else m.run(argv))

    def guest_src(self):
        src = (self.guest_sh('for p in ~/WebKit ~/webkit; do [ -d "$p/.git" ] && { echo "$p"; exit 0; }; done').out.split() or [""])[0]
        if not src:
            die("no WebKit checkout found in the build guest '%s'" % self.ws)
        return src

    def stage_plans(self):
        words, m = [], self.manager()
        for p in self.plans:
            said = self.rwk("bench", "seed", self.ws, p).out.replace("\r", "").split()
            payload = said[-1] if said else ""
            if payload.startswith("/") and m.isdir(payload):
                log("  payload pinned: %s -> %s" % (p, payload))
                words += ["--plan", p, "--payload", payload]
            elif self.o.get("allow_network_fetch"):
                warn("  %s is not pinned, and --allow-network-fetch was given: that leg clones the benchmark itself, so the\n"
                     "  benchmark install needs a network and the two arms could get different revisions of it." % p)
                words += ["--plan", p]
            else:
                die("the %s payload could not be pinned, so that leg would need a network after the reboot. Pin it:\n"
                    "    wk bench seed %s %s   then re-run; or, for an install with a route out, --allow-network-fetch" % (p, self.ws, p))
        return words

    def build_and_stage(self, label, slug):
        root = self.d.bench_root()
        before = set(self.staged_ids(root))
        info("  building %s" % label)
        path = os.path.join(self.logs, "build-%s.log" % slug)
        if not self.rwk("build", self.ws, self.preset_name, logged=path).ok:
            die("the %s build failed; its log is %s" % (label, path))
        plans = self.stage_plans()
        info("  staging %s" % label)
        if not self.rwk("bench", "stage", self.ws, "--to", self.name, "--preset", self.preset_name, *plans,
                        logged=os.path.join(self.logs, "stage-%s.log" % slug)).ok:
            die("staging %s failed" % label)
        new = sorted(set(self.staged_ids(root)) - before)
        if not new:
            die("staging %s produced no new directory on %s" % (label, self.d.c("volume") or self.name))
        log("  %s staged as %s" % (label, new[-1]))
        self.reclaim(label)
        return new[-1]

    def reclaim(self, label):
        """A profile-guided arm leaves ~100 GB of products in the guest, and once it is staged both trees are spent."""
        measured = presets.resolve(self.preset_name, "macos", "vm", self.env).build_dir(self.guest_src())
        dirs = q(measured, measured + PGO_INSTR)
        said = self.guest_sh("du -sk %s 2>/dev/null | awk '{s+=$1} END {print int(s/1048576)}'\nrm -rf %s\n"
                             "df -g / | awk 'NR==2 {print $4}'" % (dirs, dirs), mutates=True).out.split() + ["?", "?"]
        log("  reclaimed %s's products (%s GB); %s GB free in the guest now" % (label, said[0], said[1]))

    def build_ab(self):
        base, patch = self.o.get("base") or "", self.o["patch"]
        if act.dry_run():
            log("  would build the baseline (%s) in '%s' and stage it" % (base or "current HEAD", self.ws))
            log("  would then apply '%s' and stage that as the second arm" % patch)
            self.a, self.b = "<baseline %s>" % (base or "HEAD"), "<patched %s>" % patch
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
        self.a = self.build_and_stage("baseline (%s)" % base, "a")
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
        self.b = self.build_and_stage("patched (%s)" % patch, "b")
        if not self.guest_sh(restore, mutates=True).ok:
            warn("  could not restore '%s' in the guest -- the tree is left on the patched ref" % orig)
        info("  stopping the build guest")   # a running macOS VM competes for CPU with what runs next
        self.rwk("stop", self.ws)
        info("arms: A=%s  B=%s" % (self.a, self.b))

    def job(self, declared, stamp):
        o = self.o
        out = {"plans": list(self.plans), "rounds": self.rounds, "max_rounds": int(o["max_rounds"]), "detect_pct": float(o["detect"]),
               "timeout": int(o["timeout"]), "count": o["count"], "display": declared, "settle": int(o["settle"]), "n_arms": 2,
               "arms": [{"label": "A", "id": self.a, "browser_args": o.get("a_args") or ""},
                        {"label": "B", "id": self.b, "browser_args": o.get("b_args") or ""}],
               "wk_tools": BENCH_ROOT + "/wk-tools", "created_at": self.clock.iso(), "created_by": wkrecord.host_name(self.here),
               "stamp": stamp, "rehearsal": "1" if o.get("rehearse") else ""}
        out.update(pipeline.variance(self.env))
        return out

    def command(self):
        words = ["wk", "bench", "ab", "--devices", self.name, "--systems", "%s,%s" % (self.a, self.b), "--workspace", self.ws, "--rounds", str(self.rounds)]
        for key in ("max_rounds", "detect", "count", "timeout", "settle"):
            words += ["--" + key.replace("_", "-"), self.o[key]]
        return " ".join(words + [w for p in self.plans for w in ("--plan", p)])

    def create_task(self, stamp):
        """Recorded through its workspace's machine before the Mac is touched, so a killed run still names what was asked."""
        self.task = "%s-%s-mac-ab" % (stamp, self.name)
        self.home, bench = record.ws_home(self.reg, self.ws)
        self.taskdir, self.logs = os.path.join(bench, self.task), record.driver_logs(self.reg.store, self.task)
        if act.dry_run():
            return
        record.held((self.home, bench), self.ws)
        slots = [self.a or "baseline %s" % (self.o.get("base") or "HEAD"), self.b or "patched %s" % self.o.get("patch")]
        record.new_task(self.home, bench, self.task, self.lock, self.clock.iso(), [
            "devices=%s=%s" % (self.name, self.preset_name), "plans=" + ",".join(self.plans), "rounds=%d" % self.rounds,
            "slots=" + ",".join(slots)], self.command())
        self.here.mkdir_now(self.logs)

    def put_file(self, src, dest):
        """The driver delivers and what landed is judged here: a transport that wrote nothing still exits 0."""
        if self.d.bench_put_file(src, dest):
            return False
        want = self.here.run(["sh", "-c", 'wc -c < "$1"', "sh", src]).out.strip()
        got = self.mac.out("mac-size.sh", WK_PATH=dest)
        if not want or want != got:
            warn("put_file: %s is %s bytes, expected %s" % (dest, got or "unreadable", want or "unreadable"))
        return bool(want) and want == got

    def deliver(self, name, text, dest, what):
        local = os.path.join(self.logs, name)
        self.here.write(local, text)
        if not self.put_file(local, dest):
            die("could not " + what)

    def put_tree(self, src, dest):
        """Every file verified, not a sentinel: a tree stale in one file behaves as an older build, after the reboot."""
        if self.d.bench_put(src, dest, *PUT_SKIP):
            return False
        ex = [w for x in PUT_SKIP for w in ("--exclude", x)]
        want = first_line(self.here.run(["python3", os.path.join(self.root, "lib", "treehash.py"), src] + ex))
        got = (self.mac.py("lib/treehash.py", dest, *ex).splitlines() or [""])[-1]
        if not want or not got:
            warn("put_tree: could not digest %s (%s) or %s (%s), so what landed is unknown" % (src, want, dest, got))
            return False
        if want != got:
            warn("put_tree: %s hashes %s, this tree hashes %s -- what landed is not this tree" % (dest, got, want))
            return False
        log("  verified: %s is this tree file for file (%s)" % (dest, want[:16]))
        return True

    def check_arms(self, root):
        staged = self.staged_ids(root)
        for arm in (self.a, self.b):
            if arm not in staged:
                die("no staged build '%s' on %s. There is:\n%s" % (arm, self.name, "\n".join("    " + s for s in staged) or "    nothing"))

    def show(self):
        o = self.o
        if float(o["detect"]) == 0:
            info("plan: %s, exactly %d round(s), interleaved; no precision goal" % (" ".join(self.plans), self.rounds))
        else:
            info("plan: %s, %d-%s round(s), interleaved, until it resolves %s%%" % (" ".join(self.plans), self.rounds, o["max_rounds"], o["detect"]))
        if o.get("rehearse"):
            warn("  --rehearse: every leg is forced past its preflight and recorded as forced; it measures the path, not the machine")
        log("  arm A: %s%s" % (self.a or "built from %s" % (o.get("base") or "HEAD"), "  args: " + o["a_args"] if o.get("a_args") else ""))
        log("  arm B: %s%s" % (self.b or "built with %s" % o.get("patch"), "  args: " + o["b_args"] if o.get("b_args") else ""))
        for i, p in enumerate(self.plans):
            legs = (2 if i == 0 else 0) + 2 * self.rounds   # the warmup round runs the first plan, one leg per arm
            seen = ab.leg_seconds(self.reg, [(self.ws, "")], self.name, p, o["count"])
            each = statistics.median(seen) if seen else None
            log("  cost  %s: at least %d legs%s" % (p, legs, " x ~%s = ~%s" % (ab.duration(each), ab.duration(legs * each)) if seen
                                                  else "; no leg of it at --count %s measured on %s yet" % (o["count"], self.name)))

    def plant(self):
        declared = self.d.display()
        if not declared:
            die("%s declares no display: add its mode to machines/%s.conf, in points, kind first, e.g. display=\"builtin 1470x956\"\n"
                "    ('python3 lib/wk/mac.py displays' on that install prints both)" % (self.name, self.name))
        root, bh = self.staging_root(), self.d.bench_home()
        if not bh:
            die("%s's driver names no bench home, so nothing can be planted for its account" % self.name)
        if not self.o.get("patch"):
            self.check_arms(root)
        stamp = self.task.split("-", 1)[0]
        if act.dry_run():
            for line in ("sync wk-tools to %s/wk-tools" % root, "point the launch agent at %s" % AUTORUN,
                         "plant samply for the warmup round's profile", "record the task %s in %s and write its job.json" % (self.task, self.taskdir),
                         "copy that job to %s/job.json and reset %s/autorun.state" % (root, root),
                         "turn Do Not Disturb on for the bench account and read it back",
                         "install %s/Library/LaunchAgents/%s.plist" % (bh, AGENT),
                         "on that boot hold %s, dim the panel, check the browser, then measure and power off" % declared):
                log("  would " + line)
            return
        info("  syncing wk-tools onto the volume")   # /var/wk: the first-boot daemon rsyncs over ~bench's own checkout
        if not self.put_tree(self.root, root + "/wk-tools"):
            die("could not sync wk-tools onto the bench volume")
        site = "%s/%s" % (bh, SITE)
        if not self.mac.test("-d", site + "/scipy"):
            info("  installing scipy into the bench account's site-packages")
            if not self.mac.run("mac-scipy.sh", mutates=True, WK_PATH=site).ok:
                warn("  scipy did not install; the A/B will be compared from host mode instead")
        self.quiet_account(bh, root)
        self.plant_samply(root)
        self.plant_tailnet(root)
        info("  installing the autorun")
        if not self.mac.test("-r", root + "/wk-tools/lib/wk/bench/autorun.py"):
            die("the planted tree carries no lib/wk/bench/autorun.py, so the launch agent has nothing to start.")
        info("  writing the job")
        self.deliver("job.json", json.dumps(self.job(declared, stamp), indent=2) + "\n", root + "/job.json", "write the job onto the volume")
        # reset here and nowhere else: the autorun only advances it
        self.deliver("planted.state", "phase=planted\njob_stamp=%s\nattempts=0\nplanted_at=%s\n" % (stamp, self.clock.iso()),
                     root + "/autorun.state", "reset the autorun's state on the volume")
        self.mac.run("mac-mkdir.sh", mutates=True, WK_PATH="%s/ab/%s" % (root, stamp))
        info("  installing the launch agent")
        self.deliver(AGENT + ".plist", PLIST, "%s/Library/LaunchAgents/%s.plist" % (bh, AGENT), "install the launch agent")
        info("planted: %s" % stamp)
        log("  task   %s   ('wk status' lists it; 'wk bench report %s' reads it)" % (self.taskdir, self.task))
        log("  log    %s/autorun.log   ('wk bench ab --devices %s --status' tails it, in either mode)" % (root, self.name))

    def quiet_account(self, bh, root):
        """A lock mid-run and a banner over the browser are both invisible to every later gate, so both are refused here."""
        uuid = next((l.split('"')[3] for l in self.mac.out("mac-platform.sh").splitlines()
                     if "IOPlatformUUID" in l and l.count('"') >= 4), "")
        ss = "%s/Library/Preferences/ByHost/com.apple.screensaver.%s" % (bh, uuid)
        said = self.mac.run("mac-screensaver.sh", mutates=True, WK_BYHOST=bh + "/Library/Preferences/ByHost", WK_SAVER=ss,
                            WK_PREFS=bh + "/Library/Preferences/com.apple.screensaver")
        idle = (said.out.split() or [""])[-1] if said.ok else ""
        if idle == "0":
            log("  screen lock: screensaver disabled on the volume (idleTime=0, verified)")
        else:
            act.barrier("could not disable the screensaver on %s -- idleTime reads '%s', and a benchmark makes no input, so the\n"
                        "    screen lock would end a run in silence. Nothing has been rebooted." % (self.name, idle or "unreadable"), env=self.env)
        dnd = (self.mac.run("mac-dnd.sh", mutates=True, WK_TOOLS=root + "/wk-tools", WK_HOME=bh).out.split() or [""])[-1]
        if dnd == "on":
            log("  notifications: Do Not Disturb on for the bench account (verified)")
        else:
            act.barrier("could not turn Do Not Disturb on for the bench account -- it reads '%s', and no later gate sees a banner.\n"
                        "    Nothing has been rebooted." % (dnd or "unreadable"), env=self.env)

    def plant_samply(self, root):
        """No network over there, so the warmup round's profiler goes in now, where samply.fetch will look for it."""
        arch = self.mac.out("mac-arch.sh")
        path = wksamply.fetch(self.here, self.reg.store.cache_dir(), arch, "Darwin")
        if not path:
            warn("  no samply for %s here -- the warmup round will carry no profile" % (arch or "that machine"))
            return
        dest = wksamply.store_dir(root + "/cache", wksamply.triple(arch, "Darwin")) + "/samply"
        if self.put_file(path, dest) and self.mac.run("mac-executable.sh", mutates=True, WK_PATH=dest).ok:
            log("  samply %s planted for the warmup round" % wksamply.VERSION)
        else:
            warn("  could not plant samply -- the warmup round will carry no profile")

    def plant_tailnet(self, root):
        """The half that needs a network, Go and this machine's auth key; the install half runs over there at boot."""
        from wk.sysimage.mactailnet import Tailnet
        info("  collecting the tailnet payload")
        try:
            got = Tailnet(self.here, self.env).collect(self.name, os.path.join(self.reg.store.state_dir(), "mac-tailnet", "collected"))
        except Refused:
            got = ""
        if not got or not self.here.isdir(got):
            warn("  tailnet: nothing collected (needs 'wk key set tailnet' and a network here), so that install cannot be\n"
                 "  watched while it measures. The A/B still runs.")
        elif self.put_tree(got, root + "/tailnet"):
            log("  tailnet: payload on the volume; that install joins the tailnet on its next boot")
        else:
            warn("  tailnet: the payload did not land, so that install stays unreachable while it measures")

    # -- the restart, and which install came up
    def restart(self):
        """The display and the firmware default are asked again seconds before the transition; a reading that is wrong or unread stops it."""
        checks = [("the display on %s" % self.name, self.display_check)]
        if not self.guest:
            checks.append(("the firmware default on %s" % self.name, lambda: (self.firmware_is_bench(), self.fw_detail)))
        for what, ask in checks:
            ok, said = ask()
            if not ok:
                die("%s %s: %s\n    Nothing has been rebooted, and the job stays planted. Fix it and re-run, or reboot\n"
                    "    %s by hand once it reads right -- the planted job runs by itself either way." %
                    (what, "could not be read" if ok is record.UNKNOWN else "is not right", said, self.name))
        self.boot_before = self.d.boot_id()
        info("go: reboot %s now (boot before: %s)" % (self.name, self.boot_before or "unknown"))
        if not self.guest:
            log("  '%s' is the firmware default (preflight asserted it), so this restart enters bench mode by itself and\n"
                "  nobody has to be at the keyboard." % self.d.c("volume"))
        self.d.reboot()
        if not self.clock.wait_until(lambda: not self.mac.test("-d", "/"), DOWN_WAIT, DOWN_POLL):
            die("could not reboot %s -- it is still answering (a refused reboot exits 0). Reboot it by hand or from the\n"
                "    startup manager and the planted job runs." % self.name)
        info("  %s is going down" % self.name)
        if self.guest:
            self.d.arm()   # a guest's reboot is its stop, and starting it again is the transition

    def wait(self):
        """Both nodes, every poll: the benchmark install answers as its own while it measures."""
        info("wait: up to %d minutes for %s to answer on either node" % (self.boot_wait // 60, self.name))
        self.clock.sleep(BOOT_SETTLE)
        start, said = self.clock.monotonic(), False
        while True:
            mode = self.d.probe()
            if mode.startswith("bench"):
                info("  %s answers in BENCH mode (%s)" % (self.name, mode[6:]))
                return "bench"
            if mode == "host":
                now = self.d.boot_id()   # asked only here: in bench mode it is another install's boot time
                if self.boot_before and now == self.boot_before:
                    warn("  %s is answering on the SAME boot (%s) -- it never rebooted" % (self.name, now))
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

    def notify(self, headline, detail):
        if not notify.send(self.root, headline, detail, "mac-ab", env=self.env, machine=self.here):
            warn("  could not send the notification '%s'" % headline)

    def outcome(self, came):
        n, vol = self.name, self.d.c("volume") or "the benchmark install"
        if came == "bench":
            return info("%s answers in BENCH mode -- the A/B is running there. 'wk bench ab --devices %s --status' follows it,\n"
                        "  and the machine powers itself off when the job ends." % (n, n))
        if came == "silent":
            return info("%s answers on neither node: still restarting, halted, or its join did not come up. Each leg is written\n"
                        "  to the volume as it ends:  wk bench ab --devices %s --status   once one answers;  --collect   reads it." % (n, n))
        why, fix = {"host": ("came back to host mode", "the reboot did not enter '%s'; 'wk boot %s' arms the firmware" % (vol, n)),
                    "noreboot": ("never rebooted", "reboot %s by any means, the startup manager too, and it runs by itself" % n)}[came]
        warn("%s %s, so the A/B has not run. The job is planted and still valid: %s." % (n, why, fix))
        self.notify("mac-ab: %s %s" % (n, why), "the A/B has not run. The job is planted and still valid: %s." % fix)

    def go(self):
        self.check()
        self.resolve()
        if not self.guest and self.d.ch.here():
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
        if not act.confirm("plant this A/B on %s%s?" % (self.name, "" if plant_only else " and restart it into its benchmark install")):
            die("not run")
        self.create_task(self.clock.stamp())
        try:
            if self.o.get("patch"):
                self.build_ab()
            self.plant()
            if act.dry_run():
                if not plant_only:
                    log("  would re-check that the declared panel is the only display, then reboot %s through its boot helper" % self.name)
                info("dry run -- nothing on %s was changed and nothing was rebooted" % self.name)
                log("  not checked here: whether a leg would pass on the benchmark install. That gate reads the running\n"
                    "  system, and this one is not running. In bench mode, ask it:  wk bench staged --gates --plan %s" % self.plans[0])
                return 0
            if plant_only:
                info("planted and not started. The A/B runs the next time the benchmark install boots.")
                return 0
            self.restart()
            self.notify("mac-ab planted on %s" % self.name, "%s, %d-%s rounds, count %s. Arms %s / %s. %s has gone down to measure; "
                        "then 'wk bench ab --devices %s --collect'." % (" ".join(self.plans), self.rounds, self.o["max_rounds"], self.o["count"],
                                                                 self.a, self.b, self.name, self.name))
            self.outcome(self.wait())
            return 0
        finally:
            self.lock.release_all()


    # -- the back half: a planted job read back, over whichever install answers
    def back(self):
        verbs = [k for k in READS if self.o.get(k)]
        if len(verbs) > 1:
            die("--%s: one reading at a time" % " and --".join(verbs))
        extra = [k for k, v in self.o.items() if v and k not in READS + ("devices",)]
        if self.spec or extra:
            die("--%s reads a planted job and takes only --devices <mac> (got %s)" % (verbs[0], self.spec or "--" + extra[0].replace("_", "-")))
        self.resolve()
        return getattr(self, "read_" + verbs[0])()

    def read_preflight(self):
        return 1 if self.preflight() else 0

    def staging_root(self):
        root = self.d.bench_root()
        if not root:
            die("nothing on %s is readable right now: it answers on neither node, or it is in host mode with its benchmark\n"
                "    volume not attached. 'wk boot %s --status' says which." % (self.name, self.name))
        return root

    def read_status(self):
        root, mode = self.staging_root(), self.d.mode
        info("%s is in bench mode (%s) -- this is the run itself, read over its own node" % (self.name, mode[6:]) if mode.startswith("bench")
             else "%s is in host mode, and this is read off the volume it mounts" % self.name)
        for text, none in ((self.mac.read(root + "/job.json"), "  no job planted"), (self.mac.read(root + "/autorun.state"), "  no autorun state")):
            log("\n".join("  " + l for l in text.splitlines()) or none)
            log("")
        for title, text, none in (("legs", self.mac.py("lib/wkdata.py", "ab-legs", root), "(unreadable)"),
                                  ("last 20 lines of the autorun log", self.mac.out("mac-tail.sh", WK_PATH=root + "/autorun.log"), "(none)")):
            log("  %s:" % title)
            log("\n".join("    " + l for l in text.splitlines()) or "    " + none)
        return 0

    def read_collect(self):
        """Each clean leg's result directory copied as `wk bench staged` wrote it, its round and arm added from the run map."""
        root = self.staging_root()
        info("collect: reading the A/B off %s" % (self.d.c("volume") or self.name))
        st = self.mac.read(root + "/autorun.state")
        if st:
            log("  autorun state:\n" + "\n".join("    " + l for l in st.splitlines()))
        else:
            warn("  no autorun state on the volume -- the agent never ran")
        stamp = kv(st).get("job_stamp", "")
        runs = "%s/ab/%s/runs.tsv" % (root, stamp)
        tsv = self.mac.read(runs) if stamp else ""
        if not tsv.strip():
            warn("  no run map at %s -- no arm completed" % runs)
            log("  the autorun's own log is the place to look:\n    %s/autorun.log   ('wk bench ab --devices %s --status' tails it)" % (root, self.name))
            return 1
        log("\n  runs:\n" + "\n".join("    " + l for l in tsv.splitlines()) + "\n")
        bench, task = self.bench, "%s-%s-mac-ab" % (stamp, self.name)
        hit = bench.find(task)
        if not hit:
            die("job %s has no task in any workspace this machine reaches, so nothing can record it; its results stay on the volume"
                % stamp)
        m, taskdir = hit
        m.write_own(os.path.join(taskdir, "autorun.state"), st)
        if not self.collect_tree(m, root + "/ab/" + stamp, "warmup", taskdir):
            warn("  the warmup round's captures (%s/ab/%s/warmup) did not copy onto the task" % (root, stamp))
        if self.collect_runs(m, taskdir, root, tsv):
            try:
                bench.task_report(task, False, True)
            except (Refused, SystemExit, OSError, ValueError) as e:
                warn("the report did not complete (%s); the runs are recorded:  wk bench report %s" % (e, task))
        return 0

    def collect_tree(self, m, parent, name, into):
        got, packed = self.mac.run("mac-tar.sh", WK_PATH=parent, WK_DIR=name), os.path.join(into, name + ".tar.b64")
        if got.ok:
            m.write(packed, got.out)
        landed = got.ok and m.act_run(["sh", "-c", 'base64 -d < "$1" | tar -xf - -C "$2"', "sh", packed, into]).ok
        if got.ok:
            m.remove(packed)
        return landed

    def collect_runs(self, m, taskdir, root, tsv):
        rows = [r for r in (record.map_row(l) for l in tsv.splitlines() if l.strip()) if r[0] != "0" and r[4] == "clean" and r[3]]
        if not rows:
            warn("  no clean leg after the warmup round -- nothing to record on the task")
            return 0
        into, n = os.path.join(taskdir, "runs"), 0
        m.mkdir(into)
        for rnd, label, sid, rid, _, plan in rows:
            if not self.collect_tree(m, root + "/results", rid, into):
                warn("  could not copy %s onto the task" % rid)
                continue
            if act.dry_run():
                continue
            env = os.path.join(into, rid, "env.json")
            if not m.exists(env):
                warn("  %s carries no env.json, so it cannot be paired with round %s arm %s" % (rid, rnd, label))
                continue
            record.write_env(env, ["machine=" + self.name, "plan=" + plan, "ab.round=" + rnd, "ab.staged=" + sid,
                                   "ab.arm=" + label.lower()], update=True, machine=m)
            n += 1
        log("  recorded %d clean leg(s) onto %s" % (n, taskdir))
        return n

    def read_progress(self):
        info("the A/B on %s, step by step" % self.name)
        n, mode, vol, status = self.name, self.d.mode, self.d.c("volume"), "wk bench ab --devices %s --status" % self.name
        steps = []

        def step(state, title, detail="", do="", verify=""):
            steps.append(state)
            log("  %s %d. %s" % ({"yes": "[x]", "part": "[~]"}.get(state, "[ ]"), len(steps), title))
            for label, text in (("", detail), ("do:     ", do if state != "yes" else ""), ("verify: ", verify)):
                if text:
                    log("         " + label + text)

        if mode == "unreachable":
            step("part", "the run is under way", "%s answers on neither node. It is restarting, or it is off." % n, "", status + "   (once one answers)")
            return 0
        if mode.startswith("bench"):
            step("yes", "the machine is in bench mode", "%s -- the A/B is on it now" % mode[6:], "", status)
            return 0
        root = self.d.bench_root() or ""
        if not self.guest:
            image_preset = self.d.c("image_preset") or "<image-preset>"
            version = self.mac.out("mac-version.sh", WK_PATH=self.d.volume() + "/System/Library/CoreServices/SystemVersion.plist")
            step("yes" if version else "no", "the benchmark volume exists", "'%s', macOS %s" % (vol, version) if version else
                 "'%s' is not mounted here (a shutdown unmounts it; --all makes one that is not there at all)" % vol,
                 "wk sysimage build %s --all   (on the Mac)" % image_preset, "wk boot %s --status" % n)
            if not version:
                return 0
            marker = kv(self.mac.read(self.d.volume() + MARKER)).get("id", "")
            pyobjc = "yes" if self.mac.test("-d", "%s/%s/objc" % (self.d.bench_home() or "", SITE)) else "no"
            done = bool(root) and self.provisioned(root)
            step("yes" if done else "no", "it is provisioned", ("first boot completed; marker %s, pyobjc %s" if done else
                 "its first-boot log has no completion line (marker %s, pyobjc %s)") % (marker or "none", pyobjc),
                 "wk sysimage build %s --repair   (on the Mac), then boot it once" % image_preset, "wk bench ab --devices %s --preflight" % n)
        arms = self.staged_arms(root) if root else []
        shown = "; ".join("%s (%s) gated=%s" % (i, sha[:12], "yes" if g else "no") for i, sha, g in arms) or "nothing staged"
        step("yes" if len(arms) > 1 else "part" if arms else "no", "two arms are built and staged", shown,
             "wk bench ab --devices %s --patch <ref> --workspace <ws> --plant" % n, "wk bench staged --ls   (on the Mac)")
        gated = sum(1 for a in arms if a[2])
        step("yes" if arms and gated == len(arms) else "part", "each arm can prove how it was collected",
             "%d of %d carry their readings" % (gated, len(arms)), "rebuild it: wk build <ws> mac-release-pgo",
             "cat .../staged/<id>/WebKitBuild/*/wk-profile-check.json   (on the Mac)")
        armed = not self.guest and self.firmware_is_bench()
        detail, do = (("the guest carries no marker", "wk boot %s" % n) if self.guest else
                      ("it is in host mode, and '%s' is the firmware default with a helper that answers: only the restart is missing"
                       % vol, "wk bench ab --devices %s --systems <a>,<b>   (plants and restarts)" % n) if armed and self.d.restart_ready() else
                      ("it is in host mode; '%s' is the firmware default, and no restart this can make -- %s" % (vol, self.d.restart_detail()),
                       "wk machine setup %s   (one password prompt there)" % n) if armed else
                      ("it is in host mode, and " + self.fw_detail, "wk boot %s   (arms the firmware and reboots)" % n))
        step("no", "the machine is in bench mode", detail, do, "wk boot %s --status" % n)
        job = self.read_json(root + "/job.json") if root else None
        job_arms = [a.get("id", "") for a in (job or {}).get("arms") or []]
        fresh = bool(job_arms) and set(job_arms) <= {a[0] for a in arms}
        plant = "wk bench ab --devices %s --systems <a>,<b>" % n
        if job is None:
            step("no", "a job is planted for the staged arms", "none", plant, status)
        elif fresh:
            step("yes", "a job is planted for the staged arms", " ".join(job_arms), "", status)
        else:
            step("no", "a job is planted for the staged arms", "the planted job names arms that are not staged now (%s) -- it is an older "
                 "A/B task's, and its rounds below are not this one's" % " ".join(job_arms), plant, status)
        st = self.mac.read(root + "/autorun.state") if root else ""
        last = dict(l.split("=", 1) for l in st.splitlines() if "=" in l)  # last one wins: a crash mid-rewrite of autorun.state can leave a key twice, and the later line is the newer value
        results = len(self.mac.out("mac-ls.sh", WK_PATH=root + "/results").split()) if root else 0
        if not fresh:
            step("no", "the rounds are done", "nothing has run for these arms", "boot the volume; the planted job runs them", status)
        elif last.get("outcome"):
            step("yes", "the rounds are done", "%s round(s), outcome %s, %d result(s)" % (last.get("rounds_done") or "0", last["outcome"], results),
                 "", "wk bench ab --devices %s --collect" % n)
        elif last.get("rounds_done"):
            step("part", "the rounds are done", "%s so far, %d result(s)" % (last["rounds_done"], results), "", status)
        else:
            step("no", "the rounds are done", "not started", "boot the volume; the planted job runs them", status)
        step("no", "the result is read back", "", "wk bench ab --devices %s --collect" % n, "wk bench report <task>")
        return 0

    def read_json(self, path):
        try:
            return json.loads(self.mac.read(path) or "null")
        except ValueError:
            return None

    def staged_arms(self, root):
        """(id, webkit sha, gated) per staged build: gated is whether it carries the readings its collection was judged by."""
        out = []
        for ident in self.staged_ids(root):
            d = "%s/staged/%s" % (root, ident)
            stage = self.read_json(d + "/stage.json")
            if isinstance(stage, dict):
                out.append((ident, stage.get("webkit_sha") or "", self.mac.run("mac-gated.sh", WK_PATH=d).ok))
        return out
