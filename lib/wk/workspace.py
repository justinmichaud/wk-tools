"""`wk new` and `wk rm` as flows over a Driver, a Records, a Lock and a Clock. `new`'s front refuses, detaches and
follows the record; the detached run changes everything, under the workspace lock (inline under --dry-run)."""

import json
import os
import re
import shlex
import sys

from wk import act, git, job, project, record, repos, resources, sshalias
from wk.act import Refused, die, info, log, warn
from wk.machine import in_podman_machine
from wk.store import Snapshots, no_such_workspace
from wk.places import show

PLAN = ("checking", "wipe", "base", "create", "init", "agents", "fetch", "register")
NEW_TIMEOUT = 3600
NAME = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._-]*$")
REFUSALS = {
    "present": "workspace '{n}' already exists (place '{t}').\n    'wk rm {n}' first, or pick another name.",
    "broken": "'{n}' is a record without an environment: creation finished, and the\n    {t} side of it is gone -- something "
              "outside wk removed it.\n    'wk status {n}' says so:  wk rm {n}   then 'wk new {n}' to start again",
    "unreachable": "cannot reach the machine behind place '{t}', so whether '{n}' is\n    already there cannot be known. "
                   "Try again when the machine answers.",
}

def require_name(name):
    if not NAME.match(name or ""):
        die("invalid name '%s': use [a-zA-Z0-9._-], not starting with '-'" % (name or ""))


def wk_of(root):
    return os.path.join(str(root), "wk")


def rm_argv(root, place, name):
    return ["env", "WK_PLACE=%s" % place, wk_of(root), "rm", name, "--yes"]


def task_records_for(records, name):
    return [t for t in records.list() if t.field("name") == name]


def task_lines(tasks):
    """One line per job, with the command that stops it."""
    return "\n".join("    %s (pid %s on %s)  stop it:  %s" % (t.field("kind"), t.field("pid"), t.field("machine"), t.field("kill"))
                     for t in tasks)


def live_task_lines(records, name):
    return task_lines(t for t in task_records_for(records, name) if t.alive(None))


def remove_task_records(records, name):
    for t in task_records_for(records, name):
        records.machine.remove(str(t.path))


def creation_state(driver, records, name):
    """`Driver.state`, except that a present workspace whose detached run failed or died without a verdict is still
    creating: it was never announced ready, so nothing in it is worth keeping and "already exists" is not the answer."""
    state = driver.state(name)
    if state != "present":
        return state
    t = records.find("new", name)
    rc = None if t is None else t.raw("exit")
    if t is not None and (t.alive() is False if rc is None else rc.isdigit() and rc != "0"):
        return "creating"
    return state


def new_front(reg, records, name, opts, prflow):
    """Refuse, start the detached run, follow it, then the --pr (`prflow`: lib/wk/pr.py) / hints / --zed tail. 0, or Refused."""
    arch = new_asked(name, opts, prflow)
    driver, repo = new_place(reg, name, opts)
    if driver.kind == "remote" and (driver.peer or not driver.is_local):
        return new_handed_over(driver, reg.machine, reg.root, name, arch, opts)
    recs = record.of_driver(driver, records.clock, records.machine)
    if opts.get("kill"):
        return new_kill(driver, reg.machine, recs, reg.env, name, opts)
    new_refuse_existing(driver, recs, name, arch)
    if act.dry_run():
        from wk.lock import Lock
        return new_detached_run(driver, recs, Lock(driver.store, reg.machine, recs.clock), recs.clock, name,
                                opts.get("base") or "", arch, repo)
    pid, since = new_detach(driver, reg, name, arch, repo, opts.get("base") or ""), recs.clock.stamp()
    if opts.get("no_wait"):
        info("creating '%s' on %s, detached as pid %d -- this end can go away" % (name, driver.name, pid))
        log("  follow:  wk status %s --log --follow" % name)
        log("  state:   wk status %s" % name)
    else:
        new_follow(driver, recs, reg.env, name, pid, since)
        info("workspace '%s' ready%s" % (name, "" if arch == "native" else " (%s)" % arch))
        if opts.get("pr"):
            prflow.checkout(driver, reg.machine, name, opts["pr"])
        new_hints(driver, name, arch)
    zed_after(reg.machine, reg.root, name, opts)
    return 0


