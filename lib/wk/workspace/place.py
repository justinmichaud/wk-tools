"""What every module of the lane starts from: a machine's conf and wk home, and its SDK image."""

from wk.webkit import PROJECT


def place(inv, machine):
    """(the machine's Conf, its wk home)."""
    return inv.fleet.here if machine is inv.here else inv.fleet.get(machine.name), inv.wk_home_on(machine)


def image(conf):
    return conf.get("sdk_image", PROJECT["sdk_image"])
