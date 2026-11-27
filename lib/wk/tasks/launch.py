"""What `wk build`, `wk run` and `wk test` share: choosing the workspace's preset, and starting a sized task and
reporting how it ended."""

from wk import webkit
from wk.core import seams
from wk.core.act import die, info
from wk.tasks import sizing


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
    """Queue `line(jobs)` in the workspace; unless `detach`, follow it and return its exit code."""
    tasks, driver = inv.tasks(inv.here), inv.driver(ws.driver, inv.here)
    jobs, mb = sizing.size(sizing.measure(inv.here, driver.exec_argv(ws, sizing.PROBE)), preset.mb_per_job, inv.env)
    label = "%s:%s:%s" % (ws.name, what, preset.name)
    info("%s: %d jobs at %d MB each" % (label, jobs, mb))
    id = tasks.add(driver.exec_argv(ws, line(jobs), task=True), label, group=group)
    if id is None:
        return 0
    if detach:
        info("task %d is queued: `wk status %s --wait` is its outcome, `wk status %s --log` its output" % (id, ws.name, ws.name))
        return 0
    tasks.follow(id)
    task = tasks.wait(id)
    if task.state != seams.DONE:
        die("%s %s%s: `wk status %s --log` has the end of its log"
            % (label, task.state, " (exit %d)" % task.rc if task.rc else "", ws.name))
    info("%s finished" % label)
    return 0