def new_asked(name, opts, prflow):
    """The refusals of what was asked, before any place is read; the canonical arch."""
    require_name(name)
    pr = opts.get("pr")
    if pr is not None and not pr:
        die("--pr needs a spec: <user>:<branch>, <n>, or wpe:<n>")
    if pr and opts.get("no_wait"):
        die("--pr needs the workspace to be ready, and --no-wait returns before it is.\n"
            "    Drop --no-wait, or check it out afterwards:  wk pr %s %s" % (name, pr))
    if pr:
        prflow.parse_spec(pr)
    return resources.arch_canon(opts.get("arch") or "native")


def new_place(reg, name, opts):
    """(driver, repo) of the place asked for, refusing what it cannot make."""
    tname = opts.get("place") or reg.default()
    try:
        driver = reg.load(tname)
        repo = repos.Repo(opts.get("repo") or project.get("REPO"))
    except LookupError as e:
        die(str(e))
    if repo.name != project.get("REPO") and driver.kind != "container":
        die("--repo %s is container-only for now (this is place '%s'): a guest or a build\n"
            "    box makes its checkout only from a %s mirror." % (repo.name, tname, project.get("CHECKOUT")))
    if driver.kind == "local":
        die("a workspace cannot create a workspace -- run 'wk new %s' on the host" % name)
    return driver, repo


def new_refuse_existing(driver, recs, name, arch):
    if arch != "native" and driver.kind != "container":
        die("--arch %s is a container-only capability (this is place '%s')." % (arch, driver.name))
    driver.store_init()
    st = creation_state(driver, recs, name)
    if st in ("present", "broken"):
        die(REFUSALS[st].format(n=name, t=driver.name))
    t = recs.find("new", name) if st == "creating" else None
    if t is not None and t.alive(None):
        die("'%s' is already being created (pid %s, at stage\n    '%s'). Follow it:      tail -f %s\n"
            "    Or ask for it and wait:  wk enter %s --zed   /   wk build %s <preset>"
            % (name, t.field("pid"), " ".join(t.stage()), t.field("log"), name, name))


def new_detach(driver, reg, name, arch, repo, base):
    return job.detach(reg.machine, [wk_of(reg.root), "new", name, "--on", driver.name, "--arch", arch, "--repo", repo.name]
                      + (["--base", base] if base else []) + ["--_detached"], driver.create_log(name))


def new_follow(driver, recs, env, name, pid, since):
    """The detached run followed to its verdict; anything but ok dies naming what to do."""
    tname, timeout = driver.name, job._seconds(env, "WK_NEW_TIMEOUT", NEW_TIMEOUT)
    st = recs.wait("new", name, driver.create_log(name), timeout=timeout, pid=pid, floor=since, stream=sys.stderr)
    if st == "crashed":
        die("the process creating '%s' is gone without having said how it ended.\n"
            "    A re-run destroys what it left and starts again:  wk new %s --on %s   (its log: wk status %s --log --all)"
            % (name, name, tname, name))
    if st == "refused":
        die("'%s' was not created, and nothing was left half-made: the reason is\n    above, in full in 'wk status %s --log --all'" % (name, name))
    if st == "timeout":
        die("'%s' is still being created after %ds. Nothing here was undone.\n"
            "    'wk status %s' says where it is; 'wk status %s --log', what it is doing." % (name, timeout, name, name))
    if st != "ok":
        die("creating '%s' failed (%s) -- the reason is above, in full in 'wk status %s --log --all'.\n"
            "    A re-run destroys what is there and starts again:  wk new %s --on %s" % (name, st, name, name, tname))


def zed_after(here, root, name, opts):
    if not opts.get("zed"):
        return
    if opts.get("no_wait"):
        log("  open it:  wk enter %s --zed   (waits for it to be ready)" % name)
        return
    r = here.act_run([os.path.join(str(root), "cmd", "zed"), name])
    show(r)
    if not r.ok:
        warn("'%s' is there; opening it in Zed is what failed (above) -- 'wk zed %s' retries" % (name, name))


