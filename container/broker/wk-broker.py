#!/usr/bin/env python3
"""The workspace fleet-request boundary, shaped like the egress proxy: a unix
socket into the sandbox, the policy out here, a closed verb set, every action
an argv list handed to execve. Each verb resolves to a `wk` command that
already refuses on its own evidence -- a narrower door, never a wider one."""

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

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "lib"))
from wknotify import sd_notify  # noqa: E402

WK_ROOT = os.environ.get(
    "WK_ROOT", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

# Which plans a workspace may spend an hour of a physical board on, unattended.
ALLOWED_PLANS = {
    "jetstream3",                      # CPU-class, and needing no display
    "jetstream2",
    "octane",
    "kraken",
    "sunspider",
    "speedometer3",                    # GPU-class: refused with no display
    "speedometer2",
    "motionmark1.3",
}

ALLOWED_SLOTS = {"a", "b"}             # a build slot is a directory on the board

# Matched first, so no lookup is handed a path, an option or a traversal.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_REQUEST_BYTES = 8192
MAX_CONNECTIONS = 32
RUN_TIMEOUT = 6 * 3600                 # `wk pi bench --ab` on a Raspberry Pi 3


def log(msg):
    print(f"[wk-broker] {msg}", file=sys.stderr, flush=True)


class Refused(Exception):
    def __init__(self, why, remedy):
        super().__init__(why)
        self.why = why
        self.remedy = remedy


_TIMED_OUT = object()                  # not the same answer as "no answer"


def _bash(script, *args):
    # Every fleet fact is derived from evidence by shell in this repo already.
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

# Two fragments under two ceilings: run together, a wedged tailscaled swallowed
# the bridge answer too and "no route derived" was reported about a pinned board.
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
    out = {}                           # read now, never held between requests
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


def reach(machine):
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


def want_name(kind, value, remedy):
    if not isinstance(value, str) or not value:
        raise Refused(f"no {kind} was named", remedy)
    if not NAME_RE.match(value) or len(value) > 128:
        raise Refused(
            f"{kind} '{value[:64]}' is not a name: [A-Za-z0-9._-], not starting with '-'",
            remedy,
        )
    return value


def want_bench_device(args):           # the central refusal, which everything
    machines = fleet()                 # mutating goes through
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
            f"'{name}' is declared NODE_ROLE={m['role']}, not bench-device. This broker "
            f"acts on bench devices only: a workspace that can reboot a workstation is "
            f"the sandbox escape it exists to prevent.",
            f"bench devices here: {benches}. To act on '{name}', run 'wk boot {name}' "
            f"on the workstation yourself.",
        )
    if m["os"] not in ("any", host_os()):
        raise Refused(
            f"'{name}' is driven from a {m['os']} host only (NODE_OS in "
            f"boot/machines/{name}.conf), and this broker runs on {host_os()}",
            f"run the request against the broker on the {m['os']} workstation",
        )
    return m


def want_system(args, machine):
    sysid = args.get("system")         # absent: `wk boot` arms what it holds
    if not sysid:
        return None
    want_name("system", sysid, "omit 'system' to arm the system the device holds")
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


def state_dir():
    # `wk_state_dir` in lib/common.sh. Never $WK_STORE: on macOS that names
    # the podman VM's store, a path this machine cannot even create.
    return os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "wk"
    )


def requests_dir():
    return os.path.join(state_dir(), "broker")


def status_write(path, **fields):      # the schema lib/detach.sh defines
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
    # A `running` status file is a claim; the process table is the fact.
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


def _clean_env():
    # Nothing from a request reaches this, and WK_FORCE is stripped rather
    # than merely unset: this process may have been started from a shell.
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
    # A bench system reboots itself back to host mode after its manifest's
    # watchdog interval -- 15 minutes on the rpi4, shorter than a Pi 3 suite.
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


VERBS = {                              # verb -> (builder, takes the machine)
    "arm": (build_arm, True),
    "keep": (build_keep, True),
    "release": (build_release, True),
    "disarm": (build_disarm, True),
    "stage": (build_stage, True),
    "run": (build_run, True),
    "status": (build_status, False),
}


class Broker:
    def __init__(self):
        self.active = 0

    async def send(self, w, obj):
        try:                           # a closed client has not cancelled the
            w.write((json.dumps(obj) + "\n").encode())   # run it asked for
            await w.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    async def capabilities(self, w):
        machines = fleet()
        names = [
            k for k, v in sorted(machines.items())
            if v["role"] == "bench-device" and v["os"] in ("any", host_os())
        ]
        # Each of these can end in a tailscale query and an mDNS lookup, whose
        # sum froze the whole broker when they ran in a comprehension.
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
            k: f"NODE_ROLE={v['role']}"
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

        # A loop, not a task, so as to drain once more after the process is
        # reaped: a task dies the instant `wait()` returns, losing the tail.
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
        try:                           # the file, not a pipe: it is the record
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


async def publish_into_machine(machine, local_sock):
    # On macOS the containers mount the podman guest's runtime directory, so
    # the socket is carried in over a remote unix-socket forward the Mac dials
    # itself. The guest's sshd will not replace an existing one: remove it.
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
                "-o", "StrictHostKeyChecking=no",  # a rekeyed loopback VM port
                "-o", "UserKnownHostsFile=/dev/null",
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


def socket_path():
    if os.environ.get("WK_BROKER_SOCKET"):
        return os.environ["WK_BROKER_SOCKET"]
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt:
        return os.path.join(rt, "wk", "broker.sock")
    return os.path.join(state_dir(), "broker.sock")   # macOS: no runtime dir


async def main():
    path = socket_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.makedirs(requests_dir(), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)

    broker = Broker()
    server = await asyncio.start_unix_server(broker.handle, path=path)
    os.chmod(path, 0o600)              # same uid as the workspace (keep-id)
    log(f"listening on {path}")

    machines = fleet()
    benches = sorted(k for k, v in machines.items() if v["role"] == "bench-device")
    log(f"bench devices: {', '.join(benches) or '(none declared)'}")
    log(f"verbs: {', '.join(sorted(VERBS) + ['capabilities'])}")
    log(f"plans: {', '.join(sorted(ALLOWED_PLANS))}")

    tasks = [server.serve_forever()]
    publish = os.environ.get("WK_BROKER_PUBLISH_MACHINE")
    if publish:
        # launchd's minimal PATH does not carry podman's pkg location.
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
