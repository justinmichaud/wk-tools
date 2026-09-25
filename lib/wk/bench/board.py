"""A board as a bench system. `wk bench deploy` lands a lane's slot on its bench system, verified against the manifest
read back off it; `wk bench run <ws> <plan> --system <board>` measures one slot there: run-benchmark runs here and drives
the board's browser over ssh (lib/wk/bench/board_driver.py). The clock pin, the claim and the session are taken once per
boot of a system, and every leg re-reads what it runs on."""

import json
import os
import shlex
import subprocess

from wk import act, fleet, images, job, pgo, record as progress, slot as wkslot
from wk.act import die, info, log, warn
from wk.bench import pipeline, record, seed
from wk.bench.systems import System, first_line
from wk.boot import cli as bootcli
from wk.boot.driver import Onboard
from wk.kv import kv
from wk.machine import Result
from wk.quiet import lib_argv

SLOTS_DIR = "/var/wk/slots"
CACHE_DIR = "/tmp/wk-webkit-cache"
BROWSER_LOG = "/tmp/wk-browser.log"
PROF_REMOTE = "/tmp/wk-prof"
RUNNER_REF = "refs/heads/main"
DRIVER = "lib/wk/bench/board_driver.py"
DRIVERS = "Tools/Scripts/webkitpy/benchmark_runner/browser_driver"
INSTRUMENTED = pgo.COLLECT
PROCESSES = {"cog": "cog", "minibrowser": "MiniBrowser"}
TUNNEL_SETTLE = 2
COMPOSITOR_TRIES, COMPOSITOR_POLL = 20, 2
# rdk's cmake claims libWPEBackend-default.so, so the default backend would be its stub. POSIX sh: busybox ash runs it.
WAYLAND = ('eval "$(strings /proc/$(pidof weston-desktop-shell)/environ | grep -E "^(XDG_RUNTIME_DIR|WAYLAND_DISPLAY)=")" && '
           'export XDG_RUNTIME_DIR WAYLAND_DISPLAY && export WPE_BACKEND_LIBRARY=libWPEBackend-fdo-1.0.so && ')
# One line per optimizing compile, dumped from the compiler thread, which SIGSEGVs the JIT worker on some builds.
JIT_TIERS = ("JSC_reportDFGCompileTimes=1", "JSC_reportFTLCompileTimes=1")
# (EI_CLASS, e_machine) of the measured library: a lib32 image reports aarch64 from `uname -m`.
ELF = {(1, 40): "armv7l", (2, 183): "aarch64", (2, 62): "x86_64", (1, 3): "i686"}
FREE_PORT = 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'
REFUSED_OPTIONS = ("config", "browser", "browser_args", "software")


class Script(Onboard):
    """A bench/onboard/ file: the board's side of a run, read verbatim as a boot driver's boot/onboard/ files are."""

    def path(self):
        return os.path.join(self.root, "bench", "onboard", self.name)


def slot_path(name):
    return os.path.join(SLOTS_DIR, name)


def kill_cmd(browser):
    names = "%s WPEWebProcess WPENetworkProcess" % PROCESSES[browser]
    return "killall %s 2>/dev/null; sleep 1; killall -9 %s 2>/dev/null; true" % (names, names)


def elf_arch(od):
    h = [int(x) for x in od.split() if x.isdigit()]
    return ELF.get((h[4], h[18] | h[19] << 8), "") if len(h) >= 20 else ""


