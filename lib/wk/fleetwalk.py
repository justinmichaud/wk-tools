"""The fleet walk `wk sysimage ls` and `wk bench ls` both do: ask every target's own store for its rows."""

from concurrent.futures import ThreadPoolExecutor


def target_rows(reg, name, subcmd, what, label_for, warn):
    try:
        target = reg.load(name)
    except LookupError as e:
        warn(str(e))
        return []
    side, _ = target.probe()
    if side == "stopped":
        warn("the machine behind target '%s' is stopped, so the %s in its\n"
             "    store are not listed -- 'wk start' brings it up" % (name, what))
        return []
    if side != "answering":
        return []
    rc, out = target.wk(subcmd, "ls", "--continued",
                        env=dict(reg.env, WK_ROW_LABEL=label_for(target, name), WK_NO_DELEGATE="1"), quiet=True)
    if rc != 0:
        warn("'%s' did not answer the listing, so the %s in its store are not\n"
             "    here. Its wk-tools predates a listing that walks the fleet:  wk sync --tools %s" % (name, what, name))
    return [l for l in out.replace("\r", "").splitlines() if l.strip()]


def fleet_rows(reg, subcmd, what, label_for, warn):
    names = reg.walk()
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return [row for rows in pool.map(lambda n: target_rows(reg, n, subcmd, what, label_for, warn), names) for row in rows]
