"""A Mac daemon's unix socket published into the podman machine, over a remote forward on its loopback sshd."""

import asyncio
import os
import shutil
import subprocess

from wk import places
from wk.machine import Local


async def publish_once(machine, local_sock, name, log):
    rec = places.podman_vm(Local(), machine, timeout=30)
    if rec is None:
        raise OSError("podman machine '%s' is not there" % machine)
    opts, dest = places.podman_vm_route(rec)
    base = ["ssh", "-q", *opts, "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes", dest]
    rt = subprocess.run(base + ['printf %s "$XDG_RUNTIME_DIR"'], capture_output=True, timeout=30).stdout.decode().strip()
    if not rt:
        raise OSError("the machine reported no XDG_RUNTIME_DIR")
    remote = "%s/%s" % (rt, name)
    subprocess.run(base + ["mkdir -p %s && rm -f %s" % (os.path.dirname(remote), remote)], timeout=30)
    log("publishing %s into machine '%s' at %s" % (local_sock, machine, remote))
    proc = await asyncio.create_subprocess_exec(*base[:-1], "-N", "-R", "%s:%s" % (remote, local_sock), base[-1])
    return await proc.wait()


async def publish(machine, local_sock, name, log, stage):
    if not shutil.which("podman"):
        log("no podman on PATH (%s), so nothing is published into podman machine '%s' and no container reaches %s; "
            "with podman installed, './setup --stage %s' writes the LaunchAgent's PATH" % (os.environ.get("PATH"), machine, name, stage))
        return
    while True:
        try:
            rc = await publish_once(machine, local_sock, name, log)
            log("the forward into '%s' ended (rc=%d); re-establishing" % (machine, rc))
        except Exception as exc:                            # noqa: BLE001
            log("cannot publish into machine '%s': %s" % (machine, exc))
        await asyncio.sleep(5)