def runner_tree(reg, here, root):
    """(tree, sha): Tools/Scripts exported from the mirror at one commit, the runner_sha both arms of an A/B share, with the
    board driver beside the others. The export is an artifact keyed by that commit; the driver is copied in every run."""
    mirror = reg.store.mirror()
    if not here.isdir(mirror):
        die("no mirror at %s; 'wk sync' makes one. The runner tree is exported from it." % mirror)
    ref = reg.env.get("WK_BENCH_RUNNER_REF") or RUNNER_REF
    r = here.run(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    sha = r.out.strip()
    if not r.ok or not sha:
        die("the mirror has no '%s' to export a runner from ('wk sync' fetches main)" % ref)
    tree = os.path.join(reg.store.artifact_dir(), "bench-runner", sha[:12])
    if not here.exists(os.path.join(tree, "Tools", "Scripts", "run-benchmark")):
        info("exporting run-benchmark from the mirror at %s (Tools/Scripts only)" % sha[:12])
        tmp = tree + ".tmp"   # renamed into place, so a kill mid-export leaves nothing the next run reads
        here.remove(tmp)
        here.mkdir(tmp)
        if not here.act_run(["sh", "-c", 'git -C "$1" archive "$2" Tools/Scripts | tar -x -C "$3"', "sh", mirror, sha, tmp]).ok:
            die("could not export Tools/Scripts at %s from %s" % (sha, mirror))
        here.remove(tree)
        if not here.act_run(["mv", "-f", tmp, tree]).ok:
            die("could not move the exported runner into place at %s" % tree)
    here.copy_in(os.path.join(root, DRIVER), os.path.join(tree, DRIVERS, "wk_board_driver.py"))
    return tree, sha


class BoardSystem(System):
    kind = "board"
    bench_host = "image"
    host_os = "linux"

    def __init__(self, root, reg, target, ws, clock, board, driver, machine=None):
        super().__init__(root, reg, target, ws, clock)
        self.board, self.driver, self.machine = board, driver, machine
        self.pending = None   # the leg BoardRun.leg() is about to run: System.boot() takes none
        self.sysid = self.display = self.session = self.renderer = self.runner_dir = self.runner_sha = self.payload = self.plan_text = ""
        self.doc, self.facts_, self.clk, self.probed = {}, {}, {}, {}
        self.prepared_boot = self.session_boot = None
        self.profiler = self.paranoid = self.capture = ""

    def bench(self):
        if self.machine is None:
            m = self.driver.ch.machine("i_ssh")
            if isinstance(m, Result):
                die("cannot reach %s's bench system over ssh: %s" % (self.board, m.err.strip()))
            self.machine = m
        return self.machine

    def sh(self, script, mutates=False):
        argv = ["sh", "-c", script.text()]
        return self.bench().act_run(argv) if mutates else self.bench().run(argv)

    def ob(self, name, **params):
        return Script(self.root, name, **params)

    def barrier(self, what):
        """A `wk boot` arming not yet spent means the system answering ssh is not the one about to run."""
        self.driver.armed_barrier(what)

    def deploy_slot(self, name):
        images.check_slot_name(name)
        System.boot(self)
        d = images.slot_dir(self.ws, name, self.reg.env)
        if d is None:
            die("'%s' is not an image workspace ('wk sysimage ls' names them)" % self.ws)
        doc_path = os.path.join(d, "slot.json")
        if not os.path.isfile(doc_path):
            die("'%s' has no slot '%s' built.\n    Build one first:\n"
                "        wk sysimage webkit <profile> --workspace %s --commit <sha> --slot %s" % (self.ws, name, self.ws, name))
        with open(doc_path) as f:
            doc = json.load(f)
        self.barrier("Landing slot '%s' now would put it on the system that boot is about to leave." % name)
        m, dest, part = self.bench(), slot_path(name), slot_path(name) + ".part"
        info("deploying slot '%s' to %s (%s, WebKit %s)" % (name, self.board, doc.get("browser", "?"), doc.get("commit", "")[:12]))
        m.remove(part)
        m.mkdir(part)
        m.copy_tree_in(os.path.join(d, "root"), os.path.join(part, "root"))
        m.copy_in(doc_path, os.path.join(part, "slot.json"))
        if not act.dry_run():
            self.verify(part)
        m.remove(dest)
        if not m.act_run(["mv", "-f", part, dest]).ok:
            die("could not move %s into place on %s" % (part, self.board))
        if act.dry_run():
            info("dry run -- %s would land at %s" % (name, dest))
            return
        log("deployed to %s" % self.board)
        log("  slot      %s (%s)" % (name, dest))
        log("  webkit    %s" % doc.get("commit", ""))
        log("  build-id  %s  (what a run reads back out of the running process)" % doc.get("build_id", ""))
        log("  next:     wk bench run %s <plan> --system %s --slot %s" % (self.ws, self.board, name))

    def verify(self, part):
        try:
            remote = json.loads(self.bench().read(os.path.join(part, "slot.json")))
        except (OSError, ValueError):
            die("could not read the manifest back from %s -- %s is left there for inspection" % (self.board, part))
        sums = "".join("%s  %s\n" % (sha, os.path.join(part, "root", rel)) for rel, sha in sorted(remote.get("files", {}).items()))
        r = self.bench().act_run(["sh", "-c", "sha256sum -c -"], input=sums)
        if not r.ok:
            bad = "\n".join("    " + l for l in (r.out + r.err).splitlines() if l.strip() and not l.rstrip().endswith(": OK"))
            die("the copy on %s does not match the slot's manifest:\n%s\n"
                "    Nothing was replaced; %s is left there for inspection." % (self.board, bad[:2000], part))

    def boot(self):
        leg, name = self.pending, self.board
        self.driver.probe()
        self.barrier("A run now would measure the system that boot is about to leave.")
        mode = self.driver.mode
        if mode == "unreachable":
            die("cannot reach %s in either mode over ssh.\n    'wk boot %s --status' says what it is doing." % (name, name))
        if mode == "host":
            die("%s is not running a wk bench system (it answered in host mode).\n    A benchmark runs in bench mode or it does not run:\n"
                "        wk boot %s --system <id>   then   wk boot %s --keep" % (name, name, name))
        kind, _, self.sysid = mode.partition(" ")
        if kind == "base":
            act.barrier("%s is running its rescue system (%s), not a bench system. The rescue exists to keep\n    the board reachable "
                        "and to let another system be written; it is never the thing to measure.\n    Arm a bench system and boot it:  "
                        "wk boot %s --system <id>" % (name, self.sysid, name))
        want = leg.o.get("expect") or ""
        if want and self.sysid != want:
            die("%s answered as system '%s', not the '%s' this leg is for.\n    Nothing was measured; 'wk boot %s --status' says what "
                "it is running." % (name, self.sysid, want, name))
        self.doc = self.manifest(leg.slot)
        if self.doc.get("browser") not in PROCESSES:
            die("slot '%s' names browser '%s', which a board run cannot launch (%s)" % (leg.slot, self.doc.get("browser"), ", ".join(PROCESSES)))
        self.instrumented(leg)
        self.probed = kv_all(self.sh(self.driver.ob("probe.sh")).out)
        self.facts_ = kv_all(self.sh(self.ob("facts.sh")).out)
        if leg.cores and self.facts_.get("taskset") != "yes":
            die("--cores: the image on %s has no taskset, so the browser cannot be pinned there. That is the image's\n"
                "    business (util-linux's schedutils in the defconfig), not something to approximate here." % name)
        self.display = first_line(self.sh(self.ob("display.sh")))
        boot = (self.sysid, first_line(self.sh(self.driver.ob("boot-id.sh"))))
        if boot != self.prepared_boot:
            self.clk = kv_all(self.sh(self.ob("pin-clock.sh"), mutates=True).out)
            if not self.sh(self.driver.ob("keep.sh"), mutates=True).ok:
                die("could not claim %s's bench system (/run/wk-keep-running).\n    Without the claim, the image's self-return watchdog "
                    "reboots the board mid-run." % name)
            self.prepared_boot = boot
        log("%s: %s (builder %s)" % (name, self.sysid, self.probed.get("builder", "unknown")))
        for k, v in (("kernel", self.facts_.get("kernel")), ("root", self.probed.get("rootdev")), ("display", self.display or "none"),
                     ("clock", "%s kHz (max %s)" % (self.clk.get("min") or "unknown", self.clk.get("max") or "?")),
                     ("throttled", self.throttled() or "unknown")):
            log("  %-11s %s" % (k, v))

    def manifest(self, name):
        try:
            return json.loads(self.bench().read(slot_path(name) + "/slot.json"))
        except (OSError, ValueError):
            die("%s has no slot '%s' (%s).\n    The image is the runtime and a slot is what is measured; deploy one:\n"
                "        wk bench deploy <lane> %s --slot %s" % (self.board, name, slot_path(name), self.board, name))

    def instrumented(self, leg):
        """An instrumented build writes a profile as each process exits and is several times slower for it: collected from, never measured."""
        was = self.doc.get("build_config", "")
        if leg.o.get("pgo_dir") and was != INSTRUMENTED:
            die("a collection reads an instrumented build and slot '%s' is '%s', so it would write no profile at all.\n"
                "    The instrumented slot is the middle phase of 'wk sysimage webkit' and is named <slot>-instr." % (leg.slot, was or "not one"))
        if not leg.o.get("pgo_dir") and was == INSTRUMENTED:
            die("slot '%s' on %s is an instrumented build: a number taken from it is not this engine's.\n"
                "    The measured slot is the one without '-instr'." % (leg.slot, self.board))

    def throttled(self):
        t = self.facts_.get("throttled", "").strip()
        return "throttled=0x" + t if t else ""

    def build_present(self, leg):
        return True, "slot '%s' at %s (WebKit %s, build-id %s)" % (leg.slot, slot_path(leg.slot), self.doc.get("commit", "")[:12],
                                                                   self.doc.get("build_id", "")[:12])

    def checks(self, leg):
        lo, hi = self.clk.get("min", ""), self.clk.get("max", "")
        rows = [(True, "display attached", self.display) if self.display else
                (False, "display attached", "no connected DRM connector and no HDMI -- a software path, indistinguishable later"),
                (True, "clock pinned", "pinned as the run starts; a dry run pins nothing") if act.dry_run() else
                (True, "clock pinned", "%s kHz (min=max)" % lo) if lo and lo == hi else
                (False, "clock pinned", "min %s, max %s -- DVFS moves the clock under the measurement" % (lo or "unknown", hi or "unknown"))]
        notes = [] if self.clk.get("governor") == "performance" else ["governor %s" % (self.clk.get("governor") or "unknown")]
        if self.throttled() not in ("", "throttled=0x0"):
            notes.append(self.throttled())
        return rows, notes

    def deploy(self, leg):
        """The slot is on the board already; this is the session it runs in, once per boot, and the warmup leg's profiler."""
        if self.session_boot == self.prepared_boot:
            log("  session     %s, up since this boot's first leg" % self.session)
        else:
            self.session_up()
            self.session_boot = self.prepared_boot
        if leg.o.get("warmup"):
            self.profiler_stage(leg)
        if not act.dry_run():
            software = self.renderer == "pixman"
            record.write_env(os.path.join(leg.out, "env.json"), ["session_mode=" + self.session, "gpu_renderer=" + self.renderer,
                                                                 "software_reason=" + ("no display attached; weston rdp + pixman" if software else "")],
                             bool_fields=["software=" + ("1" if software else "")], update=True)

    def session_up(self):
        if not self.sh(self.ob("browsers-dead.sh"), mutates=True).ok:
            die("the image's own browser on %s would not die, even to SIGKILL; a survivor holds the GPU and\n"
                "    the vchiq service the run needs." % self.board)
        if self.facts_.get("weston") != "yes":
            self.session, self.renderer = "rdk", "dispmanx"
            log("  session     rdk backend, dispmanx (%s)" % (self.display or "no display evidence"))
            return
        if self.display:
            self.session, self.renderer = "drm", "gl"
            backend = "systemd" if self.facts_.get("systemd") == "yes" else "drm"
        elif self.facts_.get("systemd") != "yes":
            die("%s reports no attached display, and this image's weston has no RDP virtual head to synthesise one.\n"
                "    Attach a panel." % self.board)
        else:
            self.session, self.renderer, backend = "headless-rdp", "pixman", "rdp"
            info("  no display attached: RDP virtual head + pixman (software) compositing")
        if self.sh(self.ob("weston.sh", WK_BACKEND=backend), mutates=True).rc == 3:
            die("could not create the RDP backend's keys on %s (needs openssl)" % self.board)
        self.wait_compositor()
        if self.sh(self.ob("seat.sh"), mutates=True).ok:
            log("  input seat  up")
        else:
            warn("no fake input seat -- cog asserts at startup without one; the run may abort")

    def wait_compositor(self):
        if act.dry_run():
            log("  would wait up to %ds for the compositor's output" % (COMPOSITOR_TRIES * COMPOSITOR_POLL))
            return
        out = ""
        for _ in range(COMPOSITOR_TRIES):
            out = first_line(self.sh(self.ob("compositor.sh")))
            if out:
                break
            self.clock.sleep(COMPOSITOR_POLL)
        if not out:
            die("no compositor output on %s.\n    Check:  tail -30 /tmp/wk-weston.log on its bench system" % self.board)
        log("  session     %s, %s renderer (%s)" % (self.session, self.renderer, out))
        if self.renderer == "pixman":
            warn("compositing is on the CPU, so this number is not comparable with a run on real display\n  hardware. It proves the path.")

    def profiler_stage(self, leg):
        """A reboot empties /tmp, so every warmup leg stages its profiler again."""
        self.profiler = self.paranoid = self.capture = ""
        if leg.o.get("no_warmup_profile"):
            log("  profiler    off (--no-warmup-profile)")
            return
        m = self.bench()
        arch = elf_arch(m.run(["od", "-An", "-tu1", "-N20", slot_path(leg.slot) + "/root/" + self.doc.get("lib_file", "")]).out)
        if not arch:
            act.barrier("could not read the word size of the slot's own library on %s, so which profiler can run there is unknown." % self.board)
            return
        r = self.here.run(lib_argv(self.root, "lib/profiler.sh", "profiler_resolve", arch, "yes" if self.facts_.get("sysprof") else "no"))
        if not r.ok:
            act.barrier("%s\n    A warmup round exists to profile the arm it measures." % r.out.strip())
            return
        tool = r.out.split()[0]
        m.mkdir(PROF_REMOTE)
        if tool == "samply":
            binary = first_line(self.here.run(lib_argv(self.root, "lib/profiler.sh", "samply_fetch", arch)))
            if not binary:
                act.barrier("samply for %s could not be fetched to this host." % arch)
                return
            m.copy_in(binary, PROF_REMOTE + "/samply")
            m.act_run(["chmod", "755", PROF_REMOTE + "/samply"])
        self.paranoid = self.facts_.get("paranoid", "").strip()
        if not self.paranoid.lstrip("-").isdigit():
            act.barrier("%s's kernel has no /proc/sys/kernel/perf_event_paranoid, so it was built without perf events\n"
                        "    and neither profiler can sample on it." % self.board)
            return
        if int(self.paranoid) > 1:
            m.act_run(["sh", "-c", "echo 1 > /proc/sys/kernel/perf_event_paranoid"])
        self.profiler = tool
        self.capture = "%s/%s-%s.profile.%s" % (PROF_REMOTE, self.board, leg.o.get("arm", ""), "syscap" if tool == "sysprof" else "json")
        log("  profiler    %s (%s); perf_event_paranoid was %s" % (tool, r.out.split(None, 1)[1].strip(), self.paranoid))

    def launch(self, leg):
        root = slot_path(leg.slot) + "/root"
        env = wkslot.env(self.doc, root) + ["XDG_CACHE_HOME=" + CACHE_DIR]
        env += ["LLVM_PROFILE_FILE=" + leg.o["pgo_file"]] if leg.o.get("pgo_dir") else []
        env += list(JIT_TIERS) if leg.o.get("warmup") and leg.o.get("jit_tiers") else []
        exe = "/usr/bin/cog" if self.doc["browser"] == "cog" else root + "/bin/MiniBrowser"
        return "cd /tmp && %sexec %senv %s %s" % ("" if self.session == "rdk" else WAYLAND, "taskset -c %s " % leg.cores if leg.cores else "",
                                                 " ".join(env), exe)

    def warm_file(self, leg, what):
        return os.path.join(os.path.dirname(os.path.dirname(leg.out)), "warmup", "%s-%s.%s" % (self.board, leg.o.get("arm", ""), what))

    def board_env(self, leg):
        m = self.bench()
        return [("WK_BOARD_SSH", " ".join(shlex.quote(a) for a in ["ssh", *m.opts, m.dest])), ("WK_BOARD_LAUNCH", self.launch(leg)),
                ("WK_BOARD_KILL", kill_cmd(self.doc["browser"])), ("WK_BOARD_RESET", "rm -rf %s && mkdir -p %s" % (CACHE_DIR, CACHE_DIR)),
                ("WK_BOARD_URL", "127.0.0.1:%d" % leg.port),
                ("WK_BOARD_EXPECT", json.dumps(wkslot.expect(self.doc, slot_path(leg.slot) + "/root"))),
                ("WK_BOARD_EVIDENCE", os.path.join(leg.out, "verify.jsonl")),
                ("WK_BOARD_WARMUP", self.warm_file(leg, "evidence.json") if leg.o.get("warmup") else ""),
                ("WK_BOARD_CLASS", leg.klass), ("WK_BOARD_JIT_TIERS", "1" if leg.o.get("jit_tiers") else ""),
                ("WK_BOARD_PROFILE", "%s:%s" % (self.profiler, self.capture) if self.capture else ""),
                ("WK_BOARD_PGO", leg.o.get("pgo_board", "") if leg.o.get("pgo_dir") else "")]

    def run(self, leg, script, watched, log_path):
        m = self.bench()
        tunnel = os.path.join(leg.out, "tunnel.log")
        with m.forward(leg.port, tunnel) as pid:
            if pid:
                self.clock.sleep(TUNNEL_SETTLE)
                if not m.via.alive(pid):
                    text = open(tunnel).read() if os.path.isfile(tunnel) else ""
                    die("could not open the forward to %s (port %d):\n%s" % (self.board, leg.port, "".join("    " + l for l in text.splitlines(True))))
            log("  tunnel      127.0.0.1:%d on %s -> run-benchmark here" % (leg.port, self.board))
            return watched(["bash", "-c", script], self.runner_dir, log_path)

    def collect(self, leg):
        """run-benchmark ran here, so its result is already in the run directory; the board's side is evidence()'s."""

    def evidence(self, leg):
        """The board's side of a leg, taken whether or not it produced a result: a browser that outlives a run is a second
        browser on the GPU and in the memory the next one measures."""
        m, out, dry = self.bench(), leg.out, act.dry_run()
        result = os.path.join(out, "result.json")
        if not dry and not (os.path.isfile(result) and os.path.getsize(result)):
            write(os.path.join(out, "diagnose", "board-at-failure.txt"), self.sh(self.ob("at-failure.sh")).out)
        m.act_run(["sh", "-c", kill_cmd(self.doc["browser"])])
        try:
            browser_log = m.read(BROWSER_LOG)
        except OSError:
            browser_log = ""
        m.remove(BROWSER_LOG)
        if leg.o.get("warmup"):
            self.pull_profile(leg)
        if dry:
            return
        write(os.path.join(out, "browser.log"), browser_log)
        write(os.path.join(out, "board.log"), m.run(["tail", "-n", "300", "/tmp/messages"]).out)
        n = wkslot.verified(os.path.join(out, "verify.jsonl"))
        fields, bools = [], ["verified=" + ("1" if n else "")]
        if leg.o.get("settle") or leg.o.get("warmup"):
            bools.append("warmup=1")
            fields.append("warmup_kind=" + ("settle" if leg.o.get("settle") else "evidence"))
        if leg.o.get("warmup"):
            fields += ["profiler=" + (self.profiler or "none"), "host.perf_event_paranoid=" + self.paranoid]
        record.write_env(os.path.join(out, "env.json"), fields, bool_fields=bools, update=True)
        if n:
            log("  verified    the reporting WPEWebProcess ran slot '%s' (%d check(s), build-id %s)" % (leg.slot, n, self.doc.get("build_id", "")[:12]))

    def pull_profile(self, leg):
        if not self.capture:
            return
        local = self.warm_file(leg, "profile." + self.capture.rsplit(".", 1)[1])
        try:
            self.bench().copy_out(self.capture, local)
        except OSError:
            pass
        if act.dry_run():
            return
        if os.path.isfile(local) and os.path.getsize(local):
            log("  profile     %s (%s)" % (local, self.profiler))
            return
        if os.path.exists(local):
            os.unlink(local)
        act.barrier("the warmup leg for arm %s produced no %s capture on %s." % (leg.o.get("arm", ""), self.profiler, self.board))


def kv_all(text):
    return kv(text)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class BoardRun(pipeline.Run):
    """One leg on a board: its task and progress record live on this machine, where run-benchmark runs."""

    def __init__(self, root, reg, system, clock, env=None, popen=subprocess.Popen, name=""):
        super().__init__(root, reg, system, clock, env, popen)
        self.name = name or self.ws or system.board
        self.kill_cmd = ("wk bench run %s --kill --system %s" % (self.ws, system.board)) if self.ws else "kill %d" % os.getpid()

    def holders(self, res):
        return fleet_holders(self.root, self.env, self.recs, res)

    def records(self, clock):
        return progress.Records(self.reg.store.record_dir(), clock=clock, env=dict(self.reg.env, WK_ABORT_SECONDS=self.env["WK_ABORT_SECONDS"]),
                                machine=self.here)

    def put(self):
        """The record is already in this machine's store."""

    def stop(self):
        t = self.recs.find("bench", self.name)
        if t is None or not t.alive(None):
            log("no bench is running for '%s' -- 'wk status' says what it last did" % self.name)
            return 2
        return 0 if job.kill(None, self.name, t, "cancelled", self.here, self.clock, self.env) else 1

    def leg(self, plan, o):
        asked = [k for k in REFUSED_OPTIONS if o.get(k)]
        if asked:
            die("--%s: a board runs the slot it holds, in the browser its slot.json names; --slot picks the slot."
                % asked[0].replace("_", "-"))
        if o.get("collect"):
            o = dict(o, **self.collection(o))
        leg = pipeline.Leg(plan, o)
        leg.slot = o.get("slot") or "a"
        images.check_slot_name(leg.slot)
        if leg.cores and not pipeline.cores_valid(leg.cores):
            die("--cores '%s' is not a valid Linux cpu list (e.g. 0-3, 2,3, 0-1,4, 7)" % leg.cores)
        task = o.get("task") or ""
        if task and not o.get("pgo_dir") and not os.path.isfile(os.path.join(self.bench_dir, task, "task.json")):
            die("no such task '%s' (%s has no task.json); 'wk bench ls' lists the tasks" % (task, os.path.join(self.bench_dir, task)))
        leg.klass, leg.runner, leg.browser = pipeline.bench_class(plan), "browser", ""
        leg.port = 0
        self.system.pending = leg
        return leg

    def collection(self, o):
        """`--collect`: one iteration of an instrumented slot into the lane's build directory; lib/wk/pgo.py says where and how long."""
        if o.get("count") not in (None, "", "1"):
            die("--collect runs one iteration, as upstream collects: every further one overwrites the last one's profile")
        slot = images.measured_slot(o.get("slot") or "a")
        return {"pgo_dir": images.pgo_dir(self.ws, slot, self.reg.env), "pgo_board": pgo.BOARD_DIR, "pgo_file": pgo.BOARD_FILE,
                "count": "1", "timeout": o.get("timeout") or pgo.collect_timeout(self.reg.env)}

    def idle_rows(self):
        """This host serves pages to the board; its own load is not the measurement."""
        return []

    def pin(self, plan):
        """The runner tree and the plan's payload, once per system: every leg of an A/B runs one commit of each."""
        s = self.system
        if s.runner_dir:
            return s.plan_text
        s.runner_dir, s.runner_sha = runner_tree(self.reg, self.here, self.root)
        log("  runner      run-benchmark @ %s (%s)" % (s.runner_sha[:12], s.runner_dir))

        def read(path):
            try:
                return self.here.read(os.path.join(s.runner_dir, "Tools", "Scripts", path))
            except OSError:
                return None
        s.plan_text = seed.plan_json(read, plan)
        s.payload = seed.Seeder(self.here, self.lock, os.path.join(self.reg.store.artifact_dir(), "bench")).seed(plan, s.plan_text)
        return s.plan_text

    def seed(self, leg):
        self.pin(leg.plan)
        leg.payload = self.system.payload

    def begin(self, leg):
        s, o, stamp = self.system, leg.o, self.clock.stamp()
        leg.task, new = o.get("task") or "", False
        leg.id = "%s-%s-%s-%s%s" % (stamp, leg.plan, s.board, leg.slot, "-settle" if o.get("settle") else "")
        if o.get("pgo_dir"):
            leg.task, leg.out = "", os.path.join(o["pgo_dir"], leg.plan)   # one leg of the profile the next build reads, in no task
        else:
            if not leg.task:
                leg.task, new = "%s-%s-%s" % (stamp, s.board, leg.slot), True
            leg.rel = "%s/runs/%s" % (leg.task, leg.id)
            leg.out = os.path.join(self.bench_dir, leg.rel)
        steps = ["bring up the session on %s for slot '%s' (WebKit %s)" % (s.board, leg.slot, s.doc.get("commit", "")[:12]),
                 "run %s (browser, %s iteration(s)) on %s" % (leg.plan, leg.count or "default", s.board), "collect into %s" % leg.out]
        if act.dry_run():
            return steps
        log_path = os.path.join(leg.out, "run.log")
        # The board is a fleet resource: the record is its claim, unless the A/B driving this leg already holds it.
        self.task = (progress.hold(self.recs, self.holders, s.board, "bench", self.name, self.kill_cmd, log_path, steps, os.getpid(), self.env)
                     or self.recs.begin("bench", "here", self.name, self.kill_cmd, log_path, steps))
        if new:
            taskdir = os.path.join(self.bench_dir, leg.task)
            if os.path.exists(taskdir):
                die("task %s already exists (%s); a task is one request, made once" % (leg.task, taskdir))
            self.lock.hold("bench-task-" + leg.task, timeout=5)
            record.task_write(taskdir, ["task=" + leg.task, "requested=" + self.clock.iso(), "subject.kind=slots", "subject.spec=" + leg.slot,
                                        "devices=%s=%s" % (s.board, s.doc.get("profile", "")), "plans=" + leg.plan, "rounds=1", "slots=" + leg.slot]
                              + (["count=" + leg.count] if leg.count else []),
                              ["wk bench run %s %s --system %s --slot %s%s" % (self.ws, leg.plan, s.board, leg.slot, " --count " + leg.count if leg.count else "")])
        if o.get("pgo_dir"):
            self.here.remove(leg.out)
        os.makedirs(os.path.join(leg.out, "diagnose"), exist_ok=True)
        if o.get("warmup"):
            os.makedirs(os.path.dirname(s.warm_file(leg, "")), exist_ok=True)
        self.write_env(leg)
        return steps

    def write_env(self, leg):
        s, doc, o = self.system, self.system.doc, leg.o
        lo, hi = s.clk.get("min", ""), s.clk.get("max", "")
        ab = ["ab.round=" + o["round"], "ab.arm=" + o.get("arm", ""), "ab.slot_a=" + o.get("slot_a", ""), "ab.slot_b=" + o.get("slot_b", "")] if o.get("round") else []
        record.write_env(os.path.join(leg.out, "env.json"), [
            "plan=" + leg.plan, "workspace=" + doc.get("workspace", ""), "config=" + doc.get("profile", ""), "browser=" + doc.get("browser", ""),
            "count=" + leg.count, "class=" + leg.klass, "runner=browser", "arch=" + (s.facts_.get("arch") or "native"), "bench_host=" + s.bench_host,
            "display=" + s.display, "machine=" + s.board, "system=" + s.sysid, "build_slot=" + leg.slot,
            "build_config=" + doc.get("build_config", ""), "webkit_sha=" + doc.get("commit", ""), "build_id=" + doc.get("build_id", ""),
            "runner_sha=" + s.runner_sha, "local_copy=" + leg.payload, "host.kernel=" + s.facts_.get("kernel", ""),
            "host.kernel_arch=" + s.facts_.get("arch", ""), "host.governor=" + s.clk.get("governor", ""), "host.throttled=" + s.throttled(),
            "host.root_device=" + s.probed.get("rootdev", ""), "host.cpu_khz=" + lo, "cores.set=" + leg.cores,
            "subtests_excluded=" + o.get("excluded", ""), "task=" + leg.task, "preflight_notes=" + leg.notes] + ab
            + pipeline.configuration_fields(self.env),
            bool_fields=["forced=" + (self.env.get("WK_FORCE") or ""), "cores.pinned=" + leg.cores, "host.dvfs_pinned=" + ("1" if lo and lo == hi else "")])

    def watched(self, argv, cwd, path):
        if self.task is not None:
            self.task.set("log", path)
        return job.watch(argv, path, self.here, self.clock, self.env, cwd, self.popen)

    def run_browser(self, leg):
        s, o = self.system, leg.o
        port = first_line(self.here.run(["python3", "-c", FREE_PORT]))
        if not port.isdigit():
            die("could not find a free port on this host for run-benchmark's page server")
        leg.port = int(port)
        args = ["python3", "Tools/Scripts/run-benchmark", "--plan", leg.plan, "--browser", "wk-board", "--platform", "linux", "--driver", "webserver",
                "--http-server-type", "builtin", "--http-server-port", port, "--output-file", os.path.join(leg.out, "result.json"),
                "--no-adjust-unit", "--show-iteration-values", "--diagnose-directory", os.path.join(leg.out, "diagnose")]
        args += (["--count", leg.count] if leg.count else []) + (["--timeout", o["timeout"]] if o.get("timeout") else [])
        args += (["--local-copy", leg.payload] if leg.payload else []) + (["--subtests"] + leg.subtests.split() if leg.subtests else [])
        args += ["--generate-pgo-profiles"] if o.get("pgo_dir") else []
        script = "".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in s.board_env(leg))
        script += "cd %s && exec %s" % (shlex.quote(s.runner_dir), " ".join(shlex.quote(a) for a in args))
        info("running %s on %s from slot '%s' (WebKit %s)" % (leg.plan, s.board, leg.slot, s.doc.get("commit", "")[:12]))
        log("  results: %s" % leg.out)
        path = os.path.join(leg.out, "run.log")
        rc = s.run(leg, script, self.watched, path)
        s.evidence(leg)
        result = os.path.join(leg.out, "result.json")
        if rc == 0 and not act.dry_run() and not (os.path.isfile(result) and os.path.getsize(result)):
            return 1, "run-benchmark exited 0 from slot '%s' with no result" % leg.slot, path
        return rc, "run-benchmark exited %d from slot '%s'" % (rc, leg.slot), path