def new_handed_over(driver, here, root, name, arch, opts):
    """A machine that runs wk for itself makes its own workspaces and keeps their records: its `wk new`, at its own default place."""
    if driver.is_local:
        die("'%s' is this machine, and its workspaces are made at its own default\n"
            "    place:  wk new %s" % (driver.name, name))
    if opts.get("base"):
        die("--base names a snapshot in this machine's store, and %s makes '%s' from its own:\n"
            "    drop --base" % (driver.name, name))
    args = [name] + (["--arch", arch] if arch != "native" else [])
    if opts.get("pr"):
        args += ["--pr", opts["pr"]]
    args += [flag for key, flag in (("no_wait", "--no-wait"), ("kill", "--kill")) if opts.get(key)]
    r = here.act_run(driver.hand_over("new", args, tty=os.isatty(0) and os.isatty(1)), tty=True)
    if not r.ok:
        die("%s did not create '%s'; what its own wk said is above." % (driver.name, name), status=r.rc)
    zed_after(here, root, name, opts)
    return 0


def new_hints(driver, name, arch):
    if arch != "native":
        log("  %s: native 32-bit, no GPU. 'wk bench' will run CPU-class plans" % arch)
        log("  in here and refuse GPU-class ones.")
    if driver.kind == "vm":
        log("  wk start %s       boot it (its ssh alias is written then)" % name)
        log("  wk zed %s            the checkout, in Zed (once it is up)" % name)
        log("  wk build %s %s" % (name, project.get("MAC_PRESET")))
    elif driver.kind == "remote":
        log("  wk build %s <preset> build (polite: sized from that machine's load)" % name)
        log("  wk enter %s          shell, in the checkout" % name)
        if driver.host:
            log("  wk zed %s            the checkout, in Zed (ssh://%s%s)" % (name, driver.host, driver.src(name)))
        log("")
        log("  no sandbox on a shared machine, so 'wk ai claude' and 'wk doctor <ws>' refuse.")
    else:
        log("  wk enter %s          shell" % name)
        log("  wk build %s <preset> build" % name)
        log("  wk ai claude %s      sandboxed agent" % name)
        log("  wk zed %s            the checkout, in Zed" % name)


def new_kill(driver, here, records, env, name, opts):
    if any(opts.get(k) for k in ("pr", "zed", "no_wait", "base", "repo")):
        die("'wk new %s --kill' stops the creation already running and takes\n"
            "    nothing with it -- no --base, --repo, --zed, --no-wait or --pr." % name)
    stopped = job.stop(driver, records, name, "new", here, records.clock, env)
    if stopped == 1:
        die("the process creating '%s' outlived a TERM and a KILL. It holds the workspace lock:\n"
            "        ps -p %s" % (name, records.find("new", name).field("pid")))
    if stopped == 0:
        log("  what it got as far as is half-made and nothing in one is worth\n  keeping:  wk rm %s    (then 'wk new %s' to start again)" % (name, name))
    return 0


def new_detached_run(driver, records, lock, clock, name, base, arch, repo):
    """PLAN's steps under the workspace lock: a refusal ends the record `refused`, any other failure 1."""
    here = records.machine
    if driver.kind == "container":
        with lock.held("sdk"):
            driver.sdk_refresh()
    state = creation_state(driver, records, name)
    task = records.claim("new", name, "wk new %s --kill" % name, driver.create_log(name), list(PLAN), ["ws:" + name],
                         lambda _, held: die("'%s' has work running in it:\n%s" % (name, task_lines(held))))
    if driver.reads_host_mirror and repo.snapshot:
        refresh_mirror(driver, here, name)
    if driver.needs_base and repo.snapshot:
        lock.hold("store")
    try:
        _create(driver, records, task, clock, name, base, arch, repo, state)
    except Exception as e:
        if task is not None:
            task.end(e.status if isinstance(e, Refused) else 1)
        lock.release_all()
        raise
    if task is not None:
        task.end(0)
    lock.release_all()
    if task is not None:
        info("workspace '%s' created" % name)
    return 0


# Before the store lock, which `wk sync --mirror` takes for itself; a refresh that moves a ref remounts each running guest's shares.
def refresh_mirror(driver, here, name):
    r = here.act_run([wk_of(driver.root), "sync", "--mirror"])
    show(r)
    if not r.ok:
        die("the mirror refresh did not finish (above), so '%s' was not created.\n    Fix that:  wk sync --mirror   then  wk new %s" % (name, name))


