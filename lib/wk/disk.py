"""`wk doctor`'s disk section: the podman VM's disk image, the Tart guests, the store. Run as a script (in the VM), the
store's rows as probe_store writes them."""

import glob
import os
import shlex
import sys

from wk import gc, kv, places, rubble
from wk.act import log
from wk.machine import Local
from wk.store import Store
from wk.sysimage import guestbase, task
from wk.sysimage.ls import human_bytes

RUN_AS_VM_COPY = ('import sys; sys.path.insert(0, "/opt/wk-tools/lib"); '
                  'exec(compile(sys.stdin.read(), "disk.py", "exec"), {"__name__": "__main__"})')

HOME = os.environ.get("HOME", os.path.expanduser("~"))
DATA = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
here = Local()


def kb(path):
    """KiB allocated, 0 for an absent path; `sudo -n` only, so this never prompts."""
    if not os.path.exists(path):
        return 0
    r = here.run(["du", "-sk", path])
    if r.err and here.run(["sudo", "-n", "true"]).ok:
        r = here.run(["sudo", "-n", "du", "-sk", path])
    try:
        return int(r.out.split()[0])
    except (IndexError, ValueError):
        return 0


class Report:
    def __init__(self):
        self.total = 0
        self.noted = False

    def line(self, size, what, note=""):
        sys.stderr.write(("  %7s  %-28s %s\n" % (size, what, note)) if note else ("  %7s  %s\n" % (size, what)))

    def row(self, k, what, note=""):
        self.total += k
        self.line(human_bytes(k * 1024), what, note)

    def note(self, k, what, note):
        self.noted = True
        self.line("(%s)" % human_bytes(k * 1024), what, note)

    def section(self, title):
        sys.stderr.write("\n%s\n" % title)

    def df(self, path):
        log("")
        sys.stderr.write("".join("  " + l + "\n" for l in here.run(["df", "-h", path]).out.splitlines()))
        log("")

    def render(self, rows, add):
        for line in rows.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0]:
                continue
            k, what, note = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
            if k == "0" and not note:
                continue
            (self.row if add else self.note)(int(k), what, note)


def probe_store(store):
    """kb<TAB>label<TAB>note per store row; on macOS this runs in the VM and renders out here."""
    rows = [(kb(store.mirror_dir()), "the WebKit mirror", "git/WebKit.git -- refetchable (wk sync)")]
    bases = len(os.listdir(store.snapshots_dir())) if os.path.isdir(store.snapshots_dir()) else 0
    rows.append((kb(store.snapshots_dir()), "snapshots", "%d of them; unreferenced ones go with wk gc" % bases))
    for ws in store.workspaces():
        rows.append((kb(store.ws_dir(ws)), "workspace %s" % ws, "wk rm %s" % ws))
    for d in ("cache/ccache", "cache/bench", "cache/images", "cache/yocto", "cache/buildroot", "skills", "tools"):
        p = os.path.join(store.store_dir(), d)
        if os.path.isdir(p):
            rows.append((kb(p), d, ""))
    containers = os.path.join(DATA, "containers", "storage")   # podman's own rootless tree: not under the store, still the store
    if os.path.isdir(containers):
        rows.append((kb(containers), "container images", "wk gc prunes the dangling ones"))
    return "".join("%d\t%s\t%s\n" % r for r in rows)


def workspace_report(reg):
    """`wk doctor`'s disk section inside a workspace: what this workspace's checkout and builds cost."""
    rep = Report()
    marker = kv.kv_file(reg.marker_path())
    src = marker.get("src", "")
    rep.section("workspace '%s'" % marker.get("name", ""))
    rep.row(kb(os.path.join(src, ".git")), "the checkout's .git")
    for d in sorted(glob.glob(os.path.join(src, "WebKitBuild", "*", "*"))):
        if os.path.isdir(d):
            rep.row(kb(d), "build tree %s" % os.path.relpath(d, os.path.join(src, "WebKitBuild")))
    for d in (os.path.join(HOME, "Library/Developer/Xcode/DerivedData"), os.path.join(HOME, "Library/Caches/clang")):
        if os.path.isdir(d):
            rep.row(kb(d), os.path.basename(d), "compilation cache")
    for d in ("/ccache", os.path.join(HOME, ".cache", "ccache")):
        if os.path.isdir(d):
            rep.note(kb(d), "ccache", "shared, and the host's -- 'wk gc' trims it")
    rep.section("total")
    sys.stderr.write("  %7s  %s\n" % (human_bytes(rep.total * 1024), "this workspace"))
    if rep.noted:
        log("           parenthesised rows are shared and not this workspace's")
    rep.df(src)
    log("  This workspace is disposable: 'wk rm %s' on the host reclaims" % marker.get("name", ""))
    log("  all of it at once. 'wk help' is the whole-machine picture.")


