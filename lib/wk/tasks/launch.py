"""What `wk build`, `wk run` and `wk test` share: choosing the workspace's preset, and starting a sized, guarded task
and reporting how it ended."""

from wk import webkit
from wk.core import seams
from wk.core.act import die, info
from wk.core.errors import STALLED
from wk.tasks import guard, sizing

def allowed(argv):
    """`argv`, an `env` line, with WK_BUILD set: without it the build-tool shims on an agent's PATH refuse (lib/wk/agent/shim.sh)."""
    return argv[:1] + ["WK_BUILD=1"] + argv[1:]


def builds(tasks, ws):
    """The builds of the workspace named `ws` among `tasks`, oldest first."""
    return [t for t in tasks if t.label.startswith(ws + ":build:")]


def preset(inv, ws, name):
    """The preset called `name`, or with none the one the workspace built last."""
    built = builds(inv.tasks(inv.here).status(), ws.name)
    if not name and not built:
        die("%s has built nothing, so there is no preset to repeat: name one of %s"
            % (ws.name, ", ".join(webkit.names(inv.fleet.here.kind))))
    return webkit.resolve(name or built[-1].label.split(":")[2], inv.fleet.here.kind)


def start(inv, ws, preset, what, group, detach, line):
    """Queue `line(jobs)` in the workspace under the guard; unless `detach`, follow it and return its exit code."""
    here, tasks, driver = inv.fleet.here, inv.tasks(inv.here), inv.driver(ws.driver, inv.here)
    borrowed = here.kind == "borrowed"
    jobs, mb = sizing.size(sizing.measure(inv.here, driver.exec_argv(ws, sizing.PROBE)), preset.mb_per_job, borrowed, inv.env)
    budget = max(sizing.MIN_BUDGET_MB, jobs * mb)
    path = ws.path + "/build/guard.py"
    with open(guard.__file__) as f:
        inv.here.write(path, f.read())
    guarded = ["python3", "-I", path, str(budget), "1" if ws.driver == "container" else "0"]
    label = "%s:%s:%s" % (ws.name, what, preset.name)
    info("%s: %d jobs at %d MB each, a %d MB budget%s" % (label, jobs, mb, budget, ", niced: %s is borrowed" % here.name if borrowed else ""))
    argv = guarded + (sizing.POLITE if borrowed else []) + allowed(line(jobs))
    id = tasks.add(driver.exec_argv(ws, argv, task=True), label, group=group)
    if id is None:
        return 0
    if detach:
        info("task %d is queued: `wk status %s --wait` is its outcome, `wk status %s --log` its output" % (id, ws.name, ws.name))
        return 0
    tasks.follow(id)
    task = tasks.wait(id)
    if task.rc == guard.KILLED:
        die("%s ran out of memory: its log says which limit (`wk status %s --log`)" % (label, ws.name), STALLED)
    if task.state != seams.DONE:
        die("%s %s%s: `wk status %s --log` has the end of its log"
            % (label, task.state, " (exit %d)" % task.rc if task.rc else "", ws.name))
    info("%s finished" % label)
    return 0
