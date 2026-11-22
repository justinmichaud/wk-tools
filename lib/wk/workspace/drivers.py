"""The two drivers: `container` (rootless podman and the SDK image) and `plain` (the checkout itself)."""

from wk.core.act import die, must
from wk.core.seams import ABSENT, BROKEN, READY, UNKNOWN, Driver
from wk.workspace import mirror

INSIDE = "/run/.containerenv"       # podman makes it in every container
ENTER = '. "$0" && exec /bin/bash -l "$@"'


def direct(ws, argv, cwd):
    return ["sh", "-c", 'cd "$0" && exec "$@"', cwd or ws.path + "/src"] + list(argv)


class Container(Driver):
    """One container per workspace, `wk-<name>`, idling so that every command is a `podman exec`. The login's home, the
    wk home and (read-only) the mirror are mounted at the server's own paths, with the login's HOME and the host's
    name, so a session inside reads what a login on the server reads (DESIGN.md, Workspaces).
    Typed inside a container, wk has no podman: `own` ($WK_WORKSPACE) is the one workspace it can speak for."""

    kind = "container"

    def __init__(self, machine, home, image, own=None):
        self.m, self.home, self.image, self.own = machine, home, image, own

    def inside(self):
        return self.m.exists(INSIDE)

    def create(self, ws):
        if self.state(ws) == READY:
            return
        store, env = mirror.where(self.home), self.home + "/secrets/env.sh"
        if not self.m.exists(env):
            die("%s has no %s for a shell in the container to source\n    `wk key sync` delivers the credentials there" % (self.m.name, env))
        # podman caps a container at 2048 processes, threads included; its seccomp allow-list lacks
        # personality(ADDR_NO_RANDOMIZE), which lldb and gdb launch a process with.
        must(self.m, ["podman", "run", "--detach", "--replace", "--pull=never", "--name", "wk-" + ws.name, "--init",
                      "--userns=keep-id", "--uts=host", "--pids-limit=-1", "--security-opt", "seccomp=unconfined",
                      "--volume", "%s:%s" % (self.m.home, self.m.home), "--volume", "%s:%s" % (self.home, self.home),
                      "--volume", "%s:%s:ro" % (store, store), "--env", "HOME=" + self.m.home, "--env", "WK_WORKSPACE=" + ws.name,
                      "--workdir", ws.path + "/src", self.image, "sleep", "infinity"],
             "`wk doctor` names what the container needs; `wk machine setup` pulls the image")

    def destroy(self, ws):
        must(self.m, ["podman", "rm", "--force", "--ignore", "wk-" + ws.name], "`wk doctor` names what podman needs")

    def state(self, ws):
        if self.inside():
            return READY if ws.name == self.own else UNKNOWN
        r = self.m.run(["podman", "container", "exists", "wk-" + ws.name])
        if r.rc == 1:
            return ABSENT
        if not r.ok:
            die("podman did not answer on %s (exit %d): %s\n    `wk doctor` names what it needs" % (self.m.name, r.rc, r.err.strip()))
        status = self.m.run(["podman", "container", "inspect", "--format", "{{.State.Status}}", "wk-" + ws.name]).out.strip()
        return READY if status == "running" else BROKEN

    def exec_argv(self, ws, argv, cwd=None, tty=False, task=False):
        if not task and self.inside():
            return direct(ws, argv, cwd)
        return ["podman", "exec", "--interactive"] + ["--tty"] * tty + ["--workdir", cwd or ws.path + "/src", "wk-" + ws.name] + list(argv)

    def enter_argv(self, ws):
        return self.exec_argv(ws, ["/bin/bash", "-c", ENTER, self.home + "/secrets/env.sh"], tty=True)


class Plain(Driver):
    """The checkout is the whole workspace: there is nothing to create, destroy or find broken."""

    kind = "plain"

    def create(self, ws):
        pass

    def destroy(self, ws):
        pass

    def state(self, ws):
        return READY

    def exec_argv(self, ws, argv, cwd=None, tty=False, task=False):
        return direct(ws, argv, cwd)

    def enter_argv(self, ws):
        return ["sh", "-c", 'cd "$0" && exec "$SHELL" -l "$@"', ws.path + "/src"]
