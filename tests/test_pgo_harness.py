"""Where an instrumented build writes its profiles (build/pgo-run-benchmark.py).

`collect-pgo-profiles --browser minibrowser` cannot start without it: upstream's
OSXMiniDriver names no profile directory and raises at the first iteration. What
makes this worth a test is *which* class gets the answer. BrowserDriverFactory
loads each driver module by file path, so the registry's class and the one a
dotted `import webkitpy...` returns are two different objects -- and patching the
dotted one is a no-op that a real collection discovers an hour into a build
(measured 2026-09-06 in a macOS guest).

The checkout is stubbed to that exact shape, so the test fails if the harness
ever goes back to patching whatever an import happens to hand it.

Run: python3 -m unittest tests.test_pgo_harness -v
"""
import subprocess
import textwrap
import unittest

from tests.support import REPO, WkTest, scratch_dir

HARNESS = REPO / "build" / "pgo-run-benchmark.py"

BASE = '''
class BrowserDriver(object):
    @property
    def pgo_profile_output_directories(self):
        raise NotImplementedError()
'''

MINI = '''
from browser_driver import BrowserDriver
class OSXMiniDriver(BrowserDriver):
    browser_name = "minibrowser"
    def _save_screenshot_to_path(self, output_directory, filename):
        raise AssertionError("screencapture ran")
'''

SAFARI = '''
from browser_driver import BrowserDriver
class OSXSafariDriver(BrowserDriver):
    browser_name = "safari"
    @property
    def pgo_profile_output_directories(self):
        return ["/private/tmp/WebKitPGO"]
'''

# The factory as upstream builds it: each driver module loaded from its path, so
# its classes are not the ones `import webkitpy...osx_minibrowser_driver` gives.
FACTORY = '''
import importlib.util, os, sys
_here = os.path.dirname(__file__)
sys.path.insert(0, _here)

def _by_path(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_here, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

class BrowserDriverFactory(object):
    browser_drivers = {"osx": {
        "minibrowser": _by_path("osx_minibrowser_driver").OSXMiniDriver,
        "safari": _by_path("osx_safari_driver").OSXSafariDriver,
    }}
'''

RUN_BENCHMARK = '''
def main():
    return 0

def format_logger(logger):
    pass
'''

ASK = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("harness", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from webkitpy.benchmark_runner.browser_driver.browser_driver_factory import BrowserDriverFactory
cls = BrowserDriverFactory.browser_drivers["osx"]["minibrowser"]
print(cls().pgo_profile_output_directories)
print("screenshot:", cls()._save_screenshot_to_path("/tmp", "x.jpg"))
'''


def stub_checkout(root, minibrowser_answers=False):
    scripts = root / "Tools" / "Scripts"
    drivers = scripts / "webkitpy" / "benchmark_runner" / "browser_driver"
    drivers.mkdir(parents=True)
    for package in (scripts / "webkitpy", scripts / "webkitpy" / "benchmark_runner", drivers):
        (package / "__init__.py").write_text("")
    (drivers / "browser_driver.py").write_text(textwrap.dedent(BASE))
    mini = MINI
    if minibrowser_answers:
        mini += '    @property\n' \
                '    def pgo_profile_output_directories(self):\n' \
                '        return ["/upstream/said/so"]\n'
    (drivers / "osx_minibrowser_driver.py").write_text(textwrap.dedent(mini))
    (drivers / "osx_safari_driver.py").write_text(textwrap.dedent(SAFARI))
    (drivers / "browser_driver_factory.py").write_text(textwrap.dedent(FACTORY))
    (scripts / "webkitpy" / "benchmark_runner" / "run_benchmark.py").write_text(
        textwrap.dedent(RUN_BENCHMARK))
    return scripts


def ask(root, scripts):
    asker = root / "ask.py"
    asker.write_text(textwrap.dedent(ASK))
    return subprocess.run(["python3", str(asker), str(HARNESS)],
                          cwd=str(root), capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WK_WEBKIT_SCRIPTS": str(scripts)},
                          timeout=60)


class TestTheCollectionDoesNotPhotographTheScreen(WkTest):
    """run-benchmark photographs the screen into --diagnose-directory on every
    leg, and --diagnose-directory is where a collection's profiles land.
    `screencapture` needs Screen Recording, and asking for it puts a consent
    dialog over the browser that nothing headless will answer -- measured
    2026-09-06, one sat there for four hours from the first leg onward."""

    def setUp(self):
        self._scratch = scratch_dir()
        self.root = self._scratch.__enter__()

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_the_driver_a_run_instantiates_takes_no_screenshot(self):
        scripts = stub_checkout(self.root)
        cp = ask(self.root, scripts)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("screenshot: None", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("screencapture ran", cp.stdout + cp.stderr)


class TestTheClassTheFactoryUsesGetsTheAnswer(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.root = self._scratch.__enter__()

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_the_driver_a_run_instantiates_knows_where_the_profiles_go(self):
        scripts = stub_checkout(self.root)
        cp = ask(self.root, scripts)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("/private/tmp/WebKitPGO", cp.stdout, cp.stdout + cp.stderr)

    def test_the_answer_is_safaris_own_and_not_a_second_copy_of_the_path(self):
        """Both drivers load the same frameworks, so one path serves both; a
        constant spelled again here would go stale on its own."""
        text = HARNESS.read_text()
        self.assertNotIn("/private/tmp/WebKitPGO", text)
        self.assertIn("safari", text)

    def test_it_stands_aside_once_upstream_names_them(self):
        """This file exists to be deleted; when the property lands upstream it
        must not overwrite it, and it should say so."""
        scripts = stub_checkout(self.root, minibrowser_answers=True)
        cp = ask(self.root, scripts)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("/upstream/said/so", cp.stdout)
        self.assertIn("can be deleted", cp.stderr)

    def test_a_checkout_with_no_such_driver_is_refused_rather_than_patched(self):
        scripts = stub_checkout(self.root)
        factory = scripts / "webkitpy" / "benchmark_runner" / "browser_driver" / "browser_driver_factory.py"
        factory.write_text(factory.read_text().replace('"minibrowser":', '"nothing":'))
        cp = ask(self.root, scripts)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("registers no osx minibrowser", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
