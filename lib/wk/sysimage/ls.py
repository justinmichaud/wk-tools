"""The images a workspace holds, found as each builder's outputs on every read, and
`wk sysimage ls` over this store and every machine that answers for one of its own."""

import fnmatch
import os
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

from wk import act, images, slot

Image = namedtuple("Image", "builder ws path")   # path None: an image workspace holding none right now

ROW = "%-40s %-8s %-10s %-10s %-9s %-8s %s"
HEADER = ("WORKSPACE", "BOARD", "WHERE", "BUILDER", "STATE", "SIZE", "BUILT")
PGO_USE = "wpe-cross-pgo-use"


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
    if doc is None or slot.get(doc, "commit") != commit:
        return False
    return config is None or slot.get(doc, "build_config") == config


def slot_holds(ws, name, commit, env):
    """On a profile-guided release a slot holds a commit only as its measured build."""
    p = images.quiet_load(images.ws_profile(ws, env) or "", env)
    if p is None:
        return False
    return slot_is(ws, name, commit, PGO_USE if images.pgo_wanted(p["IMG_BUILDER"], p["CFG_RELEASE"]) else None, env)


class Listing:
    """This store's images, then each target whose machine answers for a store of its own, through its own wk."""

    def __init__(self, reg, label, here_label, building, warn=act.warn):
        self.reg, self.label, self.here_label, self.building, self.warn = reg, label, here_label, building, warn

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
                slot.get(doc, "slot"), slot.get(doc, "commit")[:12], slot.get(doc, "build_config", "?"),
                slot.get(doc, "built_at"), d))
        return out + (["    " + note] if note else [])

    def store_rows(self):
        return [row for image in scan(self.reg.machine, self.reg.store) for row in self.image_rows(image)]

    def target_rows(self, name):
        try:
            target = self.reg.load(name)
        except LookupError as e:
            self.warn(str(e))
            return []
        side, _ = target.probe()
        if side == "stopped":
            self.warn("the machine behind target '%s' is stopped, so the images in its\n"
                      "    store are not listed -- 'wk start' brings it up" % name)
            return []
        if side != "answering":
            return []
        label = self.here_label if target.is_here() else name
        rc, out = target.wk("sysimage", "ls", "--continued",
                            env=dict(self.reg.env, WK_ROW_LABEL=label, WK_NO_DELEGATE="1"), quiet=True)
        if rc != 0:
            self.warn("'%s' did not answer the listing, so the images in its store are not\n"
                      "    here. Its wk-tools predates a listing that walks the fleet:  wk sync --tools %s" % (name, name))
        return [l for l in out.replace("\r", "").splitlines() if l.strip()]

    def fleet_rows(self):
        names = self.reg.walk()
        if not names:
            return []
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            return [row for rows in pool.map(self.target_rows, names) for row in rows]

    def rows(self):
        return self.store_rows() + self.fleet_rows()