def _create(driver, records, task, clock, name, base, arch, repo, state):
    here, tname = records.machine, driver.name

    def stage(step):
        if task is not None:
            task.step_named(step)

    def refuse(msg):
        if task is not None:
            task.end("refused")
        die(msg)

    stage("checking")
    if state in REFUSALS:
        refuse(REFUSALS[state].format(n=name, t=tname))
    if state == "creating":
        stage("wipe")
        warn("'%s' exists but was never finished -- destroying it and starting again" % name)
        driver.destroy(name)
        left = leftovers(driver, here, name)
        if left:
            die("could not destroy the half-made workspace '%s'; still here:%s\n"
                "    'wk rm %s' retries exactly that, then 'wk new %s'" % (name, left, name, name))
        sshalias.alias_remove(here, driver.env, name)
    if driver.needs_base and repo.snapshot:
        stage("base")
        mirror = driver.store.mirror_dir()
        if not here.isdir(mirror):
            die("no %s mirror at %s:\n    wk sync    makes it, then publishes a snapshot to build from." % (project.get("CHECKOUT"), mirror))
        bases = Snapshots(driver.store, here)
        base = base or bases.current()
        if not base:
            die("no snapshot this machine can build a workspace from:  wk sync\n    publishes one.")
        why = bases.verify(base)
        if why:
            die(why)
    stage("create")
    driver.create(name, base, arch, repo)
    stage("init")
    if not act.dry_run():
        driver.wait_ready(name, clock, own=True)
    stage("agents")
    driver.install_agents(name)
    stage("fetch")
    freshen(driver, name, here, repo)
    stage("register")


def freshen(driver, name, here, repo):
    """Fetch a snapshot repo's checkout from its mirror and fast-forward onto it, then report the checkout. Never fatal."""
    if act.dry_run():
        if repo.snapshot:
            here.act_run([wk_of(driver.root), "sync", name])
        return
    if repo.snapshot and not fetch_from_mirror(driver, name, here):
        return
    report_checkout(driver, name)


def fetch_from_mirror(driver, name, here):
    wk = wk_of(driver.root)
    mirror = shlex.quote(driver.mirror_dir())
    r = driver.exec(name, ["sh", "-c", "[ -n %s ] && [ -d %s ] && echo yes || echo no" % (mirror, mirror)])
    lines = r.out.replace("\r", "").splitlines() if r.ok else []
    source = {"yes": "mirror", "no": "network"}.get(lines[-1].strip() if lines else "", "unreachable")
    if source == "mirror":
        r = here.act_run([wk, "sync", name])
        show(r)
        if not r.ok:
            warn("'%s' is there; fetching in it is what failed (above).\n"
                 "    'wk sync %s' retries; the checkout is at the snapshot it was made from." % (name, name))
    elif source == "network":
        info("no mirror in reach of '%s', so its checkout is as old as what it was made from" % name)
        log("  wk sync %s    fetches it over the network" % name)
    else:
        info("nothing to run in '%s' yet, so its checkout was not fetched in" % name)
        log("  wk sync %s    once it is up" % name)
        return False
    return True


def report_checkout(driver, name):
    """Fast-forward the checkout onto what its branch tracks, and say where it is."""
    src = driver.src(name)
    said = git.probe(lambda argv: driver.exec(name, argv), src)
    b, u, head = said.get("branch"), said.get("tracks"), said.get("head", "")
    moved = ""
    if b and u and said.get("behind", "0") != "0":
        moved = said["behind"] if driver.exec(name, ["git", "-C", src, "merge", "--ff-only", "--quiet", u]).ok else "refused"
        head = git.probe(lambda argv: driver.exec(name, argv), src).get("head", head)
    if not b and head:
        warn("'%s' is not on a branch (detached at %s), so 'git status',\n"
             "    'git pull' and 'wk pr rebase' in there have no upstream to name:\n"
             "        git checkout main    in the workspace" % (name, head))
    elif not b:
        warn("could not read the checkout in '%s' to say where it is\n    ('wk enter %s', then 'git status' in there)" % (name, name))
    elif not u:
        warn("'%s' is on '%s', which tracks nothing -- 'git pull' and\n    'wk pr' have no upstream to name:  wk sync %s --fix" % (name, b, name))
    elif moved == "refused":
        warn("'%s' in '%s' and %s have diverged, so the checkout is left where\n    it is:  git rebase %s   in the workspace" % (b, name, u, u))
    elif not moved:
        info("'%s' is on %s at %s, up to date with %s" % (name, b, head, u))
    else:
        info("'%s' is on %s at %s, %s commit(s) on from its snapshot (%s)" % (name, b, head, moved, u))


