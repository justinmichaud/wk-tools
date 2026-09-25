"""The benchmark install running an A/B by itself, started at autologin by the launch agent `wk bench ab --devices <mac>`
plants. No session on the driving machine survives the reboot into this install, so it drives itself; the tailnet node
it brings up is how a run is watched. The bench volume is the firmware default, so however the job ends the machine is
handed back or powered off; the state is advanced before a run and before the summary so a power cut repeats neither,
and the watchdog is armed before the first step that can block."""

import json
import os
import re
import shlex
import signal
import sys
import threading

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wk import act  # noqa: E402
from wk.bench.mac import AGENT, CHECK, MARKER, QUIET, WKMAC  # noqa: E402
from wk.boot.mac import BENCH_ROOT  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.kv import kv  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.quiet import DESKTOP, lib_argv  # noqa: E402

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
MAX_ATTEMPTS = 3   # so "try again" cannot mean "boot loop"
HOLD = 900
FB_PLIST = "/Library/LaunchDaemons/com.wk.bench-firstboot.plist"
FB_SELF = "/usr/local/libexec/wk-bench-firstboot.sh"
SU_PREFS = "/Library/Preferences/com.apple.SoftwareUpdate"
UPDATERS = ("system/com.apple.softwareupdated", "system/com.apple.mobile.softwareupdated")
WATCH_POLL, STALL_GRACE = 60, 900
TEMP_TRIES, TEMP_POLL = 12, 5
DEFAULTS = {"rounds": 5, "max_rounds": 40, "detect_pct": "0.3", "timeout": 1800, "count": 2, "n_arms": 2, "settle": 90}
VARIANCE = (("aslr", "WK_BENCH_ASLR"), ("env_pad", "WK_BENCH_ENV_PAD"), ("path_pad", "WK_BENCH_PATH_PAD"),
            ("shared_cache", "WK_BENCH_SHARED_CACHE"))


class Stop(Exception):
    def __init__(self, rc=0):
        self.rc = rc


def number(text):
    try:
        return float(text)
    except ValueError:
        return 0.0


