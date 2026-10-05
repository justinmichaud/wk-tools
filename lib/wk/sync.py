"""`wk sync`: each place's furniture, this machine's mirror, each place's snapshot, then a fetch per
workspace that reads the checkout's wiring back in the same round trip, `--fix` re-asserting it first."""

import contextlib
import os
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor

from wk import act, git, kv, pr, secrets
from wk.act import Refused, debug, die, info, log, warn
from wk.store import Snapshots, Store, in_vm

SCOPE_FLAGS = ("--all", "--tools", "--on", "--mirror")
FETCH_JOBS = 16
FIX_AGAIN = "'wk sync <ws> --fix' re-asserts the wiring"


def where(in_workspace, args):
    for a in args:
        if a.split("=")[0] in SCOPE_FLAGS:
            return "host"
        if not a.startswith("-"):
            return "workspace"
    return "workspace" if in_workspace else "host"


def snapshot_current(recorded, mirror_sha):
    return bool(recorded) and recorded == mirror_sha


def publish_branch(env):
    return env.get("WK_BRANCH") or "origin/main"


def fetch_script(src, mirror):
    out = "cd %s || exit 1\nrc=0\ngit fetch --all --prune --quiet || rc=1\n" % shlex.quote(src)
    if mirror:
        test = "git config --get-all %s" % shlex.quote("url.%s.insteadOf" % mirror)
    else:
        test = "git config --get-regexp %s" % shlex.quote(r"^url\..*\.insteadof$")
    out += 'if [ -n "$(%s 2>/dev/null)" ]; then echo from=mirror; else echo from=github; fi\n' % test
    return out + "exit $rc\n"


def fetch_and_check_script(src, mirror, forks, branches):
    """The fetch, then the wiring check in a subshell: `fetch=` and `check=` carry each one's status."""
    fetch = fetch_script(src, mirror).replace("exit $rc\n", "echo fetch=$rc\n")
    return "%s(\n%s\n)\necho check=$?\n" % (fetch, git.wiring_check_script(src, mirror, forks, branches))


def fetch_into_mirror(here, store, lock, src, srcspec, dest):
    """The one entry point that fetches a named ref from `src` into this machine's mirror (`wk bench ab`'s PR and branch heads), made on first use, under the store lock."""
    if in_vm(store.env):
        die("the mirror in here is the host's, mounted read-only; run this on the host")
    mirror = store.mirror_dir()
    here.mkdir(os.path.dirname(mirror))
    with lock.held("store"):
        if not here.isdir(mirror):
            info("creating bare mirror (first run: this clones all of WebKit)")
            if not (here.act_run(["git", "init", "--bare", "-q", mirror]).ok
                    and here.act_run(["git", "-C", mirror, "config", "gc.auto", "0"]).ok):
                die("could not make the mirror at %s" % mirror)
        r = here.act_run(["git", "-C", mirror, "fetch", "--quiet", src, "+%s:%s" % (srcspec, dest)])
        if not r.ok:
            sys.stderr.write(r.err)
            die("could not fetch %s from %s into the mirror" % (srcspec, src), r.rc)


def fetch_pull_into_mirror(here, store, lock, remote, n, remotes=git.REMOTES):
    url = dict(remotes).get(remote)
    if not url:
        die("no such upstream remote '%s' to fetch a pull request from" % remote)
    fetch_into_mirror(here, store, lock, url, "refs/pull/%s/head" % n, "refs/remotes/pr/" + pr.pull_refname(remote, n))


@contextlib.contextmanager
def stage(clock, name):
    t0 = clock.monotonic()
    yield
    debug("stage %s: %ds" % (name, clock.monotonic() - t0))


