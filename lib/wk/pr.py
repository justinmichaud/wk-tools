"""A PR head taken into a checkout or into the mirror (`wk ab`), and a branch pointed at its fork: git argv through a target's exec."""

import os
import re
import sys

from wk import act, git
from wk.act import die, info, log, warn
from wk.shell import sh_quote

DIGITS = re.compile(r"^[0-9]+$")


def parse_spec(spec):
    """{kind, user, branch, remote, n}: `<user>:<branch>`, `<n>` (WebKit/WebKit) or `wpe:<n>`."""
    out = dict(kind="", user="", branch="", remote="", n="")
    if spec[:1].isdigit():
        if not DIGITS.match(spec):
            die("'%s' is not a pull request number (digits only)" % spec)
        return dict(out, kind="pull", remote="origin", n=spec)
    if spec.startswith("wpe:") and spec[4:5].isdigit():
        if not DIGITS.match(spec[4:]):
            die("'%s' is not a pull request number (digits only)" % spec)
        return dict(out, kind="pull", remote="wpe", n=spec[4:])
    if ":" in spec:
        user, _, branch = spec.partition(":")
        if not user or not branch:
            die("expected <user>:<branch>, got '%s'" % spec)
        return dict(out, kind="user", user=user, branch=branch)
    die("'%s' is not a PR spec: <user>:<branch>, a pull request number, or wpe:<number>" % spec)


def ls_remote(machine, url, ref):
    r = machine.run(["git", "ls-remote", git.direct_url(url), ref])
    return (r.out.split() or [""])[0] if r.ok else ""


def branch_repo_urls(user, remotes=git.REMOTES):
    return ["https://github.com/%s/%s.git" % (user, repo) for repo in git.pr_repos(remotes)]


def branch_repos(machine, user, branch, remotes=git.REMOTES):
    """(repo, url, sha) per repository of that fork carrying the branch; every one is asked, so the caller refuses two by name."""
    out = []
    for repo, url in zip(git.pr_repos(remotes), branch_repo_urls(user, remotes)):
        sha = ls_remote(machine, url, "refs/heads/" + branch)
        if sha:
            out.append((repo, url, sha))
    return out


# Written as config, never `git branch -u`: git maps the tracking ref back through a non-origin remote's
# `+refs/remotes/<r>/*` refspec, answers with the tracking ref itself, and `git push` then refuses (measured, git 2.43).
def track(src, remote, branch):
    return [["git", "-C", src, "config", "branch.%s.remote" % branch, remote],
            ["git", "-C", src, "config", "branch.%s.merge" % branch, "refs/heads/" + branch]]


def _out(r):
    return r.out.replace("\r", "").strip() if r.ok else ""


