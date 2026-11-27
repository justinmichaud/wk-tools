"""What the workspace lane's tests share: a server whose mirror is made and fetched, and git scripted to leave files."""

from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result
from wk.workspace import mirror

HOME = "/home/u/wk"
MIRROR = HOME + "/git/WebKit.git"
UPSTREAM = "https://github.com/WebKit/WebKit.git"
SRC = HOME + "/ws/x/src"
CLONE = "[lin] git clone --shared --no-tags -c fetch.writeCommitGraph=false -c gc.writeCommitGraph=false %s %s" % (MIRROR, SRC)
MAKE = [CLONE,
        "[lin] git -C %s remote set-url origin %s" % (SRC, UPSTREAM),
        "[lin] git -C %s remote set-url --push origin no-push://push-to-the-fork-remote" % SRC,
        "[lin] git -C %s config url.%s.insteadOf %s" % (SRC, MIRROR, UPSTREAM),
        "[lin] git -C %s remote add fork https://github.com/justinmichaud/WebKit.git" % SRC,
        "[lin] git -C %s config remote.fork.tagOpt --no-tags" % SRC,
        "[lin] mkdir -p %s/ws/x/build" % HOME,
        "[lin] driver create x",
        "[lin] write %s/ws/x/.ready" % HOME]
RESERVE, RELEASE = "[vault] PUT /ws/x", "[vault] DELETE /ws/x"
WIPE = ["[lin] remove %s/ws/x/.ready" % HOME, "[lin] driver destroy x", "[lin] remove %s/ws/x" % HOME]
FETCHED = "[lin] git -C %s rev-parse --verify --quiet refs/heads/main" % MIRROR


def git(machine, fetched=True):
    """Script git on a fake server: `init --bare` and `clone` leave the file wk looks for, and the mirror has main once fetched."""
    have = {"main": fetched}

    def init(call):
        machine.files[call.argv[-1] + "/HEAD"] = "ref: refs/heads/main\n"

    def clone(call):
        machine.files[call.argv[-1] + "/.git/HEAD"] = "ref: refs/heads/main\n"
    machine.on(("git", "init"), init)
    machine.on(("git", "clone"), clone)
    machine.on(("git", "-C", MIRROR, "rev-parse"), lambda call: Result(0 if have["main"] else 1))
    machine.on(("git", "-C", MIRROR, "fetch"), lambda call: have.update(main=True))


def server(w, name="lin"):
    """`name` as setup leaves it: the mirror made, configured and fetched."""
    machine = w.machines[name]
    git(machine)
    machine.files[MIRROR + "/HEAD"] = "ref: refs/heads/main\n"
    machine.files[MIRROR + "/config"] = mirror.CONFIG
    return machine


def inv(w, here="desk", env=None):
    return Inv("/", env or {}, Fleet(lambda: w.confs, here, w.machines.get, w.machines[here]), w.clock, None, {}, w.seams)
