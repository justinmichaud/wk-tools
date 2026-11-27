"""The doctor rows that belong to no other lane: wk itself on a server, and what the controller needs."""

import shlex

from wk.core import hooks
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.machine import TIMED_OUT
from wk.provision import ansible

PYTHON = (3, 9)
LAYOUT = {"bin": "d755", "cache": "d755", "git": "d755", "ws": "d755", "secrets": "d700"}
DISK_FLOOR_GB = 20
CLOCK_SKEW = 30
TAILSCALE_TIMEOUT = 4       # `tailscale status` hangs while its daemon is wedged


def tool(m, name, argv, remedy, timeout=30, env=None):
    """A row for a program that must answer: no answer in time is unknown, any other failure is missing."""
    r = m.run(argv, timeout=timeout, env=env)
    said = (r.out or r.err).strip().split("\n")[0]
    if r.rc == TIMED_OUT:
        return DoctorRow(name, UNKNOWN, "`%s` did not answer in %ds" % (shlex.join(argv), timeout), remedy)
    return DoctorRow(name, OK if r.ok else MISSING, said, remedy)


def prerequisites(m):
    """What a server must already have; the playbook asserts both and installs neither."""
    remedy = "install it on %s with the system's package manager" % m.name
    python = tool(m, "python3", ["python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], remedy)
    if python.status == OK and tuple(int(n) for n in python.detail.split(".")) < PYTHON:
        python = DoctorRow("python3", MISSING, "%s is older than %d.%d" % ((python.detail,) + PYTHON), remedy)
    return [python, tool(m, "git", ["git", "--version"], remedy)]


def commit(m, home, remedy):
    """tools/ is a checkout of a commit, with no change to its tree: what the controller gave it."""
    sha = m.run(["git", "-C", home + "/tools", "rev-parse", "HEAD"])
    if not sha.ok:
        return DoctorRow("wk commit", MISSING, "%s has no checkout of wk" % home, remedy)
    if m.run(["git", "-C", home + "/tools", "status", "--porcelain"]).out.strip():
        return DoctorRow("wk commit", MISSING, "%s with changes: it runs what the controller did not give it" % sha.out.strip(), remedy)
    return DoctorRow("wk commit", OK, sha.out.strip())


def path(m, home, remedy):
    found = m.run(["sh", "-c", "command -v wk"]).out.strip()
    return DoctorRow("wk on PATH", OK if found == home + "/bin/wk" else MISSING, found or "the login shell finds no wk", remedy)


def layout(m, home, remedy):
    lacking = hooks.lacking(m, {"%s/%s" % (home, name): mode for name, mode in LAYOUT.items()})
    return DoctorRow("layout", MISSING if lacking else OK, ", ".join(lacking) and "absent or wrongly moded: " + ", ".join(lacking), remedy)


def disk(m, home):
    r = m.run(["df", "-Pk", home])
    if not r.ok:
        return DoctorRow("disk", UNKNOWN, "df %s: %s" % (home, r.err.strip()))
    free = int(r.out.splitlines()[-1].split()[3]) // 2 ** 20
    return DoctorRow("disk", OK if free >= DISK_FLOOR_GB else MISSING, "%d GB free in %s, and the floor is %d" % (free, home, DISK_FLOOR_GB),
                     "wk gc")


def clock(inv, m):
    before = inv.clock.now()
    far = m.run(["date", "+%s"]).out.strip()
    after = inv.clock.now()
    if not far.isdigit():
        return DoctorRow("clock", UNKNOWN, "`date +%%s` on %s answered %s" % (m.name, far or "nothing"))
    off = max(before - int(far) - 1, int(far) - after, 0)
    return DoctorRow("clock", OK if off <= CLOCK_SKEW else MISSING, "%d s from this machine's, and the bound is %d" % (off, CLOCK_SKEW),
                     "turn on time synchronisation on %s" % m.name)


def server(inv, conf):
    m, remedy = inv.machine(conf.name), "wk machine setup %s, typed on %s" % (conf.name, inv.fleet.controller.name)
    home = inv.wk_home_on(m)
    return prerequisites(m) + [commit(m, home, remedy), path(m, home, remedy), layout(m, home, remedy),
                               disk(m, home), clock(inv, m)]


def controller(inv, conf):
    m, apt = inv.here, "sudo apt install %s"
    pinned = ansible.websocat(inv.wk_home)
    return [tool(m, "ssh", ["ssh", "-V"], apt % "openssh-client"),
            tool(m, "git", ["git", "--version"], apt % "git"),
            tool(m, "tailscale", ["tailscale", "status", "--peers=false"], "install Tailscale, then `tailscale up`", TAILSCALE_TIMEOUT),
            tool(m, "ansible", ["ansible-playbook", "--version"], ansible.INSTALL, env=ansible.LOCALE),
            tool(m, "xorriso", ["xorriso", "-version"], apt % "xorriso"),
            tool(m, "qemu-nbd", ["qemu-nbd", "--version"], apt % "qemu-utils"),
            DoctorRow("websocat", OK if m.exists(pinned) else MISSING, pinned, "wk machine setup %s --stage controller" % conf.name)]
