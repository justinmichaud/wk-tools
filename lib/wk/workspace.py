"""`wk new` and `wk rm` as flows over a Target, a Records, a Lock and a Clock.

`new` is two halves: the front refuses on the terminal, detaches the driver
and follows its record; the driver does everything that changes anything,
under the workspace lock, stepping its record as it goes. Under --dry-run the
front runs the driver inline against the recorder, so nothing is made or
waited for and the plan cannot differ from the run.

The bash helpers not yet ported are `lib/wk/shell.py`'s, run on the machine
this command runs on, so a Fake answers them in a unit test.
"""

import json
import os
import re
import subprocess
import sys

from wk import act, job, record, shell, sshalias
from wk.act import Refused, die, info, log, warn
from wk.machine import Killed
from wk.pr import checkout as pr_checkout, parse_spec
from wk.store import Bases
from wk.targets import show

PLAN = ("checking", "wipe", "base", "create", "init", "fetch", "register")
NEW_TIMEOUT = 3600
NAME = re.compile(r"^[a-zA-Z0-9._][a-zA-Z0-9._-]*$")

CHECKOUT_SCRIPT = r'''
b=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")
if [ -z "$b" ]; then
    echo "detached=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    exit 0
fi
echo "branch=$b"
u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo "")
[ -n "$u" ] || exit 0
echo "upstream=$u"
behind=$(git rev-list --count "$b..$u" 2>/dev/null || echo 0)
echo "behind=$behind"
if [ "$behind" != 0 ]; then
    if git merge --ff-only --quiet "$u" 2>/dev/null; then
        echo "moved=$behind"
    else
        echo "moved=refused"
    fi
fi
echo "head=$(git rev-parse --short HEAD)"
'''


def require_name(name):
    if not NAME.match(name or ""):
        die("invalid name '%s': use [a-zA-Z0-9._-], not starting with '-'" % (name or ""))


def wk_of(root):
    return os.path.join(str(root), "wk")


def task_records_for(records, name):
    return [t for t in records.list() if t.field("name") == name]


def live_task_lines(records, name):
    """One line per running job in the workspace, with the command that stops it."""
    lines = []
    for t in task_records_for(records, name):
        if not t.alive(None):
            continue
        lines.append("    %s (pid %s on %s)  stop it:  %s" % (t.field("kind"), t.field("pid"), t.field("machine"), t.field("kill")))
    return "\n".join(lines)


def remove_task_records(records, name):
    for t in task_records_for(records, name):
        records.machine.remove(str(t.path))


def creation_state(target, records, name):
    """`Target.state`, except that a present workspace whose creation driver died without a verdict is still
    creating: it was never announced ready, so nothing in it is worth keeping and "already exists" is not the answer."""
    state = target.state(name)
    if state != "present":
        return state
    t = records.find("new", name)
    if t is not None and t.raw("exit") is None and t.holder_gone():
        return "creating"
    return state


