"""The WebKit names and steps lab code is handed (wk.project); a step imports its module when called, after the names."""

import importlib

from wk import project

CHECKOUT = "WebKit"
MIRROR = CHECKOUT + ".git"
SRC = "/src/" + CHECKOUT
BUILD_DIR = "WebKitBuild"
PR_TOOL = "git-webkit"
PR_TOOL_SETUP = "webkitscmpy.setup"
BUGZILLA = "https://bugs.webkit.org"
BUGZILLA_ENV = ("BUGS_WEBKIT_ORG_USERNAME", "BUGS_WEBKIT_ORG_PASSWORD")
ORIGIN = "https://github.com/WebKit/WebKit.git"
RELEASES = "webkitglib"
SDK = "webkit-container-sdk"
LLDB_SCRIPT = "Tools/lldb/lldb_webkit.py"
BROWSER = "MiniBrowser"
BROWSER_BUNDLE = "org.webkit.MiniBrowser"
MAC_WEB_PROCESS = "com.apple.WebKit.WebContent"
SHA_FIELD = "webkit_sha"
SHELL = "jsc"
BENCH_RUNNER = "Tools/Scripts/run-benchmark"
BENCH_BROWSERS = {"--wpe": "minibrowser-wpe", "--gtk": "minibrowser-gtk", "macos": "minibrowser"}
BROWSER_PRODUCTS = ("bin/MiniBrowser", "bin/WPEWebProcess")
SOFTWARE_ENV = ("WEBKIT_DISABLE_DMABUF_RENDERER=1",)
SHELL_PORT = "JSCOnly"
SHELL_DRIVER = "cli.js"
BENCH_PRESET = "wpe-release"
BENCH_PID_MATCH = "*run-benchmark* *cli.js*"
BENCH_DRIVERS = "Tools/Scripts/webkitpy/benchmark_runner/browser_driver"
SLOT_COMMAND = "wk sysimage webkit"
PGO_COLLECT, PGO_USE = "wpe-cross-pgo-collect", "wpe-cross-pgo-use"
BOARD_BROWSERS = {"cog": "cog", "minibrowser": "MiniBrowser"}
BOARD_HELPERS = ("WPEWebProcess", "WPENetworkProcess")
# rdk's cmake claims libWPEBackend-default.so, so the default backend would be its stub.
BOARD_BACKEND = "export WPE_BACKEND_LIBRARY=libWPEBackend-fdo-1.0.so && "
# One line per optimizing compile, dumped from the compiler thread, which SIGSEGVs the JIT worker on some builds.
JIT_TIERS = ("JSC_reportDFGCompileTimes=1", "JSC_reportFTLCompileTimes=1")
FORKS = (("fork", "justinmichaud/WebKit", "github-webkit"),
         ("forkwpe", "justinmichaud/WPEWebKit", "github-wpe"))


def _late(module, name):
    return lambda *a, **kw: getattr(importlib.import_module("wk." + module), name)(*a, **kw)


project.hand_in(
    CHECKOUT=CHECKOUT, MIRROR=MIRROR, SRC=SRC, BUILD_DIR=BUILD_DIR, PR_TOOL=PR_TOOL, PR_TOOL_SETUP=PR_TOOL_SETUP,
    BUGZILLA=BUGZILLA, BUGZILLA_ENV=BUGZILLA_ENV, ORIGIN=ORIGIN, RELEASES=RELEASES, SDK=SDK, LLDB_SCRIPT=LLDB_SCRIPT,
    BROWSER=BROWSER, BROWSER_BUNDLE=BROWSER_BUNDLE, MAC_WEB_PROCESS=MAC_WEB_PROCESS, SHA_FIELD=SHA_FIELD, FORKS=FORKS,
    SHELL=SHELL, BENCH_RUNNER=BENCH_RUNNER, BENCH_BROWSERS=BENCH_BROWSERS, BROWSER_PRODUCTS=BROWSER_PRODUCTS, SOFTWARE_ENV=SOFTWARE_ENV,
    SHELL_PORT=SHELL_PORT, SHELL_DRIVER=SHELL_DRIVER, BENCH_PRESET=BENCH_PRESET, BENCH_PID_MATCH=BENCH_PID_MATCH,
    BENCH_DRIVERS=BENCH_DRIVERS, SLOT_COMMAND=SLOT_COMMAND, PGO_COLLECT=PGO_COLLECT, PGO_USE=PGO_USE, BOARD_BROWSERS=BOARD_BROWSERS,
    BOARD_HELPERS=BOARD_HELPERS, BOARD_BACKEND=BOARD_BACKEND, JIT_TIERS=JIT_TIERS,
    preset_names=_late("presets", "names"),
    wiring_script=_late("git", "wiring_script"),
    mirror_refresh_script=_late("git", "mirror_refresh_script"),
    setup_script=_late("git", "gitwebkit_setup_script"),
    check_pr_spec=_late("pr", "parse_spec"),
    pr_checkout=_late("pr", "checkout"),
    pr_rubble=_late("pr", "rubble"),
    payload_rubble=_late("bench.seed", "rubble"),
    resolve_preset=_late("presets", "resolve"),
    default_preset=_late("presets", "default_preset"),
    bench_class=_late("bench.plans", "bench_class"),
    measure_args=_late("bench.plans", "measure_args"),
    browser_args=_late("bench.plans", "browser_args"),
    shell_argv=_late("bench.plans", "shell_argv"),
    merge_shell_results=_late("bench.plans", "merge_jsc_logs"),
    pin_payload=_late("bench.plans", "pin_payload"),
    pin_plan=_late("bench.seed", "pin"),
    board_runner_args=_late("bench.plans", "board_runner_args"),
    pgo_collection=_late("pgo", "board_collection"),
    slot_env=_late("slot", "env"),
    slot_expect=_late("slot", "expect"),
    slot_verified=_late("slot", "verified"),
)
