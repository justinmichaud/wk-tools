"""project.json's REPOS: what a workspace holds, chosen at `wk new` and read back from its marker, and the deploy keys
that push to it: rows of (key, GitHub repository, ssh alias), where a cloned repo's "" is the repository it is cloned from."""

import os
import posixpath
import re

from wk import project
from wk.act import die

SNAPSHOT = "snapshot"
GITHUB = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$")


class Repo:
    def __init__(self, name):
        table = project.get("REPOS")
        if name not in table:
            raise LookupError("unknown repo '%s' -- the known ones: %s" % (name, " ".join(names())))
        self.name, self.clone = name, table[name]["clone"]
        self.push = [list(r) for r in table[name].get("push", ())]
        self.snapshot = self.clone == SNAPSHOT
        self.checkout = project.get("CHECKOUT") if self.snapshot else table[name]["checkout"]
        self.src = posixpath.join(posixpath.dirname(project.get("SRC")), self.checkout)

    def github(self, machine, root):
        r = machine.run(["git", "-c", "safe.directory=*", "-C", str(root), "remote", "get-url", "origin"])
        m = GITHUB.match(r.out.strip()) if r.ok else None
        if not m:
            die("a %s workspace is cloned from the GitHub origin of this machine's wk-tools (%s),\n"
                "    and its origin is %s" % (self.name, root, "'%s'" % r.out.strip() if r.ok else "not set"))
        return m.group(1)

    def origin(self, machine, root):
        if self.snapshot:
            return dict(project.get("REMOTES"))["origin"]
        return "https://github.com/%s.git" % self.github(machine, root)

    def branch(self, machine, root):
        """The branch this machine's wk-tools is on, which a cloned workspace checks out: it must be on the origin too."""
        r = machine.run(["git", "-c", "safe.directory=*", "-C", str(root), "symbolic-ref", "--short", "HEAD"])
        name = r.out.strip() if r.ok else ""
        if not name:
            die("this machine's wk-tools (%s) is on no branch, and a %s workspace checks out the one it is on:\n"
                "    git -C %s switch <branch>" % (root, self.name, root))
        origin = self.origin(machine, root)
        r = machine.run(["git", "ls-remote", "--exit-code", "--heads", origin, name])
        if r.rc == 2:
            die("branch '%s' of this machine's wk-tools is not on %s, so a %s workspace cannot check it out:\n"
                "    git -C %s push -u origin %s" % (name, origin, self.name, root, name))
        if not r.ok:
            die("could not ask %s for branch '%s': %s" % (origin, name, r.err.strip() or "git ls-remote exited %d" % r.rc))
        return name

    def push_rows(self, machine, root):
        return [[k, gh or self.github(machine, root), alias] for k, gh, alias in self.push]

    def push_url(self, machine, root):
        _, gh, alias = self.push_rows(machine, root)[0]
        return "git@%s:%s.git" % (alias, gh)


def names():
    return sorted(project.get("REPOS"))


def push_keys():
    return [row for n in names() for row in Repo(n).push]


def push_rows(machine, root):
    return [row for n in names() for row in Repo(n).push_rows(machine, root)]


def default():
    return Repo(project.get("REPO"))


def marker_in(ws_dir):
    return os.path.join(ws_dir, "home", ".wk-workspace")


def of_marker(fields):
    return Repo(fields.get("repo") or project.get("REPO"))
