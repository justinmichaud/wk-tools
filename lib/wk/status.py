"""The `wk status` walk: one job per target, then the fleet's devices and
bridges, each returning the records wk.statusview draws and the worst exit
code it found. Every fact is read as the walk runs; nothing is stored. The
boot drivers and the reach probes are still bash and are asked through
lib/target.sh's libraries."""

import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import secretfile
import wkdata
from wk import record, shell, statusview, targets
from wk.clock import Clock
from wk.machine import TIMED_OUT, Local
from wk.record import Records
from wk.store import Store, lock_holder_pid

ANSI = re.compile(r"\x1b\[[0-9;]*m")
ENDED_AS_ASKED = ("ok", "cancelled", "stopped", "refused")
METHOD = {"container": "container", "vm": "macOS guest"}
RANK = {"container": 0, "vm": 1, "local": 2}
UPSTREAM_ORIGIN = "https://github.com/WebKit/WebKit.git"
SDK_TAG = re.compile(r"^(.+)-v(\d+)-[0-9a-f]+$")
SERVICES = (("wk-proxy.service", "egress proxy", "workspaces have no network without it"),
            ("wk-github-inject.service", "credential injector", "'git-webkit pr' and 'gh' in a workspace get no credential"))

WS_PROBE = r'''cd @SRC@ 2>/dev/null || exit 0
printf 'origin=%s\n' "$(git config --get remote.origin.url 2>/dev/null)"
printf 'dirty=%s\n' "$(git status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')"
printf 'untracked=%s\n' "$(git ls-files --others --exclude-standard 2>/dev/null | wc -l | tr -d ' ')"
printf 'unpushed=%s\n' "$(git rev-list --count HEAD --not --remotes 2>/dev/null)"
_u=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || _u=origin/main
printf 'upstream=%s\n' "$_u"
set -- $(git rev-list --left-right --count "$_u...HEAD" 2>/dev/null)
printf 'behind=%s\nahead=%s\n' "${1:-}" "${2:-}"
@BASE@
printf 'wsbase=%s\n' "${_b:-?}"
'''

FLEET_PROBE = r'''set +e
export WK_SSH_TIMEOUT="${WK_FLEET_TIMEOUT:-4}"
. "$WK_ROOT/lib/image.sh"
. "$WK_ROOT/image/profiles.sh"
. "$WK_ROOT/boot/machines.sh"
machine_load "$1" || exit 0
load_driver "$NODE_DRIVER" 2>/dev/null || exit 0
armed=""; probeable=yes
if b_probeable 2>/dev/null; then
    b_probe 2>/dev/null
    [ "${MODE:-}" != host ] || armed=$(record_read 2>/dev/null | kv_get image)
else
    MODE=""; probeable=no
fi
media=$(b_media 2>/dev/null || printf 'unknown')
if [ -z "${NODE_PROFILE:-}" ]; then
    reprov="missing NODE_PROFILE in boot/machines/$1.conf -- nothing to compose a recipe from"
else
    reprov=$(b_reprovision 2>/dev/null || true)
fi
printf '%s\0' "${NODE_ROLE:-workstation}" "$probeable" "${MODE:-}" "${NODE_BRIDGE:-}" "$armed" "$media" "$reprov" \
    "$(fleet_tailnet "$1")" "$(reach_without_tailnet "$1")"
'''

BRIDGE_PROBE = r'''
printf "reachable=yes\n"
if [ -x /usr/local/sbin/wk-bridge-healthcheck ]; then
    printf "role=yes\n"
    printf "sum=%s\n" "$(cat $(ls /usr/local/sbin/wk-bridge-* 2>/dev/null | sort) \
        $(ls /etc/init.d/wk-bridge-* 2>/dev/null | sort) 2>/dev/null | cksum | cut -d" " -f1)"
    if [ "$(id -u)" = 0 ]; then
        _h=$(/usr/local/sbin/wk-bridge-healthcheck 2>&1); _rc=$?
    elif command -v doas >/dev/null 2>&1; then
        _h=$(doas -n /usr/local/sbin/wk-bridge-healthcheck 2>&1); _rc=$?
    else
        _h=$(sudo -n /usr/local/sbin/wk-bridge-healthcheck 2>&1); _rc=$?
    fi
    printf "health=%s\n" "$_rc"
    printf "healthline=%s\n" "$(printf "%s" "$_h" | grep -v "^[[:space:]]*$" | tail -1)"
else
    printf "role=no\n"
fi'''


def clean(text):
    return ANSI.sub("", str(text)).replace("\r", "")


def bump(worst, code):
    """The walk's exit code only rises; anything outside 0-4 reads as 4."""
    try:
        code = int(str(code).strip())
    except ValueError:
        code = 4
    if code < 0 or code > 4:
        code = 4
    return max(worst, code)


def kv(text):
    out = {}
    for line in clean(text).splitlines():
        k, eq, v = line.partition("=")
        if eq and k not in out:
            out[k] = v
    return out


def kv_file(path):
    try:
        with open(path, errors="replace") as f:
            return kv(f.read())
    except OSError:
        return {}


