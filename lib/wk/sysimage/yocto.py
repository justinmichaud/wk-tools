"""The yocto builder's driving half: one stage of `wk sysimage build <a yocto image preset>` as a task through
task.Stage, around lib/wk/sysimage/yocto_ws.py in the workspace. The spec is WebKit's own Tools/yocto on
the release branch, so an image pins the same commits as the WebKit that runs on the board."""

import os
import re
import shlex

from wk import act, fleet, images, job, pgo, record
from wk.act import die, info, log, warn
from wk.resources import disk_gb
from wk.sysimage import task
from wk.sysimage.write import wants_wifi

STAGES = ("layers", "fetch", "image", "toolchain", "webkit", "pgo-mix")
WEBKIT_MB_PER_JOB = 2560   # one cross WebKit compile's working set, sized to WebCore's unified sources
BASE_IMAGE = "docker.io/library/ubuntu:24.04"   # a supported scarthgap build host, unlike the wkdev SDK image
SPEC = os.path.join("container", "yocto", "Containerfile")
PATTERN = "*yocto_ws.py*"
KILL_WAIT = 120      # bitbake writes sstate as it goes and shuts down slowly; a clean stop resumes the task it was in
COOKER_KILL_WAIT = 5
WEDGE_BEATS = 48     # 4 h of heartbeats naming one task; a wedged image stage has run 22800 s before anyone noticed
TASK_NAMED = re.compile(r"Running task \d+ of \d+ \(([^)]+)\)|recipe (\S+): task (do_\w+): Started|^\s*\d+: (\S+ do_\w+)", re.M)
CROSS = {"wpe-cross": "the release branch's own flags and nothing else",
         "wpe-cross-pgo-collect": "clang, thin LTO and LLVM profile generation -- the collection build, which nothing measures",
         "wpe-cross-pgo-use": "clang, full LTO and a collected profile -- the build every number from a 2.52+ board is taken from"}
USAGE = ("usage: wk sysimage build %s [--dry-run|--workspace <name>|--stage <name>|\n    --detach|--stop|--keep-work|--chromium|"
         "--no-local-layer|--no-tailnet]\n    'wk sysimage webkit %s' adds --commit, --slot, --preset and --pgo-profile.")
STAGE_HELP = """unknown stage '%s'. One of, each including the ones above it:
      layers     sync the Yocto layers only (minutes; the network-bound part)
      fetch      ... and fetch every source, without building -- one pass that
                 names every host the egress allowlist is still missing
      image      bitbake the image -- rootfs, kernel, wic  (the default; hours)
      toolchain  bitbake populate_sdk, the cross toolchain (hours)
      webkit     cross-build WebKit against that toolchain
      pgo-mix    mix a collection into the one profile the measured build
                 reads, with the toolchain that wrote it
    They are separate commands because they fail differently: 'layers' is
    egress, the rest is compilation."""


def stage_index(stage):
    if stage not in STAGES:
        die(STAGE_HELP % stage)
    return STAGES.index(stage) + 1


def stage_budget(stage, machine_jobs, machine_mb, webkit_jobs):
    """(jobs, MB) a stage books: bitbake takes the machine, a cross WebKit build its own job count, the mix one llvm-profdata."""
    if stage == "webkit":
        return webkit_jobs, webkit_jobs * WEBKIT_MB_PER_JOB
    if stage == "pgo-mix":
        return 1, WEBKIT_MB_PER_JOB
    return machine_jobs, machine_mb


def disk_need(stage, chromium, rm_work, env):
    if stage == "pgo-mix":
        return 2
    if stage == "webkit":
        return disk_gb(env)
    return (120 if chromium else 60) + (0 if rm_work else 60)


def workdir(cross_target):
    return "/src/WebKit/WebKitBuild/CrossToolChains/" + cross_target   # cross-toolchain-helper's layout


def host_workdir(ws_dir, cross_target):
    return os.path.join(ws_dir, "build", "CrossToolChains", cross_target)