def rm_plan(reg, records, name):
    """(driver, "workspace" | "record"), mutating nothing; Refused(1) absent, Refused(2) unreachable."""
    try:
        tname = reg.ws_place(name)
        driver = reg.load(tname)
    except LookupError as e:
        die(str(e))
    if reg.machine.isdir(driver.store.ws_dir(name)):
        return driver, "workspace"
    ok, why = driver.answers()
    if not ok:
        act.err("'%s' has no record here, and %s did not answer: %s\n"
                "    Nothing this end can see is there to destroy; re-run once %s answers." % (name, tname, why, tname))
        raise Refused(2)
    if driver.info(name) != "absent":
        return driver, "workspace"
    for other in [tname] + [o for o in reg.all() if o != tname]:
        try:
            t = driver if other == tname else reg.load(other)
        except LookupError:
            continue
        if record.of_driver(t, records.clock, records.machine).find("new", name) is not None:
            return t, "record"
    raise Refused(1)


def leftovers(driver, here, name):
    if act.dry_run():
        return ""
    left = ""
    if driver.info(name) != "absent":
        left += " the %s environment" % driver.name
    ws = driver.store.ws_dir(name)
    if here.isdir(ws):
        left += " " + ws
    return left


def _forget(driver, records, here, name):
    """The alias and the log first and the record last: a record that outlives them is what a re-run finds."""
    sshalias.alias_remove(here, driver.env, name)
    here.remove(driver.create_log(name))
    remove_task_records(records, name)


def rm_one(driver, records, lock, name, what):
    """Destroy one workspace, or forget one that is nothing but a record, with no claim starting meanwhile. 0 destroyed, 1 not fully."""
    recs = record.of_driver(driver, records.clock, records.machine)
    with recs.locked():
        return _rm_one(driver, recs, lock, name, what)


def _rm_one(driver, recs, lock, name, what):
    here = recs.machine
    if what == "record":
        _forget(driver, recs, here, name)
        info("'%s' had nothing left but its record; forgotten" % name)
        return 0
    if driver.kind == "local":
        driver.destroy(name)
    live = live_task_lines(recs, name)
    if live:
        die("'%s' has work running in it, and destroying it under a running job\n"
            "    leaves that job compiling into a directory that is gone:\n%s" % (name, live))
    if driver.needs_base:
        lock.hold("store")
    ws = driver.store.ws_dir(name)
    changes = os.path.join(ws, "changes")
    if here.isdir(changes):
        n = len(here.run(["find", changes, "-type", "f"]).out.splitlines())
        if n:
            warn("%s has %d modified file(s) in its overlay" % (name, n))
    driver.destroy(name)
    left = leftovers(driver, here, name)
    if left:
        warn("'%s' was not fully destroyed; still here:%s" % (name, left))
        log("  re-run 'wk rm %s' -- what is left is exactly what it will find and retry" % name)
        return 1
    _forget(driver, recs, here, name)
    if not act.dry_run():
        info("workspace '%s' destroyed" % name)
    return 0


def confirm_destroy(count, lines):
    if not act.confirm("these %d workspace(s), and every change in them, go:\n%s\ndestroy them?" % (count, lines)):
        die("aborted")


def unsaved_results(reg, found):
    """(workspace, task, why) for each bench task a removal would take that no export readable here holds."""
    from wk.bench import record as bench_record
    if reg.env.get("WK_EXPORTS_READ") or in_podman_machine() and record.host_self(reg.env):
        return []   # the workstation that handed `wk rm` over (refuse_unsaved_before_forward) read its own zips first
    out = []
    for n, driver, what in found:
        if driver.peer:
            continue   # its destroy runs the peer's own `wk rm`, which reads the exports there
        at = driver.results(n) if what != "record" else None
        if at:
            out += [(n, t, why) for t, why in bench_record.unexported(at[0], at[1], os.path.join(reg.store.home(), "Downloads"))]
    return out


