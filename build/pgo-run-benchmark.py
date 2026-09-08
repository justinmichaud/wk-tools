#!/usr/bin/env python3
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


def _no_screenshot(self, output_directory, filename):
    return None


minibrowser._save_screenshot_to_path = _no_screenshot

if __name__ == "__main__":
    format_logger(logging.getLogger())
    sys.exit(main())