def new_front(reg, records, name, opts):
    """Refuse, detach the driver, follow it, then the --pr / hints / --zed tail. 0, or Refused."""
    here, root, env = reg.machine, reg.root, reg.env
    require_name(name)
    if opts.get("sysroot"):
        die("--sysroot is not implemented (docs/Nice to have/HANDOFF-cross-compile.md).\n"
            "    It is also not this flag: --arch makes the workspace itself another\n"
            "    architecture, executed natively. A sysroot cross build stays in a native\n"
            "    workspace and is a property of the build, so it will be 'wk build\n"
            "    --sysroot', not 'wk new --sysroot'.")
    pr = opts.get("pr")
    if pr is not None and not pr:
        die("--pr needs a spec: <user>:<branch>, <n>, or wpe:<n>")
    arch = shell.arch_canon(root, here, opts.get("arch") or "native")
    tname = opts.get("target") or reg.default()
    try:
        target = reg.load(tname)
    except LookupError as e:
        die(str(e))
    recs = record.of_target(target, records.clock, records.machine)
    if opts.get("kill"):
        return new_kill(target, here, recs, env, name, opts)
    if arch != "native" and target.kind != "container":
        die("--arch %s is a container-only capability (this is target '%s').\n"
            "    32-bit ARM needs a host that can execute it: this Neoverse-N1 runs AArch32\n"
            "    at EL0, and Apple Silicon does not, which is why it lives on the Linux\n"
            "    workstation permanently." % (arch, tname))
    if pr:
        if opts.get("no_wait"):
            die("--pr needs the workspace to be ready, and --no-wait returns before it is.\n"
                "    Drop --no-wait, or check it out afterwards:  wk pr %s %s" % (name, pr))
        parse_spec(pr)
    if target.kind == "local":
        target.create(name)
    target.store_init()
    log_path = target.create_log(name)
    st = creation_state(target, recs, name)
    if st == "present":
        die("workspace '%s' already exists (target '%s').\n    'wk rm %s' first, or pick another name." % (name, tname, name))
    if st == "broken":
        die("'%s' is a record without an environment -- something outside wk removed\n"
            "    the %s side of it. 'wk status %s' says so; 'wk rm %s' clears it." % (name, tname, name, name))
    if st == "creating":
        t = recs.find("new", name)
        if t is not None and t.alive(None):
            die("'%s' is already being created (pid %s, at stage\n    '%s'). Follow it:      tail -f %s\n"
                "    Or ask for it and wait:  wk enter %s --zed   /   wk build %s <config>"
                % (name, t.field("pid"), " ".join(t.stage()), t.field("log"), name, name))
    base = opts.get("base") or ""
    if act.dry_run():
        from wk.lock import Lock
        new_driver(target, recs, Lock(target.store, here, recs.clock), recs.clock, name, base, arch)
        return 0
    since = recs.clock.stamp()
    here.mkdir(os.path.dirname(log_path))
    here.write(log_path, "")
    argv = [wk_of(root), "new", name, "--target", tname, "--arch", arch] + (["--base", base] if base else []) + ["--_detached"]
    pid = here.spawn(argv, log_path)
    if opts.get("no_wait"):
        info("creating '%s' on %s, detached as pid %d -- this end can go away" % (name, tname, pid))
        log("  follow:  tail -f %s" % log_path)
        log("  state:   wk status %s" % name)
        if opts.get("zed"):
            log("  open it:  wk enter %s --zed   (waits for it to be ready)" % name)
        return 0
    timeout = job._seconds(env, "WK_NEW_TIMEOUT", NEW_TIMEOUT)
    st = recs.wait("new", name, log_path, timeout=timeout, pid=pid, floor=since, stream=sys.stderr)
    if st == "crashed":
        die("the process creating '%s' is gone without having said how it ended.\n"
            "    Whatever it left is half-made, and a re-run destroys it and starts again:\n"
            "        wk new %s --target %s\n    What it managed to say is in %s" % (name, name, tname, log_path))
    if st == "refused":
        die("'%s' was not created, and nothing was left half-made: the reason is\n    above, in full in %s" % (name, log_path))
    if st == "timeout":
        die("'%s' is still being created after %ds.\n    It is detached, so it is still going: 'wk status %s' says where, and\n"
            "    %s says what it is doing. Nothing here was undone." % (name, timeout, name, log_path))
    if st != "ok":
        die("creating '%s' failed (%s) -- the reason is above, in full in %s.\n"
            "    A re-run destroys what is there and starts again:  wk new %s --target %s" % (name, st, log_path, name, tname))
    info("workspace '%s' ready%s" % (name, "" if arch == "native" else " (%s)" % arch))
    if pr:
        pr_checkout(target, here, name, pr)
    new_hints(target, name, arch)
    if opts.get("zed"):
        r = here.act_run([os.path.join(str(root), "cmd", "zed"), name])
        show(r)
        if not r.ok:
            warn("'%s' is there; opening it in Zed is what failed (above) -- 'wk zed %s' retries" % (name, name))
    return 0


