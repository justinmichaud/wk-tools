"""The contracts between lanes: Workspace, Driver, Tasks, Secrets. Each has a fake in wk.core.fakes.

An implementation changes state only through a Machine's mutators or `wk.core.act.act`, so it obeys --dry-run and is
counted by the kill-point tests like any other effect."""

import dataclasses
from typing import Optional

ABSENT, CREATING, READY, BROKEN, UNKNOWN = "absent", "creating", "ready", "broken", "unknown"


@dataclasses.dataclass(frozen=True)
class Workspace:
    """A workspace as one server reports it. `state` is one of absent | creating | ready | broken | unknown:
    creating is a `wk new` in flight, broken is a half-made one (no `.ready`) that nothing is making."""
    name: str
    server: str
    path: str
    driver: str
    state: str


class Driver:
    """How a workspace's commands run: `container` or `plain`. Made by `inv.driver(kind, machine)` for one machine."""

    kind = ""

    def create(self, ws):
        """Make what the driver adds to the checkout (a container; nothing for plain). Idempotent."""
        raise NotImplementedError

    def destroy(self, ws):
        """Remove it. An absent one is already destroyed."""
        raise NotImplementedError

    def state(self, ws):
        """ready | absent | broken, for the driver's part alone."""
        raise NotImplementedError

    def exec_argv(self, ws, argv, cwd=None):
        """The argv that runs `argv` inside the workspace without a terminal, from `cwd` (default: the checkout)."""
        raise NotImplementedError

    def enter_argv(self, ws):
        """The argv of an interactive login shell in the workspace."""
        raise NotImplementedError


QUEUED, RUNNING, DONE, FAILED, KILLED = "queued", "running", "done", "failed", "killed"


@dataclasses.dataclass(frozen=True)
class Task:
    """One task. `label` is what asked for it (a workspace's name); `rc` is None until it ends."""
    id: int
    label: str
    group: str
    state: str
    command: str
    rc: Optional[int] = None

    @property
    def ended(self):
        return self.state in (DONE, FAILED, KILLED)


class Tasks:
    """One machine's tasks: anything that outlives its terminal. Made by `inv.tasks(machine)`."""

    def add(self, argv, label, group="default", cwd=None):
        """Queue `argv`; returns the task's id. A group runs as many at once as the machine configures."""
        raise NotImplementedError

    def status(self):
        """Every task the machine remembers, oldest first, as Task values."""
        raise NotImplementedError

    def log(self, id):
        """The task's output so far."""
        raise NotImplementedError

    def kill(self, id):
        """End a queued or running task. Killing an ended one changes nothing."""
        raise NotImplementedError

    def wait(self, id, timeout=None):
        """Block on the Clock until the task ends or `timeout` seconds pass; returns its Task either way."""
        raise NotImplementedError


class Secrets:
    """The one credential store. Made by `inv.secrets()`. A value is a str and is never put in an argv or a log."""

    def get(self, name):
        """The value, or None when the store has no such name."""
        raise NotImplementedError

    def put(self, name, value):
        raise NotImplementedError

    def list(self):
        """The sorted names."""
        raise NotImplementedError