class Rec:
    """One record: `kind` first, strings cleaned of escapes, notes last."""

    def __init__(self, kind, **fields):
        self.d = {"kind": kind}
        self.notes = []
        for k, v in fields.items():
            self.set(k, v)

    def set(self, key, value):
        self.d[key] = clean(value) if isinstance(value, (str, int, float)) else value

    def opt(self, key, value):
        if value:
            self.set(key, value)

    def raw(self, key, value):
        self.d[key] = value

    def note(self, text):
        self.notes.append({"level": "info", "text": clean(text)})

    def warn(self, text):
        self.notes.append({"level": "warn", "text": clean(text)})

    def done(self):
        d = dict(self.d)
        if self.notes:
            d["notes"] = self.notes
        return d


def _bash(root, snippet, *args, timeout=None):
    """The snippet after the bash libraries, with `args` as its positionals."""
    return Local().run(["bash", "-c", shell.prelude(root) + snippet + "\n", "wk", *args], input="", timeout=timeout)


def sha_matches(a, b):
    """`git rev-parse --short` picks its own length per repository, so one abbreviation can be a prefix of the other."""
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def far_side_reason(target, side, why):
    if side == "unreachable":
        return "unreachable over ssh" + (": %s" % why if why else "")
    if side == "stopped":
        return "the podman machine '%s' is stopped -- 'wk start' brings it up" % target.env.get("WK_MACHINE", "wk")
    if side == "no-wk":
        return "no wk-tools there yet -- 'wk remote setup %s'" % target.name
    return "not a machine of its own"


def fleet_mode(probeable, mode, bridge):
    if probeable == "no":
        return "unknown from here"
    if mode == "host":
        return "host mode"
    if mode.startswith("base"):
        return "base image -- not a bench system"
    if mode.startswith("bench"):
        return "bench mode"
    return "unreachable" + (" via %s" % bridge if bridge else "")


def sdk_newest(local, tags):
    """The registry tag that supersedes `local`: the highest -v<N> sharing its version, else itself when listed."""
    m = SDK_TAG.match(local)
    if not m:
        return local if local in tags else None
    prefix, best_v, best = m.group(1), int(m.group(2)), local
    for t in tags:
        mm = SDK_TAG.match(t)
        if mm and mm.group(1) == prefix and int(mm.group(2)) > best_v:
            best, best_v = t, int(mm.group(2))
    return best


def sdk_record(machine, local, tags, cap):
    """`local` is t_sdk_local's image= and created=; `tags` the registry's list, None when it did not answer in `cap`."""
    image = local.get("image", "")
    if not image:
        return None
    r = Rec("sdk", machine=machine, tag=image.rsplit(":", 1)[-1])
    r.opt("pulled", local.get("created"))
    if tags is None:
        r.set("unknown", "registry did not answer within %ss" % cap)
    else:
        newest = sdk_newest(r.d["tag"], tags)
        if newest:
            r.set("upstream", newest)
        else:
            r.set("unknown", "registry has no tag matching %s" % r.d["tag"])
    return r.done()


def tools_fact(ver, expect, machine, label, in_vm=False, peer=False, dirty_here=False):
    """One machine's wk-tools against this checkout's commit; `ver` is `wk version`'s sha= and dirty=."""
    sha = ver.get("sha", "")
    insync = sha_matches(sha, expect)
    r = Rec("fact", machine=machine, type="wk-tools")
    r.opt("copy", "mounted in the podman VM" if in_vm else "")
    r.set("sha", sha)
    r.raw("dirty", ver.get("dirty") == "yes")
    r.set("expect", expect)
    r.raw("insync", insync)
    if not insync:
        if in_vm:
            r.set("fix", "./setup   (recreates the machine with this checkout mounted at /opt/wk-tools)")
        elif peer and dirty_here:
            r.set("fix", "commit and push here first -- a peer pulls, and this checkout is dirty")
        elif peer:
            r.set("fix", "wk sync --tools   (pulls on %s, and says so if it still differs)" % label)
        else:
            r.set("fix", "wk sync --tools %s" % label)
    return r.done()


