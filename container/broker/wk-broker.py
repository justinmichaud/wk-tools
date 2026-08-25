#!/usr/bin/env python3
"""The workspace fleet-request boundary.

A workspace runs with --network none and no host filesystem: it cannot reach a
bench board, and it must not be able to. Benchmarking nevertheless needs a
Raspberry Pi rebooted into a measurement system, a build put on it, a plan run
and the board handed back -- five things that only a workstation can do.

The tempting fix is to give the workspace an ssh key to the workstation. That
dissolves the sandbox in one step: the workstations run this tooling, hold the
git push keys, and can drive every other machine here. A key into that is a key
into everything.

So this is the opposite of privilege. It is the same shape as the egress proxy
(container/proxy/wk-proxy.py): a unix socket into the sandbox, a policy engine
outside it, and a fixed vocabulary of things that may be asked for. And it is
the same shape as the privileged helper (admin/wk-quiesce-priv): a closed verb
set, no passthrough, no eval, and no argument that becomes part of a shell
command -- every action here is an argv list handed to execve, never a string
handed to a shell.

What crosses the boundary is therefore an *outcome*, never a command:

    capabilities   what this broker will do, and to which machines
    status         what mode a machine is in, or how a request is going
    stage          put a workspace's build on a bench device
    arm            reboot a bench device into a testing system
    keep           claim it, so its self-return watchdog does not cut the run
    run            run a benchmark plan on it
    release        reboot it back to host mode
    disarm         cancel an arming that has not been spent

None of those is implemented here. Each one resolves to a `wk` command that
already exists and already refuses on its own evidence -- `wk boot` for every
mode transition, `wk pi deploy` and `wk pi bench` for the device lane. That is
deliberate and it is the whole design: **the broker is a narrower door to the
existing path, never a second, more permissive one.** A second arming path
would be a second set of refusals to keep in step, and the day they diverged
the workspace's would be the weaker one.

The narrowing it adds on top, which is what makes it a boundary rather than a
relay:

  1. Only a machine the fleet declares `MACH_ROLE=bench-device` may be named.
     `wk boot rpi5` is a perfectly reasonable thing for a person at a keyboard
     to type -- the rpi5 is a workstation that can also be booted into a bench
     system. Through this socket it is refused, because a workspace being able
     to reboot a workstation is the escape this whole arrangement exists to
     prevent.

  2. Only a machine this host can actually drive (MACH_OS), only a system that
     is in this machine's image store, only a plan in ALLOWED_PLANS below, and
     only a name that is a name (no path separators, no leading dash, nothing
     that could be read as an option by anything downstream).

  3. No barrier may be crossed from in here. `WK_FORCE` is never set from a
     request and never inherited into one: `wk <command> --force` is the loud,
     recorded way past a refusal (CLAUDE.md, "Refusals"), and a barrier a
     workspace can cross by asking is not a barrier. A request that hits one is
     refused with the barrier's own words, and a person runs the command.

  4. One request at a time per machine. Two armings of one board race each
     other into a state neither asked for, so a second request naming a machine
     that already has one in flight is refused by name. The in-flight set is
     *derived* -- a request record whose pid is gone is not in flight, whatever
     its status file says (CLAUDE.md rule 1; the same rule `wk boot --status`
     uses to decide that an arming has been spent).

Requests are recorded under this device's own state directory, in broker/<id>/
-- and that directory, not $WK_STORE, because on a macOS host $WK_STORE names
the podman VM's store and not a path this machine has. Each record holds the
argv that was run,
its log, and a status file in the schema lib/detach.sh defines. That is a
record of what this broker did, not a cache of anything: the machine's own
state is read back from the machine, always, by the `wk` commands above.

Reachability is never written down here either. Where a machine is reached the
answer comes from lib/reach.sh, which derives it at read time -- the tailnet
name first, and the bridge-segment address or mDNS answer when a board is not
on the tailnet -- and every reply says which one it used, because "unreachable"
and "unreachable through the phone that is unplugged" are different problems.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

WK_ROOT = os.environ.get(
    "WK_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# --- policy ------------------------------------------------------------------

# The benchmark plans a workspace may ask a board to run.
#
# This is policy, in the same sense as wk-proxy.py's ALLOWED_HOSTS, and not a
# copy of anything. WebKit's own plan registry lives in a checkout
# (Tools/Scripts/webkitpy/benchmark_runner/data/plans) and is the authority on
# what a plan *is* -- run-benchmark resolves the name over there and fails if it
# is wrong. What this list decides is a different question, which no checkout
# can answer: which of them a sandboxed workspace may spend an hour of a
# physical board on, unattended, with nobody in the room.
#
# So it is short on purpose and grows by name, with a reason, the way the
# proxy's allowlist does. A refused plan names this file.
ALLOWED_PLANS = {
    # The CPU-class suites. These are what a JSC change is actually measured
    # with here, and they need no display -- so a refusal about a missing
    # monitor cannot turn into an accidental software-composited number.
    "jetstream3",
    "jetstream2",
    "octane",
    "kraken",
    "sunspider",
    # The GPU-class ones. Allowed because they are the point of having a board
    # with a monitor on it at all; `wk pi bench` refuses them itself when no
    # display is attached, and that refusal is not crossable from here (see
    # WK_FORCE above), which is exactly the arrangement we want.
    "speedometer3",
    "speedometer2",
    "motionmark1.3",
}

# The build slots `wk pi deploy` / `wk pi bench` understand. A slot is a
# directory name on the board, so it is spelled out rather than validated by
# pattern: `--ab a,b` exists so that two builds can be alternated, and two is
# how many there are.
ALLOWED_SLOTS = {"a", "b"}

# A name that is a name. Everything a request may carry -- a machine, a
# workspace, a system id, a plan -- is matched against this before it is looked
# up in anything, so a lookup can never be handed a path, an option, or a
# traversal. lib/common.sh's valid_name is the same rule for the same reason.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Bounds. A request line is tiny; anything larger is not a request.
MAX_REQUEST_BYTES = 8192
MAX_CONNECTIONS = 32
# How long a single `wk` invocation may run before the broker gives up on it.
# Generous: `wk pi bench` with --ab is a full-suite run on a Raspberry Pi 3.
RUN_TIMEOUT = 6 * 3600


def log(msg):
    print(f"[wk-broker] {msg}", file=sys.stderr, flush=True)


class Refused(Exception):
    """A refusal, with its reason and its remedy.

    Both halves are required by CLAUDE.md: a refusal must say why and name the
    remedy. The type exists so that no code path can raise half of one.
    """

    def __init__(self, why, remedy):
        super().__init__(why)
        self.why = why
        self.remedy = remedy


# --- the fleet, as it declares itself ----------------------------------------


# Set by _bash when a fragment outlasted its ceiling, so a caller can say
# "the lookup timed out" rather than "there is no answer" -- two very
# different things to read at 2am, and lib/reach.sh's whole point is that the
# second one reads as broken hardware.
_TIMED_OUT = object()


def _bash(script, *args):
    """Run a fragment against this repo's own libraries and return its stdout.

    Everything the broker needs to know about the fleet, the image store and
    how a machine is reached is already computed by shell in this repository,
    at read time, from evidence. Re-deriving any of it in Python would be a
    second implementation of a fact -- and it is the copy in the boundary that
    would go stale, which is the failure that reads as broken hardware.

    Bounded and read-only: these fragments source libraries and print, they do
    not reach a machine. `reach_without_tailnet` is the one that can touch the
    wire (an mDNS lookup) and lib/reach.sh caps that itself.
    """
    cmd = ["bash", "-c", script, "wk-broker", *args]
    env = _clean_env()
    try:
        out = subprocess.run(
            cmd, cwd=WK_ROOT, env=env, capture_output=True, timeout=30
        )
    except subprocess.TimeoutExpired:
        return _TIMED_OUT
    return out.stdout.decode("utf-8", "replace")


_MACHINES_SH = f'''
set -euo pipefail
WK_ROOT={json.dumps(WK_ROOT)}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/boot/machines.sh"
machine_declare
'''

_IMAGES_SH = f'''
set -euo pipefail
WK_ROOT={json.dumps(WK_ROOT)}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/image.sh"
image_ids
'''

# Two fragments, not one, and deliberately: `reach_tailnet` asks the tailscale
# daemon and `reach_without_tailnet` reads two conf files and may do an mDNS
# lookup. Run together under one ceiling, a slow or wedged tailscaled swallowed
# the bridge answer as well, and the reply said "no route derived" about a board
# whose address is pinned by MAC in a file. Observed here twice in a row with
# different answers, 2026-08-24 -- which is exactly the kind of intermittent
# that gets blamed on the hardware.
_REACH_TAILNET_SH = f'''
set -euo pipefail
WK_ROOT={json.dumps(WK_ROOT)}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/reach.sh"
reach_tailnet "$1" || true
'''

_REACH_OTHER_SH = f'''
set -euo pipefail
WK_ROOT={json.dumps(WK_ROOT)}
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/reach.sh"
reach_without_tailnet "$1" || true
'''


def fleet():
    """Every machine the fleet declares, read now.

    Not held between requests. boot/machines/*.conf is five small files and a
    conf edited while the broker is up must take effect without restarting the
    boundary -- the same call the proxy makes about pi-hosts.
    """
    out = {}
    declared = _bash(_MACHINES_SH)
    if declared is _TIMED_OUT:
        raise Refused(
            "the fleet's own declarations could not be read in time "
            "(boot/machines/*.conf, through boot/machines.sh)",
            "something is wrong on the workstation, not with the request -- "
            "run 'wk boot --list' there",
        )
    for line in declared.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        name, role, mach_os, profile, note = parts[:5]
        out[name] = {
            "name": name,
            "role": role or "workstation",
            "os": mach_os or "any",
            "profile": profile,
            "note": note,
        }
    return out


def systems():
    """The complete systems in this machine's image store, by id."""
    listed = _bash(_IMAGES_SH)
    if listed is _TIMED_OUT:
        return []
    return [s for s in listed.split() if s]


def reach(machine):
    """How this host would reach that machine, tailnet first.

    Two answers, because the fleet has both kinds: rpi5 and moose are on the
    tailnet and the name is the whole address; the rpi4 is not, and is reached
    across a phone's bridged segment through a ProxyJump. Saying which was used
    is not decoration -- when a board on that segment goes quiet, every board on
    that cable went quiet at once and none of them is at fault.
    """
    ts = _bash(_REACH_TAILNET_SH, machine)
    if ts is not _TIMED_OUT and ts.strip():
        return f"tailnet: {ts.strip()}"
    other = _bash(_REACH_OTHER_SH, machine)
    if other is not _TIMED_OUT and other.strip():
        return f"not on the tailnet: {other.strip()}"
    slow = [n for n, v in (("tailscale", ts), ("ssh/mDNS", other)) if v is _TIMED_OUT]
    if slow:
        return "no route derived, and " + " and ".join(slow) + " did not answer in time"
    return "no route derived (neither the tailnet nor ssh nor mDNS answered)"


def host_os():
    return "macos" if sys.platform == "darwin" else "linux"


# --- validation --------------------------------------------------------------


def want_name(kind, value, remedy):
    if not isinstance(value, str) or not value:
        raise Refused(f"no {kind} was named", remedy)
    if not NAME_RE.match(value) or len(value) > 128:
        raise Refused(
            f"{kind} '{value[:64]}' is not a name: [A-Za-z0-9._-], not starting with '-'",
            remedy,
        )
    return value


def want_bench_device(args):
    """The central refusal. Everything mutating goes through it.

    Three questions, in the order that makes the answer most useful: is it a
    machine at all, is it a machine this workspace has any business rebooting,
    and can this host drive it.
    """
    machines = fleet()
    name = want_name(
        "machine", args.get("machine"), "ask this broker for 'capabilities' -- it lists the machines it will act on"
    )
    m = machines.get(name)
    if m is None:
        known = ", ".join(sorted(machines)) or "(none declared)"
        raise Refused(
            f"unknown machine '{name}' -- this fleet declares: {known}",
            "a machine is a file: boot/machines/<name>.conf",
        )
    if m["role"] != "bench-device":
        benches = ", ".join(
            sorted(k for k, v in machines.items() if v["role"] == "bench-device")
        ) or "(none)"
        raise Refused(
            f"'{name}' is declared MACH_ROLE={m['role']}, not bench-device. This broker "
            f"acts on bench devices only: a workspace that can reboot a workstation is "
            f"the sandbox escape it exists to prevent.",
            f"bench devices here: {benches}. To act on '{name}', run 'wk boot {name}' "
            f"on the workstation yourself.",
        )
    if m["os"] not in ("any", host_os()):
        raise Refused(
            f"'{name}' is driven from a {m['os']} host only (MACH_OS in "
            f"boot/machines/{name}.conf), and this broker runs on {host_os()}",
            f"run the request against the broker on the {m['os']} workstation",
        )
    return m


def want_system(args, machine):
    """A system id, or nothing at all.

    Nothing is the common case and the right default: `wk boot` resolves the
    newest system for the machine's own profile, which is one place that
    already knows how. What is refused here is a *named* system that this
    machine's store does not hold -- because the alternative is discovering it
    after a board has rebooted into nothing.
    """
    sysid = args.get("system")
    if not sysid:
        return None
    want_name("system", sysid, "omit 'system' to take the newest one built for this machine")
    have = systems()
    if sysid not in have:
        listed = ", ".join(sorted(have)[-6:]) or "(the store is empty)"
        raise Refused(
            f"no system '{sysid}' in this machine's image store",
            f"newest in the store: {listed}. Build one with 'wk sysimage build "
            f"{machine['profile']}' on the workstation.",
        )
    return sysid


def want_plan(args):
    plan = want_name("plan", args.get("plan"), "name one of the plans this broker allows")
    if plan not in ALLOWED_PLANS:
        raise Refused(
            f"plan '{plan}' is not in this broker's plan allowlist",
            "allowed: " + ", ".join(sorted(ALLOWED_PLANS)) + ". A plan is added to "
            "ALLOWED_PLANS in container/broker/wk-broker.py, with a reason.",
        )
    return plan


def want_slot(args):
    slot = args.get("slot")
    if not slot:
        return None
    if slot not in ALLOWED_SLOTS:
        raise Refused(
            f"'{slot}' is not a build slot",
            "slots are: " + ", ".join(sorted(ALLOWED_SLOTS)),
        )
    return slot


def want_count(args):
    count = args.get("count")
    if count in (None, ""):
        return None
    try:
        n = int(count)
    except (TypeError, ValueError):
        raise Refused(f"count '{count}' is not a number", "count is an integer, 1..20")
    if not 1 <= n <= 20:
        raise Refused(f"count {n} is out of range", "count is an integer, 1..20")
    return str(n)


# --- the request record ------------------------------------------------------


def state_dir():
    """This device's own state directory -- `wk_state_dir` in lib/common.sh,
    spelled here because nothing in this process is a shell.

    Deliberately *not* $WK_STORE, and deliberately the same answer on both
    platforms. $WK_STORE is the container store: on a macOS host it names the
    podman VM's /var/lib/wk, a path this machine cannot even create (the first
    run here died on exactly that PermissionError), and on Linux it may be a
    system directory shared with the mirror and the snapshots. What the broker
    writes is neither -- it is this device's record of what it did, the same
    kind of thing as $(wk_state_dir)/bench-stage and the zed key, which already
    live there.

    One rule rather than a per-platform one, because `wk doctor` has to name
    this directory in its machine-local ledger, and a path that is derived two
    different ways is a path one of the two readers gets wrong.
    """
    return os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "wk"
    )


def requests_dir():
    return os.path.join(state_dir(), "broker")


def status_write(path, **fields):
    """The schema lib/detach.sh defines, written the way it writes it.

    Atomic through a rename in the same directory: a truncated status file is
    an expected input on a machine that can lose power mid-write, and every
    reader here and in the shell treats the file as a claim anyway.
    """
    tmp = f"{path}.tmp.{os.getpid()}"
    fields.setdefault("updated", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with open(tmp, "w") as f:
        for k, v in fields.items():
            f.write(f"{k}={v}\n")
    os.replace(tmp, path)


def status_read(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def in_flight(machine=None):
    """Requests still running, derived rather than believed.

    A status file saying `running` is a claim; the process table is the fact.
    A broker that was killed mid-request leaves exactly such a file behind, and
    believing it would refuse every later request for that machine for ever --
    which is the bug `build_live` in lib/detach.sh exists to have fixed once.
    """
    out = []
    root = requests_dir()
    for rid in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        st = status_read(os.path.join(root, rid, "status"))
        if st.get("state") != "running" or not alive(st.get("pid")):
            continue
        if machine and st.get("machine") != machine:
            continue
        out.append((rid, st))
    return out


# --- the verbs ---------------------------------------------------------------
#
# One builder per verb, each returning a fixed argv shape with validated words
# in the holes. There is no verb that takes a command, no verb that takes a
# flag, and no way to reach a `wk` subcommand that is not named here -- so the
# vocabulary is exactly what this file lists and cannot be extended from the
# socket.


def _clean_env():
    """The environment a brokered command runs in.

    Nothing from the request reaches it -- not a variable, not a value, not a
    name. WK_FORCE in particular is stripped rather than merely not set: this
    process may have been started from a shell that had it, and a barrier a
    workspace can cross by asking is not a barrier.
    """
    keep = ("HOME", "USER", "LOGNAME", "LANG", "TERM", "SSH_AUTH_SOCK",
            "XDG_RUNTIME_DIR", "XDG_STATE_HOME", "WK_STORE", "WK_ROOT", "WK_MACHINE")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PATH"] = os.environ.get(
        "PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    )
    env["WK_BROKERED"] = "1"          # so a command can say where the request came from
    for hostile in ("WK_FORCE", "WK_YES", "WK_DEBUG"):
        env.pop(hostile, None)
    return env


def build_arm(args):
    m = want_bench_device(args)
    sysid = want_system(args, m)
    argv = [os.path.join(WK_ROOT, "wk"), "boot", m["name"]]
    if sysid:
        argv += ["--system", sysid]
    if args.get("dry_run"):
        argv += ["--dry-run"]
    return m, argv, f"arm {m['name']}" + (f" for {sysid}" if sysid else " for its newest system")


def build_keep(args):
    """Claim a running bench system: cancel its self-return watchdog.

    The one verb here that removes a safety net, so it is worth being explicit
    about why it is in the vocabulary at all. Every bench system this fleet
    writes reboots itself back to host mode after its manifest's watchdog
    interval unless something claims it -- fifteen minutes on the rpi4's
    profile. That is exactly right for an arming nobody came back for, and
    fatal for a full-suite run on a Raspberry Pi 3, which is longer than that.
    So a lane that can arm and run but not claim is a lane that cannot finish,
    and the broker would be a door to nowhere.

    What keeps it safe is that it is bounded by the same two things a person's
    `wk boot --keep` is: `wk boot` refuses it unless the machine is *already*
    in bench mode (so it cannot be used to hold a workstation), and `release`
    -- or a plain reboot, or `wk boot <m> --back` at the workstation -- hands
    the board back. Every claim is in the request record with the workspace's
    name on it.
    """
    m = want_bench_device(args)
    argv = [os.path.join(WK_ROOT, "wk"), "boot", m["name"], "--keep"]
    if args.get("dry_run"):
        argv += ["--dry-run"]
    return m, argv, f"claim {m['name']} (cancel its self-return watchdog)"


def build_release(args):
    m = want_bench_device(args)
    argv = [os.path.join(WK_ROOT, "wk"), "boot", m["name"], "--back"]
    if args.get("dry_run"):
        argv += ["--dry-run"]
    return m, argv, f"reboot {m['name']} back to host mode"


def build_disarm(args):
    m = want_bench_device(args)
    argv = [os.path.join(WK_ROOT, "wk"), "boot", m["name"], "--disarm"]
    if args.get("dry_run"):
        argv += ["--dry-run"]
    return m, argv, f"cancel {m['name']}'s unspent arming"


def build_stage(args):
    m = want_bench_device(args)
    ws = want_name(
        "workspace", args.get("workspace"),
        "name the workspace whose build should go on the board",
    )
    argv = [os.path.join(WK_ROOT, "wk"), "pi", "deploy", ws, m["name"]]
    slot = want_slot(args)
    if slot:
        argv += ["--slot", slot]
    if args.get("skeleton"):
        argv += ["--skeleton"]
    return m, argv, f"stage '{ws}' onto {m['name']}"


def build_run(args):
    m = want_bench_device(args)
    plan = want_plan(args)
    argv = [os.path.join(WK_ROOT, "wk"), "pi", "bench", m["name"], plan]
    slot = want_slot(args)
    if slot:
        argv += ["--slot", slot]
    count = want_count(args)
    if count:
        argv += ["--count", count]
    return m, argv, f"run {plan} on {m['name']}"


def build_status(args):
    m = want_bench_device(args)
    argv = [os.path.join(WK_ROOT, "wk"), "boot", m["name"], "--status"]
    return m, argv, f"read {m['name']}'s mode"


# verb -> (builder, mutates)
#
# `mutates` decides one thing: whether this verb takes the machine, so that a
# second request naming it is refused while the first is in flight. A status
# read changes nothing and must never be blocked by a run it is asking about --
# rule 6 in docs/HANDOFF-workspace-state.md, read-only is read-only absolutely.
#
# Every verb leaves a request record, mutating or not, because the record *is*
# the log the caller streams from and reads back afterwards.
VERBS = {
    "arm": (build_arm, True),
    "keep": (build_keep, True),
    "release": (build_release, True),
    "disarm": (build_disarm, True),
    "stage": (build_stage, True),
    "run": (build_run, True),
    "status": (build_status, False),
}


# --- serving -----------------------------------------------------------------


class Broker:
    def __init__(self):
        self.active = 0

    async def send(self, w, obj):
        """Write one event, and never let a gone client end a request.

        A workspace that closes the connection has not cancelled anything: the
        board is mid-arming or mid-run, and the only sane thing is to finish
        and record it. So a broken pipe here is swallowed -- the words are in
        the request log either way, and `status request=<id>` reads them back.
        """
        try:
            w.write((json.dumps(obj) + "\n").encode())
            await w.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    async def capabilities(self, w):
        machines = fleet()
        names = [
            k for k, v in sorted(machines.items())
            if v["role"] == "bench-device" and v["os"] in ("any", host_os())
        ]
        # Off the event loop and all at once. Each of these can end in a
        # tailscale query and an mDNS lookup, and doing them in a list
        # comprehension froze the whole broker for their sum -- a boundary that
        # cannot answer a second caller while it works out where the first
        # one's board is.
        where = await asyncio.gather(*(asyncio.to_thread(reach, n) for n in names))
        benches = {
            n: {
                "profile": machines[n]["profile"],
                "note": machines[n]["note"],
                "reach": r,
                "os": machines[n]["os"],
            }
            for n, r in zip(names, where)
        }
        refused = {
            k: f"MACH_ROLE={v['role']}"
            for k, v in sorted(machines.items())
            if v["role"] != "bench-device"
        }
        await self.send(
            w,
            {
                "event": "capabilities",
                "host": socket.gethostname(),
                "host_os": host_os(),
                "verbs": sorted(VERBS) + ["capabilities"],
                "machines": benches,
                "not_bench_devices": refused,
                "plans": sorted(ALLOWED_PLANS),
                "slots": sorted(ALLOWED_SLOTS),
                "systems": sorted(systems())[-10:],
                "in_flight": [
                    {"request": rid, "machine": st.get("machine"), "verb": st.get("verb")}
                    for rid, st in in_flight()
                ],
            },
        )

    async def run_request(self, w, verb, args):
        builder, mutates = VERBS[verb]
        m, argv, what = builder(args)

        if mutates:
            busy = in_flight(m["name"])
            if busy:
                rid, st = busy[0]
                raise Refused(
                    f"{m['name']} already has a request in flight: {st.get('verb')} "
                    f"(request {rid}, started {st.get('updated')})",
                    f"wait for it, or ask for its progress: status request={rid}",
                )

        # A record per request, and the id has to be one too. The stamp is to
        # the second, and two `status` reads of one machine inside one second
        # would otherwise share a directory and interleave their logs -- the
        # per-machine lock does not cover them, deliberately, because a
        # read-only verb must never be blocked by the run it is asking about.
        base = "{}-{}-{}".format(
            time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), verb, m["name"]
        )
        rid, n = base, 1
        while os.path.exists(os.path.join(requests_dir(), rid)):
            n += 1
            rid = f"{base}.{n}"
        rdir = os.path.join(requests_dir(), rid)
        os.makedirs(rdir, mode=0o700, exist_ok=True)
        logfile = os.path.join(rdir, "log")
        statusfile = os.path.join(rdir, "status")
        with open(os.path.join(rdir, "request"), "w") as f:
            json.dump({"verb": verb, "args": args, "argv": argv}, f, indent=2)

        where = await asyncio.to_thread(reach, m["name"])
        await self.send(
            w,
            {
                "event": "accepted",
                "request": rid,
                "verb": verb,
                "machine": m["name"],
                "what": what,
                "reach": where,
                # Printed on every reply, because it is the thing a reader most
                # often has to work out for themselves afterwards: what did the
                # boundary actually do on my behalf.
                "runs": " ".join(argv[1:]),
                "log": logfile,
            },
        )
        log(f"{rid}: {' '.join(argv[1:])}  [{where}]")

        out = open(logfile, "ab", buffering=0)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=WK_ROOT,
            env=_clean_env(),
            start_new_session=True,
        )
        status_write(
            statusfile, state="running", pid=proc.pid, log=logfile,
            verb=verb, machine=m["name"], stage=what,
        )

        # The log is streamed while the caller is connected and keeps being
        # written when it is not. A workspace that loses its connection has not
        # lost the request: `status request=<id>` picks the words back up, and
        # the board is not left half-armed because a socket closed.
        #
        # Drained once more *after* the process is reaped, and that final read
        # is the whole reason this is a loop here rather than a background
        # task. As a task it was cancelled the instant `wait()` returned, so
        # everything the command printed in its last 400 ms went to the log and
        # not to the caller -- which for `wk boot --status` is the entire
        # output, since it prints and exits. Measured here, 2026-08-24: the
        # exit code arrived and not one line of the reason for it.
        waiter = asyncio.ensure_future(proc.wait())
        pos = 0
        deadline = time.time() + RUN_TIMEOUT
        try:
            while True:
                pos = await self._drain(w, logfile, pos)
                if waiter.done():
                    break
                if time.time() > deadline:
                    proc.kill()
                    break
                await asyncio.sleep(0.4)
            rc = await waiter
            await self._drain(w, logfile, pos)
        finally:
            out.close()
        state = "ok" if rc == 0 else "failed"
        status_write(
            statusfile, state=state, rc=rc, log=logfile,
            verb=verb, machine=m["name"], stage=what,
        )
        await self.send(
            w, {"event": "done", "request": rid, "ok": rc == 0, "rc": rc, "state": state}
        )

    async def _drain(self, w, path, pos):
        """Send whatever the command has written since `pos`, and say where it
        got to. Reading the file rather than a pipe is deliberate: the log is
        the record, and a reader that took the bytes off a pipe would be the
        only copy of them."""
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            return pos
        for line in chunk.decode("utf-8", "replace").splitlines():
            await self.send(w, {"event": "log", "line": line})
        return pos

    async def request_status(self, w, rid):
        want_name("request", rid, "ask for 'capabilities' -- it lists what is in flight")
        rdir = os.path.join(requests_dir(), rid)
        st = status_read(os.path.join(rdir, "status"))
        if not st:
            raise Refused(f"no request '{rid}'", "'capabilities' lists what is in flight")
        live = st.get("state") == "running" and alive(st.get("pid"))
        try:
            with open(os.path.join(rdir, "log")) as f:
                tail = f.read()[-8000:]
        except OSError:
            tail = ""
        await self.send(
            w,
            {
                "event": "request",
                "request": rid,
                # Evidence, not the claim: a `running` record whose process is
                # gone is a crashed request, and saying "running" about it is
                # the lie this whole repo is arranged to avoid.
                "state": st.get("state") if live or st.get("state") != "running" else "crashed",
                "verb": st.get("verb"),
                "machine": st.get("machine"),
                "rc": st.get("rc"),
                "log_tail": tail,
            },
        )

    async def handle(self, r, w):
        if self.active >= MAX_CONNECTIONS:
            await self.send(w, {"event": "refused", "why": "the broker is busy",
                                "remedy": "retry in a moment"})
            w.close()
            return
        self.active += 1
        try:
            line = await asyncio.wait_for(r.readline(), 30)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                raise Refused("that request is not a request (too large)",
                              "one JSON object on one line")
            try:
                req = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                raise Refused("that is not JSON", "one JSON object on one line")
            if not isinstance(req, dict):
                raise Refused("a request is a JSON object", "e.g. {\"verb\": \"capabilities\"}")

            verb = req.get("verb")
            args = req.get("args") or {}
            if not isinstance(args, dict):
                raise Refused("'args' must be an object", "e.g. {\"machine\": \"rpi4\"}")

            if verb == "capabilities":
                await self.capabilities(w)
            elif verb == "status" and args.get("request"):
                await self.request_status(w, args["request"])
            elif verb in VERBS:
                await self.run_request(w, verb, args)
            else:
                raise Refused(
                    f"unknown verb '{str(verb)[:40]}'",
                    "the vocabulary is: " + ", ".join(sorted(VERBS) + ["capabilities"]),
                )
        except Refused as exc:
            log(f"REFUSE {exc.why}")
            await self.send(w, {"event": "refused", "why": exc.why, "remedy": exc.remedy})
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as exc:                            # noqa: BLE001
            log(f"internal error: {exc!r}")
            try:
                await self.send(w, {"event": "refused", "why": f"broker error: {exc}",
                                    "remedy": "the broker's log has the detail: "
                                              "journalctl --user -u wk-broker, or "
                                              "~/.local/state/wk/broker.log on macOS"})
            except OSError:
                pass
        finally:
            self.active -= 1
            try:
                w.close()
            except OSError:
                pass


# --- publishing the socket where the containers are --------------------------


async def publish_into_machine(machine, local_sock):
    """Export this socket into the podman machine, on a macOS workstation.

    On Linux the containers and the broker share a machine, so the socket in
    $XDG_RUNTIME_DIR/wk is already the one they bind-mount at /run/wk and there
    is nothing to do. On macOS they do not: the containers live inside a Fedora
    CoreOS guest and mount *its* runtime directory, while everything this
    broker calls -- the tailnet, the ssh identities, the image store, the fleet
    drivers -- is out here on the Mac and cannot move in there.

    So the door is held open from this side, by ssh with a remote unix-socket
    forward. The direction matters: the Mac dials the guest, using the identity
    podman itself created, and nothing on the Mac starts listening on a network
    address. A TCP listener would be a door for anything that can reach this
    machine; this one exists only while this process chooses to hold it.

    Re-established rather than repaired. The guest's sshd will not replace an
    existing socket file (StreamLocalBindUnlink defaults to no), so a stale one
    from a previous run is removed before each connect -- which is also what
    makes a broker restart converge instead of silently forwarding nothing.
    """
    while True:
        try:
            def q(fmt):
                return subprocess.run(
                    ["podman", "machine", "inspect", machine, "--format", fmt],
                    capture_output=True, timeout=30,
                ).stdout.decode().strip()

            port, key, user = q("{{.SSHConfig.Port}}"), q("{{.SSHConfig.IdentityPath}}"), q("{{.SSHConfig.RemoteUsername}}")
            if not port or not key:
                raise OSError(f"podman machine '{machine}' is not there")
            base = [
                "ssh", "-q", "-p", port, "-i", key,
                # The guest is recreated by `./setup` and its key changes with
                # it; this is a loopback port to a VM this machine just asked
                # podman about, so there is no identity here for a known_hosts
                # file to be protecting.
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                "-o", "ExitOnForwardFailure=yes",
                f"{user or 'core'}@localhost",
            ]
            rt = subprocess.run(
                base + ["printf %s \"$XDG_RUNTIME_DIR\""], capture_output=True, timeout=30
            ).stdout.decode().strip()
            if not rt:
                raise OSError("the machine reported no XDG_RUNTIME_DIR")
            remote = f"{rt}/wk/broker.sock"
            subprocess.run(base + [f"mkdir -p {rt}/wk && rm -f {remote}"], timeout=30)
            log(f"publishing {local_sock} into machine '{machine}' at {remote}")
            argv = base[:-1] + ["-N", "-R", f"{remote}:{local_sock}", base[-1]]
            proc = await asyncio.create_subprocess_exec(*argv)
            rc = await proc.wait()
            log(f"the forward into '{machine}' ended (rc={rc}); re-establishing")
        except Exception as exc:                            # noqa: BLE001
            log(f"cannot publish into machine '{machine}': {exc}")
        await asyncio.sleep(5)


# --- startup -----------------------------------------------------------------


def sd_notify(state):
    """Type=notify, for the same reason the proxy is: `wk doctor` treats "the
    service is active" as "the door is open", and a Type=simple service is
    active before the socket exists."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.connect(addr)
        sock.sendall(state.encode())


def socket_path():
    if os.environ.get("WK_BROKER_SOCKET"):
        return os.environ["WK_BROKER_SOCKET"]
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return os.path.join(rt, "wk", "broker.sock")
    # macOS has no XDG_RUNTIME_DIR. The Mac-side path is private to this
    # process and the forward above -- what a container sees is the guest's
    # /run/wk/broker.sock, which publish_into_machine puts there.
    return os.path.join(state_dir(), "broker.sock")


async def main():
    path = socket_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.makedirs(requests_dir(), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)

    broker = Broker()
    server = await asyncio.start_unix_server(broker.handle, path=path)
    # The workspace runs as this same uid (--userns keep-id), so 0600 is both
    # enough and the tightest thing that works -- exactly as for proxy.sock.
    os.chmod(path, 0o600)
    log(f"listening on {path}")

    machines = fleet()
    benches = sorted(k for k, v in machines.items() if v["role"] == "bench-device")
    log(f"bench devices: {', '.join(benches) or '(none declared)'}")
    log(f"verbs: {', '.join(sorted(VERBS) + ['capabilities'])}")
    log(f"plans: {', '.join(sorted(ALLOWED_PLANS))}")

    tasks = [server.serve_forever()]
    publish = os.environ.get("WK_BROKER_PUBLISH_MACHINE")
    if publish:
        # Never silently. A broker that was asked to publish and quietly did
        # not looks perfectly healthy from out here and is invisible from every
        # workspace -- which is precisely what happened on the first run on
        # this Mac, where launchd's minimal PATH does not contain podman's
        # official pkg location. The rule (CLAUDE.md, "Refusals"): an
        # unavailable dependency is reported, not worked around.
        if shutil.which("podman"):
            tasks.append(publish_into_machine(publish, path))
        else:
            log(f"asked to publish into podman machine '{publish}' but there is no "
                f"podman on PATH ({os.environ.get('PATH')}) -- no workspace in that "
                f"machine can see this broker. Fix the PATH in the LaunchAgent: "
                f"./setup --stage broker")
    else:
        log("not publishing into a podman machine: the containers here share "
            "this machine's runtime directory, so the socket above is already "
            "the one they mount at /run/wk")
    sd_notify("READY=1")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
