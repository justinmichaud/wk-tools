"""The images a workspace holds, found as each builder's outputs on every read; `wk sysimage ls` walks every machine that answers for a store of its own."""

import fnmatch
import json
import os
import time
from collections import namedtuple

from wk import act, fleetwalk, images, project
from wk.clock import Clock

Image = namedtuple("Image", "builder ws path")   # path None: an image workspace holding none right now

ROW = "%-40s %-8s %-10s %-10s %-9s %-8s %s"
HEADER = ("WORKSPACE", "BOARD", "WHERE", "BUILDER", "STATE", "SIZE", "BUILT")
HOST_BUILDERS = ("mac-volume", "guest", "fetch", "pmos")


class Unknown(Exception):
    """A builder's outputs could not be read: neither an image nor its absence."""


class Builder:
    def __init__(self, kind, pattern):
        self.kind, self.pattern = kind, pattern

    def owns(self, ws):
        return ws.startswith(self.kind + "-")

    def outputs(self, machine, ws_dir):
        return sorted(_glob(machine, ws_dir, self.pattern.split("/")))


# Under ws/<name>/build, which lib/wk/places.py's Container bind-mounts as the checkout's build tree.
BUILDERS = (Builder("yocto", "build/CrossToolChains/*/build/image/*.wic.xz"),
            Builder("buildroot", "build/buildroot/*/output/images/*.img"))


def slot_doc(path):
    with open(path) as f:
        return json.load(f)


def _glob(machine, base, parts):
    if not parts:
        return [base] if machine.exists(base) and not machine.isdir(base) else []
    head, rest = parts[0], parts[1:]
    if not any(c in head for c in "*?["):
        return _glob(machine, os.path.join(base, head), rest)
    try:
        names = machine.listdir(base)
    except OSError:
        return []
    return [p for n in names if not n.startswith(".") and fnmatch.fnmatchcase(n, head)
            for p in _glob(machine, os.path.join(base, n), rest)]


def scan(machine, store):
    """Every image in this store's workspaces; an image workspace that holds none gets a placeholder."""
    root = os.path.join(store.store_dir(), "ws")
    try:
        names = machine.listdir(root)
    except OSError:
        return []
    out = []
    for ws in (n for n in names if not n.startswith(".")):
        d = os.path.join(root, ws)
        if not machine.isdir(d):
            continue
        for b in BUILDERS:
            found = b.outputs(machine, d)
            out += [Image(b.kind, ws, p) for p in found]
            if not found and b.owns(ws):
                out.append(Image(b.kind, ws, None))
    return out


def outputs(machine, store, ws):
    return [p for b in BUILDERS for p in b.outputs(machine, store.ws_dir(ws))]


def host_profiles(env):
    for name in images.names(env):
        p = images.quiet_load(name, env)
        if p and p["IMG_BUILDER"] in HOST_BUILDERS:
            yield p


def builder_outputs(reg, clock, p):
    """A host builder's marker, read off reg.machine/reg.env, the one path holds and path also ask; Unknown when unreadable."""
    from wk.sysimage import guestbase, macvolume, pmos, task
    if p["IMG_BUILDER"] == "mac-volume":
        return macvolume.MacVolume(reg.machine, p, reg.env, clock).outputs()
    if p["IMG_BUILDER"] == "guest":
        try:
            vm = reg.load("vm")
        except LookupError:
            return []
        return guestbase.Base(vm, clock).outputs()
    if p["IMG_BUILDER"] == "fetch":
        return task.Fetch(reg.machine, p, reg.env).outputs()
    if p["IMG_BUILDER"] == "pmos":
        return pmos.outputs(reg, p)
    return None


def stamp(path):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.stat(path).st_mtime))


def human_bytes(n):
    v, units = float(n), "BKMGTP"
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v, i = v / 1024, i + 1
    return ("%.1f%s" if i and v < 10 else "%.0f%s") % (v, units[i])


def slot_docs(ws, env):
    d = images.slot_dir(ws, "x", env)
    parent = os.path.dirname(d) if d else ""
    try:
        names = sorted(os.listdir(parent)) if parent else []
    except OSError:
        return []
    out = []
    for n in names:
        sj = os.path.join(parent, n, "slot.json")
        if os.path.isfile(sj):
            out.append((os.path.dirname(sj), slot_doc(sj)))
    return out


def slot_is(ws, name, commit, preset, env):
    d = images.slot_dir(ws, name, env)
    try:
        doc = slot_doc(os.path.join(d, "slot.json")) if d else None
    except (OSError, ValueError):
        return False
    if doc is None or doc.get("commit") != commit:
        return False
    return preset is None or doc.get("build_preset") == preset


def slot_holds(ws, name, commit, env):
    """On a profile-guided release a slot holds a commit only as its measured build."""
    p = images.quiet_load(images.ws_profile(ws, env) or "", env)
    if p is None:
        return False
    return slot_is(ws, name, commit, project.get("PGO_USE") if images.pgo_wanted(p["IMG_BUILDER"], p["CFG_RELEASE"]) else None, env)


class Listing:

    def __init__(self, reg, label, here_label, building, warn=act.warn, clock=None):
        self.reg, self.label, self.here_label, self.building, self.warn = reg, label, here_label, building, warn
        self.clock = clock or Clock()

    def image_rows(self, image):
        ws, env = image.ws, self.reg.env
        building = self.building(ws)
        state = "unknown" if building is None else "building" if building else "none" if image.path is None else "ready"
        prof = images.ws_profile(ws, env)
        board, note = "", ""
        if prof:
            board = (images.quiet_load(prof, env) or {}).get("IMG_MACHINE", "")
            note = "" if board else "this checkout does not define '%s'" % prof
        if image.path is None:
            out = [ROW % (ws, board or "?", self.label, image.builder, state, "-", "-"),
                   "    no image yet; 'wk sysimage build %s' builds one" % (prof or "<profile>")]
        else:
            out = [ROW % (ws, board or "?", self.label, image.builder, state, human_bytes(os.stat(image.path).st_size),
                          stamp(image.path)), "    " + image.path]
        if state == "building":
            out.append("    building now; 'wk status %s --log' follows it" % ws)
        for d, doc in slot_docs(ws, env):
            out.append("    slot %-12s %s  %s  built %s  (%s)" % (
                doc.get("slot", ""), doc.get("commit", "")[:12], doc.get("build_preset", "?"),
                doc.get("built_at", ""), d))
        return out + (["    " + note] if note else [])

    def host_rows(self):
        rows = []
        for p in host_profiles(self.reg.env):
            name, builder, board = p["IMG_PROFILE"], p["IMG_BUILDER"], p["IMG_MACHINE"] or "-"
            try:
                found = builder_outputs(self.reg, self.clock, p) or []
            except Unknown as e:
                self.warn("cannot tell whether %s is built: %s" % (name, e))
                continue
            except act.Refused:
                self.warn("cannot tell whether %s is built: the reason is above" % name)
                continue
            if found:
                rows += [ROW % (name, board, self.label, builder, "ready", "-", "-"), "    " + found[0]]
        return rows

    def rows(self):
        mine = [row for image in scan(self.reg.machine, self.reg.store) for row in self.image_rows(image)] + self.host_rows()
        return mine + fleetwalk.fleet_rows(self.reg, "sysimage", "images",
                                           lambda d, name: self.here_label if d.is_here() else name, self.warn)
