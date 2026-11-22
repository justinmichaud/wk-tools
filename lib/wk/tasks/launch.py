"""What `wk build` and `wk test` share: finding the workspace on this server, choosing its preset, and starting a
sized, guarded task and reporting how it ended."""

import json

from wk import webkit
from wk.core import seams
from wk.core.act import die, info
from wk.core.errors import STALLED
from wk.tasks import guard, sizing


def workspace(inv, name):
    """The ready Workspace called `name` on this server."""
    here = inv.fleet.hostname
    r = inv.here.run(["wk", "ls", "--json"])
    try:
        rows = json.loads(r.out)["workspaces"]
    except ValueError:
        die("cannot list the workspaces on %s: `wk ls --json` printed %s" % (here, r.out.strip()))
    found = [seams.Workspace(server=here, **row) for row in rows if row["name"] == name]
    if not found:
        die("no workspace named %s on %s: `wk ls` lists them, `wk new %s` makes it" % (name, here, name))
    if found[0].state != seams.READY:
        die("workspace %s is %s, not ready: `wk rm %s` then `wk new %s` remakes it" % (name, found[0].state, name, name))
    return found[0]


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
    id = tasks.add(driver.exec_argv(ws, guarded + (sizing.POLITE if borrowed else []) + line(jobs)), label, group=group)
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
