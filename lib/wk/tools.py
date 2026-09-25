"""A machine is given this checkout's commit: a bundle of HEAD (3.8 MB in 0.9s) by the far Machine's `copy_in`."""

import os
import sys
import tempfile

from wk import act
from wk.machine import Local, Ssh

BUNDLE = ".git/wk-tools-push.bundle"

PREPARE = r'''set -e
d=$1
command -v git >/dev/null 2>&1 || {
    echo "no git on this machine, so wk-tools cannot be a checkout here" >&2
    exit 1
}
if [ -L "$d" ]; then
    echo "$d is a symlink, and wk-tools here is a directory; not replacing it" >&2
    exit 1
fi
if ! { [ -d "$d/.git" ] && git -C "$d" rev-parse --git-dir >/dev/null 2>&1; }; then
    if [ -e "$d" ]; then
        echo "replacing $d: it is not a git checkout" >&2
    fi
    rm -rf "$d"
    mkdir -p "$d"
    git -c init.defaultBranch=main init -q "$d"
fi
'''

# `git clean` without -x, so ignored files survive.
CONVERGE = r'''set -e
d=$1
git -C "$d" fetch -q "$d/%s" HEAD
rm -f "$d/%s"
git -C "$d" reset -q --hard "$2"
git -C "$d" clean -qfd
git -C "$d" rev-parse HEAD
''' % (BUNDLE, BUNDLE)

NOT_A_COMMIT = ("    A machine is given a commit and nothing else -- that is what lets\n"
                "    'wk status' compare the copy over there with this one by sha.\n")


def committed(root, here):
    """Why this checkout has no commit to give a machine, or "": tracked changes only, so an ignored or untracked file is none."""
    if not here.run(["git", "-C", root, "rev-parse", "--git-dir"]).ok:
        return ("%s is not a git checkout, so there is no commit to put on a machine.\n%s"
                "    Run wk from a clone:  git clone https://github.com/justinmichaud/wk-tools" % (root, NOT_A_COMMIT))
    r = here.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"])
    if r.ok and not r.out.strip():
        return ""
    return ("wk-tools here has uncommitted changes, so there is no commit to put on a machine.\n%s"
            "    Commit them and re-run:\n        git -C %s status --short\n        git -C %s commit -a" % (NOT_A_COMMIT, root, root))


def dest_ok(dest, home):
    """The floor under the far side's `rm -rf`: absolute, two components or more, no trailing slash, not the home."""
    if not dest.startswith("/") or dest.endswith("/") or dest == home:
        return False
    return "/" in dest[1:]


def _said(r):
    sys.stderr.write(r.err.replace("\r", ""))


def push(root, here, far, dest, env):
    """Whether `dest` on `far` ended a checkout at this tree's HEAD; each refusal is warned with its remedy."""
    if not dest_ok(dest, env.get("HOME", "")):
        act.warn("refusing to push wk-tools to '%s'. The far side replaces that directory\n"
                 "    outright when it is not already a checkout, so the destination must be an\n"
                 "    absolute path of at least two components, with no trailing slash, and\n"
                 "    neither / nor the account's home." % dest)
        return False
    why = committed(root, here)
    if why:
        act.warn(why)
        return False
    r = here.run(["git", "-C", root, "rev-parse", "HEAD"])
    sha = r.out.strip()
    if not r.ok or not sha:
        act.warn("cannot read HEAD in %s" % root)
        return False
    fd, bundle = tempfile.mkstemp(prefix="wk-tools-push.")
    os.close(fd)
    try:
        if not here.run(["git", "-C", root, "bundle", "create", bundle, "HEAD"]).ok:
            act.warn("could not bundle %s at %s for the push" % (root, sha))
            return False
        act.debug("pushing wk-tools %s -> %s" % (sha, dest))
        r = far.act_run(["sh", "-c", PREPARE, "sh", dest])
        _said(r)
        if not r.ok:
            act.warn("could not make %s a checkout on %s" % (dest, far.name))
            return False
        try:
            far.copy_in(bundle, "%s/%s" % (dest, BUNDLE))
        except OSError as e:
            act.warn("could not copy the bundle to %s: %s" % (far.name, e))
            return False
        r = far.act_run(["sh", "-c", CONVERGE, "sh", dest, sha])
    finally:
        os.unlink(bundle)
    if act.dry_run():
        return True
    _said(r)
    got = (r.out.replace("\r", "").strip().splitlines() or [""])[-1]
    if got == sha:
        return True
    act.warn("wk-tools at %s did not end at %s (it answers '%s')" % (dest, sha, got or "nothing"))
    return False


def main(argv, env=None):
    env = os.environ if env is None else env
    root = env.get("WK_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    here = Local()
    if argv[:1] == ["push"] and len(argv) >= 3:
        far = Ssh(argv[2], opts=argv[3:], timeout=int(env.get("WK_SSH_TIMEOUT") or 10), via=here)
        return 0 if push(root, here, far, argv[1], env) else 1
    act.die("usage: python3 -m wk.tools push <dest> <ssh destination> [ssh option]...")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except act.Refused as e:
        sys.exit(e.status)
