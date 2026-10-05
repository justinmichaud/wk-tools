"""project.json's REPOS: what a workspace holds, chosen at `wk new` and read back from its marker."""

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
        self.snapshot = self.clone == SNAPSHOT
        self.checkout = project.get("CHECKOUT") if self.snapshot else table[name]["checkout"]
        self.src = posixpath.join(posixpath.dirname(project.get("SRC")), self.checkout)

    def origin(self, machine, root):
        if self.snapshot:
            return dict(project.get("REMOTES"))["origin"]
        r = machine.run(["git", "-c", "safe.directory=*", "-C", str(root), "remote", "get-url", "origin"])
        m = GITHUB.match(r.out.strip()) if r.ok else None
        if not m:
            die("a %s workspace is cloned from the GitHub origin of this machine's wk-tools (%s),\n"
                "    and its origin is %s" % (self.name, root, "'%s'" % r.out.strip() if r.ok else "not set"))
        return "https://github.com/%s.git" % m.group(1)


def names():
    return sorted(project.get("REPOS"))


def default():
    return Repo(project.get("REPO"))


def marker_in(ws_dir):
    return os.path.join(ws_dir, "home", ".wk-workspace")


def of_marker(fields):
    return Repo(fields.get("repo") or project.get("REPO"))
