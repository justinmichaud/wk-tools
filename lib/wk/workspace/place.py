"""What every module of the lane starts from: the project's names, a machine's conf and wk home, and a checked act."""

import json
import os
import shlex

from wk.core.act import die

with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webkit", "project.json")) as _f:
    PROJECT = json.load(_f)


def place(inv, machine):
    """(the machine's Conf, its wk home). $WK_HOME is this machine's alone; another machine's is under its home."""
    if machine is inv.here:
        return inv.fleet.here, inv.wk_home
    return inv.fleet.get(machine.name), machine.home + "/wk"


def image(conf):
    return conf.get("sdk_image", PROJECT["sdk_image"])


def must(machine, argv, remedy):
    """A state change whose failure ends the command, with what it said and the remedy."""
    r = machine.act(argv)
    if not r.ok:
        die("`%s` failed on %s (exit %d): %s\n    %s" % (shlex.join(argv), machine.name, r.rc, r.err.strip(), remedy))
