"""What a server needs for tasks: the pinned pueue, its daemon, and the one-at-a-time build group."""

import json

from wk.core.act import die
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow, Stage
from wk.tasks.pueue import CONFIG, LIMITS, VERSION, Pueue

URL = "https://github.com/Nukesor/pueue/releases/download/v%s/%s-%s"
# `uname -sm` -> (the release's target, the sha256 of its pueue and pueued), from the v4.0.5 release's asset digests.
ASSETS = {
    "Linux x86_64": ("x86_64-unknown-linux-musl", {"pueue": "5bf543b1861a5e5f449e38a5c6902cbcef473dca4d510023c22ec707b7c9307f",
                                                   "pueued": "de73266add8b61d622bd011a078929a2a788609b7e8511fb07e9162e6977ad7e"}),
    "Linux aarch64": ("aarch64-unknown-linux-musl", {"pueue": "745a34f73719636dbb2b37b44e08c47f011dc46c13e49b84646abf3b88c7d3f5",
                                                     "pueued": "0b93fbd026d8df88010189e4e2f7d300a4f16354a6e4f9b2a81ec4bd072f6797"}),
    "Darwin arm64": ("aarch64-apple-darwin", {"pueue": "7b3bedb2ea9903250a741eb41fba29c6e5a066b34ae34d43eefaeb9713585c45",
                                              "pueued": "18c377cdccb7714c5545d90cf18fcc592b17ae15d5900fe8dab2f8ca9288d6f1"}),
}
SHA256 = "import hashlib, sys\nprint(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())"
PLACE = """import hashlib, os, sys
part, path, want = sys.argv[1:]
got = hashlib.sha256(open(part, "rb").read()).hexdigest()
if got != want:
    os.unlink(part)
    sys.exit("sha256 %s, pinned %s" % (got, want))
os.chmod(part, 0o755)
os.replace(part, path)"""
START = '"$0" --config "$1" --daemonize && n=0 && until "$2" --config "$1" status >/dev/null 2>&1; do n=$((n + 1)); [ $n -lt 50 ] || exit 1; sleep 0.2; done'


def install(m, pueue):
    """Both binaries at their pinned digest in the cache; True when one was fetched."""
    platform = m.run(["uname", "-sm"]).out.strip()
    if platform not in ASSETS:
        die("pueue %s has no pinned binary for %s (%s): wk's servers are %s" % (VERSION, m.name, platform, ", ".join(ASSETS)))
    target, digests = ASSETS[platform]
    stale = [tool for tool in sorted(digests) if m.run(["python3", "-I", "-c", SHA256, "%s/%s" % (pueue.bin, tool)]).out.strip() != digests[tool]]
    for tool in stale:
        path = "%s/%s" % (pueue.bin, tool)
        for what, argv in (("download", ["curl", "-fsSL", "--create-dirs", "-o", path + ".part", URL % (VERSION, tool, target)]),
                           ("verify", ["python3", "-I", "-c", PLACE, path + ".part", path, digests[tool]])):
            r = m.act(argv)
            if not r.ok:
                die("cannot %s %s %s on %s: %s\n    nothing is installed; re-run once the release is reachable" % (what, tool, VERSION, m.name, r.err.strip()))
    return bool(stale)


def daemon(m, pueue):
    """wk's config written and pueued answering under it; True when either changed."""
    config = CONFIG % (pueue.dir, pueue.dir)
    written, answers = m.read(pueue.config) == config, m.run(pueue.argv("status", "--json")).ok
    if not written:
        m.write(pueue.config, config)
    if not answers:
        r = m.act(["sh", "-c", START, pueue.bin + "/pueued", pueue.config, pueue.bin + "/pueue"])
        if not r.ok:
            die("pueued does not answer on %s after starting it: %s\n    run %s/pueued --config %s there to see why"
                % (m.name, r.err.strip(), pueue.bin, pueue.config))
    return not (written and answers)


def stages(inv, conf):
    if not conf.holds_workspaces:
        return []
    m = inv.machine(conf.name)
    pueue = Pueue(inv, m)
    return [Stage(20, "pueue", lambda: install(m, pueue)), Stage(20, "pueued", lambda: daemon(m, pueue)),
            Stage(20, "build group", lambda: pueue.limit("build", LIMITS["build"]))]


def pins(inv, conf):
    return ["pueue-" + VERSION]


def doctor(inv, conf, ws):
    if ws is not None or not conf.holds_workspaces:
        return []
    m, remedy = inv.machine(conf.name), "wk machine setup %s" % conf.name
    pueue = Pueue(inv, m)
    installed = all(m.exists("%s/%s" % (pueue.bin, tool)) for tool in ("pueue", "pueued"))
    r = m.run(pueue.argv("status", "--json"))
    rows = [DoctorRow("pueue", OK, VERSION) if installed else DoctorRow("pueue", MISSING, "%s is not in %s" % (VERSION, pueue.bin), remedy)]
    if not r.ok:
        return rows + [DoctorRow("pueued", MISSING, "the daemon does not answer", remedy),
                       DoctorRow("build group", UNKNOWN, "the daemon does not answer", remedy)]
    limit = json.loads(r.out)["groups"].get("build", {}).get("parallel_tasks")
    return rows + [DoctorRow("pueued", OK), DoctorRow("build group", OK, "one build at a time") if limit == LIMITS["build"]
                   else DoctorRow("build group", MISSING, "its parallel limit is %s, not %d" % (limit, LIMITS["build"]), remedy)]
