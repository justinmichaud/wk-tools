"""The upstreams, and the scripts wiring a checkout or a mirror to them; a fork row is `wk_push_forks`' (lib/store.sh)."""

import os
import re
import sys

from wk import images
from wk.shell import sh_quote as q

REMOTES = (
    ("origin", "https://github.com/WebKit/WebKit.git"),
    ("wpe", "https://github.com/WebPlatformForEmbedded/WPEWebKit.git"),
    ("fork", "https://github.com/justinmichaud/WebKit.git"),
    ("forkwpe", "https://github.com/justinmichaud/WPEWebKit.git"),
)
NO_PUSH = "no-push://use-a-fork-remote"
TOLERATE = "tolerate"
BARE = re.compile(r"^[A-Za-z0-9_.@%+=:,-]+$")


def repo_of(url):
    return re.sub(r"(\.git)?$", "", url).rsplit("/", 1)[-1]


def pr_repos(remotes=REMOTES):
    out = []
    for _, url in remotes:
        if repo_of(url) not in out:
            out.append(repo_of(url))
    return out


# Spelled so no url.<mirror>.insteadOf catches it: those are keyed on the `.git` form, and a mirror holds no PR head.
def direct_url(url):
    return url[:-4] if url.endswith(".git") else url


def mirror_branches(env=None):
    env = os.environ if env is None else env
    if env.get("WK_MIRROR_BRANCHES"):
        return env["WK_MIRROR_BRANCHES"].split()
    return ["main"] + images.origin_branches(env)


def upstreams(forks, remotes=REMOTES):
    names = {f[0] for f in forks}
    return [(n, u) for n, u in remotes if n != "origin" and n not in names]


def fork_for(url, forks):
    return next((f[0] for f in forks if f[1].endswith("/" + repo_of(url))), "")


def fetch_refspecs(remote, mirror, branches):
    if remote == "origin":
        return ["+refs/heads/%s:refs/remotes/origin/%s" % (b, b) for b in branches]
    if mirror:
        return ["+refs/remotes/%s/*:refs/remotes/%s/*" % (remote, remote)]
    return ["+refs/heads/*:refs/remotes/%s/*" % remote]


def _word(w):
    return w if BARE.match(w) else q(w)


# One script, not an exec per step: a guest and a build box take it down one ssh session beside other steps, and firstrun runs it as text.
def render(src, steps):
    lines = ["set -e", "cd " + q(src)]
    for step in steps:
        if isinstance(step, str):
            lines.append(step)
            continue
        argv, alt = step
        line = " ".join(_word(w) for w in argv)
        if alt == TOLERATE:
            line += " 2>/dev/null || true"
        elif alt:
            line += " 2>/dev/null || " + " ".join(_word(w) for w in alt)
        lines.append(line)
    return "\n".join(lines) + "\n"


def _set_url(name, url):
    return ["git", "remote", "add", name, url], ["git", "remote", "set-url", name, url]


STALE_REWRITES = ("git config --local --name-only --get-regexp '^url\\..*\\.(push)?insteadof$' 2>/dev/null"
                  " | while IFS= read -r k; do s=${k%.pushinsteadof}; s=${s%.insteadof};"
                  ' git config --local --remove-section "$s" 2>/dev/null || true; done')


# The URL is rewritten rather than replaced, so `remote.<r>.url` still answers with GitHub -- what git-webkit reads to find the project.
def fetch_config(mirror, branches, remotes=REMOTES):
    steps = [STALE_REWRITES]
    for name, url in remotes:
        if mirror:
            steps.append((["git", "config", "--add", "url.%s.insteadOf" % mirror, url], None))
        steps.append((["git", "config", "--unset-all", "remote.%s.fetch" % name], TOLERATE))
        steps += [(["git", "config", "--add", "remote.%s.fetch" % name, s], None) for s in fetch_refspecs(name, mirror, branches)]
        steps.append((["git", "config", "remote.%s.tagOpt" % name, "--no-tags"], None))
    # git-webkit setup fetches every remote in parallel, and two fetches writing the commit graph collide on its lock.
    return steps + [(["git", "config", "fetch.writeCommitGraph", "false"], None),
                    (["git", "config", "gc.writeCommitGraph", "false"], None)]