def task_records(records, only=None, clock=None):
    """One record per task that is running or ended badly; one that ended as asked left its product as the report."""
    clock = clock or records.clock
    ask = os.environ.get("WK_TASK_ASK_SECONDS", "5")
    out, worst = [], 0
    for t in records.list():
        if only and t.field("name") != only:
            continue
        st = t.verdict("capped")
        if st in ENDED_AS_ASKED:
            continue
        kind, name, log = t.field("kind"), t.field("name"), t.field("log")
        r = Rec("task", machine=t.field("machine"), task=t.id, task_kind=kind, name=name, state=st, since=t.field("started"))
        r.raw("steps", [s for _, s in t.steps()])
        r.set("kill", t.field("kill"))
        r.opt("subject", t.field("subject"))
        r.opt("log", log)
        r.opt("holds", t.field("holds"))
        r.opt("exit", t.field("exit"))
        r.raw("plan", t.plan())
        age = record.log_age(log, clock)
        abort = t.field("abort_after")
        shown_age = "?" if age is None else str(age)
        if st == "silent":
            r.opt("log_age", shown_age if age is not None else "")
            if age is not None and abort.isdigit() and age > int(abort):
                r.warn("silent for %ss, past the %ss this %s recorded as its watchdog's deadline" % (age, abort, kind))
                r.note("so the watchdog is gone as well: a live 'wk %s' would have\n      killed the job and written its exit "
                       "at that deadline. Re-run it to\n      rewrite the record." % kind)
                worst = bump(worst, 4)
            else:
                r.warn("no log output for %ss -- counted as busy, since nothing\n      here is evidence it stopped. "
                       "Follow it:  tail -f %s" % (shown_age, log))
                worst = bump(worst, 2)
        elif st == "running":
            r.note("alive: %s (last output %ss ago)" % (record.progress_line(log) or "running", shown_age))
            worst = bump(worst, 2)
        elif st == "starting":
            worst = bump(worst, 2)
        elif st == "died":
            r.warn("%s '%s' died without recording an exit -- its log is %s" % (kind, name, log))
            worst = bump(worst, 4)
        elif st == "unanswered":
            r.warn("'%s' did not say within %ss whether pid %s is alive, so\n      nothing here can confirm the record. "
                   "Ask it directly:  wk enter %s" % (name, ask, t.field("pid"), name))
            worst = bump(worst, 4)
        elif st == "failed":
            r.warn("exit %s" % t.field("exit"))
            for line in record.first_error(log):
                r.note("  " + line)
            worst = bump(worst, 1)
        elif st == "stalled":
            r.warn("killed after no output; see %s" % log)
            worst = bump(worst, 3)
        elif st in ("gave-up", "error"):
            r.warn("%s gave up -- see %s" % (kind, log))
            worst = bump(worst, 1)
        elif st == "oom":
            r.warn("killed for memory; see %s" % log)
            try:
                hits = [l for l in record.normalised(log).split("\n") if "wk: MEMORY LIMIT" in l]
            except OSError:
                hits = []
            if hits:
                r.note("  " + hits[-1].replace("wk: ", "", 1))
            worst = bump(worst, 3)
        out.append(r.done())
    return out, worst


