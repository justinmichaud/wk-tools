"""The yocto builder's host half: its data and its stages over task.ContainerBuilder's lifecycle, around yocto_ws.py
in the workspace. The spec is WebKit's own Tools/yocto on the release branch, so an image pins the same commits as
the WebKit that runs on the board."""

import os
import re
import shlex

from wk import fleet, images, job, pgo, record
from wk.act import die, info, log, warn
from wk.resources import disk_gb
from wk.sysimage import Failed, task
from wk.sysimage.write import wants_wifi
from wk.sysimage.yocto_ws import CROSS, STAGES, WEBKIT_MB_PER_JOB, cross_preset

PATTERN = "*yocto_ws.py*"
KILL_WAIT = 120      # bitbake writes sstate as it goes and shuts down slowly; a clean stop resumes the task it was in
COOKER_KILL_WAIT = 5
WEDGE_BEATS = 48     # 4 h of heartbeats naming one task; a wedged image stage has run 22800 s before anyone noticed
IMAGE_GB = 60        # rm_work on, Chromium dropped (yocto_ws.IMAGE_LINES)
TASK_NAMED = re.compile(r"Running task \d+ of \d+ \(([^)]+)\)|recipe (\S+): task (do_\w+): Started|^\s*\d+: (\S+ do_\w+)", re.M)
STAGE_HELP = """unknown stage '%s'. One of, each including the ones above it:
      layers     sync the Yocto layers only (minutes; the network-bound part)
      fetch      ... and fetch every source, without building -- one pass that
                 names every host the egress allowlist is still missing
      image      bitbake the image -- rootfs, kernel, wic  (the default; hours)
      toolchain  bitbake populate_sdk, the cross toolchain (hours)
      webkit     cross-build WebKit against that toolchain
      pgo-mix    mix a collection into the one profile the measured build
                 reads, with the toolchain that wrote it"""
CONFIG_WORDS = {pgo.COLLECT: "instrumented, to collect a profile from -- not a measurement",
                pgo.USE: "the measured build, against the mixed profile", "wpe-cross": "built without a profile"}


def stage_index(stage):
    if stage not in STAGES:
        die(STAGE_HELP % stage)
    return STAGES.index(stage) + 1


def stage_budget(stage, machine_jobs, machine_mb, webkit_jobs):
    if stage == "webkit":
        return webkit_jobs, webkit_jobs * WEBKIT_MB_PER_JOB
    if stage == "pgo-mix":
        return 1, WEBKIT_MB_PER_JOB
    return machine_jobs, machine_mb


def disk_need(stage, env):
    return {"pgo-mix": 2, "webkit": disk_gb(env)}.get(stage, IMAGE_GB)


def host_workdir(ws_dir, cross_target):
    return os.path.join(ws_dir, "build", "CrossToolChains", cross_target)


