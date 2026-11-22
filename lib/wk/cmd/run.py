"""Run what a workspace built, on this terminal: `jsc`, or the port's browser.

`wk run <ws> -- <args>` runs the jsc of the preset the workspace built last with <args>; for a full-port preset it
runs the port's MiniBrowser instead. `--preset` chooses another build. The exit code is the program's."""

from wk import webkit
from wk.core.decl import Command, Opt
from wk.tasks import launch

COMMAND = Command(where="server", takes=("ws",), opts=(Opt("--preset", "the build to run, not the last one", "preset"),), tail=True)


def main(args, inv):
    ws = launch.workspace(inv, args.ws)
    preset = launch.preset(inv, ws, args.preset)
    argv = inv.driver(ws.driver, inv.here).exec_argv(ws, webkit.run_argv(preset, ws.path + "/build", args.tail))
    return inv.here.act(argv, stream=True, tty=inv.stdin.isatty()).rc
