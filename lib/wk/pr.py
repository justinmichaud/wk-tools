"""A PR head taken into a checkout, and a branch pointed at its fork, rebased, or opened as a pull request: git argv
through a place's exec."""

import re
import shlex

from wk import act, git, images, places, pushgate, secrets, sync
from wk.act import die, info, log, warn
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Local
from wk.places import show
from wk.store import Store
from wk.workspace import require_name

ROOT = images.root()
DIGITS = re.compile(r"^[0-9]+$")
PLANNED_COMMIT = "0" * 40
PUSH_REFS = "refs/wk/push/"


def push_lock(ref):
    return "push-ref-" + ref[len(PUSH_REFS):].replace("/", "%")


def rubble(store, machine, lock):
    """A box push's temporary ref a killed `wk pr open` left in the mirror; the branch it copied lives on the box."""
    from wk.rubble import row
    mirror = store.mirror_dir()
    if not machine.isdir(mirror):
        return []
    r = machine.run(["git", "-C", mirror, "for-each-ref", "--format=%(refname)", PUSH_REFS])
    if not r.ok:
        return [row("push-ref", "box pushes' temporary refs in the mirror", None,
                    why="not looked at -- git for-each-ref failed: %s" % r.err.strip())]
    rows = []
    for ref in r.out.split():
        pid = lock.holder_pid(push_lock(ref))
        busy = pid is not None and machine.alive(pid)
        rows.append(row("push-ref", "%s, left in the mirror by a killed box push" % ref, 0,
                        take=lambda ref=ref: machine.act_run(["git", "-C", mirror, "update-ref", "-d", ref]).ok,
                        why="kept -- the 'wk pr open' (pid %d) pushing it holds it" % pid if busy else ""))
    return rows


def parse_spec(spec):
    out = dict(kind="", user="", branch="", remote="", n="")
    remote, _, n = spec.rpartition(":") if spec.startswith("wpe:") else ("origin", "", spec)
    if n[:1].isdigit():
        if not DIGITS.match(n):
            die("'%s' is not a pull request number (digits only)" % spec)
        return dict(out, kind="pull", remote=remote, n=n)
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


# The `branch.<b>.merge` that makes `@{u}` resolve: git maps it through the remote's fetch refspec, so a mirror-shaped remote (`git.fetch_refspecs`) is named by its tracking ref and any other by `refs/heads/<b>`. `push.default = current` (dotfiles/gitconfig) pushes to `<b>`.
def merge_ref(driver, ws, src, remote, branch):
    specs = _out(driver.exec(ws, ["git", "-C", src, "config", "--get-all", "remote.%s.fetch" % remote])).splitlines()
    if git.fetch_refspecs(remote, True, [])[0] in specs:
        return "refs/remotes/%s/%s" % (remote, branch)
    return "refs/heads/" + branch


def track(src, remote, branch, merge):
    return [["git", "-C", src, "config", "branch.%s.remote" % branch, remote],
            ["git", "-C", src, "config", "branch.%s.merge" % branch, merge]]


def converge(driver, ws, src, forks):
    cfg = driver.exec(ws, ["git", "-C", src, "config", "--get-regexp", r"^branch\..*\.(remote|merge)$"])
    seen = {}
    for line in _out(cfg).splitlines():
        key, _, value = line.partition(" ")
        branch, _, field = key[len("branch."):].rpartition(".")
        seen.setdefault(branch, {})[field] = value
    names = {f[0] for f in forks}
    out = []
    for b, c in sorted(seen.items()):
        if c.get("remote") not in names or c.get("merge") != "refs/heads/" + b:
            continue
        merge = merge_ref(driver, ws, src, c["remote"], b)
        if merge != c["merge"] and all(driver.act_exec(ws, argv).ok for argv in track(src, c["remote"], b, merge)):
            out.append("converged: %s tracks %s/%s" % (b, c["remote"], b))
    return out


def _out(r):
    return r.out.replace("\r", "").strip() if r.ok else ""


