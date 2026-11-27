"""The credentials: one file per name under the controller's <wk home>/secrets, mode 0600 in directories of 0700.

A name with a `/` is a path (`bmc/moose/user`). A file holds its value and one newline; Ansible's `file` lookup
reads the same files on the controller, which is where every playbook runs."""

from wk.core.seams import Secrets

PARTIAL = ".tmp"    # what a write killed before its rename leaves; never a name


class Files(Secrets):
    def __init__(self, machine, directory):
        self.m, self.dir = machine, directory

    def get(self, name):
        text = self.m.read("%s/%s" % (self.dir, name))
        return None if text is None else text[:-1] if text.endswith("\n") else text

    def put(self, name, value):
        parts = name.split("/")[:-1]
        self.m.act(["mkdir", "-p", "-m", "700"] + ["/".join([self.dir] + parts[:n]) for n in range(len(parts) + 1)])
        self.m.write("%s/%s" % (self.dir, name), value + "\n", mode="600")

    def list(self):
        r = self.m.run(["find", self.dir, "-type", "f"])
        under = self.dir + "/"
        return sorted(p[len(under):] for p in r.out.splitlines() if p.startswith(under) and not p.endswith(PARTIAL))


def directory(home):
    return home + "/secrets"


def files(inv):
    return Files(inv.here, directory(inv.wk_home))