def new_hints(target, name, arch):
    if arch != "native":
        log("  %s: native 32-bit, no GPU. 'wk bench' will run CPU-class plans" % arch)
        log("  in here and refuse GPU-class ones.")
    if target.kind == "vm":
        log("  wk vm start %s       boot it (its ssh alias is written then)" % name)
        log("  wk zed %s            the checkout, in Zed (once it is up)" % name)
        log("  wk build %s mac-release" % name)
    elif target.kind == "remote":
        log("  wk build %s <config> build (polite: sized from that machine's load)" % name)
        log("  wk enter %s          shell, in the checkout" % name)
        if target.host:
            log("  wk zed %s            the checkout, in Zed (ssh://%s%s)" % (name, target.host, target.src(name)))
        log("")
        log("  no sandbox on a shared machine, so 'wk ai claude' and 'wk doctor <ws>' refuse.")
    else:
        log("  wk enter %s          shell" % name)
        log("  wk build %s <config> build" % name)
        log("  wk ai claude %s      sandboxed agent" % name)
        log("  wk zed %s            the checkout, in Zed" % name)


def new_kill(target, here, records, env, name, opts):
    if any(opts.get(k) for k in ("pr", "zed", "no_wait", "base")):
        die("'wk new %s --kill' stops the creation already running and takes\n"
            "    nothing with it -- no --base, --zed, --no-wait or --pr." % name)
    stopped = job.stop(target, records, name, "new", here, records.clock, env)
    if stopped == 1:
        die("the process creating '%s' outlived a TERM and a KILL. It holds the\n"
            "    workspace lock, so nothing else can touch '%s' until it is gone:\n        ps -p %s"
            % (name, name, records.find("new", name).field("pid")))
    if stopped == 0:
        log("  what it got as far as is half-made and nothing in one is worth\n  keeping:  wk rm %s    (then 'wk new %s' to start again)" % (name, name))
    return 0


def new_driver(target, records, lock, clock, name, base, arch):
    """PLAN's steps under the workspace lock: a refusal ends the record `refused`, any other failure 1."""
    here = records.machine
    if target.kind == "container":
        with lock.held("sdk"):
            target.sdk_refresh()
    lock.hold("ws-" + name)
    if target.needs_base:
        lock.hold("store")
    state = creation_state(target, records, name)
    task = None
    if not act.dry_run():
        task = records.begin("new", "here", name, "wk new %s --kill" % name, target.create_log(name), list(PLAN))
    try:
        _create(target, records, task, clock, name, base, arch, state)
    except Killed:
        raise
    except Refused as e:
        _end(task, e.status)
        lock.release_all()
        raise
    except Exception:
        _end(task, 1)
        lock.release_all()
        raise
    _end(task, 0)
    lock.release_all()
    if task is not None:
        info("workspace '%s' created" % name)
    return 0


def _end(task, status):
    if task is not None:
        task.end(status)


def _create(target, records, task, clock, name, base, arch, state):
    here, tname = records.machine, target.name

    def stage(step):
        if task is not None:
            task.step_named(step)

    def refuse(msg):
        _end(task, "refused")
        die(msg)

    stage("checking")
    if state == "present":
        refuse("workspace '%s' already exists (target '%s').\n    'wk rm %s' first, or pick another name." % (name, tname, name))
    if state == "broken":
        refuse("'%s' is a record without an environment: creation finished, and the\n"
               "    %s side of it is gone -- something outside wk removed it. Its layer\n"
               "    may still hold work, so this will not wipe it for you:\n"
               "        wk rm %s     then 'wk new %s' to start again" % (name, tname, name, name))
    if state == "unreachable":
        refuse("cannot reach the machine behind target '%s', so whether '%s' is\n"
               "    already there cannot be known -- and creating it blind could clobber a\n"
               "    workspace of the same name. Try again when the machine answers." % (tname, name))
    if state == "creating":
        stage("wipe")
        warn("'%s' exists but was never finished -- destroying it and starting again" % name)
        log("  (an interrupted 'wk new' leaves this; nothing in it is worth keeping)")
        target.destroy(name)
        left = leftovers(target, here, name)
        if left:
            die("could not destroy the half-made workspace '%s'; still here:%s\n"
                "    'wk rm %s' retries exactly that, then 'wk new %s'" % (name, left, name, name))
        sshalias.alias_remove(here, target.env, name)
    if target.needs_base:
        stage("base")
        mirror = target.store.mirror()
        if not here.isdir(mirror):
            die("no WebKit mirror at %s, and every snapshot borrows its objects:\n"
                "    wk sync    makes it, then publishes a snapshot to build a workspace from." % mirror)
        bases = Bases(target.store, here)
        base = base or bases.current()
        if not base:
            die("no base snapshot this machine can build a workspace from:  wk sync\n"
                "    publishes one. A snapshot that is not on the branch it was published from\n"
                "    is refused here -- every workspace overlaid on it starts detached.")
        why = bases.verify(base)
        if why:
            die(why)
    stage("create")
    target.create(name, base, arch)
    stage("init")
    if not act.dry_run() and not target.ready(name, clock):
        die("'%s' was created but never finished initialising -- the push\n"
            "    keys, the Claude CLI, the lldb config and the shell rc are set up at first\n"
            "    start, and something above went wrong before the end of it.\n"
            "    Nothing here is worth repairing:  wk new %s    (destroys it and retries)" % (name, name))
    stage("fetch")
    freshen(target, name, here)
    stage("register")


