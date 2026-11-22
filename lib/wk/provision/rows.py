"""The doctor rows that belong to no other lane: wk itself on a server, and what the client needs."""

from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.provision import tools

DISK_FLOOR_GB = 20
CLOCK_SKEW = 30
TAILSCALE_TIMEOUT = 4       # `tailscale status` hangs while its daemon is wedged


def commit(inv, m, home, remedy):
    mine, theirs = tools.head(inv), tools.commit(m, home + "/tools")
    if not mine:
        return DoctorRow("wk commit", UNKNOWN, "%s has no commit to compare with" % inv.root, remedy)
    if theirs == mine:
        return DoctorRow("wk commit", OK, mine)
    return DoctorRow("wk commit", MISSING, "%s has %s, and this checkout is at %s" % (m.name, theirs or "no checkout", mine), remedy)


def path(m, home, remedy):
    found = m.run(["sh", "-c", "command -v wk"]).out.strip()
    return DoctorRow("wk on PATH", OK if found == home + "/bin/wk" else MISSING, found or "the login shell finds no wk", remedy)


def layout(m, home, remedy):
    lacking = ["%s (%s)" % found for found in tools.lacking(m, home)]
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
    m, home, remedy = inv.machine(conf.name), tools.wk_home(inv, conf), "wk machine setup %s" % conf.name
    return tools.prerequisites(m) + [commit(inv, m, home, remedy), path(m, home, remedy), layout(m, home, remedy),
                                     disk(m, home), clock(inv, m)]


def client(inv):
    m = inv.here
    addr = inv.env.get("BAO_ADDR")
    token = "BAO_TOKEN" if inv.env.get("BAO_TOKEN") else "~/.vault-token" if m.exists(m.home + "/.vault-token") else ""
    return [tools.tool(m, "ssh", ["ssh", "-V"], "install OpenSSH"),
            tools.tool(m, "git", ["git", "--version"], "install git"),
            tools.tool(m, "tailscale", ["tailscale", "status", "--peers=false"], "install Tailscale, then `tailscale up`", TAILSCALE_TIMEOUT),
            DoctorRow("BAO_ADDR", OK if addr else MISSING, addr or "not set",
                      "export BAO_ADDR=<the credential server's OpenBao address> in your shell's profile"),
            DoctorRow("store token", OK if token else MISSING, token or "no BAO_TOKEN and no ~/.vault-token", "bao login")]