# A fork records only its github.com URL, and git ignores pushInsteadOf for a remote with a `pushurl`: git-webkit takes any other host there for a GitHub instance whose credentials it hunts for in a keyring.
def push_rewrite(forks):
    return [(["git", "config", "url.git@%s:%s.git.pushInsteadOf" % (alias, repo), "https://github.com/%s.git" % repo], None)
            for _, repo, alias in forks]


def wiring(mirror, forks, branches, extra_name="", extra_url="", ssh_config="", remotes=REMOTES):
    origin = dict(remotes)["origin"]
    steps = [(["git", "remote", "set-url", "origin", origin], ["git", "remote", "add", "origin", origin]),
             (["git", "remote", "set-url", "--push", "origin", NO_PUSH], None)]
    for remote, repo, _ in forks:
        steps += [_set_url(remote, "https://github.com/%s.git" % repo),
                  (["git", "config", "--unset-all", "remote.%s.pushurl" % remote], TOLERATE)]
    for name, url in upstreams(forks, remotes):
        steps += [_set_url(name, url), (["git", "remote", "set-url", "--push", name, NO_PUSH], None)]
    if ssh_config:
        steps.append((["git", "config", "core.sshCommand", "ssh -F " + ssh_config], None))
    if extra_name and extra_url:
        steps += [_set_url(extra_name, extra_url),
                  (["git", "remote", "set-url", "--push", extra_name, "no-push://%s-is-a-local-copy" % extra_name], None),
                  (["git", "config", "--unset-all", "remote.%s.fetch" % extra_name], TOLERATE)]
        steps += [(["git", "config", "--add", "remote.%s.fetch" % extra_name, "+refs/heads/%s:refs/remotes/%s/%s" % (b, extra_name, b)], None)
                  for b in branches]
        steps.append((["git", "config", "remote.%s.tagOpt" % extra_name, "--no-tags"], None))
    return steps + fetch_config(mirror, branches, remotes) + push_rewrite(forks)


def wiring_script(src, mirror, forks, branches, extra_name="", extra_url="", ssh_config="", remotes=REMOTES):
    return render(src, wiring(mirror, forks, branches, extra_name, extra_url, ssh_config, remotes))


# The pre-push hook classifies by the rewritten URL `git remote -v` gives; every remote wk wires is public, so no secure one may carry the sentinel.
def hook_levels(forks):
    return " ".join(["--level %s=0" % NO_PUSH] + ["--level %s:%s=0" % (alias, repo) for _, repo, alias in forks])


GITWEBKIT_SETUP = '''if [ "$(git config --get webkitscmpy.setup 2>/dev/null)" = true ]; then
    state=already
else
    Tools/Scripts/git-webkit setup --defaults </dev/null >&2 || { echo setup=failed; exit 1; }
    state=ok
fi
# Re-asserted for a checkout set up before, whose hook `setup` baked without the levels. Unquoted to split into flags.
Tools/Scripts/git-webkit install-hooks $WK_HOOK_LEVELS </dev/null >&2 || { echo setup=hooks-failed; exit 1; }
echo "setup=$state"
'''


def gitwebkit_setup_script(src, forks):
    return "cd %s || exit 2\nWK_HOOK_LEVELS=%s\n%s" % (q(src), q(hook_levels(forks)), GITWEBKIT_SETUP)


