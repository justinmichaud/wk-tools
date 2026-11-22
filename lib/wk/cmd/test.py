"""Run a workspace's tests as a task: the JavaScriptCore tests of a jsc preset, the layout tests of a full port.

`wk test <ws> -- <tests and options>` tests the preset the workspace built last (`--preset` chooses another), sized
and guarded like a build, and follows the log. A word after `--` names a test; an option goes to the runner as it
is, with its value attached (`--filter=regexp`). A layout test that does not exist is refused before anything runs.

Exit code: 0 passed, 1 failed, 3 out of memory. With --detach it is 0 once queued, and `wk status <ws> --wait` is
the outcome."""

from wk import webkit
from wk.core.act import die
from wk.core.decl import Command, Opt
from wk.tasks import launch

COMMAND = Command(where="server", takes=("ws",),
                  opts=(Opt("--preset", "the build to test, not the last one", "preset"), Opt("--detach", "return once the run is queued")),
                  tail=True)


def main(args, inv):
    ws = launch.workspace(inv, args.ws)
    preset = launch.preset(inv, ws, args.preset)
    named = webkit.paths(args.tail)
    if named and not preset.jsc_only:
        r = inv.here.run(inv.driver(ws.driver, inv.here).exec_argv(ws, webkit.missing_argv(ws.path + "/src", named)))
        if not r.ok or r.out.strip():
            # run-webkit-tests runs nothing and exits 0 for a path it cannot find.
            die("no such layout test in %s: %s\n    a test is named relative to LayoutTests/ or to the checkout"
                % (ws.name, ", ".join(r.out.split()) if r.ok else "the check itself failed: " + r.err.strip()))
    return launch.start(inv, ws, preset, "test", "default", args.detach,
                        lambda jobs: webkit.test_argv(preset, ws.path + "/build", jobs, args.tail))