def task_named(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            tail = f.read().decode(errors="replace").replace("\r", "\n")
    except OSError:
        return ""
    m = None
    for m in TASK_NAMED.finditer(tail):
        pass
    if not m:
        return ""
    if m.group(1):
        return m.group(1).rsplit("/", 1)[-1]
    return "%s:%s" % (m.group(2), m.group(3)) if m.group(2) else m.group(4)


def running_stage(t):
    n = t.step_now() if t is not None else None
    return STAGES[n - 1] if n else ""


def build_subject(ws, stage, slot, commit, preset):
    if stage == "webkit":
        return "slot %s in %s at %.12s -- %s" % (slot, ws, commit, CONFIG_WORDS.get(preset, preset))
    if stage == "pgo-mix":
        return "mixing slot %s's collection in %s" % (slot, ws)
    return "%s stage of %s" % (stage or "build", ws)


class Yocto(task.ContainerBuilder):
    KIND, TITLE, BASE_VAR, PATTERN = "yocto", "Yocto", "WK_YOCTO_BASE", PATTERN
    BASE_IMAGE = "docker.io/library/ubuntu:24.04"   # a supported scarthgap build host, unlike the wkdev SDK image
    SPEC = os.path.join("container", "yocto", "Containerfile")
    NEEDS = "the Yocto builder needs a container workspace"
    NOT_HERE = ("A remote place is a shared machine -- 100 GB of scratch and days of CPU\n"
                "    are not ours to take there -- and a macOS VM workspace has no store-backed\n"
                "    Yocto cache to build against.")
    IMAGE_NOTE = "  a supported Yocto build host: GCC 13, Python 3.12, glibc 2.39 (%(spec)s)."
    SURVIVES = "the Yocto caches are in the store and survive"
    USAGE = ("usage: wk sysimage build %s [--workspace <name>|--stage <name>|--detach|--stop]\n"
             "    'wk sysimage webkit %s' adds --commit, --slot, --preset and --pgo-profile.")
    VALUED = ("--workspace", "--stage", "--commit", "--slot", "--preset", "--pgo-profile")

    def kill_cmd(self, flag, stage):
        return "wk sysimage build %s --stage %s%s --stop" % (self.spec, stage, flag)

    def stage(self, driver, ws, stage):
        st = super().stage(driver, ws, stage)
        env = {k: v for k, v in driver.env.items() if k != "WK_ABORT_SECONDS"}
        st.recs = record.of_driver(driver, self.clock, self.here, env)   # a record with no deadline: silence is not a failure
        st.env = dict(self.env, WK_KILL_WAIT=str(job.kill_wait(self.env, KILL_WAIT)))
        st.watchdog = {"abort": 0, "wedge": (WEDGE_BEATS, task_named)}
        return st

    def webkit(self, rest):
        return self.build(["--stage", "webkit"] + list(rest))

    def parse(self, rest):
        o = task.options(rest, self.FLAGS, self.VALUED, self.USAGE % (self.name, self.name))
        stage, commit, slot = o.get("--stage") or "image", o.get("--commit") or "", o.get("--slot") or ""
        stage_index(stage)
        if slot:
            images.check_slot_name(slot)
        if stage == "pgo-mix":
            if not slot:
                die("the pgo-mix stage mixes one slot's collection: --slot <name>")
            if commit:
                die("--commit builds; the pgo-mix stage builds nothing, it mixes\n    what was collected from the slot named by --slot")
        elif commit or slot:
            if stage != "webkit":
                die("--commit/--slot belong to the webkit stage (wk sysimage webkit %s)" % self.name)
            if not (commit and slot):
                die("a slot needs both --commit <sha> and --slot <name>")
            task.check_commit(commit)
        preset = o.get("--preset") or "wpe-cross"
        if stage != "webkit" and (preset != "wpe-cross" or o.get("--pgo-profile")):
            die("--preset and --pgo-profile belong to the webkit stage; '%s' builds no WebKit" % stage)
        try:
            cross_preset(preset, o.get("--pgo-profile") or "")
        except Failed as e:
            die(str(e))
        return dict(o, stage=stage, commit=commit, slot=slot, preset=preset)

    def stop(self, driver, st, ws, o):
        stage = o["stage"]
        if driver.info(ws) == "absent":
            die("no workspace '%s', so nothing is building" % ws)
        t = st.recs.find("yocto", ws)
        if t is not None and t.alive(None) and running_stage(t) != stage:
            log("no '%s' build is running in '%s'; its '%s' stage is.\n  Stop that one:  %s" % (stage, ws, running_stage(t), t.field("kill")))
            return 0
        if t is None or not t.alive(None):
            log("no '%s' build is running in '%s'" % (stage, ws))
            return 0 if self.stop_cooker(driver, ws) else 1
        st.stop()
        if not self.stop_cooker(driver, ws):
            return 1
        info("stopped. sstate is written as it goes, so restarting resumes rather than\n  starting over -- only the task it was in is redone.")
        return 0

    def refuse(self, driver, st, ws, o):
        t = st.recs.find("yocto", ws)
        if t is not None and t.alive(None):
            die("a '%s' build is already running in '%s', and the stages share one bitbake build directory.\n"
                "    Follow it:  wk status %s --log -f\n    Stop it:    %s"
                % (running_stage(t) or t.field("stage"), ws, ws, t.field("kill")))
        st.refuse_busy()

    def what(self, o):
        return "'%s' stage of %s" % (o["stage"], self.name)

    def sizes(self, st):
        budget, running, webkit_jobs = st.size(mb=WEBKIT_MB_PER_JOB)
        return st.res.envelope_cores(), st.res.envelope_mem_mb(), webkit_jobs, budget, running

    def plan(self, driver, st, ws, o):
        cores, mem, webkit_jobs, budget, running = self.sizes(st)
        jobs, mb = stage_budget(o["stage"], cores, mem, webkit_jobs)
        tag = self.host_image()[1]
        argv = (["python3", driver.tools(ws) + "/lib/wk/sysimage/yocto_ws.py", self.name, "--stage", o["stage"],
                 "--jobs", str(cores), "--mem-budget", str(mb), "--webkit-jobs", str(webkit_jobs),
                 "--sstate-ns", re.sub(r"[:/]", "-", tag.rsplit("/", 1)[-1]), "--cross-preset", o["preset"]]
                + sum((["--" + k, o[k]] for k in ("commit", "slot") if o[k]), [])
                + (["--pgo-profile", o["--pgo-profile"]] if o.get("--pgo-profile") else []))
        return dict(sized=(budget, running, jobs), plan=list(STAGES), at=stage_index(o["stage"]), mb=mb,
                    steps=[lambda: self.ensure_ws(driver, ws), lambda: self.check_target(driver, ws)],
                    say="stage '%s' for %s in '%s'" % (o["stage"], self.name, ws), argv=lambda: argv,
                    subject=build_subject(ws, o["stage"], o["slot"], o["commit"], o["preset"] if o["stage"] == "webkit" else ""),
                    need_gb=disk_need(o["stage"], self.env),
                    what={"pgo-mix": "mixing this collection", "webkit": "this WebKit cross build"}.get(o["stage"], "this image build"))

    def ws_head(self, driver, ws):
        r = driver.exec(ws, ["bash", "-c", "cd /src/WebKit && git rev-parse --abbrev-ref HEAD"])
        lines = [l for l in r.out.replace("\r", "").split("\n") if l.strip()]
        return lines[-1].strip() if r.ok and lines else ""

    def ensure_ws(self, driver, ws):
        """Then the preset's branch: a workspace's remotes read this machine's mirror, so a branch the mirror lacks is absent."""
        super().ensure_ws(driver, ws)
        branch, remote = self.p["YOC_BRANCH"], self.p["YOC_REMOTE"] or "origin"
        at = self.ws_head(driver, ws)
        if at == branch:
            return
        info("checking out '%s' in '%s' (was %s)" % (branch, ws, at or "unknown"))
        q = shlex.quote
        line = ("cd /src/WebKit && { git checkout -q %s 2>/dev/null || { git fetch -q %s %s && git checkout -q %s; }; }"
                % (q(branch), q(remote), q(branch + ":" + branch), q(branch)))
        if not driver.act_exec(ws, ["bash", "-c", line]).ok:
            die("could not check out '%s' from '%s' in '%s'. The fetch reads this machine's mirror, which\n"
                "    carries %s of origin and every head of the other upstreams; if it is behind:  wk sync\n"
                "    If it is not, 'wk sync %s --fix' re-asserts the workspace's remotes."
                % (branch, remote, ws, " ".join(["main"] + images.origin_branches(self.env)), ws))

    def sections(self, driver, ws):
        r = driver.exec(ws, ["bash", "-c", "cat /src/WebKit/Tools/yocto/targets.conf"])
        return re.findall(r"^\[([^\]]+)\]", r.out.replace("\r", ""), re.M) if r.ok else None

    def check_target(self, driver, ws):
        """Asked here: bitbake's own version of it is a config-parse error hours into the stage."""
        t, branch = self.p["YOC_TARGET"], self.p["YOC_BRANCH"]
        if self.p["YOC_PORT_TARGET_FROM"]:
            info("%s has no [%s]; the build derives one from [%s]" % (branch, t, self.p["YOC_PORT_TARGET_FROM"]))
            return
        have = self.sections(driver, ws)
        if have is None:
            die("could not read Tools/yocto/targets.conf in '%s' to check %s has a [%s] section.\n"
                "    Check the workspace is up:  wk status %s" % (ws, branch, t, ws))
        if t not in have:
            die("%s has no [%s] section in Tools/yocto/targets.conf. Add it and its local.conf upstream, or have\n"
                "    the image preset derive them (YOC_PORT_TARGET_FROM, YOC_MACHINE; see image/presets/wpewebkit-2.46-yocto-rpi5-64.conf).\n"
                "    The sections here are:\n%s" % (branch, t, "".join("      %s\n" % s for s in have)))

    def target_note(self, driver, ws):
        t = self.p["YOC_TARGET"]
        if driver.info(ws) == "absent":
            return "  (not verified: '%s' does not exist yet; the build refuses if its branch has no [%s])" % (ws, t)
        have = self.sections(driver, ws)
        if have is None:
            return "  (not verified: could not read the branch's targets.conf)"
        return "  (verified on %s)" % self.p["YOC_BRANCH"] if t in have else "  ** %s has no [%s] section: this build would refuse **" % (self.p["YOC_BRANCH"], t)

    def cooker_pid(self, driver, ws):
        """The live bitbake this image workspace's lock names, believed only while its command line there says bitbake:
        a wkdev container shares the host's PID namespace."""
        lock = os.path.join(host_workdir(driver.store.ws_dir(ws), self.p["YOC_TARGET"]), "build", "bitbake.lock")
        try:
            digits = re.sub(r"[^0-9]", "", self.here.read(lock))
        except OSError:
            return None
        if not digits or not job.match_any(job.pid_args(driver, ws, digits), "*bitbake*"):
            return None
        return int(digits)

    def stop_cooker(self, driver, ws):
        """A driver killed mid-stage leaves its cooker holding the lock the next stage refuses on; True once none is left."""
        pid = self.cooker_pid(driver, ws)
        if pid is None:
            return True
        info("a bitbake cooker (pid %d) is still in '%s' with no record holding it -- a\n  killed driver left it. Stopping it." % (pid, ws))
        if job.terminate(lambda signum: job.kill_tree_in(driver, ws, pid, signum),
                         lambda: self.cooker_pid(driver, ws) is None, self.clock, KILL_WAIT, COOKER_KILL_WAIT):
            info("the cooker is gone; sstate is written as it goes, so what it had done stands.")
            return True
        warn("pid %d outlived a TERM and a KILL. What ends it is the container:\n  wk stop %s" % (pid, ws))
        return False

    def done(self, driver, ws, o):
        info("stage '%s' ok" % o["stage"])
        if o["stage"] == "pgo-mix":
            info("the collection is mixed; the measured build reads it as\n    %s" % pgo.profile_path(o["slot"]))
        elif o["stage"] == "image":
            wic = os.path.join(host_workdir(driver.store.ws_dir(ws), self.p["YOC_TARGET"]), "build", "image", self.p["YOC_IMAGE"] + ".wic.xz")
            info("built %s-%s  (%s)" % (self.name, self.clock.stamp(), self.du(wic)))
            log("  %s\n  next:  wk sysimage write --from %s --disk <machine>:<device>\n"
                "  the image carries no WebKit -- it is the runtime. The matching\n  build is:  wk sysimage webkit %s"
                % (wic, self.spec, self.spec))
        return 0

    def report(self, driver, st, ws, o):
        p, stage, slot = self.p, o["stage"], o["slot"]
        if stage == "pgo-mix":
            return ("mix the collection for slot '%s' of %s" % (slot, self.name),
                    [("collection", images.pgo_dir(ws, slot, self.env)), ("", "%s as the builder sees it" % pgo.pgo_dir_in(slot)),
                     ("benchmarks", "%s, at WebKit's own weights (Tools/Scripts/pgo-profile)" % " ".join(pgo.BENCHMARKS)),
                     ("into", pgo.profile_path(slot)),
                     ("where", "inside %s's cross toolchain -- the clang that wrote the profiles is the only one that reads them" % ws)],
                    "mixed")
        cores, mem, webkit_jobs, budget, _ = self.sizes(st)
        free = budget.free_gb(self.store.admission_dir())
        wifi = wants_wifi(fleet.Fleet(images.root(self.env), self.env), p["IMG_MACHINE"])
        rows = [("for machine", "%s (%s)" % (p["IMG_MACHINE"], p["IMG_ARCH"])),
                ("branch", "%s  (from the '%s' remote)" % (p["YOC_BRANCH"], p["YOC_REMOTE"] or "origin")),
                ("cross-target", p["YOC_TARGET"] + self.target_note(driver, ws)), ("recipe", p["YOC_IMAGE"]),
                ("stage", "%s (it includes the ones before it)" % stage),
                ("workspace", "%s (%s)" % (ws, "not created" if driver.info(ws) == "absent" else self.ws_head(driver, ws))),
                ("jobs", "%d cores, %d MB envelope; webkit %d (%d MB/job)" % (cores, mem, webkit_jobs, WEBKIT_MB_PER_JOB)),
                ("DL_DIR", "%s (%s)" % (self.cache("downloads"), self.du(self.cache("downloads")))),
                ("SSTATE_DIR", "%s (%s)" % (self.cache("sstate"), self.du(self.cache("sstate")))),
                ("layers", "image/yocto/meta-wk*: tailscale (the card carries the key), wk-card-priv (the rescue writes)"),
                ("wifi", "wk-wifi-join; the card carries the credential" if wifi else "not needed -- %s has a cable" % (p["IMG_MACHINE"] or "this board")),
                ("disk free", "%d GB" % free if free is not None else "unknown"),
                ("into", "/src/WebKit/WebKitBuild/CrossToolChains/%s/build/image/%s.wic.xz" % (p["YOC_TARGET"], p["YOC_IMAGE"]))]
        if slot:
            rows += [("commit", o["commit"]), ("slot", "%s -> %s" % (slot, images.slot_dir(ws, slot, self.env))),
                     ("preset", "%s -- %s" % (o["preset"], CROSS[o["preset"]]))]
        return "build image %s (builder: yocto)" % self.name, rows
