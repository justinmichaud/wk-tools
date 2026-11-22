"""Workspaces: the mirror, the checkout, the two drivers and the lifecycle. `driver` is what `inv.driver` calls."""

from wk.workspace.drivers import Container, Plain
from wk.workspace.place import image, place


def driver(inv, kind, machine):
    conf, home = place(inv, machine)
    return {"container": lambda: Container(machine, home, image(conf), inv.env.get("WK_WORKSPACE")), "plain": lambda: Plain(home)}[kind]()
