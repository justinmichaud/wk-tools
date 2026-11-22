"""Build WebKit in a workspace, as a task in its server's build queue.

`wk build <ws> <preset>` sizes the job count from the cores and free memory of the place it builds in, queues the build
under the memory guard and follows its log. A server builds one thing at a time: a second build waits its turn. With
no <preset> it repeats the one the workspace built last. An unknown preset is refused with the list of them.

Exit code: 0 built, 1 failed or killed, 2 this workspace is already building, 3 out of memory. With --detach it is 0
once the build is queued, and `wk status <ws> --wait` is the outcome."""

from wk import webkit
from wk.core.act import barrier, info
from wk.core.decl import Command, Opt
from wk.core.errors import BUSY
from wk.tasks import launch
from wk.workspace import lifecycle

COMMAND = Command(where="server", takes=("ws", "preset?"),
                  opts=(Opt("--detach", "return once the build is queued"), Opt("--kill", "kill this workspace's build")))


def main(args, inv):
    ws = lifecycle.ready(inv, args.ws)
    tasks = inv.tasks(inv.here)
    building = [t for t in tasks.status() if t.group == "build" and not t.ended]
    mine = launch.builds(building, ws.name)
    if args.kill:
        for t in mine:
            tasks.kill(t.id)
        info("killed %s" % mine[0].label if mine else "%s is not building" % ws.name)
        return 0
    if mine:
        barrier("%s is already building (%s)\n    `wk status %s --wait` waits for it, `wk build %s --kill` ends it"
                % (ws.name, mine[0].label, ws.name, ws.name), BUSY)
    preset = launch.preset(inv, ws, args.preset)
    if building:
        info("%s is building %s: this build is queued behind it" % (inv.fleet.hostname, ", ".join(t.label for t in building)))
    return launch.start(inv, ws, preset, "build", "build", args.detach,
                        lambda jobs: webkit.build_argv(preset, ws.path + "/src", ws.path + "/build", jobs, inv.fleet.here.get("cmake", "")))