def cross_preset(name, profile):
    """(cc, cxx, cmake, pgo). The PGO pair share a build directory, so each states both options (WEBKIT_OPTION_CONFLICT)."""
    if name not in CROSS:
        die("no such cross preset '%s'. They are:\n%s" % (name, "".join("      %-22s %s\n" % kv for kv in CROSS.items())))
    if name != "wpe-cross-pgo-use" and profile:
        die("--pgo-profile names a profile to build against, and only 'wpe-cross-pgo-use' does")
    if name == "wpe-cross":
        return "", "", "", ""
    if name == "wpe-cross-pgo-collect":
        return "clang", "clang++", ("-DLTO_MODE=thin -DENABLE_LLVM_PROFILE_GENERATION=ON -DUSE_PGO_PROFILE=OFF -DPGO_PROFILE_DIR=%s"
                                    % pgo.BOARD_DIR), "collect"
    if not profile:
        die("wpe-cross-pgo-use needs --pgo-profile, the merged .profdata cmake reads (PGO_PROFILE_PATH)")
    return "clang", "clang++", "-DLTO_MODE=full -DENABLE_LLVM_PROFILE_GENERATION=OFF -DUSE_PGO_PROFILE=ON -DPGO_PROFILE_PATH=" + profile, "use"


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


def last_of(rest, on, off):
    got = [a for a in rest if a in (on, off)]
    return (got[-1] == on) if got else None


CONFIG_WORDS = {
    "wpe-cross-pgo-collect": "instrumented, to collect a profile from -- not a measurement",
    "wpe-cross-pgo-use": "the measured build, against the mixed profile",
    "wpe-cross": "built without a profile",
    "": "the image itself",
}


def build_subject(ws, stage, slot, commit, cross_preset):
    if stage == "webkit":
        return "slot %s in %s at %.12s -- %s" % (slot, ws, commit, CONFIG_WORDS.get(cross_preset, cross_preset))
    if stage == "pgo-mix":
        return "mixing slot %s's collection in %s" % (slot, ws)
    return "%s stage of %s" % (stage or "build", ws)


