"""wk itself on a server: this checkout's HEAD at <wk_home>/tools, `wk` on the login shell's PATH, and the layout.

    <wk_home>/tools      a checkout at exactly this checkout's commit, delivered as a git bundle
    <wk_home>/bin/wk     a link to tools/wk; <wk_home>/bin is put on PATH by one line in the login shell's profile
    <wk_home>/bin cache git ws      directories
    <wk_home>/secrets    mode 0700

A profile line, not a link in ~/.local/bin: no default login PATH holds ~/.local/bin on macOS or for root."""

import shlex

from wk.core import act
from wk.core.act import barrier, die
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.machine import TIMED_OUT

PYTHON = (3, 9)
LAYOUT = (("bin", "755", "d"), ("cache", "755", "d"), ("git", "755", "d"), ("ws", "755", "d"), ("secrets", "700", "drwx------"))
APPEND = 'printf "\\n%s\\n" "$1" >>"$2"'


def tool(m, name, argv, remedy, timeout=30):
    """A row for a program that must answer: no answer in time is unknown, any other failure is missing."""
    r = m.run(argv, timeout=timeout)
    said = (r.out or r.err).strip().split("\n")[0]
    if r.rc == TIMED_OUT:
        return DoctorRow(name, UNKNOWN, "`%s` did not answer in %ds" % (shlex.join(argv), timeout), remedy)
    return DoctorRow(name, OK if r.ok else MISSING, said, remedy)


def prerequisites(m):
    """What a server must already have; wk installs no package."""
    remedy = "install it on %s with the system's package manager" % m.name
    python = tool(m, "python3", ["python3", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"], remedy)
    if python.status == OK and tuple(int(n) for n in python.detail.split(".")) < PYTHON:
        python = DoctorRow("python3", MISSING, "%s is older than %d.%d" % ((python.detail,) + PYTHON), remedy)
    return [python, tool(m, "git", ["git", "--version"], remedy)]


def head(inv):
    """This checkout's commit, or "" when it has none."""
    return inv.here.run(["git", "-C", inv.root, "rev-parse", "HEAD"]).out.strip()


def commit(m, tools):
    """The commit checked out at `tools`, said to be changed when its tree differs; "" when it is no checkout."""
    sha = m.run(["git", "-C", tools, "rev-parse", "HEAD"])
    if not sha.ok:
        return ""
    return sha.out.strip() + (" with changes" if m.run(["git", "-C", tools, "status", "--porcelain"]).out.strip() else "")


def lacking(m, home):
    """The layout's directories that are absent or wrongly moded, as (path, mode)."""
    want = [(home + "/" + name, mode, listed) for name, mode, listed in LAYOUT]
    lines = m.run(["ls", "-ld"] + [path for path, _, _ in want]).out.splitlines()
    return [(path, mode) for path, mode, listed in want
            if not any(line.startswith(listed) and line.endswith(" " + path) for line in lines)]


def must(m, argv):
    act.must(m, argv, "`wk doctor` names what %s lacks" % m.name)


def deliver(inv, m, tools, sha):
    if commit(m, tools) == sha:
        return False
    bundle = inv.here.run(["git", "-C", inv.root, "bundle", "create", "-", "HEAD"])
    if not bundle.ok:
        die("cannot bundle %s at %s: %s" % (inv.root, sha, bundle.err.strip()))
    packed = tools + "/.git/wk.bundle"
    must(m, ["git", "init", "-q", tools])
    m.write(packed, bundle.raw, mode="600")
    must(m, ["git", "-C", tools, "fetch", "-q", packed, "HEAD"])
    m.remove(packed)
    must(m, ["git", "-C", tools, "reset", "-q", "--hard", sha])
    # Without -x: an ignored file (__pycache__) is no change, here or to `git status`.
    must(m, ["git", "-C", tools, "clean", "-qfd"])
    return True


def layout(m, home):
    made = lacking(m, home)
    for path, mode in made:
        must(m, ["install", "-d", "-m", mode, path])
    return bool(made)


def link(m, home):
    target, name = home + "/tools/wk", home + "/bin/wk"
    if m.run(["readlink", name]).out.strip() == target:
        return False
    must(m, ["ln", "-sfn", target, name])
    return True


def profile(m):
    """The file the login shell reads: zsh ~/.zprofile; bash ~/.bash_profile when there is one, else ~/.profile, as sh does."""
    if m.run(["sh", "-c", 'printf %s "$SHELL"']).out.endswith("/zsh"):
        return m.home + "/.zprofile"
    bash = m.home + "/.bash_profile"
    return bash if m.exists(bash) else m.home + "/.profile"


def on_path(m, home):
    line, rc = 'export PATH=%s:"$PATH"' % shlex.quote(home + "/bin"), profile(m)
    if line in (m.read(rc) or "").splitlines():
        return False
    must(m, ["sh", "-c", APPEND, "sh", line, rc])
    return True


def converge(inv, conf):
    """Give the server this checkout's commit and put `wk` on its PATH; True when anything changed."""
    m = inv.machine(conf.name)
    home = inv.wk_home_on(m)
    sha = head(inv)
    if not sha:
        die("%s has no commit to give a machine: run wk from a clone of its repository" % inv.root)
    if inv.here.run(["git", "-C", inv.root, "status", "--porcelain"]).out.strip():
        barrier("this checkout has uncommitted changes, and a machine is given a commit and nothing else\n"
                "    commit or remove them: git -C %s status --short" % inv.root)
    for row in prerequisites(m):
        if row.status != OK:
            die("%s on %s: %s\n    %s" % (row.name, m.name, row.detail, row.remedy))
    return any([deliver(inv, m, home + "/tools", sha), layout(m, home), link(m, home), on_path(m, home)])
