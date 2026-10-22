"""`wk stop`: one workspace's environment, every running task through its own kill command, or everything here."""
import os
import sys

from wk import act, fleet, images, places, record, store
from wk.act import Refused, confirm, die, info, log, warn
from wk.machine import Local
from wk.store import Store

ROOT = images.root()
MACHINE = Store().podman_machine()


def running_tasks(records):
    """(task, 'kind name', kill) for each task still going -- kill is each kind's own stop command."""
    rows = []
    for t in records.list():
        if t.verdict("capped") in record.RUNNING:
            rows.append((t, "%s %s" % (t.field("kind"), t.field("name")), t.field("kill")))
    return rows


def stop_tasks_here(records):
    machine = record.machine_name()
    rows = running_tasks(records)
    if not rows:
        info("no task is running on %s" % machine)
        return 0
    for _, what, kill in rows:
        log("  %s -- %s" % (what, kill))
    if not confirm("stop %d task(s) on %s? What they have not finished is dropped" % (len(rows), machine)):
        die("nothing stopped")
    os.environ["WK_YES"] = "1"   # answered once, here: a kill command of its own would ask again with no terminal
    path = "%s/bin:%s" % (ROOT, os.environ.get("PATH", ""))
    for _, what, kill in rows:
        if not Local().act_run(["env", "PATH=" + path, "sh", "-c", kill], stream=True).ok:
            warn("'%s' exited non-zero" % kill)
    if act.dry_run():
        return 0
    left = running_tasks(records)
    if left:
        warn("still running on %s:" % machine)
        for _, what, _ in left:
            sys.stderr.write("    %s\n" % what)
        return 1
    info("no task is running on %s" % machine)
    return 0


def stop_tasks_everywhere(reg, records):
    if not confirm("stop every task on this machine and every other one? What they have not finished is dropped"):
        die("nothing stopped")
    os.environ["WK_YES"] = "1"
    rc = stop_tasks_here(records)
    for a in fleet.each(reg, ["stop", "--tasks"], reg.machines(), quiet=False):
        if a.side == "none":
            continue
        if a.why:
            sys.stderr.write("%-22s %s\n" % (a.name, a.why))
            rc = max(rc, 3)
            continue
        for line in a.out.splitlines():
            sys.stdout.write("%-22s%s\n" % (a.name, line))
        rc = max(rc, a.rc)
    return rc


def stop_everything(reg, keep_vm):
    if Store().macos_host:
        vm = reg.load("vm")
        for guest, state in vm.list():   # a guest holds its memory even with the machine stopped
            if state == "running":
                vm.stop(guest)
        state = reg.load("container").machine_state()
        if state != "running":
            info("machine '%s' is already %s -- nothing to do" % (MACHINE, state))
            return 0
    ctr = reg.load("container")
    names = ctr.machine.run(ctr.podman() + ["ps", "--filter", "name=^wk-", "--format", "{{.Names}}"], input="").out.split()
    if names:
        for n in names:
            sys.stderr.write("  stopping %s\n" % n)
        ctr.machine.act_run(ctr.podman() + ["stop", "--time", "30"] + names, input="")
        info("stopped %d container(s)" % len(names))
    else:
        info("no running containers")
    if keep_vm:
        log("leaving the machine running (--keep-vm)")
    elif Store().macos_host:
        info("stopping the machine")
        Local().act_run(["podman", "machine", "stop", MACHINE], stream=True)
        info("machine stopped -- CPU and memory returned to the host")
    return 0


def main(name, keep_vm, tasks, everywhere):
    reg = places.Registry(ROOT)
    try:
        if tasks:
            if keep_vm:
                die("'--keep-vm' is about the podman machine, which 'wk stop --tasks' leaves alone")
            records = record.Records(ask_place=lambda n, pid, cap: reg.load(reg.ws_place(n)).pid_alive(n, pid, cap))
            return stop_tasks_everywhere(reg, records) if everywhere else stop_tasks_here(records)
        if name:
            if not reg.present(name).stop(name):
                raise Refused(1)
            return 0
        return stop_everything(reg, keep_vm)
    except LookupError as e:
        die(str(e))