class Yocto(task.ContainerBuilder):
    KIND, TITLE, SPEC, BASE_IMAGE, BASE_VAR, PATTERN = "yocto", "Yocto", SPEC, BASE_IMAGE, "WK_YOCTO_BASE", PATTERN
    NEEDS = "the Yocto builder needs a container workspace"
    NOT_HERE = ("A remote place is a shared machine -- 100 GB of scratch and days of CPU\n"
                "    are not ours to take there -- and a macOS VM workspace has no store-backed\n"
                "    Yocto cache to build against.")
    IMAGE_NOTE = "  a supported Yocto build host: GCC 13, Python 3.12, glibc 2.39 (%(spec)s)."
    SURVIVES = "the Yocto caches are in the store and survive"

    def kill_cmd(self, ws, stage):
        return "wk sysimage build %s --stage %s%s --stop" % (self.spec, stage, self.ws_flag(ws))

    def stage(self, driver, ws, stage):
        st = super().stage(driver, ws, stage)
        env = {k: v for k, v in driver.env.items() if k != "WK_ABORT_SECONDS"}
        st.recs = record.of_driver(driver, self.clock, self.here, env)   # a record with no deadline: silence is not a failure
        st.env = dict(self.env, WK_KILL_WAIT=str(job.kill_wait(self.env, KILL_WAIT)))
        st.watchdog = {"abort": 0, "wedge": (WEDGE_BEATS, task_named)}
        return st

    def refuse_running(self, st, ws):
        t = st.recs.find("yocto", ws)
        if t is not None and t.alive(None):
            live = running_stage(t) or t.field("stage")
            die("a '%s' build is already running in '%s', and the stages share one bitbake build directory.\n"
                "    Follow it:  wk status %s --log -f\n    Stop it:    %s" % (live, ws, ws, t.field("kill")))
        st.refuse_busy()

    def ws_head(self, driver, ws):
        r = driver.exec(ws, ["bash", "-c", "cd /src/WebKit && git rev-parse --abbrev-ref HEAD"])
        lines = [l for l in r.out.replace("\r", "").split("\n") if l.strip()]
        return lines[-1].strip() if r.ok and lines else ""

    def ensure_ws(self, driver, ws, base, tag):
        super().ensure_ws(driver, ws, base, tag)
        self.checkout(driver, ws)

    def checkout(self, driver, ws):
        """A workspace's remotes read this machine's mirror, so the fetch is local and a branch the mirror lacks is absent."""
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
        if not r.ok:
            return None
        return re.findall(r"^\[([^\]]+)\]", r.out.replace("\r", ""), re.M)

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
        if t in have:
            return
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

    def stop(self, driver, st, ws, stage):
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

    def parse(self, rest):
        o = task.options(rest, ("--stop", "--detach", "--keep-work", "--chromium", "--local-layer",
                                "--no-local-layer", "--tailnet", "--no-tailnet"),
                         ("--workspace", "--stage", "--commit", "--slot", "--preset", "--pgo-profile"), USAGE % (self.name, self.name))
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
        cc, cxx, cmake, _ = cross_preset(preset, o.get("--pgo-profile") or "")
        local, tail = last_of(rest, "--local-layer", "--no-local-layer"), last_of(rest, "--tailnet", "--no-tailnet")
        return dict(o, stage=stage, commit=commit, slot=slot, preset=preset, cc=cc, cxx=cxx, cmake=cmake,
                    chromium=bool(o.get("--chromium")) or self.p["YOC_CHROMIUM"] != "0",
                    rm_work=self.p["YOC_RM_WORK"] == "1" and not o.get("--keep-work"),
                    local=(self.p["YOC_LOCAL_LAYER"] != "0") if local is None else local,
                    tailnet=True if tail is None else tail)

    @staticmethod
    def sizes(st):
        budget, running, webkit_jobs = st.size(mb=WEBKIT_MB_PER_JOB)
        return st.res.envelope_cores(), st.res.envelope_mem_mb(), webkit_jobs, budget, running

    def argv(self, driver, ws, o, cores, stage_mb, webkit_jobs, tag):
        p, q, opt = self.p, o, task.opt
        return (["python3", driver.tools(ws) + "/lib/wk/sysimage/yocto_ws.py", "--target", p["YOC_TARGET"],
                 "--image", p["YOC_IMAGE"], "--stage", q["stage"], "--jobs", str(cores), "--mem-budget", str(stage_mb),
                 "--rm-work", "1" if q["rm_work"] else "0"]
                + opt("--port-target-from", p["YOC_PORT_TARGET_FROM"]) + opt("--port-machine", p["YOC_MACHINE"])
                + opt("--board", p["IMG_MACHINE"]) + opt("--multilib", p["YOC_MULTILIB"])
                + opt("--multilib-tune", p["YOC_MULTILIB_TUNE"])
                + ["--chromium", "1" if q["chromium"] else "0", "--cross-preset", q["preset"]]
                + opt("--cross-cc", q["cc"]) + opt("--cross-cxx", q["cxx"]) + (["--cross-cmake=" + q["cmake"]] if q["cmake"] else [])
                + (["--pgo-dir", pgo.pgo_dir_in(q["slot"]), "--pgo-lib", pgo.GLIB_LIB] if q["stage"] == "pgo-mix" else [])
                + ["--local-layer", "1" if q["local"] else "0", "--tailnet", "1" if q["tailnet"] else "0",
                   "--webkit-jobs", str(webkit_jobs), "--sstate-ns", re.sub(r"[:/]", "-", tag.rsplit("/", 1)[-1])]
                + opt("--commit", q["commit"])
                + (["--slot", q["slot"], "--image-preset", self.name] if q["slot"] and q["stage"] == "webkit" else []))

    def webkit(self, rest):
        return self.build(["--stage", "webkit"] + list(rest))

    def build(self, rest):
        o = self.parse(rest)
        driver = self.driver()
        ws = self.ws_of(o)
        st = self.stage(driver, ws, o["stage"])
        if act.dry_run():
            return self.report(driver, st, ws, o)
        if o.get("--stop"):
            return self.stop(driver, st, ws, o["stage"])
        self.refuse_running(st, ws)
        if o.get("--detach"):
            return self.detach(st, "build", rest, "'%s' stage of %s" % (o["stage"], self.name))
        base, tag = self.host_image()
        cores, mem, webkit_jobs, budget, running = self.sizes(st)
        jobs, stage_mb = stage_budget(o["stage"], cores, mem, webkit_jobs)
        what = {"pgo-mix": "mixing this collection", "webkit": "this WebKit cross build"}.get(o["stage"], "this image build")
        self.staged(st, (budget, running, jobs), list(STAGES),
                    [lambda: self.ensure_ws(driver, ws, base, tag), lambda: self.check_target(driver, ws)],
                    "stage '%s' for %s in '%s'" % (o["stage"], self.name, ws),
                    lambda: self.argv(driver, ws, o, cores, stage_mb, webkit_jobs, tag), at=stage_index(o["stage"]),
                    subject=build_subject(ws, o["stage"], o["slot"], o["commit"], o["preset"] if o["stage"] == "webkit" else ""),
                    mb=stage_mb, need_gb=disk_need(o["stage"], o["chromium"], o["rm_work"], self.env), what=what)
        return self.done(driver, ws, o)

    def done(self, driver, ws, o):
        info("stage '%s' ok" % o["stage"])
        if o["stage"] == "pgo-mix":
            info("the collection is mixed; the measured build reads it as\n    %s/output/%s.profdata"
                 % (pgo.pgo_dir_in(o["slot"]), pgo.GLIB_LIB))
            return 0
        if o["stage"] != "image":
            info("stage '%s' builds no disk image, so there is nothing more to report" % o["stage"])
            return 0
        wic = os.path.join(host_workdir(driver.store.ws_dir(ws), self.p["YOC_TARGET"]), "build", "image", self.p["YOC_IMAGE"] + ".wic.xz")
        info("built %s-%s  (%s)" % (self.name, self.clock.stamp(), self.du(wic)))
        log("  %s" % wic)
        log("  next:  wk sysimage write --from <path above> --disk <machine>:<device>")
        log("         ('wk sysimage ls' lists it with the exact path)")
        log("  the image carries no WebKit -- it is the runtime. The matching")
        log("  build is:  wk sysimage build %s --stage webkit" % self.spec)
        return 0

    def report(self, driver, st, ws, o):
        p, stage = self.p, o["stage"]
        if stage == "pgo-mix":
            log("would mix the collection for slot '%s' of %s" % (o["slot"], self.name))
            log("  collection  %s" % images.pgo_dir(ws, o["slot"], self.env))
            log("              %s as the builder sees it -- one directory, two sides of the bind mount" % pgo.pgo_dir_in(o["slot"]))
            log("  benchmarks  %s, at WebKit's own weights (Tools/Scripts/pgo-profile)" % " ".join(pgo.BENCHMARKS))
            log("  into        %s/output/%s.profdata" % (pgo.pgo_dir_in(o["slot"]), pgo.GLIB_LIB))
            log("  where       inside %s's cross toolchain -- the clang that wrote the profiles is the only one that reads them" % ws)
            log("dry run -- nothing was mixed.")
            return 0
        at = "not created" if driver.info(ws) == "absent" else self.ws_head(driver, ws)
        cores, mem, webkit_jobs, budget, _ = self.sizes(st)
        wifi = wants_wifi(fleet.Fleet(images.root(self.env), self.env), p["IMG_MACHINE"])
        free = budget.free_gb(self.store.admission_dir())
        log("would build image %s (builder: yocto)" % self.name)
        log("  for machine %s (%s)" % (p["IMG_MACHINE"], p["IMG_ARCH"]))
        log("  branch      %s  (from the '%s' remote)" % (p["YOC_BRANCH"], p["YOC_REMOTE"] or "origin"))
        log("  cross-target %s%s" % (p["YOC_TARGET"], self.target_note(driver, ws)))
        log("  recipe      %s" % p["YOC_IMAGE"])
        log("  stage       %s (it includes the ones before it)" % stage)
        log("  workspace   %s (%s)" % (ws, at))
        log("  jobs        %d cores, %d MB envelope" % (cores, mem))
        log("  DL_DIR      %s (%s)" % (self.cache("downloads"), self.du(self.cache("downloads"))))
        log("  SSTATE_DIR  %s (%s)" % (self.cache("sstate"), self.du(self.cache("sstate"))))
        log("  rm_work     %s" % ("on (--keep-work turns it off)" if o["rm_work"] else "off"))
        log("  chromium    %s" % ("in the image (--chromium)" if o["chromium"] else "dropped (about half the build; --chromium puts it back)"))
        log("  webkit jobs %d (%d MB/job)" % (webkit_jobs, WEBKIT_MB_PER_JOB))
        log("  local fixes %s" % ("image/yocto/meta-wk is added to bblayers (build-time only)" if o["local"]
                                  else "none -- the branch's own configuration, unmodified"))
        log("  tailnet     %s" % ("tailscale in the image (meta-wk-tailnet); the card carries the key" if o["tailnet"]
                                  else "off -- the board is reachable only over whatever LAN it lands on"))
        log("  wifi        %s" % ("wk-wifi-join in the image (meta-wk-wifi); the card carries the credential" if wifi
                                  else "not needed -- %s has a cable" % (p["IMG_MACHINE"] or "this board")))
        log("  rescue      wk-card-priv in the image (meta-wk-rescue), so a board's rescue can write its bench medium")
        log("  disk free   %s" % ("%d GB" % free if free is not None else "unknown"))
        log("  into        %s/build/image/%s.wic.xz" % (workdir(p["YOC_TARGET"]), p["YOC_IMAGE"]))
        if o["slot"]:
            log("  commit      %s" % o["commit"])
            log("  slot        %s -> %s" % (o["slot"], images.slot_dir(ws, o["slot"], self.env)))
            log("  preset      %s -- %s" % (o["preset"], CROSS[o["preset"]]))
            if o.get("--pgo-profile"):
                log("              against %s" % o["--pgo-profile"])
        log("dry run -- nothing was built.")
        return 0