def disk_record(store, machine, in_vm, reclaimable):
    root = store.root()
    if not os.path.isdir(root):
        return None
    r = Rec("disk", machine=machine, store=root)
    r.opt("where", "in the podman VM" if in_vm else "")
    try:
        u = shutil.disk_usage(root)
        r.set("total_mb", u.total // 1048576)
        r.set("free_mb", u.free // 1048576)
        r.set("used_pct", math.ceil(u.used * 100 / (u.used + u.free)) if u.used + u.free else 0)
    except OSError:
        pass
    try:
        snapshots = len(os.listdir(store.base_dir()))
    except OSError:
        snapshots = 0
    r.set("snapshots", snapshots)
    r.set("reclaimable", reclaimable)
    if reclaimable:
        r.note("wk gc would reclaim %d snapshot(s)" % reclaimable)
    return r.done()


def broker_record(store, machine, alive):
    """The request broker: in flight is read from the process table, since a `running` status file is a claim a killed broker leaves behind."""
    env = store.env
    sock = env.get("WK_BROKER_SOCKET") or (os.path.join(env["XDG_RUNTIME_DIR"], "wk", "broker.sock") if env.get("XDG_RUNTIME_DIR")
                                          else os.path.join(store.state_dir(), "broker.sock"))
    brdir = os.path.join(store.state_dir(), "broker")
    try:
        is_sock = stat.S_ISSOCK(os.stat(sock).st_mode)
    except OSError:
        is_sock = False
    if not is_sock and not os.path.isdir(brdir):
        return None
    r = Rec("service", machine=machine, name="request broker")
    n = 0
    if os.path.isdir(brdir):
        for rid in sorted(os.listdir(brdir)):
            st = kv_file(os.path.join(brdir, rid, "status"))
            if st.get("state") != "running" or not st.get("pid", "").isdigit() or not alive(int(st["pid"])):
                continue
            n += 1
            r.note("in flight: %s -- %s" % (rid, st.get("stage", "")))
    if is_sock:
        r.set("state", "open (%d in flight)" % n)
    else:
        r.set("state", "closed")
        r.set("fix", "./setup --stage broker   (a workspace cannot ask for a bench run without it)")
    return r.done()


def unit_program(root, unit):
    try:
        with open(os.path.join(root, "host", "units", unit)) as f:
            for line in f:
                if line.startswith("ExecStart="):
                    for tok in line.split():
                        if tok.startswith("@WK_ROOT@/"):
                            return tok[len("@WK_ROOT@/"):]
    except OSError:
        pass
    return ""


def unit_stale(root, unit, run=None):
    """A service keeps the program it exec'd: this tree's copy newer than the process is code nobody can read in the tree."""
    prog = unit_program(root, unit)
    if not prog:
        return False
    r = (run or Local().run)(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit])
    pid = r.out.strip()
    if not r.ok or not pid.isdigit() or pid == "0" or not os.path.isdir("/proc/" + pid):
        return False
    try:
        return os.path.getmtime(os.path.join(root, prog)) > os.path.getmtime("/proc/" + pid)
    except OSError:
        return False


def service_records(root, machine, run=None):
    run = run or Local().run
    out = []
    if not shutil.which("systemctl"):
        return out
    for unit, label, cost in SERVICES:
        if not run(["systemctl", "--user", "cat", unit]).ok:
            continue
        r = Rec("service", machine=machine, name=label)
        if not run(["systemctl", "--user", "is-active", "--quiet", unit]).ok:
            r.set("state", "stopped")
            r.set("fix", "systemctl --user start %s   (%s)" % (unit[:-len(".service")], cost))
        elif unit_stale(root, unit, run):
            r.set("state", "running code older than this checkout")
            r.set("fix", "./setup   (restarts it on the program that is in the tree now)")
        else:
            r.set("state", "running")
        out.append(r.done())
    return out


def lock_records(store, machine, alive):
    out = []
    try:
        names = sorted(f for f in os.listdir(store.lock_dir()) if f.endswith(".lock"))
    except OSError:
        return out
    for f in names:
        path = os.path.join(store.lock_dir(), f)
        try:
            line = os.readlink(path)
        except OSError:
            line = kv_file(os.path.join(path, "payload")) and open(os.path.join(path, "payload")).read() or ""
        pid = lock_holder_pid(path)
        r = Rec("lock", machine=machine, resource=f[:-len(".lock")])
        r.opt("pid", str(pid) if pid else "")
        m = re.search(r"cmd=(.*)$", line)
        r.opt("cmd", m.group(1) if m else "")
        m = re.search(r"at=(\S+)", line)
        r.opt("at", m.group(1) if m else "")
        r.raw("alive", bool(pid and alive(pid)))
        out.append(r.done())
    return out


def push_record(store, machine, forks, in_vm):
    """The deploy keys held on disk: a count, never a switch position, which only `wk push status` measures.
    The podman VM mounts only their public halves, so it has no row."""
    if in_vm or not forks:
        return None
    held = os.path.join(os.path.dirname(store.secrets_dir()), "push-keys")
    keys = sum(1 for f in forks if os.path.isfile(os.path.join(held, "build_key_" + f)))
    absent = len(forks) - keys
    r = Rec("switch", machine=machine, name="push credentials")
    r.set("state", "keys held" if not absent else "some keys held" if keys else "no keys")
    try:
        pat = secretfile.present(os.path.join(held, "github-pat")) == 0
    except SystemExit:
        pat = False
    r.set("detail", "%d deploy key(s), %d absent, %s -- 'wk push status' says whether they are loaded"
          % (keys, absent, "an API token" if pat else "no API token"))
    return r.done()


def capacity_record(machine, where, cores, mem_mb, free_mb, load):
    r = Rec("capacity", machine=machine)
    r.opt("where", where)
    if cores and free_mb:
        r.set("cores", cores)
        r.opt("mem_mb", mem_mb)
        r.set("free_mb", free_mb)
        r.opt("load", load)
    else:
        r.set("note", "could not measure %s on %s" % ("load/memory" if where is None else "cores/memory", machine))
    return r.done()


def host_load():
    try:
        with open("/proc/loadavg") as f:
            return f.read().split()[0]
    except OSError:
        pass
    r = Local().run(["sysctl", "-n", "vm.loadavg"])
    parts = r.out.split()
    return parts[1] if r.ok and len(parts) > 1 else ""


def quiesce_dir(store):
    return store.env.get("WK_QUIESCE_STATE") or os.path.join(store.state_dir(), "quiesce")


def quiesce_record(qdir, machine):
    bits = [word for f, word in (("caffeinate.pid", "caffeinate"), ("daemons_paused", "daemons-paused"), ("raiser.pid", "raiser"))
            if os.path.isfile(os.path.join(qdir, f))]
    if not bits:
        return None
    r = Rec("switch", machine=machine, name="quiesce", state="on", detail=" ".join(bits))
    r.warn("this machine is quiesced -- 'wk quiesce off' puts it back")
    return r.done()


def bench_records(store, machine, alive):
    """Every running benchmark task, else the newest, its state recomputed from its runs (lib/wkdata.py)."""
    bdir = store.bench_dir()
    if not os.path.isdir(bdir):
        return []
    tasks = sorted(t for t in os.listdir(bdir) if os.path.isfile(os.path.join(bdir, t, "task.json")))
    running = [t for t in tasks if alive(lock_holder_pid(store.lock_path("bench-task-" + t)))]
    out = []
    for t in running or tasks[-1:]:
        path = os.path.join(bdir, t)
        try:
            st = wkdata.task_state(path, t in running)
        except Exception:
            continue
        r = Rec("bench", machine=machine, task=t, path=path, state=st["state"], summary=st["summary"],
                subject=wkdata._subject_line(st["doc"]))
        if st["state"] == "incomplete":
            r.warn("task %s stopped before every planned run ended -- wk bench report %s says what is there" % (t, t))
        out.append(r.done())
    return out


def fleet_probe(root, name, cap):
    """The board's boot driver asked under a ceiling: None when it did not answer in `cap` seconds."""
    r = _bash(root, FLEET_PROBE, name, timeout=cap)
    if r.rc == TIMED_OUT:
        return None
    if not r.ok:
        return {"error": (r.err.strip().splitlines() or ["exit %d" % r.rc])[-1]}
    parts = r.out.split("\0")
    if len(parts) < 9:
        return {}
    keys = ("role", "probeable", "mode", "bridge", "armed", "media", "reprovision", "tailnet", "direct")
    return dict(zip(keys, parts))


def fleet_record(name, conf, fields, cap, reach=None):
    """`fields` is fleet_probe's answer; `reach` answers (tailnet, direct) for a board the probe never described."""
    r = Rec("fleet", machine=name)
    if fields is None or "error" in fields:
        r.set("role", conf.get("NODE_ROLE") or "workstation")
        r.set("mode", "no answer within %ss" % cap if fields is None else "probe failed: %s" % fields["error"])
        r.set("conf", "boot/machines/%s.conf" % name)
        tailnet, direct = reach(name) if reach else ("", "")
        r.opt("tailnet", tailnet)
        r.opt("direct", direct)
        return r.done()
    if not fields:
        return None
    r.set("role", fields["role"])
    r.set("mode", fleet_mode(fields["probeable"], fields["mode"], fields["bridge"]))
    r.set("media", fields["media"])
    r.opt("armed", fields["armed"])
    r.set("conf", "boot/machines/%s.conf" % name)
    r.opt("tailnet", fields["tailnet"])
    r.opt("direct", fields["direct"])
    r.opt("reprovision", fields["reprovision"])
    return r.done()


def machine_confs(root, env):
    """(name, conf) for every board conf with a driver and a note, as boot/machines.sh's machine_list lists them."""
    d = env.get("WK_MACHINES_DIR") or os.path.join(root, "boot", "machines")
    out = []
    try:
        names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".conf"))
    except OSError:
        return out
    for n in names:
        conf = targets.read_conf(os.path.join(d, n + ".conf"))
        if conf.get("NODE_DRIVER") and conf.get("NODE_NOTE"):
            out.append((n, conf))
    return out


