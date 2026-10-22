"""A planted A/B run by the bench install itself at autologin: the machine readied, the job's `AB` into the task the plant
wrote, the machine held and powered off however it ends. State advances before a step, so a power cut repeats none."""

import json
import os
import re
import shlex
import signal
import sys
import threading

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wk import act, places, screen  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench.ab import PLANTED, AB, Arm  # noqa: E402
from wk.bench.mac import CHECK, WKMAC, Volume, bench_root  # noqa: E402
from wk.bench.mac_ab import AGENT  # noqa: E402
from wk.bench.pipeline import VARIANCE  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.images import MARKER  # noqa: E402
from wk.kv import kv  # noqa: E402
from wk.machine import Local  # noqa: E402

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
MAX_ATTEMPTS = 3   # so "try again" cannot mean "boot loop"
HOLD = 900
SU_PREFS = "/Library/Preferences/com.apple.SoftwareUpdate"
UPDATERS = ("system/com.apple.softwareupdated", "system/com.apple.mobile.softwareupdated")
WATCH_POLL, STALL_GRACE = 60, 900
TEMP_TRIES, TEMP_POLL = 12, 5


class Stop(Exception):
    def __init__(self, rc=0):
        self.rc = rc


class Autorun:
    def __init__(self, m, clock, env, background=None):
        self.m, self.clock, self.env = m, clock, env
        self.background = background or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.root = bench_root(env)
        self.job_path, self.state_path, self.log_path = self.root + "/job.json", self.root + "/autorun.state", self.root + "/autorun.log"
        self.agent = os.path.join(env.get("HOME", ""), "Library/LaunchAgents", AGENT + ".plist")
        self.hold_secs = int(env.get("WK_MAC_BENCH_HOLD") or HOLD)
        self.left = self.stay = self.had_job = self.armed = False
        self.lock = threading.Lock()
        self.job = {}

    def say(self, *lines):
        for msg in lines:
            sys.stdout.write("[%s] %s\n" % (self.clock.iso(), msg))
        sys.stdout.flush()

    def state(self):
        try:
            return kv(self.m.read(self.state_path))
        except OSError:
            return {}

    def state_set(self, **new):
        with self.lock:
            rows = [(k, v) for k, v in self.state().items() if k not in new] + [(k, str(v)) for k, v in new.items()]
            self.m.write(self.state_path, "".join("%s=%s\n" % r for r in rows))
            self.m.act_run(["sync"])   # the next thing this has to survive is an ungraceful reboot

    def tool(self, rel):
        return os.path.join(TREE, rel)

    def wkmac(self, *args):
        return self.m.run(["python3", self.tool(WKMAC)] + list(args))

    def pause(self, secs):
        if act.dry_run():
            sys.stderr.write("would wait %ss\n" % secs)
        else:
            self.clock.sleep(secs)

    def logged(self, argv):
        if act.dry_run():
            sys.stderr.write("would run: %s\n" % shlex.join(argv))
            return 0
        sys.stdout.flush()
        return self.m.run_tty(argv).rc

    def running(self, *pattern):
        return self.m.run(["pgrep"] + list(pattern)).ok

    def sudo(self, *argv):
        return self.m.act_run(["sudo", "-n"] + list(argv)).ok

    def hold(self):
        """The install stays up and reachable a while after its job, so the log and the legs can be read over the tailnet."""
        if self.hold_secs <= 0:
            return
        try:
            node = kv(self.m.read(self.root + "/tailnet/tailnet.conf")).get("hostname") or "<the bench node>"
        except OSError:
            node = "<the bench node>"
        self.say("holding this install up for %ds, reachable:" % self.hold_secs, "    ssh %s tail -120 %s" % (node, self.log_path))
        self.pause(self.hold_secs)

    def leave(self, why):
        """A boot that ran a job holds, then powers off; one that ran none stays at its login window. Nothing is blessed:
        the firmware default stays this volume, and the host install is the person's pick at the power button."""
        with self.lock:
            if self.left:
                return
            self.left = True
        self.state_set(left_at=self.clock.iso())
        if not self.had_job:
            self.say("%s: nothing ran, so this install stays at its login window" % why)
            return
        self.hold()
        self.say("powering off: %s" % why, "  the host install is the power-button pick at the next startup")
        self.m.act_run(["sync"])
        # halt, not `shutdown -h`: that asks loginwindow, and any modal dialog on the screen vetoes it.
        if not self.sudo("/sbin/halt"):
            self.say("WARNING: could not power off")

    def quit(self, why, outcome="", attempt_back=False, agent=False, rc=0):
        if attempt_back:
            self.state_set(attempts=int(self.state().get("attempts") or 1) - 1)
        if outcome:
            self.state_set(phase="done", outcome=outcome)
        if agent and self.m.exists(self.agent):   # the file only: `launchctl bootout` would kill this process, its own child
            self.m.remove(self.agent)
            self.say("removed the launch agent (%s)" % self.agent)
        self.leave(why)
        raise Stop(rc)

    def begin(self):
        boot = re.search(r"sec\s*=\s*(\d+)", self.m.run(["sysctl", "-n", "kern.boottime"]).out)
        self.say("=== wk bench autorun: boot %s ===" % (boot.group(1) if boot else ""))
        if not self.m.exists(MARKER):
            self.say("not bench mode (%s absent) -- this agent has nothing to do here" % MARKER)
            if self.m.exists(self.agent):
                self.m.remove(self.agent)
            raise Stop(0)
        self.say("bench mode: %s" % kv(self.m.read(MARKER)).get("id", ""))
        try:
            self.job = json.loads(self.m.read(self.job_path))
        except (OSError, ValueError):
            self.job = {}
        if not isinstance(self.job, dict) or not self.job.get("task"):
            self.say("no job at %s -- nothing to run" % self.job_path)
            self.quit("no job", agent=True)
        if self.state().get("phase") == "done":
            self.say("the job is already finished; this volume is the firmware default and booted again")
            self.quit("job already complete", agent=True)
        attempts = int(self.state().get("attempts") or 0) + 1
        self.state_set(attempts=attempts)
        if attempts > MAX_ATTEMPTS:
            self.say("attempt %d exceeds the limit of %d -- abandoning the job" % (attempts, MAX_ATTEMPTS))
            self.quit("too many attempts", outcome="abandoned", agent=True)
        self.say("attempt %d of %d" % (attempts, MAX_ATTEMPTS))
        self.had_job = True
        j = self.job
        self.display = j.get("display") or ""
        self.say("job: %s; plans %s, rounds %s-%s, detect %s%%, count %s, timeout %ss" % (
            j["task"], " ".join(j.get("plans") or []), j.get("rounds"), j.get("max_rounds"), j.get("detect"), j.get("count"), j.get("timeout")),
            "     variance: " + " ".join("%s=%s" % (k, j.get(k) or "unset") for k, _ in VARIANCE))
        if j.get("rehearsal"):
            self.say("     REHEARSAL: every leg is forced past its preflight and recorded as forced; it measures the path, not the machine")

    def watchdog(self):
        while True:
            self.clock.sleep(WATCH_POLL)
            if self.state().get("phase") == "done":
                return
            r = self.m.run(["stat", "-f", "%m", self.log_path])
            quiet = int(self.clock.now()) - int(r.out.strip()) if r.ok and r.out.strip().isdigit() else 0
            if quiet < self.stall:
                continue
            self.say("WATCHDOG FIRED -- nothing written for %ds; the run is not coming back" % quiet)
            self.state_set(phase="done", outcome="watchdog")
            self.leave("watchdog: nothing written for %ds" % quiet)
            return

    def arm(self):
        self.stall = int(self.job.get("timeout") or PLANTED["timeout"]) + STALL_GRACE
        self.say("watchdog: %ds of silence" % self.stall)
        self.background(self.watchdog)
        self.armed = True

    def dim_display(self):
        """The panel is a load on the package the browser runs on, so it goes down before anything that can stall."""
        r = self.m.act_run(["python3", self.tool(WKMAC), "brightness", "--set", "0"])
        if not r.ok:
            self.say("the display would not go to minimum brightness (rc=%d, read back '%s'), and a varying backlight is a varying load"
                     % (r.rc, r.out.strip() or "nothing"))
            self.quit("the display would not dim")
        self.say("display at minimum brightness (reads %s)" % r.out.strip())

    def hold_auto_brightness(self):
        self.m.act_run(["python3", self.tool(WKMAC), "auto-brightness", "--off"])
        r = self.wkmac("auto-brightness")
        got = r.out.strip()
        if got in ("off", "none"):
            return self.say("ambient light: compensation off (read back)" if got == "off" else "ambient light: this panel has no sensor to hold")
        self.say("ambient light: still reads '%s' (rc=%d) after being turned off, so the panel's load could vary" % (got or "nothing", r.rc))
        self.quit("ambient-light compensation could not be turned off", attempt_back=True)

    def refuse_wrong_displays(self):
        """The topology only, before the mode is touched: with a second panel there is no one built-in mode to converge to."""
        if not self.display:
            self.say("the job names no display; from host mode set display in machines/<mac>.conf, then plant again")
            self.quit("the job names no display", outcome="no-display-expectation", agent=True)
        r = self.m.run(["/usr/bin/python3", self.tool(CHECK), "--displays-only"])
        said = (r.out + r.err).strip()
        if r.ok:
            return self.say("displays: %s" % said)
        self.say("the screen this would be measured on is not the declared one:", *("  " + l for l in said.splitlines()))
        self.say("  Nothing runs. Disconnect the monitor and boot this volume again; the job stays planted and spends no attempt.")
        self.quit("the display is not the declared one", attempt_back=True)

    def converge_display_mode(self):
        """WindowServer reads its mode at start, so a mode that is not the declared one is written and this boot repeated."""
        want = (self.display.split() + ["", ""])[1]
        r = self.wkmac("display-mode")
        running = r.out.strip() if r.ok else ""
        if running == want:
            return self.say("display mode: %s, as the job declares" % running)
        self.say("display mode: running at %s, and the job declares %s" % (running or "unreadable", want))
        if self.state().get("mode_declared") == want:
            self.say("  %s was written for this boot and the panel still comes up at %s: MotionMark scores the area it draws, so"
                     % (want, running or "unreadable"), "  set the mode by hand from host mode and re-plant, or declare the one it comes up at:",
                     '    display="%s %s"  in machines/<mac>.conf' % (self.display.split()[0], running or "<what it reads>"))
            self.quit("the declared display mode cannot be set", outcome="display-mode-unsettable", agent=True)
        if not self.sudo("python3", self.tool(WKMAC), "display-mode", "--declare", want):
            self.say("  the WindowServer configuration would not take %s." % want)
            self.quit("the declared display mode could not be written", outcome="display-mode-unwritable", agent=True)
        self.state_set(mode_declared=want, attempts=int(self.state().get("attempts") or 1) - 1)
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
        if self.logged(py + ["wk.sysimage.mactailnet", "install", "/", self.root + "/tailnet"]) != 0:
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
        front = screen.blocker(self.m, TREE)
        if front == "?":
            self.say("WARNING: could not ask the window server what is on the screen; a run timing out with no error is this")
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

    def stop_updates(self):
        """The preference reads back false yet a scan can still run, so the daemons are booted out until power-off."""
        self.say("stopping the software-update scanner")
        for svc in UPDATERS:
            if self.sudo("launchctl", "bootout", svc):
                self.say("  booted out %s" % svc)
            elif self.m.run(["sudo", "-n", "launchctl", "print", svc]).ok:
                self.say("  WARNING: could not boot out %s; a scan can still start inside a run, and the leg it crosses is dropped" % svc)
        for key in ("AutomaticCheckEnabled", "AutomaticDownload"):
            self.sudo("defaults", "write", SU_PREFS, key, "-bool", "false")

    def measure(self):
        j = self.job
        env = dict(self.env, WK_STORE=self.root, **{name: str(j[key]) for key, name in VARIANCE if j.get(key)},
                   **({"WK_FORCE": "1"} if j.get("rehearsal") else {}))
        vol = Volume(TREE, places.Registry(TREE, env, self.m), j.get("workspace") or "", self.clock, j.get("machine") or "")
        arms = [Arm(a.get("label") or "AB"[i], vol, a.get("id") or "", a.get("browser_args") or "") for i, a in enumerate((j.get("arms") or [])[:2])]
        o = {k: str(j[k]) for k in ("rounds", "max_rounds", "detect", "count", "timeout", "task") if j.get(k) not in (None, "")}
        ab = AB(TREE, vol.reg, self.clock, vol.ws, list(j.get("plans") or ()), o, "slot", arms, lambda taskdir: None, PLANTED)
        try:
            ab.go()
        except Refused as e:
            self.say("the A/B stopped: exit %s" % e.status)
        self.state_set(outcome=ab.outcome or "failed")

    def body(self):
        self.begin()
        self.arm()
        self.dim_display()
        if not self.m.exists(self.tool("wk")):
            self.say("FATAL: no wk at %s -- cannot run anything" % self.tool("wk"))
            self.quit("no wk-tools", outcome="no-wk-tools", agent=True, rc=1)
        self.converge_self()
        self.hold_auto_brightness()
        self.refuse_wrong_displays()
        self.converge_display_mode()
        self.state_set(phase="running", started_at=self.clock.iso())
        settle = int(self.job.get("settle") or PLANTED["settle"])
        self.say("settling for %ds" % settle)
        self.pause(settle)
        self.wait_for_temp()
        self.clear_the_screen()
        self.stop_updates()
        self.say("quiescing")
        if self.logged(["env", "WK_STORE=" + self.root, self.tool("wk"), "quiesce", "on"]) != 0:
            self.say("WARNING: quiesce reported a problem; each leg's preflight judges it")
        self.measure()
        self.say("leaving the machine quiesced: quiet is this install's permanent state")
        self.state_set(phase="done", finished_at=self.clock.iso())
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
    root = bench_root(os.environ)
    m.mkdir(root)
    if not act.dry_run():
        fd = os.open(root + "/autorun.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    return Autorun(m, Clock(), os.environ).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