# `git config remote.<r>.url`, not `git remote get-url`, which applies the mirror rewrite; it is also the value git-webkit reads.
def wiring_check_script(src, mirror, forks, branches, skip_env="", remotes=REMOTES):
    out = ['cd %s || exit 2' % q(src), 'bad=0',
           'u=$(git config --get remote.origin.url 2>/dev/null || echo "")',
           'p=$(git remote get-url --push origin 2>/dev/null || echo "")',
           'case "$u" in\n  %s) ;;\n  "") echo "problem: no origin remote at all"; bad=1 ;;\n'
           '  *)  echo "problem: origin is $u -- origin must be upstream (WebKit/WebKit); a local copy is what a second remote is for"; bad=1 ;;\nesac'
           % dict(remotes)["origin"],
           'if [ -n "$u" ]; then case "$p" in\n  no-push://*) ;;\n'
           '  *) echo "problem: origin accepts a push ($p) -- there is no write access to upstream, and this is how a push goes to the wrong repository"; bad=1 ;;\nesac\nfi']
    for name, url in upstreams(forks, remotes):
        out += ['u=$(git config --get remote.%s.url 2>/dev/null || echo "")' % name,
                'p=$(git remote get-url --push %s 2>/dev/null || echo "")' % name,
                'case "$u" in\n  %s) ;;\n  "") echo "problem: no %s remote (upstream %s), so its branches cannot be fetched at all"; bad=1 ;;\n'
                '  *)  echo "problem: %s is $u, not %s"; bad=1 ;;\nesac' % (url, name, url, name, url),
                'if [ -n "$u" ]; then case "$p" in\n  no-push://*) ;;\n'
                '  *) echo "problem: %s accepts a push ($p) -- we never push to an upstream"; bad=1 ;;\nesac\nfi' % name]
    for remote, repo, alias in forks:
        out += ['u=$(git config --get remote.%s.url 2>/dev/null || echo "")' % remote,
                'p=$(git remote get-url --push %s 2>/dev/null || echo "")' % remote,
                'case "$u" in\n  https://github.com/%s.git) ;;\n  "") echo "problem: no %s remote (the fork)"; bad=1 ;;\n'
                '  *)  echo "problem: %s fetches from $u, not https://github.com/%s.git"; bad=1 ;;\nesac' % (repo, remote, remote, repo),
                'r=$(git config --get remote.%s.pushurl 2>/dev/null || echo "")' % remote,
                'if [ -n "$u" ]; then if [ -n "$r" ]; then\n  echo "problem: %s records $r as a push URL -- git ignores the ssh-alias rewrite for a remote '
                'that has one, and git-webkit reads every remote URL and takes a host other than github.com for a GitHub instance of its own, whose '
                'credentials it then looks for in a keyring"; bad=1\nfi\ncase "$p" in\n  git@%s:%s.git) ;;\n'
                '  *) echo "problem: %s pushes to $p, not git@%s:%s.git -- the deploy key is chosen by that ssh alias, so no key is offered at all"; bad=1 ;;\n'
                'esac\nfi' % (remote, alias, repo, remote, alias, repo)]
        if skip_env:
            continue
        out += ['c=$(git config core.sshCommand 2>/dev/null || echo "")',
                'case "$c" in\n  *"-F "*) f=${c#*-F }; f=${f%%%% *}; h=$(ssh -G -F "$f" %s 2>/dev/null | sed -n "s/^hostname //p") ;;\n'
                '  *) h=$(ssh -G %s 2>/dev/null | sed -n "s/^hostname //p") ;;\nesac\n'
                'if [ "$h" != github.com ]; then\n  echo "problem: the ssh alias %s resolves to ${h:-nothing}, not github.com -- the deploy key is '
                'chosen by that alias, so a push offers no key at all"; bad=1\nfi' % (alias, alias, alias)]
    names = {f[0] for f in forks}
    keep = "|".join(['""'] + ["%s/%s" % (n, b) for n, _ in remotes if n not in names for b in branches])
    out.append('b=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")\nif [ -n "$b" ]; then\n'
               '  up=$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || echo "")\n  case "$up" in\n    %s) ;;' % keep)
    for name, url in remotes:
        f = fork_for(url, forks) if name not in names else ""
        if f:
            out.append('    %s/*) echo "problem: branch $b tracks $up, and we never push to %s -- it belongs to the fork: %s/$b"; bad=1 ;;' % (name, name, f))
    out.append('  esac\nfi')
    return "\n".join(out + fetch_check(mirror, branches, remotes) + ["exit $bad"]) + "\n"


def fetch_check(mirror, branches, remotes=REMOTES):
    out = []
    if mirror:
        out.append('ins=" $(git config --get-all %s 2>/dev/null | tr "\\n" " ")"' % q("url.%s.insteadOf" % mirror))
    for name, url in remotes:
        out += ['w=%s' % q(" ".join(fetch_refspecs(name, mirror, branches))),
                'g=$(git config --get-all remote.%s.fetch 2>/dev/null | tr "\\n" " "); g="${g%% }"' % name,
                'if [ "$g" != "$w" ]; then echo "problem: %s asks for $g, not $w -- that is a remote-tracking ref per branch of the upstream, '
                'over the network"; bad=1; fi' % name,
                't=$(git config --get remote.%s.tagOpt 2>/dev/null || echo "")' % name,
                'if [ "$t" != --no-tags ]; then echo "problem: %s follows tags, so every fetch re-negotiates every tag the upstream has"; bad=1; fi' % name]
        if mirror:
            out.append('case "$ins" in *" %s "*) ;; *) echo "problem: %s is not rewritten to %s, so a fetch of it goes to github.com"; bad=1 ;; esac'
                       % (url, name, mirror))
    # origin's refspecs name one head each, and a fetch dies on the first the mirror lacks -- taking `git-webkit setup`'s fetch with it.
    for b in branches if mirror else []:
        out.append('git -C %s rev-parse --verify --quiet %s >/dev/null 2>&1 || { echo "problem: the mirror %s carries no %s, which origin asks it for '
                   '-- every fetch in here fails on it; \'wk sync --mirror\' on the machine that keeps it"; bad=1; }'
                   % (q(mirror), q("refs/heads/" + b), mirror, "refs/heads/" + b))
    return out