def fleet_holders(root, env, records, res):
    return progress.fleet_holders(res, records, progress.fleet_stores(root, env, records.machine))


def require_board(root, env, board):
    try:
        conf = fleet.Fleet(root, env).load(board)
    except fleet.ConfError as e:
        die(str(e))
    if not conf or conf.get("KIND") != "board":
        die("'%s' names no board in machines/ (wk boot --list)" % board)


def claim(root, env, board, what):
    """A deploy's hold on the board, a record of its own; None under --dry-run or inside a driver that holds it."""
    records = progress.Records(env=env)
    return progress.hold(records, lambda res: fleet_holders(root, env, records, res), board, "bench", what,
                         "kill %d" % os.getpid(), "", [what], os.getpid(), env)


def for_board(root, reg, ws, clock, board, machine=None, target=None, driver=None):
    require_board(root, reg.env, board)
    if driver is None:
        bconf = bootcli.load_conf(root, board, reg.env)
        if bconf is None:
            die("machines/%s.conf declares no NODE_DRIVER and NODE_NOTE, so nothing can tell what %s is running\n"
                "    or whether a `wk boot` arming is about to reboot it." % (board, board))
        driver = bootcli.driver_for(root, bconf)
    if target is None and ws:
        try:
            target = reg.load(reg.ws_target(ws))
        except LookupError as e:
            die(str(e))
    return BoardSystem(root, reg, target, ws, clock, board, driver, machine)


def request(root, reg, verb, words, typed):
    """A deploy or a board run typed in a workspace: one broker request (container/broker/wk-broker.py), which runs `typed` on the workstation."""
    return bootcli.broker_request(root, verb, [w for w in words if not w.endswith("=")], reg.env, reg.machine, typed)