class Autorun:
    def __init__(self, m, clock, env, tools=TREE, out=None, thread=threading.Thread):
        self.m, self.clock, self.env, self.tools = m, clock, env, tools
        self.out, self.thread = out or sys.stdout, thread
        self.root = env.get("WK_AB_ROOT") or BENCH_ROOT
        self.job_path, self.state_path = self.root + "/job.json", self.root + "/autorun.state"
        self.log_path = self.root + "/autorun.log"
        self.agent = os.path.join(env.get("HOME", ""), "Library/LaunchAgents", AGENT + ".plist")
        self.hold_secs = int(number(env.get("WK_MAC_BENCH_HOLD", str(HOLD))))
        self.left = self.stay = self.had_job = self.armed = False
        self.lock = threading.Lock()
        self.runs = ""
        self.job = {}

    def say(self, msg):
        self.out.write("[%s] %s\n" % (self.clock.iso(), msg))
        self.out.flush()

    def state(self):
        try:
            return kv(self.m.read(self.state_path))
        except OSError:
            return {}

    def state_get(self, key):
        return self.state().get(key, "")

    def state_set(self, key, value):
        with self.lock:
            rows = [(k, v) for k, v in self.state().items() if k != key] + [(key, str(value))]
            self.m.write(self.state_path, "".join("%s=%s\n" % r for r in rows))
            self.m.act_run(["sync"])   # the next thing this has to survive is an ungraceful reboot

    def field(self, path, default=""):
        v = self.job
        for part in path.split("."):
            if isinstance(v, list) and part.isdigit() and int(part) < len(v):
                v = v[int(part)]
            elif isinstance(v, dict):
                v = v.get(part)
            else:
                return default
        if v is None or v == "":
            return default
        return ("1" if v else "") if isinstance(v, bool) else str(v)

    def load_job(self):
        try:
            self.job = json.loads(self.m.read(self.job_path))
        except (OSError, ValueError):
            self.job = {}
        return isinstance(self.job, dict) and bool(self.job)

    def tool(self, rel):
        return os.path.join(self.tools, rel)

    def wkmac(self, *args):
        return self.m.run(["python3", self.tool(WKMAC)] + list(args))

    def pause(self, secs):
        if act.dry_run():
            sys.stderr.write("would wait %ss\n" % secs)
        else:
            self.clock.sleep(secs)

    def logged(self, argv):
        if act.dry_run():
            sys.stderr.write("would run: %s\n" % " ".join(shlex.quote(a) for a in argv))
            return 0
        self.out.flush()
        return self.m.run_tty(argv).rc

    def wk(self, *args):
        """On this install the store is the job's root: it is the bench account's, and root's default is not."""
        pads = ["%s=%s" % (name, self.field(key)) for key, name in VARIANCE if self.field(key)]
        return ["env", "WK_STORE=" + self.root] + pads + [self.tool("wk")] + list(args)

    def running(self, *pattern):
        return self.m.run(["pgrep"] + list(pattern)).ok

    def sudo(self, *argv):
        return self.m.act_run(["sudo", "-n"] + list(argv)).ok

    def host_install(self):
        """The one mounted macOS system volume that is not this one and carries no bench marker: wk-boot-priv's gate."""
        try:
            names = self.m.listdir("/Volumes")
        except OSError:
            names = []
        here = self.m.run(["stat", "-f", "%d", "/"]).out.strip()
        found = [v for v in ("/Volumes/" + n for n in names)
                 if self.m.exists(v + "/System/Library/CoreServices/SystemVersion.plist") and not self.m.exists(v + MARKER)
                 and self.m.run(["stat", "-f", "%d", v]).out.strip() != here]
        return found[0] if len(found) == 1 else ""

    def hold(self):
        """Host mode needs a password typed at the machine, so a boot with no number is held where its log can be read."""
        if not self.had_job or self.hold_secs <= 0 or self.m.exists(self.runs + "/runs.tsv") and self.m.read(self.runs + "/runs.tsv").strip():
            return
        try:
            node = kv(self.m.read(self.root + "/tailnet/tailnet.conf")).get("hostname") or "<the bench node>"
        except OSError:
            node = "<the bench node>"
        self.say("no number came out of this boot. Holding the machine here for %ds," % self.hold_secs)
        self.say("  where it is reachable and host mode would not be:")
        self.say("    ssh %s tail -120 %s" % (node, self.log_path))
        self.pause(self.hold_secs)

    def leave(self, why):
        with self.lock:
            if self.left:
                return
            self.left = True
        self.state_set("left_at", self.clock.iso())
        self.hold()
        host = self.host_install()
        if host:
            r = self.wkmac("volume-group", host)
            want = r.out.strip() if r.ok else ""
            self.sudo("bless", "--mount", host, "--setBoot")
            got = self.wkmac("boot-volume").out.strip().split(":")[-1]
            if want and got == want:
                self.say("handing the machine back: %s" % why)
                self.say("  the firmware now names %s, so this reboot comes up in host mode" % host)
                self.m.act_run(["sync"])
                if not self.sudo("/sbin/reboot"):
                    self.say("WARNING: could not reboot")
                return
            self.say("  the firmware still names this volume (reads '%s', wanted '%s')" % (got or "nothing", want or "unreadable"))
            self.say("  so it powers off instead: a reboot would land back here and measure again.")
        else:
            self.say("  no single host install is mounted, so there is nothing to hand back to")
        self.say("powering off: %s" % why)
        self.m.act_run(["sync"])
        # halt, not `shutdown -h`: that asks loginwindow, and any modal dialog on the screen vetoes it.
        if not self.sudo("/sbin/halt"):
            self.say("WARNING: could not power off")

    def quit(self, why, outcome="", attempt_back=False, agent=False, rc=0):
        if attempt_back:
            self.state_set("attempts", int(self.state_get("attempts") or 1) - 1)
        if outcome:
            self.state_set("phase", "done")
            self.state_set("outcome", outcome)
        if agent:
            self.remove_agent()
        self.leave(why)
        raise Stop(rc)

    def remove_agent(self):
        """The file only: `launchctl bootout` would kill this process, its own child."""
        if self.m.exists(self.agent):
            self.m.remove(self.agent)
            self.say("removed the launch agent (%s)" % self.agent)

    def cancel_pending_reboot(self):
        if not self.running("-x", "shutdown"):
            return
        self.say("  a reboot is scheduled by something else -- cancelling it")
        self.sudo("pkill", "-x", "shutdown")
        self.pause(2)
        self.say("  WARNING: shutdown is still pending; this run may be cut off" if self.running("-x", "shutdown")
                 else "  reboot cancelled")

    def stand_aside_if_provisioning(self):
        if not self.running("-f", "wk-bench-firstboot"):
            return
        self.say("provisioning is running right now -- standing aside so it can finish.")
        self.say("  It reboots at the end, and this agent starts again on that boot.")
        self.stay = True
        raise Stop(0)

    def defuse_firstboot(self):
        """A daemon that outlives its provisioning re-syncs an older tree and ends with `shutdown -r +1` on every boot."""
        if not (self.m.exists(FB_PLIST) or self.m.exists(FB_SELF)):
            return self.cancel_pending_reboot()
        self.say("the first-boot daemon outlived its provisioning -- defusing it")
        if self.running("-f", "wk-bench-firstboot"):
            self.say("  it is re-running right now -- stopping it before it schedules a reboot")
            self.sudo("pkill", "-f", "wk-bench-firstboot")
        self.sudo("rm", "-f", FB_PLIST, FB_SELF)
        self.say("  WARNING: could not remove %s -- it will run again next boot" % FB_PLIST if self.m.exists(FB_PLIST)
                 else "  removed the first-boot daemon; later boots are ordinary boots")
        self.cancel_pending_reboot()

    def begin(self):
        boot = re.search(r"sec\s*=\s*(\d+)", self.m.run(["sysctl", "-n", "kern.boottime"]).out)
        self.say("=== wk bench autorun: boot %s ===" % (boot.group(1) if boot else ""))
        if not self.m.exists(MARKER):
            self.say("not bench mode (%s absent) -- this agent has nothing to do here" % MARKER)
            self.remove_agent()
            raise Stop(0)
        self.say("bench mode: %s" % kv(self.m.read(MARKER)).get("id", ""))
        self.stand_aside_if_provisioning()
        self.defuse_firstboot()
        if not self.load_job():
            self.say("no job at %s -- nothing to run" % self.job_path)
            self.quit("no job", agent=True)
        if self.state_get("phase") == "done":
            self.say("the job is already finished, and this volume booted again -- so it is the")
            self.say("firmware default. Powering off rather than looping.")
            self.quit("job already complete", agent=True)
        attempts = int(self.state_get("attempts") or 0) + 1
        self.state_set("attempts", attempts)
        if attempts > MAX_ATTEMPTS:
            self.say("attempt %d exceeds the limit of %d -- abandoning the job" % (attempts, MAX_ATTEMPTS))
            self.quit("too many attempts", outcome="abandoned", agent=True)
        self.say("attempt %d of %d" % (attempts, MAX_ATTEMPTS))
        self.had_job = True

    def read_job(self):
        f = self.field
        self.plans = [str(p) for p in self.job.get("plans")] if isinstance(self.job.get("plans"), list) else []
        self.plans = self.plans or ["speedometer3"]
        for key in ("rounds", "max_rounds", "timeout", "count", "n_arms", "settle"):
            setattr(self, key, int(number(f(key, str(DEFAULTS[key])))))
        self.detect = f("detect_pct", DEFAULTS["detect_pct"])
        self.display, self.rehearsal = f("display"), f("rehearsal")
        self.say("job: plans=%s rounds=%d-%d detect=%s%% arms=%d timeout=%ds count=%d" % (
            " ".join(self.plans), self.rounds, self.max_rounds, self.detect, self.n_arms, self.timeout, self.count))
        self.say("     variance: " + " ".join("%s=%s" % (k, f(k) or "unset") for k, _ in VARIANCE))
        self.say("     wk-tools=%s  display=%s" % (self.tools, self.display or "unpinned"))
        if self.rehearsal:
            self.say("     REHEARSAL: every leg is forced past its own preflight, and every number")
            self.say("     it takes is recorded as forced. This measures the path, not the machine.")
        self.runs = "%s/ab/%s" % (self.root, self.state_get("job_stamp") or "unstamped")
        self.m.mkdir(self.runs)

    def silent_for(self):
        r = self.m.run(["stat", "-f", "%m", self.log_path])
        return int(self.clock.now()) - int(r.out.strip()) if r.ok and r.out.strip().isdigit() else 0

    def watchdog(self):
        while True:
            self.clock.sleep(WATCH_POLL)
            if self.state_get("phase") == "done":
                return
            quiet = self.silent_for()
            if quiet < self.stall:
                continue
            self.say("WATCHDOG FIRED -- nothing written for %ds; the run is not coming back" % quiet)
            self.state_set("phase", "done")
            self.state_set("outcome", "watchdog")
            self.summarise()
            self.leave("watchdog: nothing written for %ds" % quiet)
            return

    def arm(self):
        self.stall = self.timeout + STALL_GRACE
        self.say("watchdog: %ds of silence" % self.stall)
        self.thread(target=self.watchdog, daemon=True).start()
        self.armed = True

    def dim_display(self):
        """The panel is a load on the package the browser runs on, so it goes down before anything that can stall."""
        r = self.m.act_run(["python3", self.tool(WKMAC), "brightness", "--set", "0"])
        if not r.ok:
            self.say("the display would not go to minimum brightness (rc=%d, read back '%s')." % (r.rc, r.out.strip() or "nothing"))
            self.say("  A backlight that varies is a load that varies, so nothing runs.")
            self.quit("the display would not dim")
        self.say("display at minimum brightness (reads %s)" % r.out.strip())

    def refuse_unpinned_display(self):
        if self.display:
            return
        self.say("the job names no display, so what a round would be measured at is unknown.")
        self.say("  From host mode: set NODE_DISPLAY in machines/mbp.conf, then plant again.")
        self.quit("the job names no display", outcome="no-display-expectation", agent=True)

    def hold_auto_brightness(self):
        self.m.act_run(["python3", self.tool(WKMAC), "auto-brightness", "--off"])
        r = self.wkmac("auto-brightness")
        got = r.out.strip()
        if got == "off":
            return self.say("ambient light: compensation off (read back)")
        if got == "none":
            return self.say("ambient light: this panel has no sensor to hold")
        self.say("ambient light: still reads '%s' (rc=%d) after being turned off." % (got or "nothing", r.rc))
        self.say("  A brightness the sensor can raise again is a load that varies, so nothing runs.")
        self.quit("ambient-light compensation could not be turned off", attempt_back=True)

    def refuse_wrong_displays(self):
        """The topology only, before the mode is touched: with a second panel there is no one built-in mode to converge to."""
        r = self.m.run(["/usr/bin/python3", self.tool(CHECK), "--displays-only"])
        said = (r.out + r.err).strip()
        if r.ok:
            return self.say("displays: %s" % said)
        self.say("the screen this would be measured on is not the declared one:")
        for line in said.splitlines():
            self.say("  " + line)
        self.say("  Nothing runs and no mode is written. Disconnect the monitor and boot")
        self.say("  this volume again -- the job stays planted and spends no attempt.")
        self.quit("the display is not the declared one", attempt_back=True)

    def converge_display_mode(self):
        """WindowServer reads its mode at start, so a mode that is not the declared one is written and this boot repeated."""
        want = (self.display.split() + ["", ""])[1]
        r = self.wkmac("display-mode")
        running = r.out.strip() if r.ok else ""
        if running == want:
            return self.say("display mode: %s, as the job declares" % running)
        self.say("display mode: running at %s, and the job declares %s" % (running or "unreadable", want))
        if self.state_get("mode_declared") == want:
            self.say("  %s was written into the WindowServer configuration for this boot and" % want)
            self.say("  the panel still comes up at %s, so the write does not take." % (running or "unreadable"))
            self.say("  Nothing is measured at a mode that is not the declared one: MotionMark's")
            self.say("  score is the area it draws. From host mode, set the mode on this install")
            self.say("  by hand and re-plant, or declare the mode it does come up at:")
            self.say('    NODE_DISPLAY="%s %s"  in machines/mbp.conf' % (self.display.split()[0], running or "<what it reads>"))
            self.quit("the declared display mode cannot be set", outcome="display-mode-unsettable", agent=True)
        if not self.sudo("python3", self.tool(WKMAC), "display-mode", "--declare", want):
            self.say("  the WindowServer configuration would not take %s." % want)
            self.quit("the declared display mode could not be written", outcome="display-mode-unwritable", agent=True)
        self.state_set("mode_declared", want)
        self.state_set("attempts", int(self.state_get("attempts") or 1) - 1)
        self.say("  wrote it; restarting so WindowServer comes up at %s" % want)
        self.stay = True
        self.m.act_run(["sync"])
        if not self.sudo("/sbin/reboot"):
            self.say("WARNING: could not restart")
        raise Stop(0)

    def converge_self(self):
        self.say("converging this install from the planted tree")
        py = ["sudo", "-n", "env", "PYTHONPATH=" + self.tool("lib"), "python3", "-m"]
        self.say("  payload staged into this install" if self.logged(py + ["wk.sysimage.macvolume", "stage-payload", "/"]) == 0
                 else "  WARNING: the payload did not fully stage; the log above says which file")
        vol = os.path.normpath(os.path.join(self.tools, "..", "tailnet"))
        if not self.m.isdir(vol):
            self.say("  no tailnet payload at %s, so this install has no tailnet identity to join with" % vol)
        elif self.logged(py + ["wk.sysimage.mactailnet", "install", "/", vol]) == 0:
            self.say("  tailnet payload installed")
        else:
            self.say("  WARNING: the tailnet payload would not install; this install stays unreachable")
        self.say("  tailnet: joined, so this run can be watched while it measures"
                 if self.logged(["sudo", "-n", self.tool("bench/mac-tailnet.sh"), "join"]) == 0
                 else "  tailnet: did not join (see the log); the run is unobservable but not affected")

    def wait_for_temp(self):
        """run-benchmark's `patch` writes into DARWIN_USER_TEMP_DIR, which the per-user bootstrap may not have made yet."""
        for _ in range(TEMP_TRIES):
            tmp = self.m.run(["getconf", "DARWIN_USER_TEMP_DIR"]).out.strip()
            if tmp and self.m.isdir(tmp) and self.m.run(["test", "-w", tmp]).ok:
                return self.say("temp: %s" % tmp)
            self.say("waiting for the per-user temp directory (%s)" % (tmp or "unset"))
            self.pause(TEMP_POLL)
        self.say("WARNING: no writable per-user temp directory -- run-benchmark's patch step will fail")

    def clear_the_screen(self):
        """A window over MiniBrowser throttles it into a timeout; killing Setup Assistant ends the desktop session."""
        front = self.m.run(lib_argv(self.tools, QUIET, "screen_blocker")).out.strip()
        if front == "?":
            self.say("WARNING: could not ask the window server what is on the screen; a run that")
            self.say("    times out with no error is this and nothing else")
        elif front:
            self.say("on the screen, and nothing this job put there: %s" % front)
            self.sudo("touch", "/var/db/.AppleSetupDone")
            for w in filter(None, front.split(",")):
                if w == "Setup Assistant":
                    self.say("  leaving '%s': killing it ends the desktop session" % w)
                else:
                    self.say("  closing '%s'" % w)
                    self.sudo("pkill", "-f", w + ".app")
            self.pause(10)
        if self.running("-x", "SecurityAgent"):
            self.say("a modal authentication panel is up (SecurityAgent) -- dismissing it")
            self.sudo("killall", "-9", "SecurityAgent")   # it holds XPC transactions open instead of exiting
            self.pause(3)
            self.say("  WARNING: it is still up; the browser may not get focus" if self.running("-x", "SecurityAgent")
                     else "  dismissed")

    def stop_updates(self):
        """The preference reads back false yet a scan can still run, so the daemons are booted out until power-off."""
        self.say("stopping the software-update scanner")
        for svc in UPDATERS:
            if self.sudo("launchctl", "bootout", svc):
                self.say("  booted out %s" % svc)
            elif not self.m.run(["sudo", "-n", "launchctl", "print", svc]).ok:
                self.say("  %s is not loaded" % svc)
            else:
                self.say("  WARNING: could not boot out %s and it is still loaded --" % svc)
                self.say("    a scan can still start inside a run; each arm's scan check says if one does.")
        for key in ("AutomaticCheckEnabled", "AutomaticDownload"):
            self.sudo("defaults", "write", SU_PREFS, key, "-bool", "false")

    def update_stamp(self):
        """Out of the plist file, since cfprefsd answers values it does not carry; any Last*Date moving means a scan ran."""
        text = self.m.run(["/usr/bin/plutil", "-p", SU_PREFS + ".plist"]).out
        return "".join(sorted(l.replace(" ", "") for l in text.splitlines() if re.search(r'"Last[A-Za-z]*Date"', l)))

    def refuse_unprovisioned(self):
        """After `wk quiesce on`, which rewrites the user half of these rows once this account's session has started."""
        if not self.m.exists(self.tool(DESKTOP)):
            self.say("no %s, so nothing here can judge what this volume is set to." % self.tool(DESKTOP))
            self.quit("no quiet-desktop table to judge this volume by")
        probe = self.m.run(lib_argv(self.tools, DESKTOP, "wk_quiet_desktop_probe")).out
        rows = self.m.run(lib_argv(self.tools, DESKTOP, "wk_quiet_desktop_findings", probe, "")).out
        wrong = [c[1] for c in (l.split("\t") for l in rows.splitlines()) if len(c) > 1 and c[0] == "wrong"]
        if not wrong:
            return
        installed = "yes" if self.m.exists(FB_PLIST) or self.m.exists(FB_SELF) else "no"
        self.say("this volume is not set up as a measured Mac (first-boot daemon installed: %s):" % installed)
        for w in wrong:
            self.say("  " + w)
        self.say("  Every leg would be refused for these. From host mode:")
        self.say("    wk sysimage build perf-macos-tolken --repair    then boot this volume once")
        self.quit("this volume is not set up as a measured Mac")

    def refuse_throttled_browser(self):
        """The browser the legs' settings are for, once and before any round: no --force crosses it."""
        sid = self.field("arms.0.id")
        base = "%s/staged/%s/WebKitBuild" % (self.root, sid)
        try:
            dirs = [base + "/" + n for n in self.m.listdir(base) if self.m.isdir(base + "/" + n)]
        except OSError:
            dirs = []
        if not dirs:
            self.say("no products under %s/staged/%s -- nothing to check the browser with" % (self.root, sid))
            self.quit("arm A is not staged")
        self.say("browser check against arm A's build (%s)" % sid)
        if self.logged(["/usr/bin/python3", self.tool(CHECK), "--build-directory", dirs[0], "--expect-display", self.display,
                        "--json", self.runs + "/browser-check.json"]) == 0:
            return self.say("  the browser here is accelerated and unthrottled (readings above)")
        self.say("  this install cannot present a browser worth measuring (faults above).")
        self.say("  Every round would measure that instead of the patch, so nothing runs.")
        self.quit("browser check failed")

    def newest_result(self):
        try:
            names = self.m.listdir(self.root + "/results")
        except OSError:
            return ""
        return names[-1] if names else ""

    def rows(self):
        try:
            return [l.split("\t") for l in self.m.read(self.runs + "/runs.tsv").splitlines() if l]
        except OSError:
            return []

    def write_rows(self, rows):
        self.m.write(self.runs + "/runs.tsv", "".join("\t".join(r) + "\n" for r in rows))

    def leg(self, r, plan, i, profile=""):
        """A software-update scan across one arm is a number to drop, not a reason to disbelieve the rest."""
        label, sid, bargs = self.field("arms.%d.label" % i, "arm%d" % i), self.field("arms.%d.id" % i), self.field("arms.%d.browser_args" % i)
        self.say("--- round %d, %s, arm %s (staged %s) ---" % (r, plan, label, sid))
        if self.logged(["sudo", "-n"] + lib_argv(self.tools, DESKTOP, "wk_quiet_daemons_pause")) != 0:
            self.say("    WARNING: could not re-pause the background daemons; the leg's own gate will say which came back")
        argv = self.wk("bench", "staged", "--plan", plan, "--timeout", str(self.timeout), "--expect-display", self.display)
        argv += (["--force"] if self.rehearsal else []) + (["--id", sid] if sid else []) + ["--count", str(self.count)]
        argv += (["--browser-args", bargs] if bargs else []) + (["--profile", profile] if profile else [])
        before, stamp = self.newest_result(), self.update_stamp()
        rc = self.logged(argv)
        if rc != 0:
            self.say("--- round %d, %s, arm %s: FAILED (rc=%d) ---" % (r, plan, label, rc))
            self.state_set("fail_%s_%s_%d" % (plan, label, r), rc)
            return False
        self.say("--- round %d, %s, arm %s: OK ---" % (r, plan, label))
        self.state_set("ok_%s_%s_%d" % (plan, label, r), 1)
        after, clean = self.update_stamp(), "clean"
        if after != stamp:
            clean = "scanned"
            self.say("    CONTAMINATED: a software-update scan ran during this arm")
            self.say("      before: %s" % stamp)
            self.say("      after:  %s" % after)
        got = self.newest_result()
        if got and got != before:
            self.write_rows(self.rows() + [[str(r), label, sid, got, clean, plan]])
            self.say("    -> results/%s (%s)" % (got, clean))
        else:
            self.say("    WARNING: no new result directory appeared")
        return True

    def arm_results(self, plan, label):
        return ",".join("%s/results/%s" % (self.root, row[3]) for row in self.rows()
                        if len(row) > 5 and row[5] == plan and row[1] == label and row[4] == "clean")

    def detect_off(self):
        """The job carries JSON, so `--detect 0` arrives as `0.0`."""
        return number(self.detect) == 0

    def plan_resolves(self, plan):
        a, b = self.arm_results(plan, "A"), self.arm_results(plan, "B")
        if not (a and b):
            return False
        r = self.m.run(["/usr/bin/python3", self.tool("lib/wkdata.py"), "ab-precision", "--a", a, "--b", b, "--target", self.detect])
        if not r.ok:
            return False
        self.say("  %s: %s" % (plan, " ".join(r.out.split())))
        return "met=yes" in r.out.splitlines()

    def warmup(self):
        """Discarded: it absorbs a freshly copied tree's first run and carries the profile the measured rounds cannot take."""
        self.say("warmup round -- discarded; it profiles each arm and settles the machine")
        self.m.mkdir(self.runs + "/warmup")
        plan = self.plans[0]
        for i in range(self.n_arms):
            label = self.field("arms.%d.label" % i, "arm%d" % i)
            if not self.leg(0, plan, i, "%s/warmup/%s-%s.json.gz" % (self.runs, plan, label)):
                self.say("  the warmup leg for arm %s did not complete" % label)
        if self.m.exists(self.runs + "/runs.tsv"):
            self.write_rows([row for row in self.rows() if row[0] != "0"])
        self.say("warmup done; captures in %s/warmup" % self.runs)

    def rounds_loop(self):
        """Interleaved and counterbalanced (ABBA): the machine drifts, and a fixed order puts that drift on one arm."""
        ceiling = self.rounds if self.detect_off() else self.max_rounds
        any_ok, r = False, 1
        while r <= ceiling:
            for plan in self.plans:
                for i in range(self.n_arms):
                    any_ok = self.leg(r, plan, i if r % 2 else self.n_arms - 1 - i) or any_ok
            if not any_ok:
                self.say("round %d produced nothing at all -- every arm failed the same way, and" % r)
                self.say("the next round has nothing different to try. Stopping here so the")
                self.say("machine hands itself back instead of burning the schedule.")
                return self.state_set("outcome", "all-failed-round-%d" % r)
            self.state_set("rounds_done", r)
            if r >= self.rounds and not self.detect_off():
                self.say("precision after round %d (target %s%%):" % (r, self.detect))
                unresolved = [p for p in self.plans if not self.plan_resolves(p)]
                if not unresolved:
                    self.say("every plan resolves %s%% -- stopping at round %d" % (self.detect, r))
                    return self.state_set("outcome", "resolved-at-round-%d" % r)
                self.say("  still coarser than %s%%: %s" % (self.detect, " ".join(unresolved)))
            r += 1
        if self.detect_off():
            self.say("ran the %d round(s) asked for; no precision target was set, so what" % self.rounds)
            self.say("these numbers resolve is whatever 'wk bench precision' says of them.")
            return self.state_set("outcome", "rounds-done")
        self.say("reached the ceiling of %d rounds without resolving %s%% on every plan." % (self.max_rounds, self.detect))
        self.say("The numbers are real; the claim they support is the one the precision lines above allow.")
        self.state_set("outcome", "hit-max-rounds")

    def summarise(self):
        self.say("summarising")
        if self.logged(self.wk("bench", "ab-summary", "--root", self.root, "--runs", self.runs + "/runs.tsv",
                               "--out", self.runs + "/summary.txt")) != 0:
            self.say("(no summary -- 'wk bench ab-summary' failed; the results are still on the volume)")

    def body(self):
        self.begin()
        self.read_job()
        self.arm()
        self.dim_display()
        if not self.m.exists(self.tool("wk")):
            self.say("FATAL: no wk at %s -- cannot run anything" % self.tool("wk"))
            self.quit("no wk-tools", outcome="no-wk-tools", agent=True, rc=1)
        self.refuse_unpinned_display()
        self.converge_self()
        self.hold_auto_brightness()
        self.refuse_wrong_displays()
        self.converge_display_mode()
        self.state_set("phase", "running")
        self.state_set("plans", " ".join(self.plans))
        self.state_set("started_at", self.clock.iso())
        self.say("settling for %ds" % self.settle)   # the agent starts at login, the moment the machine is least quiet
        self.pause(self.settle)
        self.wait_for_temp()
        self.cancel_pending_reboot()
        self.clear_the_screen()
        self.stop_updates()
        self.say("  scan stamp before the job: %s" % self.update_stamp())
        self.say("quiescing")
        if self.logged(self.wk("quiesce", "on")) != 0:
            self.say("WARNING: quiesce reported a problem; the runner will judge it")
        self.refuse_unprovisioned()
        self.refuse_throttled_browser()
        self.warmup()
        self.rounds_loop()
        self.say("leaving the machine quiesced: quiet is this install's permanent state")
        self.state_set("phase", "done")
        self.state_set("finished_at", self.clock.iso())
        self.summarise()
        self.say("=== job finished ===")
        self.leave("job finished")

    def run(self):
        try:
            self.body()
        except Stop as e:
            return e.rc
        finally:
            if self.armed and not self.stay:
                self.leave("run finished or failed")
        return 0


def main(argv):
    if argv:
        sys.stderr.write("usage: python3 %s  (run by the launch agent %s)\n" % (os.path.relpath(__file__, TREE), AGENT))
        return 2
    os.environ["PATH"] = PATH
    m = Local()
    root = os.environ.get("WK_AB_ROOT") or BENCH_ROOT
    m.mkdir(root)
    if not act.dry_run():
        fd = os.open(root + "/autorun.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return Autorun(m, Clock(), os.environ).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
