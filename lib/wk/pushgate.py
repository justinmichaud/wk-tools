"""Who may publish from a workspace: a container's caller by its ancestry in the shared pid namespace's /proc (served only below
the entry of a person's own run, `container/bin/wk-person`, with no agent session between); a guest, which has no pid the service can see, by its agents."""

import os
import re
import struct

PERSON = "container/bin/wk-person"
# A macOS guest has no /proc; the process is `claude` from any install, or `node <prefix>/bin/pi`.
AGENT_PS_SCAN = '''ps -Ao pid=,comm= | while read -r p c; do
    case "$c" in */claude/versions/*|*/claude|claude) printf "%s\\n" "$p" ;; esac
done
ps -Ao pid=,command= | while read -r p c; do
    case "$c" in "node "*/bin/pi|"node "*/bin/pi" "*|*/pi-coding-agent/*) printf "%s\\n" "$p" ;; esac
done'''

NOT_RUNNING = ("absent", "created", "configured", "exited", "stopped")
CGROUP_ID = re.compile(r"libpod-([0-9a-f]{64})\.scope")
REQUEST = re.compile(r"^(git-receive-pack|git-upload-pack) '/?([^'\s]+?)(?:\.git)?'$")
PROTOCOL = re.compile(r"^version=[0-9]$")


STDOUT, STDERR, EXIT = 1, 2, 3


def frame(kind, data):
    return struct.pack(">BI", kind, len(data)) + data


class Denied(Exception):
    """`kind` names who the caller was found to be: agent, own (a run of wk's), orphan (nothing of a session behind it)."""

    def __init__(self, why, remedy, kind=""):
        super().__init__(why)
        self.why, self.remedy, self.kind = why, remedy, kind


