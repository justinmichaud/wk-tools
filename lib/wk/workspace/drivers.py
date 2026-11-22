"""The two drivers: `container` (rootless podman and the SDK image) and `plain` (the checkout itself)."""

from wk.core.act import die
from wk.core.seams import ABSENT, BROKEN, READY, Driver
from wk.workspace import mirror
from wk.workspace.place import must


class Container(Driver):
    """One container per workspace, `wk-<name>`, idling so that every command is a `podman exec`. The workspace and
    the mirror are mounted at the server's own paths, so the checkout's alternates and every path wk prints hold on
    both sides."""

    kind = "container"

    def __init__(self, machine, home, image):
        self.m, self.home, self.image = machine, home, image

    def create(self, ws):
        if self.state(ws) == READY:
            return
        store, secrets = mirror.where(self.home), self.home + "/secrets"
        if not self.m.exists(secrets):
            die("%s has no %s to mount in the container\n    `wk key sync` delivers the credentials there" % (self.m.name, secrets))
        # podman caps a container at 2048 processes, threads included; its seccomp allow-list lacks
        # personality(ADDR_NO_RANDOMIZE), which lldb and gdb launch a process with.
        must(self.m, ["podman", "run", "--detach", "--replace", "--pull=never", "--name", "wk-" + ws.name, "--init",
                      "--userns=keep-id", "--pids-limit=-1", "--security-opt", "seccomp=unconfined",
                      "--volume", "%s:%s" % (ws.path, ws.path), "--volume", "%s:%s:ro" % (store, store),
                      "--volume", "%s:%s:ro" % (secrets, secrets), "--workdir", ws.path + "/src", self.image, "sleep", "infinity"],
             "`wk doctor` names what the container needs; `wk machine setup` pulls the image")

    def destroy(self, ws):
        must(self.m, ["podman", "rm", "--force", "--ignore", "wk-" + ws.name], "`wk doctor` names what podman needs")

    def state(self, ws):
        r = self.m.run(["podman", "container", "exists", "wk-" + ws.name])
        if r.rc == 1:
            return ABSENT
        if not r.ok:
            die("podman did not answer on %s (exit %d): %s\n    `wk doctor` names what it needs" % (self.m.name, r.rc, r.err.strip()))
        status = self.m.run(["podman", "container", "inspect", "--format", "{{.State.Status}}", "wk-" + ws.name]).out.strip()
        return READY if status == "running" else BROKEN

    def exec_argv(self, ws, argv, cwd=None):
        return ["podman", "exec", "--interactive", "--workdir", cwd or ws.path + "/src", "wk-" + ws.name] + list(argv)

    def enter_argv(self, ws):
        return ["podman", "exec", "--interactive", "--tty", "--workdir", ws.path + "/src", "wk-" + ws.name, "/bin/bash", "-l"]


class Plain(Driver):
    """The checkout is the whole workspace: there is nothing to create, destroy or find broken."""

    kind = "plain"

    def create(self, ws):
        pass

    def destroy(self, ws):
        pass

    def state(self, ws):
        return READY

    def exec_argv(self, ws, argv, cwd=None):
        return ["sh", "-c", 'cd "$0" && exec "$@"', cwd or ws.path + "/src"] + list(argv)

    def enter_argv(self, ws):
        return ["sh", "-c", 'cd "$0" && exec "$SHELL" -l "$@"', ws.path + "/src"]
