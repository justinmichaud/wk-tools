#!/usr/bin/env python3
# run-benchmark, plus the one thing WebKit's own MiniBrowser driver does not say:
# where an instrumented build writes its raw PGO profiles. OSXSafariDriver names
# /private/tmp/WebKitPGO and OSXMiniDriver names nothing, so
# `collect-pgo-profiles --browser minibrowser` raises NotImplementedError before
# the first benchmark. The constant is baked into the frameworks themselves
# (__llvm_profile_filename, Source/WebKit/Shared/Cocoa/WebKit2InitializeCocoa.mm),
# so it is the same for whatever host process loads them.
#
# TODO: upstream the same five lines onto OSXMiniDriver and delete this file.

import logging
import os
import sys

scripts = os.environ.get("WK_WEBKIT_SCRIPTS")
if not scripts:
    sys.exit("pgo-run-benchmark: WK_WEBKIT_SCRIPTS must name the checkout's Tools/Scripts")
sys.path.insert(0, scripts)

from webkitpy.benchmark_runner.run_benchmark import main, format_logger
from webkitpy.benchmark_runner.browser_driver.osx_minibrowser_driver import OSXMiniDriver

OSXMiniDriver.pgo_profile_output_directories = property(
    lambda self: ["/private/tmp/WebKitPGO"])

if __name__ == "__main__":
    format_logger(logging.getLogger())
    sys.exit(main())