def retarget(driver, ws, src, forks, branches, remotes=git.REMOTES):
    """Point the checked-out branch at the fork it can be pushed to; the lines to report."""
    b = _out(driver.exec(ws, ["git", "-C", src, "symbolic-ref", "--quiet", "--short", "HEAD"]))
    if not b:
        return []
    up = _out(driver.exec(ws, ["git", "-C", src, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]))
    names = {f[0] for f in forks}
    ups = [(n, u) for n, u in remotes if n not in names]
    if up in {"%s/%s" % (n, m) for n, _ in ups for m in branches}:
        return []
    f = next((git.fork_for(u, forks) for n, u in ups if up.startswith(n + "/") and git.fork_for(u, forks)), "")
    if not f:
        return []
    driver.act_exec(ws, ["git", "-C", src, "fetch", "-q", f, b])
    if not driver.exec(ws, ["git", "-C", src, "rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (f, b)]).ok:
        return ["left alone: %s is not on %s yet -- push it first:  git push %s %s" % (b, f, f, b)]
    if all(driver.act_exec(ws, argv).ok for argv in track(src, f, b, merge_ref(driver, ws, src, f, b))):
        return ["retargeted: %s now tracks %s/%s" % (b, f, b)]
    return []


def _probe(driver, name, src, branch):
    dirty = driver.exec(name, ["git", "-C", src, "status", "--porcelain"])
    if not dirty.ok:
        die("could not reach the checkout in '%s'" % name)
    local = _out(driver.exec(name, ["git", "-C", src, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch]))
    return local, len([l for l in dirty.out.replace("\r", "").splitlines() if l.strip()])


def _source(driver, here, name, src, pr, remotes):
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
    urls = driver.exec(name, ["git", "-C", src, "config", "--get-regexp", r"^remote\..*\.url$"])
    remote = next((l.split()[0][len("remote."):-len(".url")] for l in urls.out.replace("\r", "").splitlines()
                   if len(l.split()) == 2 and l.split()[1] == url), "")
    if remote:
        return repo, url, remote, branch, "refs/heads/" + branch, head, False
    remote = user if repo == git.pr_repos(remotes)[0] else "%s-%s" % (user, repo.lower())
    return repo, url, remote, branch, "refs/heads/" + branch, head, True


def checkout(driver, here, name, spec, remotes=git.REMOTES):
    """Fetch the one ref into the workspace's checkout and check it out; a local branch with commits the head lacks is kept unless --force."""
    pr = parse_spec(spec)
    src = driver.src(name)
    repo, url, remote, branch, src_ref, head, add = _source(driver, here, name, src, pr, remotes)
    local, dirty = _probe(driver, name, src, branch)
    if dirty:
        warn("'%s' has %d uncommitted change(s); the checkout carries them across" % (name, dirty))

    def step(argv, why):
        if not driver.act_exec(name, ["git", "-C", src] + argv).ok:
            die(why)
    if add:
        driver.act_exec(name, ["git", "-C", src, "remote", "add", remote, url])
        step(["remote", "set-url", remote, url], "could not add the remote '%s' in '%s'; nothing was checked out" % (remote, name))
    tracking = "refs/remotes/%s/%s" % (remote, branch)
    # By URL, never a remote, and --no-prune: every workspace's gitconfig has `fetch.prune = true`, and this must not retire origin/main.
    step(["fetch", "--quiet", "--no-prune", git.direct_url(url), "%s:%s" % (src_ref, tracking)],
         "could not fetch '%s' into '%s'; nothing was checked out" % (branch, name))
    reset = False
    if local and local != head:
        r = driver.exec(name, ["git", "-C", src, "rev-list", "--count", "%s..%s" % (tracking, branch)])
        ahead = _out(r) if r.ok else "unknown"
        if ahead == "0":
            reset = True
        elif ahead == "unknown" or not ahead.isdigit():
            act.barrier("cannot tell whether '%s' in '%s' has work the PR head does not.\n    Checking it out will leave it as it is." % (branch, name))
        elif act.forced():
            act.barrier("discarding %s local commit(s) on '%s' in '%s'." % (ahead, branch, name))
            reset = True
        else:
            warn("local '%s' has %s commit(s) the PR head does not have" % (branch, ahead))
            log("  it is checked out as it is; nothing is discarded.\n  to take the PR head instead and lose those commits:\n"
                "    wk pr %s %s --force" % (name, spec))
    why = "could not check out '%s' in '%s'" % (branch, name)
    if driver.exec(name, ["git", "-C", src, "show-ref", "--verify", "--quiet", "refs/heads/" + branch]).ok:
        step(["checkout", "--quiet", branch], why)
        if reset:
            step(["reset", "--hard", "--quiet", tracking], why)
    else:
        step(["checkout", "--quiet", "-b", branch, tracking], why)
    # A pull request head is no branch on the remote: nothing to track, and nothing to push back to.
    if pr["kind"] == "pull":
        driver.act_exec(name, ["git", "-C", src, "branch", "--quiet", "--unset-upstream", branch])
    else:
        for argv in track(src, remote, branch, merge_ref(driver, name, src, remote, branch)):
            step(argv[3:], why)
    info("'%s' is on %s (%s, from %s)" % (name, branch, repo, remote))
    log("  " + _out(driver.exec(name, ["git", "-C", src, "--no-pager", "log", "--oneline", "-1"])))


def pr_refname(user, repo, branch):
    return "%s/%s/%s" % (user, repo, branch)


def pull_refname(remote, n):
    return "%s/%s" % (remote, n)


def mirror_rev(machine, mirror, ref):
    r = machine.run(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    return r.out.strip() if r.ok else ""


def resolved_or_planned(machine, mirror, dest, what, fetch):
    """Under a dry run, `dest` from the mirror already there, or `fetch` planned unrun."""
    if act.dry_run():
        sha = mirror_rev(machine, mirror, dest)
        if sha:
            return sha
        log("would fetch %s into the mirror" % what)
        return PLANNED_COMMIT
    fetch()
    return mirror_rev(machine, mirror, dest)


def pr_open_target(src, run=lambda argv: Local().run(argv)):
    """(base-repo, "<owner>:<branch>", fork-remote, branch) for the checkout at `src`."""
    def ask(*args):
        r = run(["git", "-C", str(src)] + list(args))
        return r.rc, r.out.strip()

    rc, branch = ask("symbolic-ref", "--quiet", "--short", "HEAD")
    if rc != 0 or not branch:
        die("'wk pr open' needs a branch checked out, not a detached HEAD")
    if branch == "main":
        die("'wk pr open' cannot open 'main' as a pull request -- create a branch first")
    if ask("status", "--porcelain")[1]:
        die("the tree has uncommitted changes -- commit or stash them: git -C %s status" % src)
    rc, up = ask("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0 or not up:
        die("'%s' has no upstream, so 'wk pr open' cannot tell which project it belongs to -- "
            "track one first: git branch -u origin/main (WebKit) or wpe/main (WPEWebKit)" % branch)
    # Not `git remote get-url`: through the mirror's insteadOf every remote reads as the mirror, and both projects as one.
    up_url = ask("config", "--get", "remote.%s.url" % up.split("/", 1)[0])[1]
    up_repo = git.repo_of(up_url)
    push_forks = secrets.forks()
    # The project's upstream, not the fork `wk pr` leaves a branch tracking: a PR against that is one against yourself.
    fork = next((r for r, repo, _ in push_forks if git.repo_of(repo) == up_repo), None)
    if not fork:
        die("'%s' tracks '%s' (%s), not a known upstream (origin or wpe) -- "
            "'wk pr open' only knows WebKit and WPEWebKit" % (branch, up, up_url))
    rc, url = ask("config", "--get", "remote.%s.url" % fork)
    if rc != 0 or not url:
        die("no '%s' remote here -- 'wk sync --fix' wires it" % fork)
    owner = re.search(r"[:/]([^/]+)/[^/]+$", re.sub(r"\.git$", "", url))
    forks = {r for r, _, _ in push_forks}
    base = next((re.sub(r"^https://github\.com/|\.git$", "", u) for n, u in git.REMOTES
                 if n not in forks and u.endswith("/%s.git" % up_repo)), "")
    if not base:
        die("no upstream among the wired remotes for the project '%s', so there is nothing to open '%s' against"
            % (up_repo, branch))
    return base, "%s:%s" % (owner.group(1) if owner else "", branch), fork, branch


def pr_rebase(driver, name):
    src, mirror = driver.src(name), driver.mirror_dir()
    info("fetching in '%s' from the mirror, then rebasing onto origin/main" % name)
    if mirror and driver.exec(name, ["test", "-d", mirror]).ok:
        r = driver.act_exec(name, ["git", "-C", src, "fetch", "--prune", "--quiet", mirror,
                                   "+refs/heads/*:refs/remotes/origin/*"])
    else:
        r = driver.act_exec(name, ["git", "-C", src, "fetch", "--prune", "--quiet", "origin"])
    if r.ok:
        r = driver.act_exec(name, ["git", "-C", src, "rebase", "origin/main"])
    if r.ok:
        info("'%s' is rebased onto origin/main" % name)
        r = driver.exec(name, ["git", "-C", src, "--no-pager", "log", "--oneline", "-1"])
        log("  %s" % r.out.replace("\r", "").strip())
        return 0
    show(r)
    warn("the rebase stopped -- almost always a conflict, and the sequencer holds where.")
    log("  in the workspace:  git status; git rebase --continue   (or --abort)")
    return 1


def pr_open(driver, name, draft, web):
    src = driver.src(name)
    info("reading '%s's branch and remotes" % name)
    base, head, fork, branch = pr_open_target(src, run=lambda argv: driver.exec(name, argv))
    info("pushing '%s' to '%s'" % (branch, fork))
    if driver.kind == "remote" and not driver.is_here() and not driver.peer:
        r = push_from_here(driver, src, fork, branch)
    else:
        r = driver.act_exec(name, ["git", "-C", src, "push", "-u", fork, branch], person=True)
    show(r)
    if not r.ok:
        die("the push to '%s' failed -- see above; a refusal there is the push service's, and names its remedy" % fork)
    info("opening a pull request: %s <- %s" % (base, head))
    driver.here.exec(["gh", "pr", "create", "--repo", base, "--head", head, "--fill"]
                     + (["--draft"] if draft else []) + (["--web"] if web else []))


def push_from_here(driver, src, fork, branch):
    here, store = driver.here, Store(driver.env)
    sec = secrets.Secrets(ROOT, driver.env, here)
    key = sec.push_key_path(fork)
    if not here.exists(key):
        die("'wk pr open' pushes '%s' from here with the deploy key '%s', and this machine holds none.\n    wk key deploy" % (branch, fork))
    ref, lock = "%s%s/%s" % (PUSH_REFS, driver.name, branch), Lock(store, here, Clock())
    repo = next(r for f, r, _ in sec.forks() if f == fork)
    with lock.held(push_lock(ref)):
        sync.fetch_into_mirror(here, store, lock, "%s:%s" % (driver.ssh_host(""), src), "refs/heads/" + branch, ref)
        r = here.act_run(["git", "-C", store.mirror_dir(), "-c", "core.sshCommand=" + shlex.join(pushgate.ssh_prefix(key)), "push",
                          "git@github.com:%s.git" % repo, "%s:refs/heads/%s" % (ref, branch)])
        here.act_run(["git", "-C", store.mirror_dir(), "update-ref", "-d", ref])
    return r


def _load(reg, name, usage):
    if not name:
        die("usage: %s; see wk pr -h" % usage)
    require_name(name)
    return reg.present(name)


def main(name, words, draft, web):
    """`wk pr`'s verbs: `words` is the dispatcher's positionals, the verb first."""
    verb = words[0]
    reg = places.Registry(ROOT)
    if verb == "rebase":
        return pr_rebase(_load(reg, name, "wk pr rebase [<workspace>]; run inside a workspace, or name one"), name)
    if verb == "open":
        return pr_open(_load(reg, name, "wk pr open <workspace> [--draft] [--web]"), name, draft, web)
    spec = words[1]
    if not name:
        die("'wk pr %s' does not say which workspace to check it out into.\n"
            "    Name one:        wk pr <workspace> %s   ('wk ls' lists them)\n"
            "    or run it in one: wk pr %s" % (spec, spec, spec))
    checkout(_load(reg, name, ""), reg.machine, name, spec)
    return 0
