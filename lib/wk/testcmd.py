"""`wk test`: a suite run under a watchdog and a record, or one layout test in front of lldb."""
import os
import re
import shlex
import sys

from wk import images, job, places, presets, record, session, store
from wk.act import Refused, die, dry_run, info, log, warn
from wk.build import size_for
from wk.clock import Clock
from wk.ldpath import LLDB_PIN_OPTS, lldb_waiting

ROOT = images.root()
# The suite runner, in the announcing shell's own -lc text.
PID_MATCH = "*Tools/Scripts/run-*"
_SUMMARY = re.compile(r"^(Ran |Test suite|[0-9]+ test|All [0-9]+ test)", re.I | re.M)
_FAILURE = re.compile(r"FAIL|crash|timed out", re.I)


def missing_layout_paths(runner, src, paths):
    """The `paths` (a `?query` stripped) under neither LayoutTests/ nor the checkout, asked in one round trip."""
    if not paths:
        return []
    q = shlex.quote(src)
    r = runner(["sh", "-c", "for t in " + shlex.join(paths) + "; do p=${t%%\\?*}; [ -e " + q + '/LayoutTests/"$p" ] || [ -e '
                + q + '/"$p" ] || printf "%s\\n" "$t"; done'])
    return [l for l in r.out.replace("\r", "").splitlines() if l] if r.ok else []


def size_and_admit(reg, driver, name, preset, recs):
    budget, _, jobs, mbpj, _ = size_for(reg, driver, name, preset, recs)
    admission = reg.store.admission_dir()
    budget.disk_admit("this test run", preset.disk_gb, budget.free_gb(admission), "%s's filesystem" % admission)
    return budget, jobs, mbpj


def jsc_filter(paths):
    names = [re.escape(p[len("JSTests/"):] if p.startswith("JSTests/") else p) for p in paths]
    return ["--filter", "^(%s)(\\..*)?$" % "|".join(names)] if names else []


def build_cmd(suite, src, out_env, render_env, wrap, lower, port, jobs, debug_flags, pass_args):
    if suite == "jsc":
        envs, runner, flags = (out_env, wrap), "run-javascriptcore-tests", "--no-fail-fast --memory-limited"
    else:
        envs, runner, flags = (out_env, render_env, wrap), "run-webkit-tests", "--no-retry-failures --child-processes=%d %s" % (jobs, debug_flags)
    head = " ".join(x for x in envs + ("nice -n 10 Tools/Scripts/" + runner,) if x)
    return ("cd %s && %s --%s %s --no-build %s %s" % (src, head, lower, port, flags, shlex.join(pass_args))).rstrip()