def retarget(target, ws, src, forks, branches, remotes=git.REMOTES):
    """Point the checked-out branch at the fork it can be pushed to: the lines to report, none when it is already right."""
    b = _out(target.exec(ws, ["git", "-C", src, "symbolic-ref", "--quiet", "--short", "HEAD"]))
    if not b:
        return []
    up = _out(target.exec(ws, ["git", "-C", src, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]))
    names = {f[0] for f in forks}
    ups = [(n, u) for n, u in remotes if n not in names]
    if up in {"%s/%s" % (n, m) for n, _ in ups for m in branches}:
        return []
    f = next((git.fork_for(u, forks) for n, u in ups if up.startswith(n + "/") and git.fork_for(u, forks)), "")
    if not f:
        return []
    target.act_exec(ws, ["git", "-C", src, "fetch", "-q", f, b])
    if not target.exec(ws, ["git", "-C", src, "rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (f, b)]).ok:
        return ["left alone: %s is not on %s yet -- push it first:  git push %s %s" % (b, f, f, b)]
    if all(target.act_exec(ws, argv).ok for argv in track(src, f, b)):
        return ["retargeted: %s now tracks %s/%s" % (b, f, b)]
    return []


def _probe(target, name, src, branch):
    dirty = target.exec(name, ["git", "-C", src, "status", "--porcelain"])
    if not dirty.ok:
        die("could not reach the checkout in '%s'" % name)
    local = _out(target.exec(name, ["git", "-C", src, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch]))
    return local, len([l for l in dirty.out.replace("\r", "").splitlines() if l.strip()])


def _source(target, here, name, src, pr, remotes):
    if pr["kind"] == "pull":
        remote = pr["remote"]
        url = dict(remotes).get(remote)
        if not url:
            die("no such upstream remote '%s'" % remote)
        branch = "pr/%s" % pr["n"] if remote == "origin" else "pr/%s-%s" % (remote, pr["n"])
        src_ref = "refs/pull/%s/head" % pr["n"]
        head = ls_remote(here, url, src_ref)
        if not head:
            die("no pull request #%s on %s (checked %s)" % (pr["n"], git.repo_of(url), url))
        return git.repo_of(url), url, remote, branch, src_ref, head, False
    user, branch = pr["user"], pr["branch"]
    found = branch_repos(here, user, branch, remotes)
    if not found:
        die("no branch '%s' in %s under '%s'.\n    Checked: %s" % (branch, "/".join(git.pr_repos(remotes)), user,
                                                             " ".join(branch_repo_urls(user, remotes))))
    if len(found) > 1:
        die("'%s' exists in more than one of %s's repositories:\n%s\n    They are different projects; check the PR page for which one it is and\n"
            "    fetch that remote by hand." % (branch, user, "\n".join("    %s %s %s" % f for f in found)))
    repo, url, head = found[0]
    urls = target.exec(name, ["git", "-C", src, "config", "--get-regexp", r"^remote\..*\.url$"])
    remote = next((l.split()[0][len("remote."):-len(".url")] for l in urls.out.replace("\r", "").splitlines()
                   if len(l.split()) == 2 and l.split()[1] == url), "")
    if remote:
        return repo, url, remote, branch, "refs/heads/" + branch, head, False
    remote = user if repo == git.pr_repos(remotes)[0] else "%s-%s" % (user, repo.lower())
    return repo, url, remote, branch, "refs/heads/" + branch, head, True


def checkout(target, here, name, spec, remotes=git.REMOTES):
    """Fetch the one ref into the workspace's checkout and check it out; a local branch with commits the head lacks is kept unless --force."""
    pr = parse_spec(spec)
    src = target.src(name)
    repo, url, remote, branch, src_ref, head, add = _source(target, here, name, src, pr, remotes)
    local, dirty = _probe(target, name, src, branch)
    if dirty:
        warn("'%s' has %d uncommitted change(s); the checkout carries them across" % (name, dirty))

    def step(argv, why):
        if not target.act_exec(name, ["git", "-C", src] + argv).ok:
            die(why)
    if add:
        target.act_exec(name, ["git", "-C", src, "remote", "add", remote, url])
        step(["remote", "set-url", remote, url], "could not add the remote '%s' in '%s'; nothing was checked out" % (remote, name))
    tracking = "refs/remotes/%s/%s" % (remote, branch)
    # By URL, never a remote, and --no-prune: every workspace's gitconfig has `fetch.prune = true`, and this must not retire origin/main.
    step(["fetch", "--quiet", "--no-prune", git.direct_url(url), "%s:%s" % (src_ref, tracking)],
         "could not fetch '%s' into '%s'; nothing was checked out" % (branch, name))
    reset = False
    if local and local != head:
        r = target.exec(name, ["git", "-C", src, "rev-list", "--count", "%s..%s" % (tracking, branch)])
        ahead = _out(r) if r.ok else "unknown"
        if ahead == "0":
            reset = True
        elif ahead == "unknown" or not ahead.isdigit():
            act.barrier("cannot tell whether '%s' in '%s' has work the PR head does not.\n    Checking it out will leave it as it is." % (branch, name))
        elif os.environ.get("WK_FORCE"):
            act.barrier("discarding %s local commit(s) on '%s' in '%s'." % (ahead, branch, name))
            reset = True
        else:
            warn("local '%s' has %s commit(s) the PR head does not have" % (branch, ahead))
            log("  it is checked out as it is; nothing is discarded.\n  to take the PR head instead and lose those commits:\n"
                "    wk pr %s %s --force" % (name, spec))
    why = "could not check out '%s' in '%s'" % (branch, name)
    if target.exec(name, ["git", "-C", src, "show-ref", "--verify", "--quiet", "refs/heads/" + branch]).ok:
        step(["checkout", "--quiet", branch], why)
        if reset:
            step(["reset", "--hard", "--quiet", tracking], why)
    else:
        step(["checkout", "--quiet", "-b", branch, tracking], why)
    # A pull request head is no branch on the remote: nothing to track, and nothing to push back to.
    if pr["kind"] == "pull":
        target.act_exec(name, ["git", "-C", src, "branch", "--quiet", "--unset-upstream", branch])
    else:
        for argv in track(src, remote, branch):
            step(argv[3:], why)
    info("'%s' is on %s (%s, from %s)" % (name, branch, repo, remote))
    log("  " + _out(target.exec(name, ["git", "-C", src, "--no-pager", "log", "--oneline", "-1"])))


def pr_refname(user, repo, branch):
    return "%s/%s/%s" % (user, repo, branch)


def pull_refname(remote, n):
    return "%s/%s" % (remote, n)


def mirror_fetch(here, store, lock, src, srcspec, dest):
    """One ref from `src` into this machine's mirror, made on first use, under the store lock; a dry run fetches too, to plan from."""
    if store.env.get("WK_IN_VM"):
        die("the mirror in here is the host's, mounted read-only; run this on the host")
    mirror = store.mirror()
    here.mkdir(os.path.dirname(mirror))
    with lock.held("store"):
        if not here.isdir(mirror):
            info("creating bare mirror (first run: this clones all of WebKit)")
            if not (here.run(["git", "init", "--bare", "-q", mirror]).ok
                    and here.run(["git", "-C", mirror, "config", "gc.auto", "0"]).ok):
                die("could not make the mirror at %s" % mirror)
        r = here.run(["git", "-C", mirror, "fetch", "--quiet", src, "+%s:%s" % (srcspec, dest)])
        if not r.ok:
            sys.stderr.write(r.err)
            die("could not fetch %s from %s into the mirror" % (srcspec, src), r.rc)


def mirror_fetch_pull(here, store, lock, remote, n, remotes=git.REMOTES):
    url = dict(remotes).get(remote)
    if not url:
        die("no such upstream remote '%s' to fetch a pull request from" % remote)
    mirror_fetch(here, store, lock, url, "refs/pull/%s/head" % n, "refs/remotes/pr/" + pull_refname(remote, n))


def main(argv):
    from wk.clock import Clock
    from wk.lock import Lock
    from wk.machine import Local
    from wk.store import Store
    verb, a = (argv[0] if argv else ""), argv[1:]
    here, store = Local(), Store()

    def fetch(*args):
        return lambda: mirror_fetch(here, store, Lock(store, here, Clock()), *args)
    verbs = {
        "parse-spec": (1, lambda: sys.stdout.write("".join("PR_%s=%s\n" % (k.upper(), sh_quote(v)) for k, v in sorted(parse_spec(a[0]).items())))),
        "branch-repo": (2, lambda: sys.stdout.write("".join("%s %s %s\n" % f for f in branch_repos(here, *a)))),
        "branch-repo-urls": (1, lambda: sys.stdout.write("".join(u + "\n" for u in branch_repo_urls(a[0])))),
        "pr-refname": (3, lambda: sys.stdout.write(pr_refname(*a))),
        "pull-refname": (2, lambda: sys.stdout.write(pull_refname(*a))),
        "mirror-fetch": (3, lambda: fetch(*a)()),
        "mirror-fetch-pr": (3, lambda: fetch(a[0], "refs/heads/" + a[1], "refs/remotes/pr/" + a[2])()),
        "mirror-fetch-pull": (2, lambda: mirror_fetch_pull(here, store, Lock(store, here, Clock()), *a)),
    }
    if verb not in verbs or len(a) != verbs[verb][0]:
        sys.stderr.write("usage: python3 -m wk.pr %s\n" % "|".join(sorted(verbs)))
        return 2
    try:
        verbs[verb][1]()
    except act.Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
