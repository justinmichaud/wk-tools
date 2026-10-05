"""`wk run|test <ws> --profile[=<mode>]`: the jsc shell, MiniBrowser (`--browser`) or a running process (`--attach`)
under one profiler, its env-var wall written down once; `--fetch` copies the artifacts out through pull_dir."""

import os
import shlex
import time

from wk import buildconf, images, store, targets
from wk.act import Refused, die, dry_run, info, log, warn
from wk.ldpath import perf_events, prelude, require_tool
from wk.store import Store
from wk.workspace import require_name

MODES = ("sampling", "bytecode", "samply", "sysprof", "instruments", "heaptrack", "massif")
ONLY_WITH_PROFILE = ("--browser", "--process", "--attach", "--jit-dump", "--markers", "--top", "--fetch", "--env")


def asked(args):
    return args.flag("--profile") or args.value("--profile") is not None


def refuse_mixed(args, own):
    """A profiler flag without --profile, or one of the command's own `own` flags with it, is refused naming which."""
    if not asked(args):
        stray = [o for o in args.order if o in ONLY_WITH_PROFILE]
        if stray:
            die("%s is --profile's: add --profile[=<mode>]" % stray[0])
        return
    mixed = [o for o in args.order if o in own]
    if mixed:
        die("--profile runs on its own; drop %s" % mixed[0])


def options(args):
    o = {"mode": args.value("--profile") or "sampling", "process": "web", "extra_env": args.values("--env")}
    if args.value("--process") is not None:
        o["process"] = args.value("--process")
        o["process_explicit"] = True
    if args.value("--attach") is not None:
        o["attach"] = args.value("--attach")
    if args.value("--top") is not None:
        o["top"] = args.value("--top")
    if args.flag("--jit-dump"):
        o["jitdump"] = True
    if args.flag("--markers"):
        o["markers"] = True

    loose = list(args.positionals)
    given = {"--browser": args.flag("--browser"), "--fetch": args.flag("--fetch")}
    for opt in args.order:
        if not given.get(opt):
            continue
        if opt == "--browser" and "browser" not in o:
            o["browser"] = True
            if loose:
                o["url"] = loose.pop(0)
        elif opt == "--fetch" and "fetch" not in o:
            o["fetch"] = True
            if loose:
                o["fetch_dir"] = loose.pop(0)
    o["args"] = loose + args.tail
    return o


def browser_target(cfg, url, process, process_explicit, mode, args, outdir):
    """(subject, target_cmd, mb_prefix) for `--browser`: the Apple ports profile MiniBrowser
    directly, the CMake ports profile it through `run-minibrowser`, which is not itself the subject."""
    if cfg.xcode():
        if process_explicit:
            die("--process is not wired up for the Apple ports: MiniBrowser is profiled\n"
                "    directly here, not through a process picker. '--attach\n"
                "    %s' reaches the web process once it is up." % cfg.web_process_name())
        subject = "MiniBrowser %s" % url
        cmd = shlex.quote(cfg.browser_path("$SRC")) + " " + cfg.browser_url_flag() + " " + shlex.quote(url)
        if args:
            cmd += " " + shlex.join(args)
        return subject, cmd, False

    if mode in ("heaptrack", "massif"):
        die("--browser --profile=%s is not wired up on the CMake ports: %s has to\n"
            "    be there from the process's first allocation, and its own launch line is a\n"
            "    shell fragment ('cd <dir> && %s'), not the plain command\n"
            "    WEBKIT_MINI_BROWSER_PREFIX needs. Owed --\n"
            "    docs/Urgent/HANDOFF-linux-minibrower.md. Use --profile=samply against the\n"
            "    browser, or profile the jsc shell instead." % (mode, mode, mode))
    if process_explicit and mode in ("sampling", "bytecode", "sysprof"):
        die("--process is meaningless with --profile=%s: it covers the whole browser\n"
            "    process tree, not one process. Drop --process, or use --profile=samply to pick one." % mode)

    type_flag = "--%s" % cfg.type.lower()
    launch_env = "WPE_BROWSER=minibrowser"
    launch_cmd = "Tools/Scripts/run-minibrowser %s %s -- %s" % (cfg.port, type_flag, shlex.quote(url))
    if args:
        launch_cmd += " " + shlex.join(args)

    if mode == "samply" and process != "ui":
        # samply cannot be a prefix of a process the UI process spawns later, so it attaches instead: a fixed wait, then the newest process of that name.
        proc_name = {"web": cfg.web_process_name(), "network": cfg.network_process_name(),
                    "gpu": cfg.gpu_process_name()}.get(process, "")
        if not proc_name:
            die("no %s process for the resolved config (%s:%s)" % (process, cfg.buildsys, cfg.port))
        subject = "MiniBrowser %s, samply attaching to the newest %s after launch" % (url, proc_name)
        cmd = ("%s %s >%s 2>&1 &\n"
              "sleep 5\n"
              '_pid=$(pgrep -n -x %s 2>/dev/null) || true\n'
              '[ -n "$_pid" ] || { echo "error: no %s appeared within 5s of launching MiniBrowser -- see %s" >&2; exit 1; }\n'
              'echo "attaching samply to %s pid $_pid" >&2\n'
              'exec samply record --save-only -o %s -p "$_pid"'
              % (launch_env, launch_cmd, shlex.quote(os.path.join(outdir, "browser.log")), shlex.quote(proc_name),
                 proc_name, os.path.join(outdir, "browser.log"), proc_name, shlex.quote(os.path.join(outdir, "profile.json"))))
        return subject, cmd, False

    subject = "MiniBrowser %s" % url
    mb_prefix = False
    if mode in ("samply", "sysprof"):
        subject += " (process: ui, %s prefixed via WEBKIT_MINI_BROWSER_PREFIX)" % mode
        mb_prefix = True
    return subject, "%s %s" % (launch_env, launch_cmd), mb_prefix


