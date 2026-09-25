"""The yocto builder's driving half: one stage of `wk sysimage build <a yocto profile>` as a task through
task.Stage, around lib/wk/sysimage/yocto_target.py in the workspace. The spec is WebKit's own Tools/yocto on
the release branch, so an image pins the same commits as the WebKit that runs on the board."""

import os
import re
import shlex
import signal as sig

from wk import act, build, fleet, images, job, pgo, record
from wk.act import die, info, log, warn
from wk.buildconf import DISK_GB
from wk.resources import Budget, Resources, build_jobs
from wk.sysimage import task
from wk.sysimage.write import wants_wifi

STAGES = ("layers", "fetch", "image", "toolchain", "webkit", "pgo-mix")
WEBKIT_MB_PER_JOB = 2560   # one cross WebKit compile's working set, sized to WebCore's unified sources
BASE_IMAGE = "docker.io/library/ubuntu:24.04"   # a supported scarthgap build host, unlike the wkdev SDK image
SPEC = os.path.join("container", "yocto", "Containerfile")
PATTERN = "*yocto_target.py*"
KILL_WAIT = 120      # bitbake writes sstate as it goes and shuts down slowly; a clean stop resumes the task it was in
COOKER_KILL_WAIT = 5
WEDGE_BEATS = 48     # 4 h of heartbeats naming one task; a wedged image stage has run 22800 s before anyone noticed
TASK_NAMED = re.compile(r"Running task \d+ of \d+ \(([^)]+)\)|recipe (\S+): task (do_\w+): Started|^\s*\d+: (\S+ do_\w+)", re.M)
CROSS = {"wpe-cross": "the release branch's own flags and nothing else",
         "wpe-cross-pgo-collect": "clang and LLVM profile generation -- the collection build, which nothing measures",
         "wpe-cross-pgo-use": "clang and a collected profile -- the build every number from a 2.52+ board is taken from"}
USAGE = ("usage: wk sysimage build %s [--dry-run|--workspace <name>|--stage <name>|\n    --detach|--stop|--keep-work|--chromium|"
         "--no-local-layer|--no-tailnet]\n    'wk sysimage webkit %s' adds --commit, --slot, --config and --pgo-profile.")
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
        return int(env.get("WK_BUILD_DISK_GB") or DISK_GB)
    return (120 if chromium else 60) + (0 if rm_work else 60)


def workdir(cross_target):
    return "/src/WebKit/WebKitBuild/CrossToolChains/" + cross_target   # cross-toolchain-helper's layout


def host_workdir(ws_dir, cross_target):
    return os.path.join(ws_dir, "build", "CrossToolChains", cross_target)


def cross_config(name, profile):
    """(cc, cxx, cmake, pgo). The PGO pair share a build directory, so each states both options (WEBKIT_OPTION_CONFLICT)."""
    if name not in CROSS:
        die("no such cross config '%s'. They are:\n%s" % (name, "".join("      %-22s %s\n" % kv for kv in CROSS.items())))
    if name == "wpe-cross":
        if profile:
            die("--pgo-profile names a profile to build against, and 'wpe-cross' does not\n    build against one. That is 'wpe-cross-pgo-use'.")
        return "", "", "", ""
    if name == "wpe-cross-pgo-collect":
        if profile:
            die("--pgo-profile names a profile to build against, and '%s' does not\n    build against one. That is 'wpe-cross-pgo-use'." % name)
        return "clang", "clang++", ("-DENABLE_LLVM_PROFILE_GENERATION=ON -DUSE_PGO_PROFILE=OFF -DPGO_PROFILE_DIR=%s"
                                    % pgo.BOARD_DIR), "collect"
    if not profile:
        die("wpe-cross-pgo-use: no profile given. The measured build reads one merged .profdata,\n"
            "    and cmake refuses without it (PGO_PROFILE_PATH); 'wk sysimage webkit' collects one first.")
    return "clang", "clang++", "-DENABLE_LLVM_PROFILE_GENERATION=OFF -DUSE_PGO_PROFILE=ON -DPGO_PROFILE_PATH=" + profile, "use"


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


