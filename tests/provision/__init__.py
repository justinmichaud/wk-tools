"""What the provisioning tests share.

    lanes(self, "tests.provision.toy")    # hooks are collected from this package, not lib/wk, until the test ends
    checkout(w.machines["desk"])          # the client's checkout is clean at SHA
    server(w.machines["lin"])             # git, ls, install, ln, the profile, df and date answer from the fake's files

toy/ is two lanes whose stages, rows and rubble a test sets with files on the fake machine; own/ is the provisioning
lane alone, with one lane that pins a download."""

import functools
from unittest import mock

from tests import ROOT
from wk.core import hooks
from wk.core.machine import Result
from wk.provision import tools

SHA = "5e1f" * 10
HOME = "/home/u/wk"
TOOLS = HOME + "/tools"
LISTED = {"755": "drwxr-xr-x", "700": "drwx------"}
DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 900000000 100 %d 1%% /\n"


def lanes(case, package):
    patch = mock.patch.object(hooks, "collect", functools.partial(hooks.collect, package=package))
    patch.start()
    case.addCleanup(patch.stop)


def checkout(m, sha=SHA, status=""):
    git = ("git", "-C", ROOT)
    m.on(git + ("rev-parse", "HEAD"), sha + "\n" if sha else Result(128, "", "fatal: not a git repository"))
    m.on(git + ("status", "--porcelain"), status)
    m.on(git + ("bundle", "create", "-", "HEAD"), Result(0, "", "", sha.encode()))


def client(m):
    m.on(("ssh", "-V"), Result(0, "", "OpenSSH_9.6\n"))
    m.on(("git", "--version"), "git version 2.43.0\n")
    m.on(("tailscale", "status"), "100.64.0.1 desk\n")


def server(m, shell="/bin/bash", home=HOME):
    f, repo, git = m.files, home + "/tools", ("git", "-C", home + "/tools")
    m.on(("true",), "")
    m.on(("python3",), "3.11\n")
    m.on(("git", "--version"), "git version 2.43.0\n")
    m.on(git + ("rev-parse", "HEAD"), lambda c: f.get(repo + "/.git/HEAD") or Result(128, "", "fatal: not a git repository"))
    m.on(git + ("status", "--porcelain"), lambda c: " M wk\n" if repo + "/edited" in f else "")
    m.on(("git", "init"), lambda c: f.update({repo + "/.git/config": ""}))
    m.on(git + ("fetch",), lambda c: f.update({repo + "/.git/FETCH_HEAD": f[c.argv[5]].decode()}))
    m.on(git + ("reset",), lambda c: f.update({repo + "/.git/HEAD": c.argv[-1]}) if f[repo + "/.git/FETCH_HEAD"] == c.argv[-1]
         else Result(128, "", "fatal: unknown revision"))
    m.on(git + ("clean",), lambda c: f.pop(repo + "/edited", None) and None)
    m.on(("ls", "-ld"), lambda c: "".join("%s  2 u  u  64 Nov 22 16:07 %s\n" % (f[path], path) for path in c.argv[2:] if path in f))
    m.on(("install", "-d", "-m"), lambda c: f.update({c.argv[4]: LISTED[c.argv[3]]}))
    m.on(("readlink",), lambda c: f.get(c.argv[1]) or Result(1))
    m.on(("ln", "-sfn"), lambda c: f.update({c.argv[3]: c.argv[2]}))
    m.on(("sh", "-c", 'printf %s "$SHELL"'), shell)
    m.on(("sh", "-c", tools.APPEND), lambda c: f.update({c.argv[5]: f.get(c.argv[5], "") + "\n%s\n" % c.argv[4]}))
    m.on(("sh", "-c", "command -v wk"), lambda c: home + "/bin/wk" if home + "/bin/wk" in f else Result(1))
    m.on(("df", "-Pk"), DF % (100 * 2 ** 20))
    m.on(("date", "+%s"), "1000\n")
    m.on(("du", "-sk"), lambda c: "%d\t%s\n" % (len(f[c.argv[2]]) * 1024, c.argv[2]))