def bridge_role_sum(root):
    """cksum over the role's files in the order the phone sums them: bin, then init.d, each sorted."""
    data = b""
    for sub in ("bin", "init.d"):
        d = os.path.join(root, "bridge", sub)
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            with open(os.path.join(d, n), "rb") as f:
                data += f.read()
    cp = subprocess.run(["cksum"], input=data, stdout=subprocess.PIPE)
    return cp.stdout.split()[0].decode() if cp.stdout.split() else ""


def bridge_ssh(name, script, as_root, connect_timeout, cap):
    argv = ["ssh", "-o", "BatchMode=yes"] + (["-l", "root"] if as_root else []) + ["-o", "ConnectTimeout=%s" % connect_timeout, name, script]
    r = Local().run(argv, input="", timeout=cap)
    return r.out if r.ok else ""


def bridge_record(name, conf, want, fields, reach):
    r = Rec("bridge", name=name, device=conf.get("BR_DEVICE") or "?", segment=conf.get("BR_SEGMENT") or "?")
    r.opt("note", conf.get("BR_NOTE"))
    r.set("conf", "bridge/hosts/%s.conf" % name)
    tailnet, direct = reach(name)
    r.opt("tailnet", tailnet)
    r.opt("direct", direct)
    line = fields.get("healthline", "")
    if fields.get("reachable") != "yes":
        r.set("state", "unreachable")
    elif fields.get("role") != "yes":
        r.set("state", "no bridge role")
        r.warn("it answers, and nothing on it is a bridge -- 'wk bridge setup %s' provisions it" % name)
    else:
        if fields.get("health") == "0":
            r.set("state", "up")
        elif any(w in line for w in ("doas", "sudo", "not permitted", "Authentication required")):
            r.set("state", "role installed")
            r.warn("its health check needs root and this end has no\n      non-interactive route to it -- "
                   "'wk bridge status %s' can bootstrap one" % name)
        else:
            r.set("state", "unhealthy")
        r.opt("health", line)
        insync = bool(fields.get("sum")) and fields.get("sum") == want
        r.raw("role_insync", insync)
        if not insync:
            r.warn("the role on it is not this repository's -- 'wk bridge setup %s' re-provisions it" % name)
    return r.done()


def wait_until_idle(poll, timeout, interval, clock, label, info, warn):
    """Polls while `poll` says busy (2); `timeout` seconds of elapsed time, not of sleeps, ends the wait without a verdict."""
    start = clock.monotonic()
    said = False
    while True:
        rc = poll()
        if rc != 2:
            return rc
        if not said:
            info("waiting for%s to finish (wk status says busy)" % (" '%s'" % label if label else ""))
            said = True
        elapsed = int(clock.monotonic() - start)
        if timeout > 0 and elapsed >= timeout:
            warn("still busy after %ds -- the work continues; this only stopped waiting" % elapsed)
            return rc
        clock.sleep(interval)


