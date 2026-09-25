"""`wk sync`: each target's furniture, this machine's mirror, each target's snapshot, then a fetch per
workspace that reads the checkout's wiring back in the same round trip, `--fix` re-asserting it first."""

import contextlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from wk import act, git, pr, secrets, shell
from wk.act import Refused, debug, die, info, log, warn
from wk.store import Bases, Store

SCOPE_FLAGS = ("--all", "--tools", "--target", "--machine", "--mirror")
FETCH_JOBS = 16
FIX_AGAIN = "'wk sync <ws> --fix' re-asserts the wiring"
BROKER_SOCKET = "/run/wk/broker.sock"


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
    out = "cd %s || exit 1\nrc=0\ngit fetch --all --prune --quiet || rc=1\n" % shell.sh_quote(src)
    if mirror:
        test = "git config --get-all %s" % shell.sh_quote("url.%s.insteadOf" % mirror)
    else:
        test = "git config --get-regexp %s" % shell.sh_quote(r"^url\..*\.insteadof$")
    out += 'if [ -n "$(%s 2>/dev/null)" ]; then echo from=mirror; else echo from=github; fi\n' % test
    return out + "exit $rc\n"


def fetch_and_check_script(src, mirror, forks, branches):
    """The fetch, then the wiring check in a subshell: `fetch=` and `check=` carry each one's status."""
    fetch = fetch_script(src, mirror).replace("exit $rc\n", "echo fetch=$rc\n")
    return "%s(\n%s\n)\necho check=$?\n" % (fetch, git.wiring_check_script(src, mirror, forks, branches))


@contextlib.contextmanager
def stage(clock, name):
    t0 = clock.monotonic()
    yield
    debug("stage %s: %ds" % (name, clock.monotonic() - t0))