def main(args, cmd, reg=None):
    name = store.ws_name()
    o = options(args)

    if o["process"] not in ("ui", "web", "network", "gpu"):
        die("no such process '%s' -- there are: ui web network gpu" % o["process"])

    require_name(name)

    # Every refusal that reads only the arguments comes first: resolving the workspace walks the fleet.
    if o.get("attach") and o.get("browser"):
        die("--attach and --browser are two ways to name the same thing: one starts\n"
            "    the browser under the profiler, the other profiles a process that is\n"
            "    already running. Pick one.")
    if o.get("process_explicit") and not o.get("browser"):
        die("--process only makes sense with --browser: it names which of\n"
            "    MiniBrowser's own processes (ui, web, network, gpu) to point the profiler\n"
            "    at, and without --browser there is no MiniBrowser to name one of.")
    reg = reg or targets.Registry(images.root())

    config = store.build_config() or reg.default_config(name)
    try:
        tname = reg.ws_target(name)
        target = reg.load(tname)
    except LookupError as e:
        die(str(e))
    try:
        cfg = buildconf.resolve(config, target.os(), target.kind, target.env)
    except LookupError:
        die("unknown config '%s' (wk build --list)" % config)

    mode = o["mode"]
    if mode == "native":
        mode = "instruments" if cfg.xcode() else "samply"
    if mode not in MODES:
        die("no such mode '%s' -- there are: %s (and 'native')" % (mode, " ".join(MODES)))

    src = target.src(name)
    var, run_dir = cfg.run_var(), cfg.run_dir(src)

    # Not /tmp: a profile is worth more than the ten minutes it took to record, and /tmp on a container workspace is the first thing a restart takes away.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run = "%s-%s" % (stamp, mode)
    outdir = os.path.join(target.home(), "wk-profile", run)

    attach, browser, url = o.get("attach", ""), o.get("browser", False), o.get("url", "")
    args = o["args"]
    mb_prefix, cmake_browser = False, False
    bin_path = ""
    if attach:
        subject, target_cmd = "attach:%s" % attach, ""
    elif browser:
        url = url or "about:blank"
        bin_path = cfg.browser_path(src)
        cmake_browser = not cfg.xcode()
        subject, target_cmd, mb_prefix = browser_target(cfg, url, o["process"], o.get("process_explicit"), mode, args, outdir)
    else:
        bin_path = cfg.jsc_path(src)
        subject = "jsc" + (" " + args[0] if args else "")
        # jsc takes its options *before* the script: a `--sample` after the file name is silently handed to the script as an argument.
        target_cmd = ""

    if mode == "sysprof":
        o["markers"] = o["jitdump"] = True
    jsc_env = []
    if o.get("markers"):
        jsc_env.append("JSC_useTextMarkers=1")
        jsc_env.append("JSC_textMarkersDirectory=%s" % outdir)
    if o.get("jitdump"):
        jsc_env.append("JSC_useJITDump=1")
        jsc_env.append("JSC_jitDumpDirectory=%s" % outdir)
        if not cfg.xcode() and browser:
            warn("JSC_useJITDump=1 has been seen to crash the WebKitGTK UI process on startup")
            log("  (claude/skills/jsc-marker-trace). If the browser dies at once, that is why.")
    for e in o["extra_env"]:
        if e:
            jsc_env.append(e)

    needs, jsc_flags, tee, wrap, pre, post, artifact = "", "", "", "", "", "", ""

    if mode == "sampling":
        if attach:
            die("the sampling profiler is a JSC option, not an attach: it has to be\n"
                "    on when the process starts. Use --profile=samply (or instruments) to profile\n"
                "    something already running.")
        if browser:
            jsc_env.append("JSC_useSamplingProfiler=1")
            jsc_env.append("JSC_collectExtraSamplingProfilerData=1")
            jsc_env.append("JSC_reportSamplingProfilerData=1")
            artifact = "the report goes to the browser's stderr at exit"
        else:
            jsc_flags = "--sample"
            if o.get("top"):
                top = int(o["top"])
                jsc_flags += " --samplingProfilerTopFunctionsCount=%d --samplingProfilerTopBytecodesCount=%d" % (top, top * 2)
            artifact = os.path.join(outdir, "report.txt")
            tee = artifact
    elif mode == "bytecode":
        if attach:
            die("the bytecode profiler is a JSC option and has to be on when the\n"
                "    process starts -- there is nothing to attach to.")
        jsc_env.append("JSC_useProfiler=true")
        jsc_env.append("JSC_dumpProfilerDataAtExit=true")
        # JSC writes JSCProfile-<pid>-<n>.json into the working directory, so the file is moved out afterwards -- identified by being newer than a stamp, not newest in the tree.
        artifact = os.path.join(outdir, "JSCProfile.json")
        pre = "touch %s" % shlex.quote(os.path.join(outdir, ".stamp"))
        post = ('p=$(find %s -maxdepth 1 -name \'JSCProfile-*.json\' -newer %s 2>/dev/null | head -1)\n'
               '[ -n "$p" ] || { echo \'no JSCProfile-*.json was written -- did the run reach exit?\'; exit 1; }\n'
               'mv "$p" %s\n'
               'printf \'summary\\nquit\\n\' | %s/Tools/Scripts/display-profiler-output %s'
               % (shlex.quote(src), shlex.quote(os.path.join(outdir, ".stamp")), shlex.quote(artifact),
                  shlex.quote(src), shlex.quote(artifact)))
    elif mode == "samply":
        needs = "samply"
        artifact = os.path.join(outdir, "profile.json")
        if attach:
            if attach.isdigit():
                wrap = "samply record --save-only -o %s -p %s" % (shlex.quote(artifact), attach)
            else:
                die("samply attaches by pid. Find it in the workspace first:\n"
                    "    wk enter %s -- pgrep -f %s" % (name, attach))
        else:
            wrap = "samply record --save-only -o %s --" % shlex.quote(artifact)
    elif mode == "sysprof":
        if cfg.xcode():
            die("sysprof profiles Linux; '%s' is an Apple-port build. Use --profile=instruments there." % config)
        if attach:
            die("sysprof-cli records a command it launches, and samples the whole system while it\n"
                "    runs; there is no attach. Use --profile=samply to attach to a pid.")
        needs = "sysprof-cli"
        artifact = os.path.join(outdir, "capture.syscap")
        wrap = "sysprof-cli --force %s --" % shlex.quote(artifact)
    elif mode == "instruments":
        if not cfg.xcode():
            die("xctrace profiles Mach-O processes on macOS; '%s' is a %s\n"
                "    build. Use --profile=samply there." % (config, cfg.buildsys))
        needs = "xctrace"
        artifact = os.path.join(outdir, "trace.trace")
        if attach:
            wrap = "xctrace record --template 'Time Profiler' --output %s --attach %s" % (shlex.quote(artifact), shlex.quote(attach))
        else:
            wrap = "xctrace record --template 'Time Profiler' --output %s --launch --" % shlex.quote(artifact)
    else:
        if cfg.xcode():
            die("%s is a Linux tool and this is an Apple-port build.\n"
                "    For allocations on macOS use Instruments' Allocations template by hand --\n"
                "    '--profile=instruments' records Time Profiler only." % mode)
        if attach:
            die("%s has to be there from the first allocation; there is nothing\n"
                "    useful to attach to." % mode)
        if mode == "heaptrack":
            needs = "heaptrack"
            artifact = os.path.join(outdir, "heaptrack.*.zst")
            wrap = "cd %s && heaptrack" % shlex.quote(outdir)
        else:
            needs = "valgrind"
            artifact = os.path.join(outdir, "massif.out")
            wrap = "valgrind --tool=massif --massif-out-file=%s" % shlex.quote(artifact)

    # A CMake --browser run builds its own command line above: prepending wrap here would wrap Tools/Scripts/run-minibrowser rather than MiniBrowser.
    if cmake_browser:
        if mb_prefix:
            jsc_env.append("WEBKIT_MINI_BROWSER_PREFIX=%s" % shlex.quote(wrap))
        wrap = ""

    bin_present = False
    if not attach and bin_path:
        if target.exec(name, ["test", "-x", bin_path]).ok:
            bin_present = True
        elif not dry_run():
            die("nothing to profile: no %s at %s\n"
                "    Build it first:  wk build %s %s" % (os.path.basename(bin_path), bin_path, name, config))

    if needs and not dry_run():
        require_tool(target, name, needs)
    if mode in ("samply", "sysprof") and not dry_run():
        perf_events(target, name, mode)

    if not target_cmd:
        target_cmd = shlex.quote(bin_path) + (" " + jsc_flags if jsc_flags else "")
        if args:
            target_cmd += " " + shlex.join(args)

    # pipefail with the tee, or the run's status becomes tee's own and every crash reads as a clean exit.
    if tee:
        target_cmd = "set -o pipefail; %s 2>&1 | tee %s" % (target_cmd, shlex.quote(tee))

    cmd = prelude(var, run_dir) + "\nmkdir -p %s\n" % shlex.quote(outdir)
    if pre:
        cmd += pre + "\n"
    if jsc_env:
        cmd += "export " + " ".join(jsc_env) + "\n"
    if wrap:
        cmd += wrap + " "
    cmd += target_cmd

    if dry_run():
        info_lines_dry(name, tname, config, subject, bin_path, bin_present, outdir, cmd, post, mode)
        return 0

    info("%s: %s in '%s' (%s)" % (mode, subject, name, config))
    log("  output: %s" % outdir)

    # exec_tty inherits this stdio, so reports and a crash's text print here, and xctrace/samply still get ctrl-c.
    r = target.exec_tty(name, ["bash", "-lc", "cd %s && %s" % (src, cmd)])
    if not r.ok:
        raise Refused(r.rc)

    if post:
        target.exec_tty(name, ["bash", "-lc", post])   # its own failure never stops the report below

    info("recorded in '%s': %s" % (name, artifact))

    if o.get("fetch"):
        fetch_dir = o.get("fetch_dir") or os.path.join(Store(reg.env).state_dir(), "profiles", name, run)
        # The whole run directory in one copy (pull_dir): a loop over pull only reaches top-level files, silently missing instruments' .trace bundle.
        try:
            target.pull_dir(name, outdir, fetch_dir)
            info("copied to %s" % fetch_dir)
        except OSError:
            warn("could not copy '%s' out of '%s'" % (outdir, name))

    closing_lines(cmd, mode, artifact, name)
    return 0