def refuse_unsaved(reg, found):
    unsaved = unsaved_results(reg, found)
    if unsaved:
        act.barrier("'wk rm' would destroy bench tasks no export holds as they are now:\n%s" % "\n".join(
            "    %s: %s -- %s\n        wk bench export %s" % (n, t, why, t) for n, t, why in unsaved))


def refuse_unsaved_before_forward(reg, names):
    """A Mac's container removal runs in the podman machine, which cannot read a zip here, so this host reads them first."""
    driver = reg.load("container")
    refuse_unsaved(reg, [(n, driver, "workspace") for n in names])


def rm_names(reg, records, names):
    """The named removals: each planned, one question for all, each destroyed independently; the worst status."""
    from wk.lock import Lock
    worst = 0
    found = []
    for n in names:
        require_name(n)
        try:
            found.append((n,) + rm_plan(reg, records, n))
        except Refused as e:
            if e.status == 1:
                act.err(no_such_workspace(n))
            worst = 1
    if not found:
        return worst
    refuse_unsaved(reg, found)
    confirm_destroy(len(found), "\n".join("    %s@%s" % (n, t.name) for n, t, _ in found))
    for n, driver, what in found:
        lock = Lock(driver.store, records.machine, records.clock)
        try:
            worst = max(worst, rm_one(driver, records, lock, n, what))
        except Refused as e:
            worst = max(worst, e.status)
        finally:
            lock.release_all()
    return worst


def all_workspaces(reg, records):
    """`wk ls`'s rows as (place, name): a row another machine's wk answered carries its label, which is this machine's place for it."""
    r = reg.machine.run([wk_of(reg.root), "ls", "--json"])
    if not r.ok:
        die("'wk ls --json' did not answer with the workspaces to destroy, so\n    nothing here knows what every workspace is")
    label = record.machine_name(reg.env, records.machine) + ":"
    rows = []
    for w in json.loads(r.out).get("workspaces", []):
        place, name = w.get("place", ""), w.get("name", "")
        if place.startswith(label):
            place = place[len(label):]
        place = place.split(":")[0]
        if place and name:
            rows.append((place, name))
    return rows


def rm_all(reg, records):
    """Every workspace `wk ls` finds, asked once as <name>@<place>, each through the one path a named removal takes."""
    rows = all_workspaces(reg, records)
    if not rows:
        info("no workspaces -- nothing to destroy")
        return 0
    confirm_destroy(len(rows), "\n".join("    %s@%s" % (n, t) for t, n in rows))
    worst = 0
    for t, n in rows:
        worst = max(worst, reg.machine.act_run(rm_argv(reg.root, t, n), input="", stream=True).rc)
    return worst


SELFTEST_PREFIX = "wk-test-"


def rubble(listed, stored, here, root, selftest_live, clock):
    """`listed`: the places whose environments this process lists; `stored`: those whose store it reads."""
    from wk.rubble import remover, row
    rows = []

    def rm(t, n):
        return lambda: here.act_run(rm_argv(root, t.name, n)).ok

    def names(t):
        try:
            dirs = here.listdir(os.path.dirname(t.store.ws_dir("x"))) if t in stored else []
        except OSError:
            dirs = []
        return sorted(set(dirs) | {n for n, _ in t.list()})

    for t in stored:
        recs = record.of_driver(t, clock, here)
        for task in recs.list():
            n = task.field("name")
            if task.field("kind") != "new" or task.alive(None) or any(here.isdir(s.store.ws_dir(n)) for s in stored):
                continue
            rows.append(row("creation-record", "creation record of '%s' on %s, its workspace gone" % (n, t.name), None,
                            take=remover(here, str(task.path), t.create_log(n))))
        for n in names(t):
            if not n.startswith(SELFTEST_PREFIX) and creation_state(t, recs, n) == "creating" and not t.creating_now(n):
                rows.append(row("half-made", "workspace '%s' on %s, half-made and nothing creating it" % (n, t.name), None,
                                "--purge-rubble", rm(t, n)))
    for t in listed:
        for n in (n for n in names(t) if n.startswith(SELFTEST_PREFIX)):
            rows.append(row("selftest-ws", "workspace '%s' on %s, left by a selftest" % (n, t.name), None, take=rm(t, n),
                            why="kept -- the live 'wk selftest' running here made it" if selftest_live else ""))
    return rows