def machine_report(root, reg):
    """`wk doctor`'s disk section: everything wk stores on this machine, with the total."""
    store, rep = reg.store, Report()
    macos_host = store.macos_host
    if macos_host:
        ctr = reg.load("container")
        vm_name = store.podman_machine()
        rep.section("the podman VM (container workspaces)")
        mdir = os.path.join(DATA, "containers", "podman", "machine")
        images = glob.glob(os.path.join(mdir, "*", vm_name + "-*.raw")) + glob.glob(os.path.join(mdir, "*", vm_name + ".raw"))
        ceiling = ((places.podman_vm(here, vm_name) or {}).get("Resources") or {}).get("DiskSize") or "?"
        for f in images:
            rep.row(kb(f), "disk image", "sparse, %s GB ceiling; grows, never shrinks" % ceiling)
        if not images:
            rep.line("??", "disk image", "not found under %s" % mdir)
        for f in glob.glob(os.path.join(mdir, "*", "cache")):
            k = kb(f)
            if k > 0:
                rep.row(k, "downloaded machine image", "re-downloadable")
        rep.section("the container store, inside that image (already counted above)")
        if ctr.machine_state() == "running":
            with open(__file__) as me:   # this file, not the VM's copy, which is only as new as `wk sync --tools container`
                cp = here.run(["podman", "machine", "ssh", vm_name, "--",
                               "WK_STORE=/var/lib/wk python3 -c %s" % shlex.quote(RUN_AS_VM_COPY)], input=me.read())
            if cp.out.strip():
                rep.render(cp.out, add=False)
            else:
                rep.line("??", "the store inside the VM", "the VM answered nothing; its tooling may be missing: wk sync --tools container")
        else:
            rep.line("??", "the store inside the VM", "the machine is stopped: 'wk start', then re-run")
    else:
        rep.section("the store (%s)" % store.store_dir())
        rep.render(probe_store(store), add=True)
    tart_home = guestbase.tart_home(os.environ)
    if macos_host and os.path.isdir(tart_home):
        rep.section("macOS guests (vm place, %s)" % tart_home)
        base = reg.load("vm").base() if reg.vm_listed() else None
        for d in sorted(glob.glob(os.path.join(tart_home, "vms", "*"))):
            if not os.path.isdir(d):
                continue
            v = os.path.basename(d)
            if v == base:
                rep.row(kb(d), "golden base '%s'" % v, "every guest clones it")
            elif v.startswith("wk-"):
                rep.row(kb(d), "guest '%s'" % v[3:], "wk rm %s" % v[3:])
            else:
                rep.row(kb(d), "guest '%s'" % v, "not wk's")
        cache = os.path.join(tart_home, "cache")
        if os.path.isdir(cache):
            rep.row(kb(cache), "tart's pulled-image cache", "wk gc trims it")
    rep.section("this host")
    if os.path.isdir(store.state_dir()):
        rep.row(kb(store.state_dir()), "host-side state", "logs, keys, remote build status")
        cache = task.cache_dir(os.environ)
        if cache.startswith(store.state_dir() + "/") and os.path.isdir(cache):
            k = kb(cache)
            if k > 0:
                rep.note(k, "fetched base images", "inside the row above; a re-fetchable input, kept by wk gc")
    rep.row(kb(root), "wk-tools checkout", root)
    rep.section("total")
    sys.stderr.write("  %7s  %s\n" % (human_bytes(rep.total * 1024), "wk's storage on this machine"))
    if rep.noted:
        log("           parenthesised rows are not in the total: they are inside a row above")
    log("           du counts allocated blocks (%s) -- df below is the filesystem's own answer"
        % ("APFS clones share them: an upper bound" if os.uname().sysname == "Darwin" else "hardlinked snapshots once"))
    rep.df(store.store_dir() if os.path.isdir(store.store_dir()) else HOME)
    rep.section("what 'wk gc' reclaims or names (inside the rows above, or on another machine)")
    for r in sorted(gc.Gc(root, reg).rows(), key=lambda r: r.kind):
        sys.stderr.write(rubble.line(r, (), "a plain 'wk gc' takes it") + "\n")


if __name__ == "__main__":
    sys.stdout.write(probe_store(Store()))
