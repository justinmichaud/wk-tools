"""`wk sysimage`: build, find, write and remove the images a machine boots for one run. WsBuild is what the
in-workspace halves (yocto_ws.py, buildroot_ws.py) share."""

import os
import sys

from wk import slot
from wk.machine import isolated_module
from wk.store import Store

TOOLS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class Failed(Exception):
    pass


def fail(text):
    raise Failed(text)


class WsBuild:
    PREFIX = ""

    def __init__(self, a, machine, env, clock, tools=TOOLS):
        self.a, self.m, self.env, self.clock, self.tools = a, machine, env, clock, tools
        self.jobs = int(a.jobs) if a.jobs else int(machine.run(["nproc"]).out.strip() or 4)

    def say(self, text):
        sys.stdout.write("%s: %s\n" % (self.PREFIX, text))
        sys.stdout.flush()

    def ok(self, argv, why):
        r = self.m.act_run(argv)
        if not r.ok:
            fail("%s\n    %s" % (why, (r.err or r.out).strip()))
        return r

    def guarded(self, jobs, argv, mb_per_job=None):
        pre = ["env", "WK_MB_PER_JOB=%d" % mb_per_job] if mb_per_job else []
        return pre + ["bash", "-c", '. %s/build/guard.sh && guard_run "$0" -- "$@"' % self.tools, str(jobs)] + list(argv)

    def need_caches(self, names):
        for name in names:
            if not self.env.get(name):
                fail("%s are not set in this workspace; lib/wk/places.py's Container mounts them from the store, "
                     "where they outlive it" % "/".join(names))
            self.m.mkdir(self.env[name])

    def fetch_commit(self, src, commit):
        mirror = Store(self.env).container_mirror_dir()
        if not mirror:
            fail("WK_MIRROR names the mirror this container mounts (lib/wk/places.py's Container), and it is not set")
        if not self.m.run(["git", "-C", src, "cat-file", "-e", commit + "^{commit}"]).ok \
                and not self.m.act_run(["git", "-C", src, "fetch", "--quiet", mirror, commit]).ok:
            fail("%s is not in this machine's mirror; 'wk bench ab' and 'wk pr' fetch a PR head into it first" % commit)

    def say_source(self, src):
        self.say("source        %s @ %s" % (src, self.m.run(["git", "-C", src, "log", "-1", "--format=%h (%s)"]).out.strip()[:80]))

    def verify_fresh(self, path, start):
        """make and cross-toolchain-helper both exit 0 having done nothing, so the output must postdate the stage."""
        r = self.m.run(["find", path, "-maxdepth", "1", "-type", "f", "-printf", "%T@\n"])
        stamps = [float(x) for x in r.out.split()] if r.ok else []
        if not stamps:
            fail("the build reported success but left nothing at %s" % path)
        if int(max(stamps)) < int(start):
            fail("the build reported success but nothing at %s is newer than this stage" % path)

    def manifest(self, root, slotdir, fields, readelf=()):
        rev = self.m.run(["git", "-C", self.tools, "rev-parse", "--short", "HEAD"])
        fields = dict(fields, built_at=self.clock.iso(), wk_tools=rev.out.strip() if rev.ok else "unknown")
        sj = os.path.join(slotdir, "slot.json")
        self.ok(isolated_module(os.path.join(self.tools, "lib"), "wk.slot") + ["manifest"] + list(readelf) + [root, sj]
                + ["%s=%s" % kv for kv in sorted(fields.items())], "could not write %s" % sj)
        try:
            return slot.recorded_build_id(self.m, sj)
        except ValueError as e:
            fail(str(e))

    def main(self):
        try:
            self.run()
        except Failed as e:
            sys.stderr.write("%s: error: %s\n" % (self.PREFIX, e))
            return 1
        return 0
