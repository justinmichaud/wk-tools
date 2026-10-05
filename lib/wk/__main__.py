"""python3 -m wk <module> [args]: that module's own entry, run with the WebKit names handed in (wk.webkit)."""
import runpy
import sys

from wk import webkit  # noqa: F401

if len(sys.argv) < 2:
    sys.exit("usage: python3 -m wk <module> [args]")
sys.argv.pop(0)
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
