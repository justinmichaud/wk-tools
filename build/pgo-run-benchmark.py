#!/usr/bin/env python3
# `collect-pgo-profiles --browser minibrowser` raises NotImplementedError at the first iteration: OSXMiniDriver names no profile directory. Both drivers load the same frameworks, whose __llvm_profile_filename fixes the path, so Safari's own answer is handed over rather than copied.
# The class patched is the one BrowserDriverFactory registered: measured 2026-09-06, the factory loads each driver module by file path, so a dotted import gives a different class object and patching that one is a no-op a collection discovers an hour into a build.
# TODO: upstream the property onto OSXMiniDriver and delete this file.

import logging
import os
import sys

scripts = os.environ.get("WK_WEBKIT_SCRIPTS")
if not scripts:
    sys.exit("pgo-run-benchmark: WK_WEBKIT_SCRIPTS must name the checkout's Tools/Scripts")
sys.path.insert(0, scripts)

from webkitpy.benchmark_runner.browser_driver.browser_driver_factory import BrowserDriverFactory
from webkitpy.benchmark_runner.run_benchmark import main, format_logger

drivers = BrowserDriverFactory.browser_drivers.get("osx", {})
minibrowser = drivers.get("minibrowser")
safari = drivers.get("safari")
if minibrowser is None or safari is None:
    sys.exit("pgo-run-benchmark: this checkout registers no osx minibrowser/safari driver")

WHERE = "pgo_profile_output_directories"
if WHERE in vars(minibrowser):
    print(f"pgo-run-benchmark: {minibrowser.__name__} names its own profile directories; "
          "this file has nothing left to do and can be deleted", file=sys.stderr)
else:
    setattr(minibrowser, WHERE, vars(safari)[WHERE])


# run-benchmark photographs the screen into --diagnose-directory on every leg, and `screencapture` needs Screen Recording: asking puts a consent dialog over the browser that nothing headless answers (measured 2026-09-06, four hours from leg one). A picture is worth nothing to a profile.
def _no_screenshot(self, output_directory, filename):
    return None


minibrowser._save_screenshot_to_path = _no_screenshot

if __name__ == "__main__":
    format_logger(logging.getLogger())
    sys.exit(main())