def fetch_from(probe):
    if probe == "yes":
        return "mirror"
    if probe == "no":
        return "network"
    return "unreachable"


def checkout_script(src):
    return "cd %s || exit 2\n" % shell.sh_quote(src) + CHECKOUT_SCRIPT


def freshen(target, name, here):
    """Fetch the checkout from the mirror beside its snapshot and fast-forward onto it. Never fatal."""
    wk = wk_of(target.root)
    if act.dry_run():
        here.act_run([wk, "sync", name])
        return
    mirror = shell.sh_quote(target.mirror_dir())
    r = target.exec(name, ["sh", "-c", "[ -n %s ] && [ -d %s ] && echo yes || echo no" % (mirror, mirror)])
    lines = r.out.replace("\r", "").splitlines() if r.ok else []
    source = fetch_from(lines[-1].strip() if lines else "")
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
        return
    r = target.exec(name, ["sh", "-c", checkout_script(target.src(name))])
    kv = {}
    for line in (r.out.replace("\r", "").splitlines() if r.ok else []):
        k, _, v = line.partition("=")
        kv.setdefault(k, v)
    if kv.get("detached"):
        warn("'%s' is not on a branch (detached at %s), so 'git status',\n"
             "    'git pull' and 'wk pr rebase' in there have no upstream to name:\n"
             "        git checkout main    in the workspace" % (name, kv["detached"]))
        return
    b = kv.get("branch")
    if not b:
        warn("could not read the checkout in '%s' to say where it is\n    ('wk enter %s', then 'git status' in there)" % (name, name))
        return
    u = kv.get("upstream")
    if not u:
        warn("'%s' is on '%s', which tracks nothing -- 'git pull' and\n    'wk pr' have no upstream to name:  wk sync %s --fix" % (name, b, name))
        return
    head, moved = kv.get("head", ""), kv.get("moved", "")
    if moved == "refused":
        warn("'%s' in '%s' and %s have diverged, so the checkout is left where\n    it is:  git rebase %s   in the workspace" % (b, name, u, u))
    elif not moved:
        info("'%s' is on %s at %s, up to date with %s" % (name, b, head, u))
    else:
        info("'%s' is on %s at %s, %s commit(s) on from its snapshot (%s)" % (name, b, head, moved, u))


def rm_plan(reg, records, name):
    """(target, "workspace" | "record"), mutating nothing; Refused(1) absent, Refused(2) unreachable."""
    try:
        tname = reg.ws_target(name)
        target = reg.load(tname)
    except LookupError as e:
        die(str(e))
    if reg.machine.isdir(target.store.ws_dir(name)):
        return target, "workspace"
    ok, why = target.answers()
    if not ok:
        act.err("'%s' has no record here, and %s did not answer: %s\n"
                "    Nothing this end can see is there to destroy; re-run once %s answers." % (name, tname, why, tname))
        raise Refused(2)
    if target.info(name) != "absent":
        return target, "workspace"
    if record.of_target(target, records.clock, records.machine).find("new", name) is not None:
        return target, "record"
    for other in reg.all():
        if other == tname:
            continue
        try:
            t = reg.load(other)
        except LookupError:
            continue
        if record.of_target(t, records.clock, records.machine).find("new", name) is not None:
            return t, "record"
    raise Refused(1)


