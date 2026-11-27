"""What the provisioning tests share.

    lanes(self, "tests.provision.toy")    # hooks are collected from this package, not lib/wk, until the test ends
    checkout(w.machines["vault"])         # the controller's checkout is clean at SHA, which is on a remote branch
    controller(w.machines["vault"])       # the controller's tools answer: ssh, git, tailscale, ansible-playbook, xorriso, qemu-nbd
    server(w.machines["lin"])             # git, ls, the profile, df and date answer from the fake's files
    provisioned(w.machines["lin"])        # the files the playbook's wk role leaves

toy/ is a lane whose rows and rubble a test sets with files on the fake machine; own/ is the provisioning lane
alone, with one lane that pins a download."""

import functools
from unittest import mock

from tests import ROOT
from wk.core import hooks
from wk.core.machine import Result
from wk.provision import ansible

SHA = "5e1f" * 10
HOME = "/home/u/wk"
TOOLS = HOME + "/tools"
LAYOUT = {"bin": "drwxr-xr-x", "cache": "drwxr-xr-x", "git": "drwxr-xr-x", "ws": "drwxr-xr-x", "secrets": "drwx------"}
DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 900000000 100 %d 1%% /\n"


def lanes(case, package):
    patch = mock.patch.object(hooks, "collect", functools.partial(hooks.collect, package=package))
    patch.start()
    case.addCleanup(patch.stop)


def checkout(m, sha=SHA, status="", pushed="  origin/rewrite\n"):
    git = ("git", "-C", ROOT)
    m.on(git + ("rev-parse", "HEAD"), sha + "\n" if sha else Result(128, "", "fatal: not a git repository"))
    m.on(git + ("status", "--porcelain"), status)
    m.on(git + ("branch", "--remotes", "--contains"), pushed)


def controller(m, home=HOME):
    m.on(("ssh", "-V"), Result(0, "", "OpenSSH_9.6\n"))
    m.on(("git", "--version"), "git version 2.43.0\n")
    m.on(("tailscale", "status"), "100.64.0.1 vault\n")
    m.on(("ansible-playbook", "--version"), "ansible-playbook [core 2.19.3]\n  config file = None\n")
    m.on(("xorriso", "-version"), "xorriso 1.5.6\n")
    m.on(("qemu-nbd", "--version"), "qemu-nbd 8.2.2\n")
    m.files[ansible.websocat(home)] = ""


def server(m, home=HOME):
    f, repo, git = m.files, home + "/tools", ("git", "-C", home + "/tools")
    m.on(("true",), "")
    m.on(("python3",), "3.11\n")
    m.on(("git", "--version"), "git version 2.43.0\n")
    m.on(git + ("rev-parse", "HEAD"), lambda c: f.get(repo + "/.git/HEAD") or Result(128, "", "fatal: not a git repository"))
    m.on(git + ("status", "--porcelain"), lambda c: " M wk\n" if repo + "/edited" in f else "")
    m.on(("ls", "-ld"), lambda c: "".join("%s  2 u  u  64 Nov 22 16:07 %s\n" % (f[path], path) for path in c.argv[2:] if path in f))
    m.on(("sh", "-c", "command -v wk"), lambda c: home + "/bin/wk" if home + "/bin/wk" in f else Result(1))
    m.on(("df", "-Pk"), DF % (100 * 2 ** 20))
    m.on(("date", "+%s"), "1000\n")
    m.on(("du", "-sk"), lambda c: "%d\t%s\n" % (len(f[c.argv[2]]) * 1024, c.argv[2]))


def provisioned(m, home=HOME, sha=SHA):
    """The files the playbook's wk role leaves; a directory is a file holding the mode `ls -ld` prints for it."""
    m.files.update({"%s/%s" % (home, name): mode for name, mode in LAYOUT.items()})
    m.files.update({home + "/tools/.git/HEAD": sha, home + "/bin/wk": home + "/tools/wk"})