def info_lines_dry(name, tname, config, subject, bin_path, bin_present, outdir, cmd, post, mode):
    info("dry run -- nothing was profiled")
    built = ("built: " if bin_present else "NOT BUILT: ") + bin_path if bin_path else ""
    log("  workspace: %s (%s), config: %s, mode: %s" % (name, tname, config, mode))
    log("  subject:   %s%s" % (subject, " (%s)" % built if built else ""))
    log("  output:    %s" % outdir)
    log("  in the workspace:")
    for line in cmd.splitlines():
        log("    " + line)
    if post:
        log("  and afterwards:")
        for line in post.splitlines():
            log("    " + line)


def closing_lines(cmd, mode, artifact, name):
    if mode == "samply":
        log("  view it:  samply load %s          (in the workspace)" % artifact)
        log("            or --fetch it here and open it at profiler.firefox.com --")
        log("            the file is self-contained and the site uploads nothing")
    elif mode == "sysprof":
        log("  view it:  sysprof %s            (in the workspace), or --fetch it" % artifact)
        log("            and open it in Sysprof here; JS frames are named from the JIT dump beside it")
    elif mode == "instruments":
        log("  view it:  open %s                 (on the machine that has it)" % artifact)
        log("            --fetch brings the whole .trace bundle here (pull_dir")
        log("            copies the directory, not just its top-level files)")
    elif mode == "bytecode":
        log("  the summary is above; for the interactive tool:")
        log("    wk enter %s -- Tools/Scripts/display-profiler-output %s" % (name, artifact))
        log("    then: summary | bytecode <hash> | log <hash>")
    elif mode == "sampling":
        log("  the tier breakdown decides the next step: mostly FTL/DFG/Baseline")
        log("  means the cost is in generated JS -- 'wk %s %s --profile=bytecode'." % (cmd, name))
        log("  Mostly C/C++ means the engine itself -- '--profile=native'.")