def leftovers(target, here, name):
    if act.dry_run():
        return ""
    left = ""
    if target.info(name) != "absent":
        left += " the %s environment" % target.name
    ws = target.store.ws_dir(name)
    if here.isdir(ws):
        left += " " + ws
    return left


def _forget(target, records, here, name):
    """The alias and the log first and the record last: a record that outlives them is what a re-run finds."""
    sshalias.alias_remove(here, target.env, name)
    here.remove(target.create_log(name))
    remove_task_records(records, name)


def rm_one(target, records, lock, name, what):
    """Destroy one workspace, or forget one that is nothing but a record. 0 destroyed, 1 not fully."""
    here = records.machine
    recs = record.of_target(target, records.clock, records.machine)
    if what == "record":
        lock.hold("ws-" + name)
        _forget(target, recs, here, name)
        info("'%s' had nothing left but its record; forgotten" % name)
        return 0
    if target.kind == "local":
        target.destroy(name)
    live = live_task_lines(recs, name)
    if live:
        die("'%s' has work running in it, and destroying it under a running job\n"
            "    leaves that job compiling into a directory that is gone:\n%s" % (name, live))
    lock.hold("ws-" + name)
    if target.needs_base:
        lock.hold("store")
    ws = target.store.ws_dir(name)
    changes = os.path.join(ws, "changes")
    if here.isdir(changes):
        n = len(here.run(["find", changes, "-type", "f"]).out.splitlines())
        if n:
            warn("%s has %d modified file(s) in its overlay" % (name, n))
    target.destroy(name)
    left = leftovers(target, here, name)
    if left:
        warn("'%s' was not fully destroyed; still here:%s" % (name, left))
        log("  re-run 'wk rm %s' -- what is left is exactly what it will find and retry" % name)
        return 1
    _forget(target, recs, here, name)
    if not act.dry_run():
        info("workspace '%s' destroyed" % name)
    return 0


def confirm_destroy(count, lines):
    if not act.confirm("these %d workspace(s), and every change in them, go:\n%s\ndestroy them?" % (count, lines)):
        die("aborted")


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
                act.err("no such workspace: %s" % n)
            worst = 1
    if not found:
        return worst
    confirm_destroy(len(found), "\n".join("    %s@%s" % (n, t.name) for n, t, _ in found))
    for n, target, what in found:
        lock = Lock(target.store, records.machine, records.clock)
        try:
            worst = max(worst, rm_one(target, records, lock, n, what))
        except Refused as e:
            worst = max(worst, e.status)
        finally:
            lock.release_all()
    return worst


def all_workspaces(reg, records):
    """`wk ls`'s rows as (target, name): a row another machine's wk answered carries its label, which is this machine's target for it."""
    r = reg.machine.run([wk_of(reg.root), "ls", "--json"])
    if not r.ok:
        die("'wk ls --json' did not answer with the workspaces to destroy, so\n    nothing here knows what every workspace is")
    label = record.machine_name(reg.env, records.machine) + ":"
    rows = []
    for w in json.loads(r.out).get("workspaces", []):
        target, name = w.get("target", ""), w.get("name", "")
        if target.startswith(label):
            target = target[len(label):]
        target = target.split(":")[0]
        if target and name:
            rows.append((target, name))
    return rows


def rm_all(reg, records):
    """Every workspace `wk ls` finds, asked once as <name>@<target>, each through the one path a named removal takes."""
    rows = all_workspaces(reg, records)
    if not rows:
        info("no workspaces -- nothing to destroy")
        return 0
    confirm_destroy(len(rows), "\n".join("    %s@%s" % (n, t) for t, n in rows))
    worst = 0
    for t, n in rows:
        cp = act.act(["env", "WK_TARGET=%s" % t, wk_of(reg.root), "rm", n, "--yes"], stdin=subprocess.DEVNULL)
        if cp is not None:
            worst = max(worst, cp.returncode)
    return worst