class Yocto(task.ContainerBuilder):
    KIND, TITLE, SPEC, BASE_IMAGE, BASE_VAR = "yocto", "Yocto", SPEC, BASE_IMAGE, "WK_YOCTO_BASE"
    NEEDS = "the Yocto builder needs a container workspace"
    NOT_HERE = ("A remote target is a shared machine -- 100 GB of scratch and days of CPU\n"
                "    are not ours to take there -- and a macOS VM workspace has no store-backed\n"
                "    Yocto cache to build against.")
    IMAGE_NOTE = "  a supported Yocto build host: GCC 13, Python 3.12, glibc 2.39 (%(spec)s)."
    SURVIVES = "the Yocto caches are in the store and survive"

    def kill_cmd(self, ws, stage):
        return "wk sysimage build %s --stage %s%s --stop" % (self.spec, stage, self.ws_flag(ws))

    def stage(self, target, ws, stage):
        st = task.Stage(self.reg, target, ws, "yocto", stage, self.kill_cmd(ws, stage), self.clock, self.popen)
        env = {k: v for k, v in target.env.items() if k != "WK_ABORT_SECONDS"}
        st.recs = record.of_target(target, self.clock, self.here, env)   # a record with no deadline: silence is not a failure
        st.env = dict(self.env, WK_KILL_WAIT=self.env.get("WK_KILL_WAIT") or str(KILL_WAIT))
        st.watchdog = {"abort": 0, "wedge": (WEDGE_BEATS, task_named)}
        return st

    def refuse_running(self, st, ws):
        t = st.recs.find("yocto", ws)
        if t is not None and t.alive(None):
            live = running_stage(t) or t.field("stage")
            die("a '%s' build is already running in '%s', and the stages share one\n    bitbake build directory -- two cookers "
                "in it is what bitbake's own lock\n    exists to prevent.\n    Follow it:  wk logs %s -f\n    Stop it:    %s"
                % (live, ws, ws, t.field("kill")))
        st.refuse_busy()

    def ws_head(self, target, ws):
        r = target.exec(ws, ["bash", "-c", "cd /src/WebKit && git rev-parse --abbrev-ref HEAD"])
        lines = [l for l in r.out.replace("\r", "").split("\n") if l.strip()]
        return lines[-1].strip() if r.ok and lines else ""

    def ensure_ws(self, target, ws, base, tag):
        """The branch is the version pin."""
        super().ensure_ws(target, ws, base, tag)
        self.checkout(target, ws)

    def checkout(self, target, ws):
        """A workspace's remotes read this machine's mirror, so the fetch is local and a branch the mirror lacks is absent."""
        branch, remote = self.p["YOC_BRANCH"], self.p["YOC_REMOTE"] or "origin"
        at = self.ws_head(target, ws)
        if at == branch:
            return
        info("checking out '%s' in '%s' (was %s)" % (branch, ws, at or "unknown"))
        q = shlex.quote
        line = ("cd /src/WebKit && { git checkout -q %s 2>/dev/null || { git fetch -q %s %s && git checkout -q %s; }; }"
                % (q(branch), q(remote), q(branch + ":" + branch), q(branch)))
        if not target.act_exec(ws, ["bash", "-c", line]).ok:
            die("could not check out '%s' from '%s' in '%s'.\n    That fetch reads this machine's mirror and no upstream, and "
                "the mirror\n    carries every branch a lane checks out -- %s of origin, every head of\n    the other upstreams "
                "-- so a missing one means the mirror is behind this\n    checkout:\n        wk sync\n    If the mirror does "
                "have it, 'wk sync %s' reports the workspace's remotes\n    and '--fix' re-asserts them."
                % (branch, remote, ws, " ".join(["main"] + images.origin_branches(self.env)), ws))

    def sections(self, target, ws):
        r = target.exec(ws, ["bash", "-c", "cat /src/WebKit/Tools/yocto/targets.conf"])
        if not r.ok:
            return None
        return re.findall(r"^\[([^\]]+)\]", r.out.replace("\r", ""), re.M)

    def check_target(self, target, ws):
        """Asked here: bitbake's own version of it is a config-parse error hours into the stage."""
        t, branch = self.p["YOC_TARGET"], self.p["YOC_BRANCH"]
        if self.p["YOC_PORT_TARGET_FROM"]:
            info("%s has no [%s]; the build derives one from [%s]" % (branch, t, self.p["YOC_PORT_TARGET_FROM"]))
            return
        have = self.sections(target, ws)
        if have is None:
            die("could not read Tools/yocto/targets.conf in '%s', so whether %s has a\n    [%s] section is unknown -- and a "
                "build configured from a section that is\n    not there fails inside bitbake, hours later. Check the "
                "workspace is up:  wk status %s" % (ws, branch, t, ws))
        if t in have:
            return
        die("%s has no [%s] section in Tools/yocto/targets.conf, so there is nothing\n    for bitbake to configure from. "
            "The machine is not what is missing -- the\n    meta-raspberrypi these manifests pin carries it -- but WebKit's "
            "own glue is:\n    the section and the local.conf it names. Add both upstream, or have the\n    profile derive them "
            "(YOC_PORT_TARGET_FROM=<a target this branch has>,\n    YOC_MACHINE=<the MACHINE it selects>; "
            "image/configs/wpewebkit-2.46-yocto-rpi5-64.conf\n    is the worked example). The sections here are:\n%s"
            % (branch, t, "".join("      %s\n" % s for s in have)))

    def target_note(self, target, ws):
        t = self.p["YOC_TARGET"]
        if target.info(ws) == "absent":
            return "  (not verified: '%s' does not exist yet; the build refuses if its branch has no [%s])" % (ws, t)
        have = self.sections(target, ws)
        if have is None:
            return "  (not verified: could not read the branch's targets.conf)"
        return "  (verified on %s)" % self.p["YOC_BRANCH"] if t in have else "  ** %s has no [%s] section: this build would refuse **" % (self.p["YOC_BRANCH"], t)

    def cooker_pid(self, target, ws):
        """The live bitbake this lane's lock names, believed only while its command line there says bitbake:
        a wkdev container shares the host's PID namespace."""
        lock = os.path.join(host_workdir(target.store.ws_dir(ws), self.p["YOC_TARGET"]), "build", "bitbake.lock")
        try:
            digits = re.sub(r"[^0-9]", "", self.here.read(lock))
        except OSError:
            return None
        if not digits or not job.match_any(job.pid_args(target, ws, digits), "*bitbake*"):
            return None
        return int(digits)

    def stop_cooker(self, target, ws):
        """A driver killed mid-stage leaves its cooker holding the lock the next stage refuses on; True once none is left."""
        pid = self.cooker_pid(target, ws)
        if pid is None:
            return True
        info("a bitbake cooker (pid %d) is still in '%s' with no record holding it -- a\n  killed driver left it. Stopping it." % (pid, ws))
        for signum, wait in ((sig.SIGTERM, KILL_WAIT), (sig.SIGKILL, COOKER_KILL_WAIT)):
            job.kill_tree_in(target, ws, pid, signum)
            for _ in range(wait):
                if self.cooker_pid(target, ws) is None:
                    info("the cooker is gone; sstate is written as it goes, so what it had done stands.")
                    return True
                self.clock.sleep(1)
        warn("pid %d outlived a TERM and a KILL. What ends it is the container:\n  wk stop %s" % (pid, ws))
        return False

    def stop(self, target, st, ws, stage):
        if target.info(ws) == "absent":
            die("no workspace '%s', so nothing is building" % ws)
        t = st.recs.find("yocto", ws)
        if t is not None and t.alive(None) and running_stage(t) != stage:
            log("no '%s' build is running in '%s'; its '%s' stage is.\n  Stop that one:  %s" % (stage, ws, running_stage(t), t.field("kill")))
            return 0
        if t is None or not t.alive(None):
            log("no '%s' build is running in '%s'" % (stage, ws))
            return 0 if self.stop_cooker(target, ws) else 1
        st.stop()
        if not self.stop_cooker(target, ws):
            return 1
        info("stopped. sstate is written as it goes, so restarting resumes rather than\n  starting over -- only the task it was in is redone.")
        return 0

    def parse(self, rest):
        o = task.options(rest, ("--dry-run", "--stop", "--detach", "--keep-work", "--chromium", "--local-layer",
                                "--no-local-layer", "--tailnet", "--no-tailnet"),
                         ("--workspace", "--stage", "--commit", "--slot", "--config", "--pgo-profile"), USAGE % (self.name, self.name))
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
            if not re.match(r"^[0-9a-f]{40}$", commit):
                die("--commit takes a full sha (40 hex digits), got '%s'" % commit)
        config = o.get("--config") or "wpe-cross"
        if stage != "webkit" and (config != "wpe-cross" or o.get("--pgo-profile")):
            die("--config and --pgo-profile belong to the webkit stage; '%s' builds no WebKit" % stage)
        cc, cxx, cmake, _ = cross_config(config, o.get("--pgo-profile") or "")
        local, tail = last_of(rest, "--local-layer", "--no-local-layer"), last_of(rest, "--tailnet", "--no-tailnet")
        return dict(o, stage=stage, commit=commit, slot=slot, config=config, cc=cc, cxx=cxx, cmake=cmake,
                    chromium=bool(o.get("--chromium")) or self.p["YOC_CHROMIUM"] != "0",
                    rm_work=self.p["YOC_RM_WORK"] == "1" and not o.get("--keep-work"),
                    local=(self.p["YOC_LOCAL_LAYER"] != "0") if local is None else local,
                    tailnet=True if tail is None else tail)

    def sizes(self):
        env = dict(self.env, WK_MB_PER_JOB=str(WEBKIT_MB_PER_JOB))
        res, budget = Resources(self.here, env), Budget(self.here, env, self.clock)
        running = budget.running(build.holder_alive(self.reg))
        return res.envelope_cores(), res.envelope_mem_mb(), build_jobs(res, budget, running), budget, running

    def argv(self, target, ws, o, cores, stage_mb, webkit_jobs, tag):
        p, q = self.p, o

        def opt(flag, value):
            return [flag, value] if value else []

        return (["python3", target.tools(ws) + "/lib/wk/sysimage/yocto_target.py", "--target", p["YOC_TARGET"],
                 "--image", p["YOC_IMAGE"], "--stage", q["stage"], "--jobs", str(cores), "--mem-budget", str(stage_mb),
                 "--rm-work", "1" if q["rm_work"] else "0"]
                + opt("--port-target-from", p["YOC_PORT_TARGET_FROM"]) + opt("--port-machine", p["YOC_MACHINE"])
                + opt("--board", p["IMG_MACHINE"]) + opt("--multilib", p["YOC_MULTILIB"])
                + opt("--multilib-tune", p["YOC_MULTILIB_TUNE"])
                + ["--chromium", "1" if q["chromium"] else "0", "--cross-config", q["config"]]
                + opt("--cross-cc", q["cc"]) + opt("--cross-cxx", q["cxx"]) + (["--cross-cmake=" + q["cmake"]] if q["cmake"] else [])
                + (["--pgo-dir", images.pgo_dir_in(q["slot"]), "--pgo-lib", pgo.GLIB_LIB] if q["stage"] == "pgo-mix" else [])
                + ["--local-layer", "1" if q["local"] else "0", "--tailnet", "1" if q["tailnet"] else "0",
                   "--webkit-jobs", str(webkit_jobs), "--sstate-ns", re.sub(r"[:/]", "-", tag.rsplit("/", 1)[-1])]
                + opt("--commit", q["commit"])
                + (["--slot", q["slot"], "--profile", self.name] if q["slot"] and q["stage"] == "webkit" else []))

    def webkit(self, rest):
        return self.build(["--stage", "webkit"] + list(rest))

    def build(self, rest):
        o = self.parse(rest)
        target = self.target()
        ws = o.get("--workspace") or images.image_ws(self.name, self.env)
        st = self.stage(target, ws, o["stage"])
        if act.dry_run():
            return self.report(target, ws, o)
        if o.get("--stop"):
            return self.stop(target, st, ws, o["stage"])
        self.refuse_running(st, ws)
        if o.get("--detach"):
            return st.detach([os.path.join(self.root, "wk"), "sysimage", "build", self.spec] + [a for a in rest if a != "--detach"],
                             "'%s' stage of %s" % (o["stage"], self.name))
        base, tag = self.host_image()
        cores, mem, webkit_jobs, budget, running = self.sizes()
        jobs, stage_mb = stage_budget(o["stage"], cores, mem, webkit_jobs)
        what = {"pgo-mix": "mixing this collection", "webkit": "this WebKit cross build"}.get(o["stage"], "this image build")
        lock = st.admit(budget, running, jobs, disk_need(o["stage"], o["chromium"], o["rm_work"], self.env), what)
        try:
            t = st.begin(list(STAGES))
            t.set("subject", images.build_subject(ws, o["stage"], o["slot"], o["commit"], o["config"] if o["stage"] == "webkit" else ""))
            try:
                self.ensure_ws(target, ws, base, tag)
                self.check_target(target, ws)
            except act.Refused as e:
                t.end(e.status)
                target.task_put(ws, t)
                raise
            t.step_state(stage_index(o["stage"]), "running")   # this stage, and no claim about the ones before it
            target.task_put(ws, t)
            info("stage '%s' for %s in '%s'" % (o["stage"], self.name, ws))
            st.run(t, budget, jobs, self.argv(target, ws, o, cores, stage_mb, webkit_jobs, tag), PATTERN, stage_mb)
        finally:
            lock.release_all()
        return self.done(target, ws, o)

    def done(self, target, ws, o):
        info("stage '%s' ok" % o["stage"])
        if o["stage"] == "pgo-mix":
            info("the collection is mixed; the measured build reads it as\n    %s/output/%s.profdata"
                 % (images.pgo_dir_in(o["slot"]), pgo.GLIB_LIB))
            return 0
        if o["stage"] != "image":
            info("stage '%s' builds no disk image, so there is nothing more to report" % o["stage"])
            return 0
        wic = os.path.join(host_workdir(target.store.ws_dir(ws), self.p["YOC_TARGET"]), "build", "image", self.p["YOC_IMAGE"] + ".wic.xz")
        info("built %s-%s  (%s)" % (self.name, self.clock.stamp(), self.du(wic)))
        log("  %s" % wic)
        log("  next:  wk sysimage write --from <path above> --disk <machine>:<device>")
        log("         ('wk sysimage ls' lists it with the exact path)")
        log("  the image carries no WebKit -- it is the runtime. The matching")
        log("  build is:  wk sysimage build %s --stage webkit" % self.spec)
        return 0

    def du(self, path):
        words = self.here.run(["du", "-sh", path]).out.split()
        return words[0] if words else "not created yet"

    def report(self, target, ws, o):
        p, stage = self.p, o["stage"]
        if stage == "pgo-mix":
            log("would mix the collection for slot '%s' of %s" % (o["slot"], self.name))
            log("  collection  %s" % images.pgo_dir(ws, o["slot"], self.env))
            log("              %s as the builder sees it -- one directory, two sides of the bind mount" % images.pgo_dir_in(o["slot"]))
            log("  benchmarks  %s, at WebKit's own weights (Tools/Scripts/pgo-profile)" % " ".join(pgo.BENCHMARKS))
            log("  into        %s/output/%s.profdata" % (images.pgo_dir_in(o["slot"]), pgo.GLIB_LIB))
            log("  where       inside %s's cross toolchain -- the clang that wrote the profiles is the only one that reads them" % ws)
            log("dry run -- nothing was mixed.")
            return 0
        at = "not created" if target.info(ws) == "absent" else self.ws_head(target, ws)
        cores, mem, webkit_jobs, budget, _ = self.sizes()
        cache = os.path.join(self.store.root(), "cache", "yocto")
        wifi = wants_wifi(fleet.Fleet(images.root(self.env), self.env), p["IMG_MACHINE"])
        free = budget.free_gb(self.env.get("WK_STORE") or self.store.root())
        log("would build image %s (builder: yocto)" % self.name)
        log("  for machine %s (%s)" % (p["IMG_MACHINE"], p["IMG_ARCH"]))
        log("  branch      %s  (from the '%s' remote)" % (p["YOC_BRANCH"], p["YOC_REMOTE"] or "origin"))
        log("  cross-target %s%s" % (p["YOC_TARGET"], self.target_note(target, ws)))
        log("  recipe      %s" % p["YOC_IMAGE"])
        log("  stage       %s (it includes the ones before it)" % stage)
        log("  workspace   %s (%s)" % (ws, at))
        log("  jobs        %d cores, %d MB envelope" % (cores, mem))
        log("  DL_DIR      %s (%s)" % (os.path.join(cache, "downloads"), self.du(os.path.join(cache, "downloads"))))
        log("  SSTATE_DIR  %s (%s)" % (os.path.join(cache, "sstate"), self.du(os.path.join(cache, "sstate"))))
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
            log("  config      %s -- %s" % (o["config"], CROSS[o["config"]]))
            if o.get("--pgo-profile"):
                log("              against %s" % o["--pgo-profile"])
        log("dry run -- nothing was built.")
        return 0