class Sync:
    def __init__(self, reg, clock, lock, scope, only="", place="", fix=False):
        self.reg, self.clock, self.lock = reg, clock, lock
        self.here, self.root, self.env = reg.machine, reg.root, reg.env
        self.scope, self.only, self.place, self.fix = scope, only, place, fix
        self.branches = git.mirror_branches(self.env)
        self._forks = None

    def forks(self):
        if self._forks is None:
            self._forks = secrets.forks()
        return self._forks

    # -- the scope

    def places(self):
        if self.scope == "here":
            return self.reg.here()
        if self.place:
            return [self.place]
        return self.reg.walk()

    def touches_here(self):
        here = self.reg.here()
        return any(t in here for t in self.places())

    def mirror_is_here(self):
        return not in_vm(self.env)

    # The mirror is mounted read-only in a workspace and in the podman VM, so the refresh is asked of the broker, which runs `wk sync --mirror`.
    def mirror_refresh_request(self):
        store = Store(self.env)
        sock = store.workspace_runtime_socket() if self.reg.in_workspace() else store.runtime_socket()
        if not self.here.run(["test", "-S", sock]).ok:
            warn("no request broker at %s, so this machine's mirror was not\n"
                 "    refreshed -- only this workspace's own fetch ran, against whatever the\n"
                 "    mirror already had. Somebody with the workstation opens the door with:\n"
                 "        ./setup --stage broker     ('wk doctor' says whether it is reachable)\n"
                 "    The refresh itself, out there:  wk sync --mirror" % sock)
            return 1
        client = os.path.join(str(self.root), "container", "broker", "wk-broker-client.py")
        if not self.here.exists(client):
            die("this workspace's copy of wk-tools has no broker client\n    (%s). Refresh it:  wk sync --tools container   on the workstation." % client)
        r = self.here.act_run(["env", "WK_BROKER_SOCKET=" + sock, "python3", client, "sync"])
        sys.stderr.write(r.out + r.err)
        return 0 if r.ok else 1

    def load(self, name):
        try:
            return self.reg.load(name)
        except LookupError as e:
            die(str(e))

    # -- the run

    def run(self):
        if self.scope == "mirror":
            if self.reg.in_workspace() or not self.mirror_is_here():
                return self.mirror_refresh_request()
            return self.refresh_mirror()
        if self.scope == "ws":
            return self.sync_one()
        if self.place:
            self.load(self.place)
        rc = self.sync_furniture()
        if self.touches_here() and self.mirror_is_here():
            rc |= self.refresh_mirror()
        for t in self.places():
            rc |= self.sync_store_of(t)
        return rc

    def sync_one(self):
        rc = 0
        try:
            driver = self.load(self.reg.ws_place(self.only))
        except LookupError as e:
            die(str(e))
        if self.reg.in_workspace():
            rc = self.mirror_refresh_request()
        else:
            driver.store_init()
        return rc | self.fetch_workspaces(driver, [self.only])

    def sync_furniture(self):
        bad = 0
        for t in self.places():
            try:
                ok = self.load(t).sync(named=bool(self.place))
            except Refused:
                ok = False
            bad += 0 if ok else 1
        info("'wk status' compares every copy against this one")
        if bad:
            warn("%d place(s) did not take the tooling -- see above" % bad)
            return 1
        return 0

    def sync_store_of(self, name):
        driver = self.load(name)
        if driver.needs_base and not Store(self.env).is_local():
            return self.sync_in_vm(driver)
        rc = 0
        if driver.needs_base:
            try:
                with self.lock.held("store"):
                    self.sync_snapshot(driver)
            except Refused:
                rc = 1
            rc |= self.base_wiring(driver)
        if self.scope != "tools":
            rc |= self.sync_place(driver)
        return rc

    def sync_in_vm(self, driver):
        word = "--tools" if self.scope == "tools" else "--on"
        if driver.far_side() != "answering":
            warn("the podman machine is stopped, so %s's snapshot was not published and its\n"
                 "    workspaces did not fetch:  wk start, then  wk sync %s %s" % (driver.name, word, driver.name))
            return 1
        info("%s's snapshot and workspaces are in the podman VM -- syncing in there" % driver.name)
        rc, out = driver.wk("sync", word, driver.name, *(["--fix"] if self.fix else []))
        sys.stderr.write(out)
        return 0 if rc == 0 else 1

    # A peer is asked by name, never a scope word: what a scope means is its own copy of wk-tools to decide.
    def sync_place(self, driver):
        if driver.kind == "remote" and driver.peer:
            names = [n for n, _ in driver.list()]
            if not names:
                info("no workspaces on %s" % driver.name)
                return 0
            info("%s holds its own workspaces -- asking it to fetch in each" % driver.name)
            rc = 0
            for w in names:
                code, out = driver.wk("sync", w, *(["--fix"] if self.fix else []), env=dict(driver.env, WK_NO_DELEGATE="1"))
                sys.stderr.write(out)
                if code:
                    rc = 1
                    warn("%s did not fetch in '%s' -- see above%s" % (driver.name, w, (
                        "; a usage error is a copy of wk-tools older than 'wk sync --fix':\n"
                        "    wk sync --tools %s" % driver.name) if self.fix and code == 2 else ""))
            return rc
        names = driver.workspaces()
        if not names:
            info("no workspaces on %s" % driver.name)
            return 0
        return self.fetch_workspaces(driver, names)

    # -- the mirror and the snapshot

    def refresh_mirror(self):
        with self.lock.held("store"):
            moved = self.sync_mirror()
        return self.remount_guests() if moved else 0

    def sync_mirror(self):
        mirror = self.reg.store.mirror_dir()
        refs = self.mirror_refs(mirror)
        self.here.mkdir(os.path.dirname(mirror))
        if self.here.isdir(mirror):
            debug("ok: mirror exists")
        else:
            info("creating bare mirror (first run: this clones all of WebKit)")
        branches = self.branches
        info("fetching %s (origin: %s)" % (" ".join(r[0] for r in git.REMOTES), " ".join(branches)))
        with stage(self.clock, "mirror fetch"):
            r = self.here.act_run(["sh", "-c", git.mirror_refresh_script(mirror, branches)])
        if not r.ok:
            warn("the mirror refresh did not finish")
        for line in r.out.splitlines():
            f = line.split()
            if len(f) == 3 and f[0] == "mirror-fetch":
                log("  %-8s %s" % (f[1], "ok" if f[2] == "ok" else "FAILED (continuing)"))
        if act.dry_run():
            return True

        def has(b):
            return self.here.run(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/heads/" + b]).ok
        if not has("main"):
            die("main was not fetched into the mirror; nothing can be published from it")
        # The refresh is the one producer of what every workspace's refspecs ask for, so a branch still absent is one origin does not advertise.
        gap = [b for b in branches if not has(b)]
        if gap:
            warn("origin advertises no %s, so the mirror carries none of it and every\n"
                 "    workspace wired to ask for it fails its fetch. An image configuration names\n"
                 "    it (image/configs, CFG_BRANCH); 'wk doctor' reports the mirror the same way." % " ".join(gap))
        return self.mirror_refs(mirror) != refs

    def mirror_refs(self, mirror):
        return self.here.run(["git", "-C", mirror, "for-each-ref", "--format=%(objectname) %(refname)"]).out

    def remount_guests(self):
        """A running guest's mirror share holds the inode of each ref the refresh renamed over; one that cannot remount is named."""
        if "vm" not in self.reg.all():
            return 0
        vm = self.load("vm")
        for ws, st in vm.list():
            if st != "running":
                continue
            why = vm.remount_mirror(ws)
            if why:
                warn("'%s' could not remount the mirror share (%s), so it reads the mirror as it\n"
                     "    was before this refresh and every fetch in it fails:  wk stop %s, then  wk start %s" % (ws, why, ws, ws))
        return 0

    def git(self, tree, *args):
        return self.here.run(["git", "-C", tree] + list(args))

    def snapshot_checkout(self, tree, branch):
        """Why `branch` cannot be checked out, or "". `-B` resets: a hardlinked snapshot inherits the last one's branch at its old sha."""
        r = self.git(tree, "rev-parse", "--symbolic-full-name", branch)
        ref = "refs/remotes/" + branch if act.dry_run() else (r.out.strip() if r.ok else "")
        parts = ref.split("/")
        if len(parts) < 4 or parts[:2] != ["refs", "remotes"]:
            return ("'%s' is not a branch this mirror carries.\n"
                    "    A snapshot is published from a remote-tracking branch, spelled\n"
                    "    <remote>/<branch>:  origin/main (the default), wpe/wpe-2.46.\n"
                    "    Only %s of origin is in the mirror at all -- WK_MIRROR_BRANCHES=<branch>\n"
                    "    carries another one in." % (branch, " ".join(self.branches)))
        upstream = "/".join(parts[2:])
        local = "/".join(parts[3:])
        if not self.here.act_run(["git", "-C", tree, "checkout", "--quiet", "-B", local, ref]).ok:
            return "could not check %s out as branch %s" % (upstream, local)
        if not self.here.act_run(["git", "-C", tree, "branch", "--quiet", "--set-upstream-to", upstream, local]).ok:
            return "branch %s does not track %s" % (local, upstream)
        return ""

    # `--shared`: the snapshot borrows the mirror's objects, and every workspace overlaid on it borrows them too.
    def sync_snapshot(self, driver):
        driver.store_init()
        store, here = driver.store, self.here
        mirror = store.mirror_dir()
        if not here.isdir(mirror):
            die("no mirror at %s to publish a snapshot from -- 'wk sync' on the host makes it" % mirror)
        main_sha = self.git(mirror, "rev-parse", "refs/heads/main").out.strip()
        branch = publish_branch(self.env)
        new_id = self.clock.stamp()
        new_dir = os.path.join(store.snapshots_dir(), new_id)
        new_tree = os.path.join(new_dir, "WebKit")
        bases = Snapshots(store, here)
        prev = bases.newest_complete()
        if branch == "origin/main" and prev and not bases.verify(prev):
            try:
                recorded = here.read(store.snapshot_sha_file(prev)).strip()
            except OSError:
                recorded = ""
            if snapshot_current(recorded, main_sha):
                debug("ok: mirror unchanged; base %s is already %s" % (prev, main_sha[:10]))
                return

        def fail(why):
            here.remove(new_dir)
            die(why)
        if here.isdir(new_dir):
            here.remove(new_dir)
        here.mkdir(new_dir)
        if prev:
            info("snapshotting %s -> %s" % (prev, new_id))
            with stage(self.clock, "snapshot publish (cp -al)"):
                r = here.act_run(["cp", "-al", store.snapshot_tree(prev), new_tree])
        else:
            info("no previous snapshot; checking out %s (this takes a few minutes)" % branch)
            with stage(self.clock, "snapshot publish (clone)"):
                r = here.act_run(["git", "clone", "--quiet", "--shared", mirror, new_tree])
        if not r.ok:
            fail("could not make snapshot %s: %s" % (new_id, r.err.strip()))
        # Wired before the fetch, so the fetch reads this machine's mirror and a workspace overlaid on the tree inherits the wiring.
        info("wiring snapshot remotes for workspace use")
        if not here.act_run(["sh", "-c", git.wiring_script(new_tree, mirror, self.forks(), self.branches)]).ok:
            fail("could not wire snapshot %s" % new_id)
        if not here.act_run(["git", "-C", new_tree, "fetch", "--all", "--prune", "--quiet"]).ok:
            fail("snapshot %s could not fetch from %s" % (new_id, mirror))
        why = self.snapshot_checkout(new_tree, branch)
        if why:
            fail(why)
        head = self.git(new_tree, "symbolic-ref", "--short", "HEAD").out.strip() or branch.split("/", 1)[-1]
        info("checked out on branch %s (tracking %s)" % (head, branch))
        # A stray file in the lower layer appears in every workspace built from this snapshot and cannot be deleted from any of them.
        here.act_run(["git", "-C", new_tree, "reset", "--hard", "--quiet"])
        here.act_run(["git", "-C", new_tree, "clean", "-qfdx"])
        sha = self.git(new_tree, "rev-parse", "HEAD").out.strip()
        if not act.dry_run() and not sha:
            fail("snapshot %s has no HEAD to record" % new_id)
        # `sha` is the completion marker, written last; `branch` goes first, so a complete snapshot has everything beside it.
        here.write(os.path.join(new_dir, "branch"), branch + "\n")
        here.write(store.snapshot_sha_file(new_id), sha + "\n")
        info("published base %s (%s)" % (new_id, sha[:10]))

    def base_wiring(self, driver):
        """The current snapshot is where every future workspace gets its remotes from."""
        base = Snapshots(driver.store, self.here).current()
        tree = driver.store.snapshot_tree(base) if base else ""
        if not tree or not self.here.isdir(os.path.join(tree, ".git")):
            return 0
        mirror = driver.store.mirror_dir()
        r = self.here.run(["sh", "-c", git.wiring_check_script(tree, mirror, self.forks(), self.branches, "skip-env")])
        if r.ok:
            return 0
        warn("the snapshot %s is wired wrong, and every new workspace starts from it:\n%s"
             % (base, "".join("    - %s\n" % l[len("problem: "):] for l in r.out.splitlines() if l.startswith("problem: ")).rstrip("\n")))
        if not self.fix:
            log("  re-assert it:  wk sync --on %s --fix" % driver.name)
            return 1
        if not self.here.act_run(["sh", "-c", git.wiring_script(tree, mirror, self.forks(), self.branches)]).ok:
            warn("could not re-wire the snapshot %s" % base)
            return 1
        info("re-wired the snapshot %s" % base)
        return 0

    # -- the workspaces

    def fetch_workspaces(self, driver, names):
        info("fetching in %d workspace(s) -- %s" % (len(names), " ".join(r[0] for r in git.REMOTES)))
        with ThreadPoolExecutor(max_workers=min(len(names), FETCH_JOBS)) as pool:
            results = list(pool.map(lambda ws: self.fetch_one(driver, ws), names))
        failed = wired = 0
        for code, text in results:
            sys.stderr.write(text)
            if code == "skipped" and self.scope == "ws":
                die("workspace '%s' is not there to fetch in\n    ('wk status %s' says what it is; 'wk ls' lists the ones there are)"
                    % (self.only, self.only))
            failed += code == "failed"
            wired += code == "wired"
        if failed:
            warn("%d workspace(s) did not fetch -- %s it\n    reads, and 'wk enter <ws>' then 'git fetch --all' says why" % (failed, FIX_AGAIN))
        if wired:
            warn("%d workspace(s) fetched but are wired wrong (above) -- %s" % (wired, FIX_AGAIN))
        return 1 if failed or wired else 0

    def fetch_one(self, driver, ws):
        """(ok | skipped | failed | wired, its lines): these run at once, so nothing is printed here."""
        try:
            st = driver.state(ws)
        except Refused:
            st = "unreachable"
        if st != "present":
            return "skipped", "  %-24s %s -- skipped\n" % (ws, st)
        src, mirror = driver.src(ws), driver.mirror_dir()
        notes = []
        fixed = self.fix_one(driver, ws, src, mirror, notes) if self.fix else True
        with stage(self.clock, "workspace fetch %s" % ws):
            r = driver.act_exec(ws, ["sh", "-c", fetch_and_check_script(src, mirror, self.forks(), self.branches)])
        lines = r.out.replace("\r", "").splitlines()
        said = kv.kv(r.out)
        problems = ["    - %s" % l[len("problem: "):] for l in lines if l.startswith("problem: ")]
        if said.get("fetch") != "0":
            return "failed", "  %-24s FAILED (continuing)\n%s" % (ws, "".join(p + "\n" for p in problems + notes))
        if said.get("from") == "mirror":
            row = "ok  (%s)" % mirror
        elif said.get("from") == "github":
            row = "ok  (over the network: this checkout reads no mirror%s)" % (
                ", though this machine keeps %s -- wk sync %s --fix" % (mirror, ws) if mirror else "")
        else:
            row = "ok  (it did not say which source it read)"
        if "check" not in said:
            problems.append("    - the wiring check did not report (it never reached its end)")
        if not fixed or said.get("check") != "0":
            return "wired", "  %-24s %s -- wired wrong:\n%s" % (ws, row, "".join(p + "\n" for p in notes + problems))
        return "ok", "  %-24s %s\n%s" % (ws, row, "".join(p + "\n" for p in notes))

    def fix_one(self, driver, ws, src, mirror, notes):
        """`git-webkit setup` runs only where the injector puts the credential it reads: a container or a guest."""
        n, u, c = driver.wiring_args()
        r = driver.act_exec(ws, ["sh", "-c", git.wiring_script(src, mirror, self.forks(), self.branches, n, u, c)])
        if not r.ok:
            notes.append("    could not re-wire '%s'" % ws)
            return False
        notes.append("    re-wired")
        notes.extend("    " + l for l in pr.retarget(driver, ws, src, self.forks(), self.branches) + pr.converge(driver, ws, src, self.forks()))
        if driver.kind not in ("container", "vm"):
            return True
        r = driver.act_exec(ws, ["sh", "-c", git.gitwebkit_setup_script(src, self.forks())])
        said = (r.out.replace("\r", "").strip().splitlines() or [""])[-1]
        if not r.ok:
            notes.extend("    " + l for l in r.err.replace("\r", "").splitlines() if l.strip())
            notes.append("    'git-webkit setup' did not finish (%s); 'wk key push on' if the read token\n"
                         "    is off, then 'wk sync %s --fix' again" % (said or "no answer", ws))
            return False
        notes.append("    git-webkit: %s" % said)
        return True