class Proc:
    def __init__(self, root="/proc"):
        self.root = root

    def _read(self, pid, name):
        try:
            with open(os.path.join(self.root, str(pid), name), "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return None

    def ppid(self, pid):
        text = self._read(pid, "status") or ""
        m = re.search(r"^PPid:\s*(\d+)", text, re.M)
        return int(m.group(1)) if m else None

    def comm(self, pid):
        return (self._read(pid, "comm") or "").strip()

    def cmdline(self, pid):
        return [w for w in (self._read(pid, "cmdline") or "").split("\0") if w]

    def exe(self, pid):
        try:
            return os.readlink(os.path.join(self.root, str(pid), "exe"))
        except OSError:
            return ""


def agent_word(words, exe):
    first = os.path.basename(words[0]) if words else ""
    if first == "claude" or "/claude/versions/" in exe or exe.endswith("/claude"):
        return "claude"
    program = words[1:] if first.startswith("python") else words
    if program[:2] and os.path.basename(program[0]) == "wk" and program[1:2] == ["ai"]:
        return "wk ai"
    if first == "node" and any(w.endswith("/bin/pi") or "/pi-coding-agent/" in w for w in words[1:]):
        return "pi"
    return ""


def is_person_entry(words):
    return any(w.endswith("/" + PERSON) for w in words[:2])


def remedy(ws):
    return "a publish is made from a shell of your own:  wk enter %s" % ws


def walk_up(proc, pid, ws, until, agents=True):
    """The chain from `pid` up to the first process `until` takes, left out; None past pid 1 or a gone parent; Denied past an agent."""
    chain, seen = [], set()
    while pid and pid > 1 and pid not in seen:
        if until(pid):
            return chain
        seen.add(pid)
        parent = proc.ppid(pid)
        if parent is None:
            return None
        agent = agents and agent_word(proc.cmdline(pid), proc.exe(pid))
        if agent:
            raise Denied("an agent session (%s, pid %d) is behind this caller, and nothing an agent runs may publish" % (agent, pid),
                         "end the session, then " + remedy(ws), "agent")
        chain.append(pid)
        pid = parent
    return None


def person(proc, pid, ws):
    chain = walk_up(proc, pid, ws, lambda p: proc.comm(p) == "conmon")
    if chain is None:
        raise Denied("no shell of yours is behind this caller (pid %s): it runs on its own, left behind by a session or a job that ended"
                     % (pid or "?"), remedy(ws), "orphan")
    words = proc.cmdline(chain[-1]) if chain else []
    if not is_person_entry(words):
        raise Denied("this caller descends from a run of wk's own (pid %s: %s), not from a shell of yours"
                     % (chain[-1] if chain else pid, " ".join(words)[:60] or "?"),
                     remedy(ws) + "\n    (a build, a probe and 'wk enter --own' never publish)", "own")


def bridged(proc, pid, command, ws):
    try:
        person(proc, command, ws)
        persons = True
    except Denied:
        persons = False
    if walk_up(proc, pid, ws, lambda p: p == command, persons) is None:
        raise Denied("pid %s is not under the command this bridge serves (pid %d)" % (pid or "?", command), remedy(ws), "orphan")


def agent_pids(t, ws):
    r = t.exec(ws, ["sh", "-c", AGENT_PS_SCAN])
    if not r.ok:
        return [] if t.info(ws) in NOT_RUNNING else None
    return [p for p in r.out.replace("\r", "").split() if p.isdigit()]


def allow(t, ws, proc=None, pid=None):
    if t.kind != "vm":
        person(proc, pid, ws)
        return
    pids = agent_pids(t, ws)
    if pids is None:
        raise Denied("could not ask '%s' whether an agent runs in it, and it may be running one" % ws,
                     "'wk status %s' names what is wrong with it" % ws)
    if pids:
        raise Denied("an agent (claude or pi) runs in '%s' (pid %s), and nothing it can reach may publish" % (ws, " ".join(pids)),
                     "end the session, then push again; a person's own shell (wk enter %s) pushes once no agent runs there" % ws)


def command(rows, host, line):
    alias, m = host.rpartition("@")[2], REQUEST.match(line)
    if m:
        for key, repo, a in rows:
            if a == alias and m.group(2) == repo:
                return key, "%s '%s.git'" % (m.group(1), repo)
    raise Denied("'%s' on '%s' is not a push or fetch of this workspace's repositories" % (line[:80], host[:60]),
                 "a workspace pushes through git, to: %s" % " ".join("%s (%s)" % (a, r) for _, r, a in rows))


def ssh_prefix(key_path, protocol=""):
    """ssh with one deploy key and no agent, up to the destination; `protocol` is git's GIT_PROTOCOL, passed only as a version."""
    return (["ssh", "-T", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=accept-new", "-i", key_path]
            + (["-o", "SendEnv=GIT_PROTOCOL"] if PROTOCOL.match(protocol or "") else []))


def ssh_argv(key_path, line, protocol=""):
    return ssh_prefix(key_path, protocol) + ["git@github.com", line]


class Gate:
    def __init__(self, reg, proc="/proc"):
        self.reg, self.proc = reg, Proc(proc)

    def container_of(self, pid):
        """The workspace whose container holds `pid`, or None: its cgroup names the container by its full id."""
        text = self.proc._read(pid, "cgroup")
        m = CGROUP_ID.search(text) if text else None
        if not m:
            return None
        t = self.reg.load("container")
        r = t.machine.run(t.podman() + ["ps", "-a", "--no-trunc", "--filter", "name=^wk-", "--format", "{{.ID}}\t{{.Names}}"])
        for line in r.out.splitlines():
            cid, _, name = line.partition("\t")
            if cid == m.group(1) and name.startswith("wk-"):
                return name[3:]
        return None

    def guest_at(self, addr):
        t = self.reg.load("vm")
        return next((ws for ws, state in t.list() if state == "running" and t.ip(ws) == addr), None)

    def allow(self, t, ws, pid):
        allow(t, ws, self.proc, pid)

    def rows(self, t, ws):
        return t.repo(ws).push_rows(t.machine, t.tools_src())
