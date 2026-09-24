"""A benchmark's payload, fetched once per upstream commit and pinned; `python3 -m wk.bench.seed` is lib/bench.sh's seed_payload."""

import json
import os
import re
import sys

from wk import act
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Local
from wk.store import Store

PLANS = "webkitpy/benchmark_runner/data/plans"
SEED_WAIT = 3600


def plan_json(read, plan):   # read(<path under Tools/Scripts>); a plan file may hold only another plan's name
    for _ in range(5):
        body = read("%s/%s.plan" % (PLANS, plan))
        if body is None:
            act.die("no such plan: %s (plans live in Tools/Scripts/%s)" % (plan, PLANS))
        if body.startswith("{"):
            return body
        plan = re.sub(r"\.plan$", "", "".join(body.split()))
    act.die("plan %s indirects too many times" % plan)


def plan_spec(text):
    try:
        p = json.loads(text)
    except ValueError:
        return None
    if "git_repository" in p:
        g = p["git_repository"]
        return "git", g["url"], g.get("branch", "main"), "."
    if "github_source" in p:
        m = re.match(r"https://github.com/([^/]+/[^/]+)/tree/([0-9a-f]+)/?(.*)", p["github_source"])
        if not m:
            return None
        return "git", "https://github.com/%s.git" % m.group(1), m.group(2), m.group(3) or "."
    if "local_copy" in p:
        return "local", p["local_copy"], "", ""
    return None


class Seeder:
    def __init__(self, machine, lock, seed_dir):
        self.machine, self.lock, self.seed_dir = machine, lock, seed_dir

    def seed(self, plan, text):
        spec = plan_spec(text)
        if spec is None:
            act.warn("cannot pre-seed %s; run-benchmark will fetch it itself" % plan)
            return ""
        kind, url, ref, subdir = spec
        if kind != "git":
            return ""
        r = self.machine.run(["git", "ls-remote", url, ref])
        sha = (r.out.split() or [ref])[0] if r.ok else ref
        dest = os.path.join(self.seed_dir, "%s-%s" % (plan, sha[:12]))
        self.machine.mkdir(self.seed_dir)
        with self.lock.held("bench-seed-%s" % os.path.basename(dest), timeout=SEED_WAIT):
            return self._fetch(plan, url, ref, sha, subdir, dest)

    def _fetch(self, plan, url, ref, sha, subdir, dest):
        """Assembled beside the seed directory under the payload's lock and renamed in whole, so a kill leaves nothing a re-run trusts."""
        m = self.machine
        tmp = os.path.join(self.seed_dir, ".tmp-" + os.path.basename(dest))
        if m.isdir(os.path.join(dest, ".wk-seeded")):
            act.debug("payload cached: %s" % dest)
            for rubble in (os.path.join(dest, ".git"), tmp):
                if m.exists(rubble):
                    m.remove(rubble)
            return dest
        act.info("seeding %s payload from %s@%s" % (plan, url, sha[:12]))
        m.remove(tmp)
        m.mkdir(tmp)
        repo = os.path.join(tmp, "repo")
        if not m.act_run(["git", "clone", "-q", url, repo]).ok:
            m.remove(tmp)
            act.warn("could not clone %s; run-benchmark will fetch the payload itself" % url)
            return ""
        if not m.act_run(["git", "-C", repo, "checkout", "-q", sha]).ok:
            m.act_run(["git", "-C", repo, "checkout", "-q", ref])
        payload = repo if subdir == "." else os.path.join(repo, subdir)
        # No .git in a pinned payload: a clone carries fsmonitor's Unix domain socket, which no copy tool can reproduce.
        m.remove(os.path.join(payload, ".git"))
        m.mkdir(os.path.join(payload, ".wk-seeded"))
        m.write(os.path.join(payload, ".wk-seeded", "origin"),
                "url=%s\nref=%s\nsha=%s\nsubdir=%s\n" % (url, ref, sha, subdir))
        m.remove(dest)
        m.act_run(["mv", payload, dest])
        m.remove(tmp)
        act.info("seeded %s" % dest)
        return dest


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: python3 -m wk.bench.seed <seed dir> <plan>  (the plan's JSON on stdin)\n")
        return 2
    machine = Local()
    try:
        print(Seeder(machine, Lock(Store(), machine, Clock()), argv[0]).seed(argv[1], sys.stdin.read()))
    except act.Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
