"""What setup converges, doctor reports and gc reclaims for workspaces: the mirror, the container driver's build
environment (podman, subordinate ids and the SDK image) and half-made workspaces."""

from wk.core.act import die, must
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow, GcRow, Stage
from wk.core.seams import BROKEN
from wk.workspace import lifecycle, mirror
from wk.workspace.place import image, place


def subids(m):
    """Rootless podman maps a container's other users onto the ranges /etc/subuid and /etc/subgid grant this one."""
    user = m.run(["id", "-un"]).out.strip()
    lacking = [f for f in ("/etc/subuid", "/etc/subgid") if not any(line.startswith(user + ":") for line in (m.read(f) or "").splitlines())]
    return DoctorRow("subordinate ids", MISSING if lacking else OK, "no range for %s in %s" % (user, " or ".join(lacking)) if lacking else "",
                     "on %s: sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 %s" % (m.name, user))


def sdk(m, conf):
    r = m.run(["podman", "image", "exists", image(conf)])
    status = OK if r.ok else MISSING if r.rc == 1 else UNKNOWN
    return DoctorRow("sdk image", status, image(conf) if r.rc < 2 else "podman failed (exit %d): %s" % (r.rc, r.err.strip()),
                     "wk machine setup %s" % conf.name)


def environment(m, conf):
    """The rows of the container driver's build environment, the SDK image last; the plain driver has none."""
    if conf.driver == "plain":
        return []
    return [DoctorRow("podman", OK if m.run(["podman", "--version"]).ok else MISSING, "", "install podman on %s" % conf.name),
            subids(m), sdk(m, conf)]


def prepare(m, conf):
    """Setup's stage: every row is ok, after pulling the SDK image where that is the one thing missing."""
    rows = environment(m, conf)
    pull = rows[-1].name == "sdk image" and rows[-1].status == MISSING
    for row in rows[:-1] if pull else rows:
        if row.status != OK:
            die("%s lacks %s%s\n    %s" % (conf.name, row.name, row.detail and ": " + row.detail, row.remedy))
    if pull:
        must(m, ["podman", "pull", image(conf)], "check that %s reaches the registry, then `wk machine setup %s` again" % (conf.name, conf.name))
    return pull


def stages(inv, conf):
    if not conf.holds_workspaces:
        return []
    m = inv.machine(conf.name)
    found = [Stage(30, "mirror", lambda: mirror.setup(m, place(inv, m)[1]))]
    return found + [Stage(40, "build environment", lambda: prepare(m, conf))] if conf.driver == "container" else found


def doctor(inv, conf, ws):
    if ws is not None or not conf.holds_workspaces:
        return []
    m = inv.machine(conf.name)
    home = place(inv, m)[1]
    held = m.read(mirror.where(home) + "/config") == mirror.CONFIG and mirror.fetched(m, home)
    return [DoctorRow("mirror", OK if held else MISSING, mirror.where(home), "wk machine setup %s" % conf.name)] + environment(m, conf)


def gc(inv, conf):
    if not conf.holds_workspaces:
        return []
    m = inv.machine(conf.name)
    return [GcRow("workspace %s" % ws.name, "half-made on %s: no .ready, and no `wk new` is making it" % conf.name,
                  lambda ws=ws: lifecycle.wipe(inv, m, ws))
            for ws in lifecycle.listing(inv, m) if ws.state == BROKEN and not lifecycle.made(m, ws)]