def main(name, opts, reg=None, clock=None):
    clock = clock or Clock()
    reg = reg or places.Registry(ROOT)
    driver = reg.present(name)
    recs = job.records_of(driver, clock, reg.machine)

    kill_cmd = "wk test%s --kill" % ("" if reg.in_workspace() else " " + name)
    if opts["kill"]:
        if job.stop(driver, recs, name, "test", reg.machine, clock, reg.env) == 1:
            die("the test run in '%s' outlived a TERM and a KILL.\n    Look at it:  wk enter %s  and then  pgrep -af run-" % (name, name))
        return 0

    preset_name = store.build_preset() or presets.default_preset(reg, name)
    preset = presets.resolve_or_die(preset_name, driver)

    # Asked of the preset, not of preset.port: on macOS a jsc-* preset is an Apple-port build with no port flag.
    if opts["suite"] == "layout" and preset.jsc_only:
        die("'%s' builds JavaScriptCore alone, which has no layout tests. Name a full-port preset\n"
            "    that has been built in '%s':\n        wk test %s --layout --preset gtk-release-asan\n"
            "    ('wk status %s' says what it was last built with; 'wk build --list' has the presets.)"
            % (preset_name, name, name, name))
    src = driver.src(name)

    # The same personality wrapper the build ran under: without it `uname -m` in an armhf container answers aarch64.
    arch = driver.arch(name)
    wrap = presets.ARCH.get(arch, {}).get("wrapper", "")
    budget, jobs, mbpj = size_and_admit(reg, driver, name, preset, recs)

    render_env, render_desc = "", "GPU"
    if preset.buildsys == "xcode":
        render_desc = "the guest's GPU"
        if opts["gpu"]:
            warn("--gpu has no meaning for an Apple port; ignoring")
    elif opts["gpu"]:
        info("using the real GPU (requires /dev/dri in the workspace)")
        session.here(ROOT, reg.env).mode_warn()
    else:
        render_env, render_desc = "LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe WEBKIT_DISABLE_DMABUF_RENDERER=1", "software rendering"

    if opts["suite"] == "layout" and opts["pass"] and not dry_run():
        missing = missing_layout_paths(lambda argv: driver.exec(name, argv), src, opts["pass"])
        if missing:
            die("no such test in '%s':\n%s\n"
                "    run-webkit-tests runs nothing and exits 0 for a path it cannot find. Paths are relative\n"
                "    to LayoutTests/ (or to the checkout): 'wk enter %s', then look under %s/LayoutTests."
                % (name, "\n".join("      " + m for m in missing), name, src))

    attach = debug_flags = ""
    if opts["lldb"]:
        if opts["suite"] != "layout":
            die("--lldb debugs a layout test; for jsc use 'wk run --lldb'")
        attach = preset.web_process_name() if opts["lldb"] == "web" else preset.test_runner_name()
        if not attach:
            die("'%s' builds no layout-test binaries to debug -- use a wpe-*, gtk-* or mac-* preset" % preset_name)
        if not opts["pass"]:
            die("--lldb needs a test to debug, e.g. fast/dom/Comment")
        jobs = 1   # one driver, so one process to catch; at a breakpoint it is silent, so no watchdog and no record
        debug_flags = "--no-timeout" + (" --additional-env-var=%s" % preset.web_process_pause_env() if opts["lldb"] == "web" else "")

    out_env = "WEBKIT_OUTPUTDIR=" + preset.output_dir(src) if preset.output_dir(src) else ""

    info("JSC tests (%s, -j%d)" % (preset_name, jobs) if opts["suite"] == "jsc"
         else "layout tests (%s, %s, %d processes)" % (preset_name, render_desc, jobs))
    passed = opts["pass"] if opts["suite"] == "layout" else jsc_filter(opts["paths"]) + opts["tail"]
    cmd = build_cmd(opts["suite"], src, out_env, render_env, wrap, preset.type.lower(), preset.port, jobs, debug_flags, passed)

    if dry_run():
        log("dry run -- nothing was run.")
        log("  workspace: %s (%s, %s)" % (name, driver.name, ", ".join(filter(None, (driver.state(name), presets.arch_label(arch))))))
        log("  suite:     %s (%s)" % (opts["suite"], preset_name))
        log("  would run: bash -lc %s" % shlex.quote(cmd))
        if opts["lldb"]:
            log("  in front of: lldb %s -o 'process attach --name %s --waitfor'" % (LLDB_PIN_OPTS, attach))
        return 0

    if not driver.sync_tools(name):
        die("pushing wk-tools into '%s' failed -- the reason is above" % name)

    if opts["lldb"]:
        info("debugging %s (%s) in '%s', lldb waiting for %s" % (" ".join(opts["pass"]), preset_name, name, attach))
        log("  it attaches as that process starts; 'continue' runs the test")
        log("  test output: /tmp/wk-test-lldb.log (in the workspace)")
        driver.exec_tty(name, ["bash", "-lc", lldb_waiting(cmd, "/tmp/wk-test-lldb.log", attach)])
        return 0

    log_path = os.path.join(driver.store.ws_dir(name), "test.log")
    start = clock.now()
    task = recs.claim("test", name, kill_cmd, log_path, ["%s/%s in %s" % (opts["suite"], preset_name, name)], [budget.resource()],
                      lambda _, held: budget.admit("this test run", jobs, budget.rows(held)), fields={"jobs": jobs, "budget_mb": jobs * mbpj})
    task.step(1)
    log("  log: %s" % log_path)
    log("  stop: %s" % kill_cmd)
    watcher = job.PidWatch(driver, name, task, log_path, "test", PID_MATCH, job.pid_tries(reg.env))
    with job.Signals():
        try:
            watcher.start()
            run_argv, cwd = driver.exec_argv(name, ["bash", "-lc", "echo \"wk: test pid $$\" >&2\n%s" % cmd])
            rc = job.watch(run_argv, log_path, reg.machine, clock, reg.env, cwd)
        except job.Interrupted as e:
            warn("interrupted -- stopping the test run in '%s'" % name)
            if not job.kill(driver, name, task, "cancelled", reg.machine, clock, reg.env):
                warn("it is still running; stop it with:  %s" % kill_cmd)
            raise Refused(job.EXIT_OF.get(e.signum, 130))
        finally:
            watcher.stop()

    try:
        lines = record.normalised(log_path).split("\n")
    except OSError:
        lines = []
    summary = [l for l in lines if _SUMMARY.match(l)][-4:]
    mins, secs = divmod(int(clock.now() - start), 60)

    what = "%s/%s in '%s'" % (opts["suite"], preset_name, name)
    sys.stderr.write("\n")
    if rc != 0 and task.field("stopping"):
        task.end(rc)
        warn("TESTS STOPPED  %s  (by '%s' after %dm%ds)" % (what, kill_cmd, mins, secs))
        raise Refused(rc)
    task.end("stalled" if rc == 124 else rc)
    if rc == 124:
        die("TESTS STALLED  %s (killed after %dm%ds with no output)\n    log: %s" % (what, mins, secs, log_path))
    (info, warn)[rc != 0]("TESTS OK  %s  (%dm%ds)" % (what, mins, secs) if rc == 0
                          else "TESTS FAILED  %s  (exit %d after %dm%ds)" % (what, rc, mins, secs))
    for l in summary:
        log("  " + l)
    if rc == 0:
        return 0
    log("failures:")
    for l in [l for l in lines if _FAILURE.search(l)][:10]:
        log("  " + l)
    log("  full log: %s" % log_path)
    raise Refused(rc)
