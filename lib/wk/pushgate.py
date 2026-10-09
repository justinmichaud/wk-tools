"""Who may push from a workspace: which workspace a caller is, whether an agent runs in it, and the one command a push may run."""

import re
import struct

# The process is `claude`, `node` or a version string, but its exe resolves under the install; pi is `node <prefix>/bin/pi`. A macOS guest has no /proc. `[n]` keeps this scan's own command line from matching.
AGENT_PID_SCAN = '''if [ -d /proc/self ]; then
    for d in /proc/[0-9]*; do
        p=${d#/proc/}
        case "$(readlink "$d/exe" 2>/dev/null)" in
            */claude/versions/*|*/claude) printf "%s\\n" "$p"; continue ;;
        esac
        case "$(tr '\\0' ' ' < "$d/cmdline" 2>/dev/null)" in
            "node "*/bin/pi|"node "*/bin/pi" "*|*/pi-codi[n]g-agent/*) printf "%s\\n" "$p" ;;
        esac
    done
else
    ps -Ao pid=,comm= | while read -r p c; do
        case "$c" in */claude/versions/*|*/claude|claude) printf "%s\\n" "$p" ;; esac
    done
    ps -Ao pid=,command= | while read -r p c; do
        case "$c" in "node "*/bin/pi|"node "*/bin/pi" "*|*/pi-codi[n]g-agent/*) printf "%s\\n" "$p" ;; esac
    done
fi'''

NOT_RUNNING = ("absent", "created", "configured", "exited", "stopped")
CGROUP_ID = re.compile(r"libpod-([0-9a-f]{64})\.scope")
REQUEST = re.compile(r"^(git-receive-pack|git-upload-pack) '/?([^'\s]+?)(?:\.git)?'$")
PROTOCOL = re.compile(r"^version=[0-9]$")


STDOUT, STDERR, EXIT = 1, 2, 3


def frame(kind, data):
    return struct.pack(">BI", kind, len(data)) + data


class Denied(Exception):
    def __init__(self, why, remedy):
        super().__init__(why)
        self.why, self.remedy = why, remedy


def agent_pids(t, ws):
    r = t.exec(ws, ["sh", "-c", AGENT_PID_SCAN])
    if not r.ok:
        return [] if t.info(ws) in NOT_RUNNING else None
    return [p for p in r.out.replace("\r", "").split() if p.isdigit()]


def allow(t, ws):
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
        self.reg, self.proc = reg, proc

    def container_of(self, pid):
        """The workspace whose container holds `pid`, or None: its cgroup names the container by its full id."""
        try:
            with open("%s/%d/cgroup" % (self.proc, pid)) as f:
                m = CGROUP_ID.search(f.read())
        except OSError:
            return None
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

    def rows(self, t, ws):
        return t.repo(ws).push_rows(t.machine, t.tools_src())
