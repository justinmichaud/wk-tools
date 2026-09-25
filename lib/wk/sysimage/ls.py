"""The images a workspace holds, found as each builder's outputs on every read; `wk sysimage ls` walks every machine that answers for a store of its own."""

import fnmatch
import os
import time
from collections import namedtuple

from wk import act, fleetwalk, images, slot
from wk.clock import Clock

Image = namedtuple("Image", "builder ws path")   # path None: an image workspace holding none right now

ROW = "%-40s %-8s %-10s %-10s %-9s %-8s %s"
HEADER = ("WORKSPACE", "BOARD", "WHERE", "BUILDER", "STATE", "SIZE", "BUILT")
PGO_USE = "wpe-cross-pgo-use"
HOST_BUILDERS = ("mac-volume", "guest")


class Builder:
    def __init__(self, kind, pattern):
        self.kind, self.pattern = kind, pattern

    def owns(self, ws):
        return ws.startswith(self.kind + "-")

    def outputs(self, machine, ws_dir):
        return sorted(_glob(machine, ws_dir, self.pattern.split("/")))


# Under ws/<name>/build, which targets/container.sh bind-mounts as /src/WebKit/WebKitBuild.
BUILDERS = (Builder("yocto", "build/CrossToolChains/*/build/image/*.wic.xz"),
            Builder("buildroot", "build/buildroot/*/output/images/*.img"))


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
    root = os.path.join(store.root(), "ws")
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
    """A host builder's marker, read off reg.machine/reg.env, the one path holds and path also ask."""
    from wk.sysimage import guestbase, macvolume
    try:
        if p["IMG_BUILDER"] == "mac-volume":
            return macvolume.MacVolume(reg.machine, p, reg.env, clock).outputs()
        if p["IMG_BUILDER"] == "guest":
            return guestbase.Base(reg.load("vm"), clock).outputs()
    except LookupError:
        return []
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
            out.append((os.path.dirname(sj), slot.load(sj)))
    return out


def slot_is(ws, name, commit, config, env):
    d = images.slot_dir(ws, name, env)
    try:
        doc = slot.load(os.path.join(d, "slot.json")) if d else None
    except (OSError, ValueError):
        return False
    if doc is None or doc.get("commit") != commit:
        return False
    return config is None or doc.get("build_config") == config


def slot_holds(ws, name, commit, env):
    """On a profile-guided release a slot holds a commit only as its measured build."""
    p = images.quiet_load(images.ws_profile(ws, env) or "", env)
    if p is None:
        return False
    return slot_is(ws, name, commit, PGO_USE if images.pgo_wanted(p["IMG_BUILDER"], p["CFG_RELEASE"]) else None, env)


class Listing:
    """This store's images, then each target whose machine answers for a store of its own, through its own wk."""

    def __init__(self, reg, label, here_label, building, warn=act.warn, clock=None):
        self.reg, self.label, self.here_label, self.building, self.warn = reg, label, here_label, building, warn
        self.clock = clock or Clock()

    def image_rows(self, image):
        ws, env = image.ws, self.reg.env
        state = "building" if self.building(ws) else "none" if image.path is None else "ready"
        prof = images.ws_profile(ws, env)
        board, note = "", ""
        if prof:
            board = (images.quiet_load(prof, env) or {}).get("IMG_MACHINE", "")
            note = "" if board else "this checkout does not define '%s'" % prof
        if image.path is None:
            out = [ROW % (ws, board or "?", self.label, image.builder, state, "-", "-"), "    no image here yet",
                   "    a build is running here -- 'wk logs %s' follows it" % ws if state == "building"
                   else "    'wk sysimage build %s' builds one" % (prof or "<profile>")]
        else:
            st = os.stat(image.path)
            out = [ROW % (ws, board or "?", self.label, image.builder, state, human_bytes(st.st_size), stamp(image.path)),
                   "    " + image.path]
            if state == "building":
                out.append("    a build is running here -- these bytes are the previous image; 'wk logs %s' follows it" % ws)
        for d, doc in slot_docs(ws, env):
            out.append("    slot %-12s %s  %s  built %s  (%s)" % (
                doc.get("slot", ""), doc.get("commit", "")[:12], doc.get("build_config", "?"),
                doc.get("built_at", ""), d))
        return out + (["    " + note] if note else [])

    def host_rows(self):
        rows = []
        for p in host_profiles(self.reg.env):
            found = builder_outputs(self.reg, self.clock, p) or []
            if not found:
                continue
            name, builder, board = p["IMG_PROFILE"], p["IMG_BUILDER"], p["IMG_MACHINE"] or "-"
            rows.append(ROW % (name, board, self.label, builder, "ready", "-", "-"))
            rows.append("    " + found[0])
        return rows

    def store_rows(self):
        return [row for image in scan(self.reg.machine, self.reg.store) for row in self.image_rows(image)] + self.host_rows()

    def _label(self, target, name):
        return self.here_label if target.is_here() else name

    def rows(self):
        return self.store_rows() + fleetwalk.fleet_rows(self.reg, "sysimage", "images", self._label, self.warn)