# The layout fetch_refspecs reads, with origin narrowed (it advertises 924 heads); gc.auto 0, or a repack breaks the `--shared` clones borrowing its objects.
def mirror_refresh_script(mirror, branches, remotes=REMOTES):
    out = ["set -e", "M=%s" % q(mirror), 'if [ ! -d "$M" ]; then', '    git init --bare -q "$M"', '    git -C "$M" config gc.auto 0', "fi"]
    for name, url in remotes:
        out += ['git -C "$M" remote set-url %s %s 2>/dev/null || git -C "$M" remote add %s %s' % (name, q(url), name, q(url)),
                'git -C "$M" config remote.%s.tagOpt --no-tags' % name]
        if name == "origin":
            out.append('git -C "$M" config --unset-all remote.origin.fetch 2>/dev/null || true')
            out += ['git -C "$M" config --add remote.origin.fetch %s' % q("+refs/heads/%s:refs/heads/%s" % (b, b)) for b in branches]
        else:
            out.append('git -C "$M" config --replace-all remote.%s.fetch %s' % (name, q("+refs/heads/*:refs/remotes/%s/*" % name)))
    out += ["for r in %s; do" % " ".join(n for n, _ in remotes),
            '    if git -C "$M" fetch --prune -q "$r" 2>/dev/null; then', '        echo "mirror-fetch $r ok"',
            '    else echo "mirror-fetch $r FAILED"', "    fi", "done"]
    # TODO: upstream -- `git fetch` in a bare repository overwrites HEAD with that remote's default branch (measured, git 2.48.1).
    out.append('git -C "$M" symbolic-ref HEAD %s' % q("refs/heads/" + branches[0]))
    return "\n".join(out) + "\n"


def origin_branch_fetch_step(branch, mirror):
    net = "git fetch -q origin %s" % q(branch)
    if not mirror:
        return net
    return ("if [ -d %s ] && git -C %s rev-parse --verify --quiet %s >/dev/null 2>&1\n        then git fetch -q %s %s\n        else %s\n        fi"
            % (q(mirror), q(mirror), q("refs/heads/" + branch), q(mirror), q("+refs/heads/%s:refs/remotes/origin/%s" % (branch, branch)), net))


def _forks_in():
    return [tuple(line.split()) for line in sys.stdin.read().splitlines() if len(line.split()) == 3]


def main(argv):
    verb, a = (argv[0] if argv else ""), argv[1:]
    branches = mirror_branches()
    verbs = {
        "remotes": ((0, 0), lambda: "".join("%-8s %s\n" % r for r in REMOTES)),
        "mirror-branches": ((0, 0), lambda: " ".join(branches) + "\n"),
        "mirror-refresh-script": ((1, 1), lambda: mirror_refresh_script(a[0], branches)),
        "wiring-script": ((2, 5), lambda: wiring_script(a[0], a[1], _forks_in(), branches, *a[2:5])),
        "wiring-check-script": ((2, 3), lambda: wiring_check_script(a[0], a[1], _forks_in(), branches, *a[2:3])),
        "gitwebkit-setup-script": ((1, 1), lambda: gitwebkit_setup_script(a[0], _forks_in())),
        "hook-levels": ((0, 0), lambda: hook_levels(_forks_in())),
    }
    if verb not in verbs or not verbs[verb][0][0] <= len(a) <= verbs[verb][0][1]:
        sys.stderr.write("usage: python3 -m wk.git %s\n" % "|".join(sorted(verbs)))
        return 2
    sys.stdout.write(verbs[verb][1]())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