class Walk:
    """One `wk status`: the targets this machine lists, each a job, plus the
    fleet's devices and bridges; `records()` yields what they report."""

    def __init__(self, root, name=None, fleet=True, devices=True, env=None, clock=None, reg=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.clock = clock or Clock()
        self.reg = reg or targets.Registry(self.root, env=self.env)
        self.name = name
        self.fleet = fleet and not name
        self.devices = devices and not name
        self.this_machine = self.env.get("WK_ROW_LABEL") or record.machine_name(self.env)
        self.is_self = bool(self.env.get("WK_HOST_SELF")) or not self.env.get("WK_ROW_LABEL")
        self.in_vm = bool(self.env.get("WK_IN_VM"))
        self.lock = threading.Lock()
        self.tasks_said = set()
        self.tooling_said = False
        self.bases = {}
        self.health_owner = None
        self.worst = 0
        self._git = None
        self._loaded = {}

    # -- the walk

    def targets(self):
        names = self.reg.walk()
        return sorted(names, key=lambda t: (RANK.get(self.reg.kind(t), 3), t))

    def target(self, name):
        """The one driver object per target for this walk, so a machine is probed at most once."""
        with self.lock:
            if name not in self._loaded:
                self._loaded[name] = self.reg.load(name)
            return self._loaded[name]

    def machine_of_target(self, name):
        try:
            return self.machine_of(self.target(name))
        except LookupError:
            return self.this_machine

    def machine_of(self, target):
        if target.kind == "remote" and not target.is_local and target.host:
            return target.name
        return self.this_machine

    def _job(self, tname, name):
        def run():
            try:
                target = self.target(tname)
            except LookupError as e:
                return [Rec("raw", machine=self.this_machine, text=str(e)).done()], 4
            try:
                return self.report_target(target, name)
            except Exception as e:
                return [Rec("raw", machine=self.machine_of(target), text="%s: %s: %s" % (tname, type(e).__name__, e)).done()], 4
        return run

    def records(self, markers=True):
        """The stream: a plan, each job's records as it ends and a flush for it, then the exit; `markers=False` is the batch in start order."""
        if self.name:
            tname = self.env.get("WK_TARGET") or self.reg.ws_target(self.name)
            jobs = [(tname, self._job(tname, self.name))]
        else:
            names = self.targets()
            self.health_owner = next((t for t in names if self._is_here(t)), None)
            jobs = [(t, self._job(t, None)) for t in names]
            if self.devices:
                jobs += [("devices", self.fleet_devices), ("bridges", self.bridges)]
        if markers:
            plan = [{"job": t, "machine": self.machine_of_target(t)} for t, _ in jobs if t not in ("devices", "bridges")]
            plan += [{"job": t} for t, _ in jobs if t in ("devices", "bridges")]
            yield {"kind": "plan", "jobs": plan}
        worst = 0
        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
            futures = [(job, pool.submit(fn)) for job, fn in jobs]
            if markers:
                by_future = {f: job for job, f in futures}
                for f in as_completed(by_future):
                    recs, w = f.result()
                    worst = bump(worst, w)
                    for r in recs:
                        yield r
                    yield {"kind": "flush", "job": by_future[f]}
            else:
                for _, f in futures:
                    recs, w = f.result()
                    worst = bump(worst, w)
                    for r in recs:
                        yield r
        self.worst = worst
        yield {"kind": "exit", "code": worst}

    def worst_only(self):
        for _ in self.records(markers=False):
            pass
        return self.worst

    def _is_here(self, tname):
        try:
            return self.target(tname).is_here()
        except LookupError:
            return False

    # -- one target

    def report_target(self, target, name):
        out, worst = [], 0
        gm = self.machine_of(target)
        method = METHOD.get(target.kind, "native")
        out.append(self.machine_seen(gm))
        side, why = target.probe()
        has_wk = side == "answering"
        if has_wk:
            args = ["status", "--no-devices" if gm == self.this_machine else "--no-fleet", "--records"] + ([name] if name else [])
            recs, rc = self.delegate(target, gm, args)
            out += recs
            if gm != self.this_machine and self.fleet:
                out += self.report_machine(target, gm, has_wk)
                out.append(self.capacity_remote(target, gm))
            return out, bump(worst, rc)
        if side == "unreachable":
            worst = bump(worst, 4)
        if side in ("unreachable", "stopped"):
            out.append(Rec("raw", machine=gm, text="%s: %s" % (target.name, far_side_reason(target, side, why))).done())
            return out, worst
        records = self.records_of(target)
        if name:
            r, w = self.workspace(target, gm, method, name, records)
            out.append(r)
            worst = bump(worst, w)
        else:
            names = sorted(set(target.store.workspaces()) | {n for n, _ in target.list()})
            with ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda ws: self.workspace(target, gm, method, ws, records), names))
            for r, w in rows:
                out.append(r)
                worst = bump(worst, w)
        recs, w = self.tasks(records, name)
        out += recs
        worst = bump(worst, w)
        if self.fleet:
            out += self.report_machine(target, gm, has_wk)
            if gm == self.this_machine and target.name == self.health_owner:
                out += self.report_self()
                out += self.health(target, gm)
        return out, worst

    def records_of(self, target):
        def ask(ws, pid, cap):
            r = target.exec(ws, ["kill", "-0", str(pid)], timeout=cap)
            return True if r.rc == 0 else False if r.rc == 1 else None
        return Records(target.store.record_dir(), clock=self.clock, ask_target=ask, env=target.env)

    def machine_seen(self, m):
        r = Rec("machine", name=m)
        if m == self.this_machine and self.is_self:
            r.raw("self", True)
        else:
            tailnet, direct = self.reach(m)
            r.opt("tailnet", tailnet)
            r.opt("direct", direct)
        conf = self.reg.conf_path(m)
        if os.path.isfile(conf):
            r.set("conf", os.path.relpath(conf, self.root))
        return r.done()

    def reach(self, m):
        out = _bash(self.root, '. "$WK_ROOT/lib/reach.sh"; . "$WK_ROOT/boot/machines.sh"; '
                    'printf "tailnet=%s\\ndirect=%s\\n" "$(reach_tailnet "$1")" "$(reach_without_tailnet "$1")"', m).out
        f = kv(out)
        return f.get("tailnet", ""), f.get("direct", "")

    def reach_fleet(self, m):
        out = _bash(self.root, '. "$WK_ROOT/lib/reach.sh"; . "$WK_ROOT/boot/machines.sh"; '
                    'printf "tailnet=%s\\ndirect=%s\\n" "$(fleet_tailnet "$1")" "$(reach_without_tailnet "$1")"', m).out
        f = kv(out)
        return f.get("tailnet", ""), f.get("direct", "")

    def delegate(self, target, gm, args):
        """A machine of its own answers in its own records, stripped of the markers that end its jobs."""
        env = dict(os.environ, WK_ROW_LABEL=gm, WK_NO_DELEGATE="1")
        rc, out = target.wk(*args, env=env, quiet=True)
        out = clean(out)
        if out.lstrip().startswith("{"):
            return list(statusview.strip_markers(statusview.records_from_lines(out.splitlines()))), rc
        recs = []
        if out.strip():
            r = Rec("raw", machine=gm, text=out.strip())
            r.warn("this machine's wk did not answer in records -- its own words are above.\n      "
                   "'wk sync --tools %s' pushes the current tooling." % gm)
            recs.append(r.done())
        return recs, rc

    def current_base(self, target):
        with self.lock:
            if target.name not in self.bases:
                r = _bash(self.root, "load_target %s >/dev/null 2>&1; current_base" % shell.sh_quote(target.name))
                self.bases[target.name] = r.out.strip() if r.ok else ""
            return self.bases[target.name]

    def remake_hint(self, target, ws):
        far = self.reg.remote_marker_field("target")
        if far:
            return "from the workstation:  wk new %s --target %s" % (ws, far)
        return "wk new %s --target %s" % (ws, target.name)

    def workspace(self, target, gm, method, ws, records):
        r = Rec("workspace", machine=gm, method=method, name=ws)
        worst = 0
        info = target.info(ws)
        st = target.state(ws, info=info)
        r.set("state", info if st == "present" else st)
        r.set("ws", st)
        r.opt("branch", target.branch(ws))
        probe = {}
        if st == "present":
            script = WS_PROBE.replace("@SRC@", shell.sh_quote(target.src(ws))).replace("@BASE@", targets.UPSTREAM_LINE_BODY)
            probe = kv(target.exec(ws, ["sh", "-c", script]).out)
            origin = probe.get("origin", "")
            if origin and origin != UPSTREAM_ORIGIN:
                r.warn("origin is %s, not upstream -- 'wk remotes %s --fix'" % (origin, ws))
            for f in ("dirty", "untracked", "unpushed", "upstream", "behind", "ahead"):
                if probe.get(f) and probe[f] != "0":
                    r.set(f, probe[f])
        base = targets.image_base(self.root, ws) or (probe.get("wsbase") if probe.get("wsbase") != "?" else None)
        r.opt("base", base)
        snap = target.store.ws_base_id(ws)
        if snap:
            r.set("snap", snap)
            cur = self.current_base(target)
            if cur and cur != snap:
                try:
                    newer = [b for b in os.listdir(target.store.base_dir()) if b > snap]
                except OSError:
                    newer = []
                r.set("snap_behind", len(newer))
        if st == "creating":
            new = records.find("new", ws)
            if new and new.alive(None):
                r.note("being created right now -- its 'new' task below says how far it is")
                worst = bump(worst, 2)
            else:
                r.warn("creation never finished, and nothing is creating it now")
                r.note("usually there is nothing in one worth keeping -- remake it:")
                r.note("  " + self.remake_hint(target, ws))
                r.note("or, if the checkout is complete and only the marker is missing,")
                r.note("any command that gates on it takes --force")
                worst = bump(worst, 4)
        elif st == "broken":
            r.warn("the record says a %s workspace and the machine has none --\n      something outside wk removed it "
                   "(podman rm, tart delete, an rm -rf over there)" % target.name)
            r.note("clear the record:  wk rm %s" % ws)
            if os.path.isdir(target.store.ws_dir(ws)):
                r.note("what is left of it here: %s" % target.store.ws_dir(ws))
            worst = bump(worst, 4)
        elif st == "unreachable":
            r.warn("the machine behind '%s' did not answer within %ss" % (target.name, self.env.get("WK_SSH_TIMEOUT", "10")))
            r.note("this is not 'absent': nothing about the workspace was checked at all,")
            r.note("and anything below is the last thing this machine wrote about it")
            worst = bump(worst, 4)
        build = records.find("build", ws)
        r.raw("subs", [{"kind": "build", "state": build.verdict("capped"), "config": build.field("config")}] if build else [])
        return r.done(), worst

    def tasks(self, records, only):
        with self.lock:
            key = str(records.root)
            if key in self.tasks_said:
                return [], 0
            self.tasks_said.add(key)
        return task_records(records, only, self.clock)

    # -- what a machine is, apart from its workspaces

    def git_here(self):
        with self.lock:
            if self._git is None:
                head = Local().run(["git", "-C", self.root, "rev-parse", "--short", "HEAD"])
                st = Local().run(["git", "-C", self.root, "status", "--porcelain"])
                self._git = (head.out.strip() if head.ok else "", bool(st.ok and st.out.strip()))
            return self._git

    def version_here(self):
        return kv(Local().run([os.path.join(self.root, "cmd", "version")]).out)

    def report_machine(self, target, gm, has_wk):
        if target.kind == "remote" and has_wk:
            ver = kv(target.wk("version", quiet=True)[1])
            keys = clean(target.wk("key", "fingerprints", quiet=True)[1])
            peer = target.peer
        elif target.name == self.reg.default():
            self.tooling_said = True
            ver = self.version_here()
            keys = Local().run([os.path.join(self.root, "cmd", "key"), "fingerprints"]).out
            peer = False
        else:
            return []
        out = []
        if ver:
            expect, dirty_here = self.git_here()
            out.append(tools_fact(ver, expect, gm, target.name, in_vm=self.in_vm, peer=peer, dirty_here=dirty_here))
        for line in keys.splitlines():
            if line.strip():
                out.append(Rec("fact", machine=gm, type="key", text=line).done())
        return out

    def report_self(self):
        with self.lock:
            if self.tooling_said:
                return []
            self.tooling_said = True
        ver = self.version_here()
        if not ver:
            return []
        r = Rec("fact", machine=self.this_machine, type="wk-tools", sha=ver.get("sha", ""))
        r.raw("dirty", ver.get("dirty") == "yes")
        r.raw("insync", True)
        return [r.done()]

    def capacity_remote(self, target, m):
        return capacity_record(m, None, str(target.cores()), "", str(target.mem_mb()), str(target.load()))

    def health(self, target, m):
        store = target.store
        alive = Local().alive
        out = []
        r = _bash(self.root, "load_target %s >/dev/null 2>&1; unreferenced_bases | grep -c ." % shell.sh_quote(target.name))
        out.append(disk_record(store, m, self.in_vm, int(r.out.strip() or 0) if r.out.strip().isdigit() else 0))
        if target.kind == "container":
            local = kv(_bash(self.root, "load_target container >/dev/null 2>&1; t_sdk_local").out)
            if local.get("image"):
                cap = int(self.env.get("WK_FLEET_TIMEOUT", "4"))
                up = _bash(self.root, "load_target container >/dev/null 2>&1; t_sdk_upstream", timeout=cap)
                tags = [l.strip() for l in up.out.splitlines() if l.strip()] if up.ok and up.out.strip() else None
                out.append(sdk_record(m, local, tags, cap))
        if not self.in_vm:
            out.append(broker_record(store, m, alive))
        out += service_records(self.root, m)
        out += lock_records(store, m, alive)
        forks = [l.split()[0] for l in _bash(self.root, "wk_push_forks").out.splitlines() if l.split()]
        out.append(push_record(store, m, forks, self.in_vm))
        r = _bash(self.root, '. "$WK_ROOT/lib/resources.sh"; printf "%s\\n%s\\n%s\\n" "$(host_cores)" "$(host_mem_mb)" "$(avail_mem_mb)"')
        lines = r.out.split("\n") + ["", "", ""]
        out.append(capacity_record(m, "the podman VM" if self.in_vm else "", lines[0] if r.ok else "", lines[1], lines[2] if r.ok else "",
                                   host_load()))
        out.append(quiesce_record(quiesce_dir(store), m))
        out += bench_records(store, m, lambda pid: bool(pid and alive(pid)))
        return [r for r in out if r]

    # -- the fleet

    def fleet_devices(self):
        if self.reg.in_workspace() or not os.path.isfile(os.path.join(self.root, "boot", "machines.sh")):
            return [], 0
        cap = int(self.env.get("WK_FLEET_TIMEOUT", "4")) * 5
        confs = machine_confs(self.root, self.env)

        def one(item):
            name, conf = item
            return fleet_record(name, conf, fleet_probe(self.root, name, cap), cap, self.reach_fleet)

        with ThreadPoolExecutor(max_workers=max(1, len(confs))) as pool:
            recs = list(pool.map(one, confs))
        return [r for r in recs if r], 0

    def bridges(self):
        d = os.path.join(self.root, "bridge", "hosts")
        try:
            names = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".conf"))
        except OSError:
            return [], 0
        want = bridge_role_sum(self.root)
        connect = self.env.get("WK_FLEET_TIMEOUT", "4")
        cap = float(self.env.get("WK_BRIDGE_TIMEOUT", "20"))

        def one(name):
            out = bridge_ssh(name, BRIDGE_PROBE, True, connect, cap)
            if not out.startswith("reachable="):
                out = bridge_ssh(name, BRIDGE_PROBE, False, connect, cap)
            return bridge_record(name, targets.read_conf(os.path.join(d, name + ".conf")), want, kv(out), self.reach)

        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            return list(pool.map(one, names)), 0
