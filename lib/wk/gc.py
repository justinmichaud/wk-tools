"""`wk gc`: every module's rubble rows, one question, then each chosen row taken through the machine. A plain run takes
what loses no work and names the rest with the flag or command that takes it. On a macOS host the container store is
the podman VM's, and the VM's own wk answers that half: its rows join this one's before the question."""

import json
import os
import sys

from wk import act, images, reach, rubble as rb, workspace
from wk.bridge import provision as bridge_provision
from wk.act import die, info, log, warn
from wk.bench import mac as benchmac, seed
from wk.bench.board import SLOTS_DIR
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Ssh
from wk.store import Store, rubble as store_rubble
from wk.sysimage import guestbase, pmos, task


def build_outputs(store, env):
    """Where each image builder leaves bytes; `pmos` builds on its host and `guest` is a tart VM, each with rows of its own."""
    return {"buildroot": os.path.join(store.root(), "cache", "buildroot"),
            "yocto": os.path.join(store.root(), "cache", "yocto"),
            "fetch": task.cache_dir(env),
            "mac-volume": os.path.join(os.path.dirname(task.cache_dir(env)), "mac-tailnet"),
            "pmos": None, "guest": None}


class Gc:
    def __init__(self, root, reg, clock=None):
        self.root, self.reg, self.here, self.env = str(root), reg, reg.machine, reg.env
        self.store = Store(self.env)
        self.clock = clock or Clock()
        self.mac_host = self.store.macos_host
        self.host_half = not self.env.get("WK_IN_VM")
        self.store_half = not self.mac_host
        self._container = None

    def container(self):
        if self._container is None:
            self._container = self.reg.load("container")
        return self._container

    def vm(self):
        return self.reg.load("vm") if self.host_half and self.reg.vm_listed() else None

    def remotes(self):
        out = []
        for m in self.reg.machines():
            try:
                t = self.reg.load(m)
            except LookupError:
                continue
            if t.kind == "remote" and not t.peer:
                out.append(t)
        return out

    def boards(self):
        f = self.reg.fleet
        return [(n, f.load(n)) for n in f.names(("board",))]

    def board_machine(self, dest):
        return Ssh(dest, opts=["-l", "root"] + reach.UNPINNED, timeout=reach.ssh_timeout(self.env), via=self.here)

    def reach(self):
        return reach.Reach(self.here, self.env, self.reg.fleet)

    def pmos_hosts(self):
        return pmos.build_hosts(self.env)

    def pmos_machine(self, host):
        return pmos.ssh_machine(self.reg.fleet, self.env, self.here, host)

    def install(self):
        return benchmac.Install(self.root, self.here, self.env)

    def lock(self):
        return Lock(self.store, self.here, self.clock)

    def rows(self):
        lock, vm = self.lock(), self.vm()
        stored = ([self.container()] if self.store_half else []) + ([vm] if vm else [])
        listed = ([self.container()] if self.host_half else []) + ([vm] if vm else [])
        pid = lock.holder_pid("selftest")
        rows = store_rubble(self.store, self.here, self.host_half)
        if self.store_half:
            rows += self.image_rows() + self.ccache_rows()
        rows += seed.rubble(self.here, lock, os.path.join(self.store.artifact_dir(), "bench"))
        rows += bridge_provision.rubble(self.store, self.here, lock)
        rows += self.runner_rows() + self.build_output_rows()
        rows += workspace.rubble(listed, stored, self.here, self.root, pid is not None and self.here.alive(pid), self.clock)
        if self.host_half:
            rows += (guestbase.rubble(vm) if vm else []) + self.board_rows() + self.remote_rows()
            rows += pmos.rubble(self.pmos_hosts(), self.pmos_machine, self.env)
            if self.mac_host:
                rows += benchmac.rubble(self.install())
        if self.host_half and not self.store_half:
            rows = self.with_vm(rows, vm)
        return rows

    def with_vm(self, rows, vm):
        """The VM's rows, and the host's mirror kept while anything in the VM or a guest may borrow it."""
        c = self.container()
        if c.machine_state() != "running":
            far = [rb.row("store", "the container store in the podman VM", None,
                          why="not looked at -- the podman machine is stopped: 'wk start', then 'wk gc' again")]
        else:
            _, out = c.wk("gc", "--rows", quiet=True)
            far = [r for r in (rb.from_line(l, "podman VM") for l in out.splitlines()) if r]
        blocked = next((r.why for r in far if r.kind in ("mirror", "store") and r.why), "")
        guests = [n for n, _ in vm.list()] if vm else []
        if guests:
            blocked = "kept -- guest(s) %s clone from it: 'wk rm' them first" % " ".join(guests)
        return [r._replace(why=blocked) if r.kind == "mirror" and blocked and not r.why else r for r in rows] + far

    def image_rows(self):
        r = self.here.run(["podman", "images", "--format", "json"])
        try:
            imgs = json.loads(r.out or "[]") if r.ok else None
        except ValueError:
            imgs = None
        if imgs is None:
            why = r.err.strip().splitlines()[-1] if r.err.strip() else "its output is not JSON"
            return [rb.row("container-image", "container images", None, why="not looked at -- 'podman images' failed: " + why)]
        rows = []
        dangling = [i for i in imgs if i.get("Dangling")]
        if dangling:
            rows.append(rb.row("dangling-images", "%d dangling container image(s)" % len(dangling),
                               sum(int(i.get("Size") or 0) for i in dangling) // 1024,
                               take=lambda: self.here.act_run(["podman", "image", "prune", "-f"]).ok))
        for i in imgs:
            if not i.get("Dangling") and not i.get("Containers"):
                name = (i.get("Names") or [i.get("Id", "")[:12]])[0]
                rows.append(rb.row("container-image", "container image %s, no container on it" % name, int(i.get("Size") or 0) // 1024,
                                   "--purge-images", lambda i=i: self.here.act_run(["podman", "rmi", i.get("Id", "")]).ok))
        r = self.here.run(["podman", "volume", "ls", "--filter", "dangling=true", "--quiet"])
        if r.ok and r.out.split():
            rows.append(rb.row("dangling-volumes", "%d container volume(s) no container mounts" % len(r.out.split()), None,
                               take=lambda: self.here.act_run(["podman", "volume", "prune", "-f"]).ok))
        return rows

    def ccache_rows(self):
        d = os.path.join(self.store.root(), "cache", "ccache")
        if not self.here.isdir(d) or not self.here.run(["ccache", "--version"]).ok:
            return []
        size = self.container().ccache_maxsize()
        argv = ["env", "CCACHE_DIR=" + d, "ccache"]
        return [rb.row("ccache", "ccache, down to %s" % size, rb.du_kb(self.here, d),
                       take=lambda: all([self.here.act_run(argv + ["--max-size=" + size]).ok,
                                         self.here.act_run(argv + ["--cleanup"]).ok]))]

    def runner_rows(self):
        """Tools/Scripts trees exported per WebKit commit: the newest stays, the rest are re-exported on demand."""
        d = os.path.join(self.store.artifact_dir(), "bench-runner")
        if not self.here.isdir(d):
            return []
        order = self.here.run(["ls", "-1At", d]).out.split()
        newest = next((n for n in order if not n.startswith(".tmp-")), None)
        return [rb.row("runner", "benchmark runner tree %s" % n, rb.du_kb(self.here, os.path.join(d, n)),
                       take=rb.remover(self.here, os.path.join(d, n))) for n in order if n != newest]

    def build_output_rows(self):
        rows, seen = [], set()
        for builder, p in sorted(build_outputs(self.store, self.env).items()):
            if p and p not in seen and self.here.isdir(p):
                seen.add(p)
                rows.append(rb.row("build-output", "%s build output %s" % (builder, p), rb.du_kb(self.here, p),
                                   "--purge-builds", rb.remover(self.here, p)))
        return rows

    def board_rows(self):
        rows, rch = [], None
        for name, conf in self.boards():
            dest = conf.get("NODE_BENCH_SSH") or conf.get("NODE_SSH") or name
            rch = rch or self.reach()
            why = rch.offline(dest)
            m = None if why else self.board_machine(dest)
            r = m.run(["ls", "-1A", SLOTS_DIR]) if m else None
            if r is not None and r.rc == 255:
                why = "%s did not answer over ssh" % dest
            if why:
                rows.append(rb.row("board-slot", "%s: the slots on its bench system" % name, None, why="not looked at -- " + why))
                continue
            for s in (r.out.split() if r.ok else []):
                d = SLOTS_DIR + "/" + s
                if s.endswith(".part"):
                    rows.append(rb.row("board-slot", "%s: slot copy %s, its deploy never finished" % (name, s), rb.du_kb(m, d),
                                       take=rb.remover(m, d)))
                elif s.endswith(images.INSTR_SUFFIX):
                    rows.append(rb.row("board-slot", "%s: instrumented slot %s" % (name, s), rb.du_kb(m, d), "--purge-rubble",
                                       rb.remover(m, d)))
        return rows

    def remote_rows(self):
        rows = []
        for t in self.remotes():
            ok, why = t.answers()
            if not ok:
                rows.append(rb.row("remote-mirror", "%s: its store's mirror" % t.name, None,
                                   why="not looked at -- %s did not answer: %s" % (t.name, why)))
                continue
            kb = rb.du_kb(t.machine, t.root_there() + "/git/WebKit.git")
            if kb is not None:
                rows.append(rb.row("remote-mirror", "%s: its store's mirror" % t.name, kb, "wk machine rm %s" % t.name))
        return rows

    def print_rows(self):
        for r in self.rows():
            sys.stdout.write(rb.to_line(r) + "\n")
        return 0

    def run(self, flags):
        with self.lock().held("store"):
            return self._run(flags)

    def _run(self, flags):
        rows = self.rows()
        refused = [r for r in rows if r.flag in flags and r.take and r.why]
        if refused:
            die("\n    ".join("%s: %s" % (r.what, r.why) for r in refused) + "\n    Nothing was changed.")
        info("what wk left on this machine, and what takes it")
        for r in rows:
            sys.stderr.write(rb.line(r, flags, "goes") + "\n")
        chosen = [r for r in rows if rb.takes(r, flags)]
        if not chosen:
            info("nothing to reclaim")
            return 0
        kbs = [r.kb for r in chosen if r.kb is not None]
        if not act.confirm("remove the %d item(s) marked 'goes' (%s at least)?" % (len(chosen), rb.size(sum(kbs)))):
            die("nothing was changed")
        rc = 0
        if any(r.take == rb.FAR for r in chosen):
            rc, out = self.container().wk("gc", "--yes", *flags)   # answered once, here
            sys.stderr.write(out)
            if rc:
                warn("the podman VM's half exited %d, so the mirror it borrows is kept" % rc)
                chosen = [r for r in chosen if r.kind != "mirror"]
        for r in chosen:
            if r.take == rb.FAR:
                continue
            why = ""
            try:
                ok = r.take() is not False
            except OSError as e:
                ok, why = False, ": %s" % e
            except act.Refused:
                ok = False   # its refusal is printed above
            if not ok:
                warn("could not reclaim %s%s" % (r.what, why))
                rc = rc or 1
        if self.store_half:
            self.fstrim()
        info("gc complete")
        return rc

    def fstrim(self):
        """A sparse disk image returns freed blocks to its host only when the guest discards them."""
        if not self.here.run(["sudo", "-n", "true"]).ok:
            log("  skipping fstrim (it needs a password) -- 'sudo fstrim -av' returns the freed space")
            return
        if not self.here.act_run(["sudo", "-n", "fstrim", "-av"]).ok:
            warn("fstrim failed; the freed space may not return to the host")