class Sync:
    def __init__(self, reg, clock, lock, scope, only="", target="", fix=False):
        self.reg, self.clock, self.lock = reg, clock, lock
        self.here, self.root, self.env = reg.machine, reg.root, reg.env
        self.scope, self.only, self.target, self.fix = scope, only, target, fix
        self.branches = git.mirror_branches(self.env)
        self._forks = None

    def forks(self):
        if self._forks is None:
            self._forks = secrets.forks()
        return self._forks

    # -- the scope

    def targets(self):
        if self.scope == "here":
            return self.reg.here()
        if self.target:
            return [self.target]
        return self.reg.walk()

    def touches_here(self):
        here = self.reg.here()
        return any(t in here for t in self.targets())

    def mirror_is_here(self):
        return not self.env.get("WK_IN_VM")

    # The mirror is mounted read-only in a workspace, so the refresh is asked of the broker, which runs `wk sync --mirror`.
    def mirror_refresh_request(self):
        sock = self.env.get("WK_BROKER_SOCKET") or BROKER_SOCKET
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
            if self.reg.in_workspace():
                return self.mirror_refresh_request()
            if not self.mirror_is_here():
                die("the mirror in here is the host's, mounted read-only.\n    Run it on the machine that keeps it.")
            with self.lock.held("store"):
                self.sync_mirror()
            return 0
        if self.scope == "ws":
            return self.sync_one()
        if self.target:
            self.load(self.target)
        rc = self.sync_furniture()
        if self.touches_here() and self.mirror_is_here():
            with self.lock.held("store"):
                self.sync_mirror()
        for t in self.targets():
            rc |= self.sync_store_of(t)
        return rc

    def sync_one(self):
        rc = 0
        try:
            target = self.load(self.reg.ws_target(self.only))
        except LookupError as e:
            die(str(e))
        if self.reg.in_workspace():
            rc = self.mirror_refresh_request()
        else:
            target.store_init()
        return rc | self.fetch_workspaces(target, [self.only])

    def sync_furniture(self):
        bad = 0
        for t in self.targets():
            try:
                ok = self.load(t).sync(named=bool(self.target))
            except Refused:
                ok = False
            bad += 0 if ok else 1
        info("'wk status' compares every copy against this one")
        if bad:
            warn("%d target(s) did not take the tooling -- see above" % bad)
            return 1
        return 0

    def sync_store_of(self, name):
        target = self.load(name)
        if target.needs_base and not Store(self.env).is_local():
            return self.sync_in_vm(target)
        rc = 0
        if target.needs_base:
            try:
                with self.lock.held("store"):
                    self.sync_snapshot(target)
            except Refused:
                rc = 1
            rc |= self.base_wiring(target)
        if self.scope != "tools":
            rc |= self.sync_target(target)
        return rc

    def sync_in_vm(self, target):
        word = "--tools" if self.scope == "tools" else "--target"
        if target.far_side() != "answering":
            warn("the podman machine is stopped, so %s's snapshot was not published and its\n"
                 "    workspaces did not fetch:  wk start, then  wk sync %s %s" % (target.name, word, target.name))
            return 1
        info("%s's snapshot and workspaces are in the podman VM -- syncing in there" % target.name)
        rc, out = target.wk("sync", word, target.name, *(["--fix"] if self.fix else []))
        sys.stderr.write(out)
        return 0 if rc == 0 else 1

    # A peer is asked by name, never a scope word: what a scope means is its own copy of wk-tools to decide.
    def sync_target(self, target):
        if target.kind == "remote" and target.peer:
            names = [n for n, _ in target.list()]
            if not names:
                info("no workspaces on %s" % target.name)
                return 0
            info("%s holds its own workspaces -- asking it to fetch in each" % target.name)
            rc = 0
            for w in names:
                code, out = target.wk("sync", w, *(["--fix"] if self.fix else []), env=dict(target.env, WK_NO_DELEGATE="1"))
                sys.stderr.write(out)
                if code:
                    rc = 1
                    warn("%s did not fetch in '%s' -- see above%s" % (target.name, w, (
                        "; a usage error is a copy of wk-tools older than 'wk sync --fix':\n"
                        "    wk sync --tools %s" % target.name) if self.fix and code == 2 else ""))
            return rc
        names = target.workspaces()
        if not names:
            info("no workspaces on %s" % target.name)
            return 0
        return self.fetch_workspaces(target, names)

    # -- the mirror and the snapshot

    def sync_mirror(self):
        mirror = self.reg.store.mirror()
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
            return

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
    def sync_snapshot(self, target):
        target.store_init()
        store, here = target.store, self.here
        mirror = store.mirror()
        if not here.isdir(mirror):
            die("no mirror at %s to publish a snapshot from -- 'wk sync' on the host makes it" % mirror)
        main_sha = self.git(mirror, "rev-parse", "refs/heads/main").out.strip()
        branch = publish_branch(self.env)
        new_id = self.clock.stamp()
        new_dir = os.path.join(store.base_dir(), new_id)
        new_tree = os.path.join(new_dir, "WebKit")
        bases = Bases(store, here)
        prev = bases.newest_complete()
        if branch == "origin/main" and prev and not bases.verify(prev):
            try:
                recorded = here.read(store.base_sha_file(prev)).strip()
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
                r = here.act_run(["cp", "-al", store.base_path(prev), new_tree])
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
        here.write(store.base_sha_file(new_id), sha + "\n")
        info("published base %s (%s)" % (new_id, sha[:10]))

    def base_wiring(self, target):
        """The current snapshot is where every future workspace gets its remotes from."""
        base = Bases(target.store, self.here).current()
        tree = target.store.base_path(base) if base else ""
        if not tree or not self.here.isdir(os.path.join(tree, ".git")):
            return 0
        mirror = target.store.mirror()
        r = self.here.run(["sh", "-c", git.wiring_check_script(tree, mirror, self.forks(), self.branches, "skip-env")])
        if r.ok:
            return 0
        warn("the base snapshot %s is wired wrong, and every new workspace starts from it:\n%s"
             % (base, "".join("    - %s\n" % l[len("problem: "):] for l in r.out.splitlines() if l.startswith("problem: ")).rstrip("\n")))
        if not self.fix:
            log("  re-assert it:  wk sync --target %s --fix" % target.name)
            return 1
        if not self.here.act_run(["sh", "-c", git.wiring_script(tree, mirror, self.forks(), self.branches)]).ok:
            warn("could not re-wire the base snapshot %s" % base)
            return 1
        info("re-wired the base snapshot %s" % base)
        return 0

    # -- the workspaces

    def fetch_workspaces(self, target, names):
        info("fetching in %d workspace(s) -- %s" % (len(names), " ".join(r[0] for r in git.REMOTES)))
        with ThreadPoolExecutor(max_workers=min(len(names), FETCH_JOBS)) as pool:
            results = list(pool.map(lambda ws: self.fetch_one(target, ws), names))
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

    def fetch_one(self, target, ws):
        """(ok | skipped | failed | wired, its lines): these run at once, so nothing is printed here."""
        try:
            st = target.state(ws)
        except Refused:
            st = "unreachable"
        if st != "present":
            return "skipped", "  %-24s %s -- skipped\n" % (ws, st)
        src, mirror = target.src(ws), target.mirror_dir()
        notes = []
        fixed = self.fix_one(target, ws, src, mirror, notes) if self.fix else True
        with stage(self.clock, "workspace fetch %s" % ws):
            r = target.act_exec(ws, ["sh", "-c", fetch_and_check_script(src, mirror, self.forks(), self.branches)])
        lines = r.out.replace("\r", "").splitlines()
        said = dict(l.split("=", 1) for l in lines if l.startswith(("from=", "fetch=", "check=")))
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

    def fix_one(self, target, ws, src, mirror, notes):
        """`git-webkit setup` runs only where the injector puts the credential it reads: a container or a guest."""
        n, u, c = target.wiring_args()
        r = target.act_exec(ws, ["sh", "-c", git.wiring_script(src, mirror, self.forks(), self.branches, n, u, c)])
        if not r.ok:
            notes.append("    could not re-wire '%s'" % ws)
            return False
        notes.append("    re-wired")
        notes.extend("    " + l for l in pr.retarget(target, ws, src, self.forks(), self.branches))
        if target.kind not in ("container", "vm"):
            return True
        r = target.act_exec(ws, ["sh", "-c", git.gitwebkit_setup_script(src, self.forks())])
        said = (r.out.replace("\r", "").strip().splitlines() or [""])[-1]
        if not r.ok:
            notes.extend("    " + l for l in r.err.replace("\r", "").splitlines() if l.strip())
            notes.append("    'git-webkit setup' did not finish (%s); 'wk push on' if the read token\n"
                         "    is off, then 'wk sync %s --fix' again" % (said or "no answer", ws))
            return False
        notes.append("    git-webkit: %s" % said)
        return True
