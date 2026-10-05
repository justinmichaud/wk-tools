"""The shell preludes cmd/run, cmd/gui, cmd/test and cmd/profile put in front of a target command: the loader path, prepended so the wkdev image's own jhbuild/libwpe prefix survives, and the lldb that starts (run, not found: the image's /opt/swift lldb links a libxml2 it lacks), pinned to the parent after ~/.lldbinit; and the checks a recorder needs before it runs: the tool, and the kernel's perf events."""

import os
import shlex

from wk.act import die, dry_run, info
from wk.machine import HAVE, Local, is_linux
from wk.store import Store, in_vm
from wk.sudo import QUIESCE_PRIV as PRIV


def prelude(var, dir_):
    return 'export %s="%s${%s:+:${%s}}"' % (var, dir_, var, var)


LLDB_PRELUDE = """LLDB=""
for _c in lldb $(ls /usr/bin/lldb-[0-9]* 2>/dev/null | sort -Vr); do
    command -v "$_c" >/dev/null 2>&1 || continue
    "$_c" --version >/dev/null 2>&1 && { LLDB="$_c"; break; }
done
[ -n "$LLDB" ] || { printf 'error: no lldb here that will start -- `lldb` resolves to %s\\n' \\
    "$(command -v lldb || echo 'nothing')" >&2; exit 127; }"""

LLDB_PIN_OPTS = "-O 'settings set target.process.follow-fork-mode parent'"


def lldb_waiting(run, log, attach, *commands):
    opts = " ".join("-o " + shlex.quote(c) for c in commands + ("process attach --name %s --waitfor" % attach,))
    return '%s\n( sleep 2; %s >%s 2>&1 ) &\nexec "$LLDB" %s %s' % (LLDB_PRELUDE, run, log, LLDB_PIN_OPTS, opts)


def require_tool(target, name, tool):
    if not target.exec(name, list(HAVE) + [tool]).ok:
        die("%s is not installed in '%s'. It is provisioning, not a per-run step:\n"
            "        wk enter %s     and install it there" % (tool, name, name))


def _paranoid(target, name):
    r = target.exec(name, ["cat", "/proc/sys/kernel/perf_event_paranoid"])
    par = "".join(c for c in r.out if c.isdigit() or c == "-")
    return int(par) if par.lstrip("-").isdigit() else None


def perf_events(target, name, tool):
    """perf_event_paranoid at 1 or less, which rr, samply and sysprof need; the sysctl is not namespaced, so a container cannot set it."""
    par = _paranoid(target, name)
    if par is not None and par > 1 and is_linux() and os.access(PRIV, os.X_OK):
        info("perf_event_paranoid is %d on this host -- asking the quiesce helper to unrestrict it" % par)
        Local().act_run(["sudo", "-n", PRIV, "perf-on"])
        par = _paranoid(target, name)
    if par is None or par <= 1:
        return
    head = "perf_event_paranoid is %d in '%s', and %s needs 1 or less.\n" % (par, name, tool)
    if not is_linux():
        die(head + "    It is a host setting and cannot be written from inside a workspace (the\n"
            "    sysctl is not namespaced -- sudo in there fails with 'permission denied on\n"
            "    key'). On the machine that runs the workspace:\n"
            "        echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid\n"
            "    On a booted benchmark image it is set already.")
    if not os.access(PRIV, os.X_OK):
        die(head + "    The privileged helper that can lower it is not installed on this host.\n"
            "    Remedy:  ./setup --stage quiesce")
    die(head + "    Asking the quiesce helper ('%s perf-on') did not bring it down --\n"
        "    passwordless sudo is likely not set up for it.\n"
        "    Remedy:  ./setup --stage quiesce" % PRIV)


def on_apple_cpu(target):
    """A container on a macOS host runs in its podman machine, on the host's own CPU."""
    return target.kind == "container" and (Store(target.env).macos_host or in_vm(target.env)) and os.uname().machine in ("arm64", "aarch64")


def rr_ready(target, name):
    if target.os() != "linux":
        die("rr records Linux processes only: it replays from ptrace and the CPU's perf counters,\n"
            "    and '%s' runs %s. Debug it live instead:  --lldb" % (name, target.os()))
    if on_apple_cpu(target):
        die("rr does not support Apple Silicon CPUs (its docs: Intel, AMD and certain AArch64 server cores),\n"
            "    and '%s' runs in this Mac's podman machine, on its CPU. Debug it live instead:  --lldb,\n"
            "    or record in a workspace on a Linux build machine" % name)
    if not dry_run():
        require_tool(target, name, "rr")
        perf_events(target, name, "rr")


def rr_trace_dir(target):
    return "export _RR_TRACE_DIR=%s" % shlex.quote(target.home() + "/wk-rr")
