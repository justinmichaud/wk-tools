"""The mirror: a server's one bare WebKit repository, which every workspace's checkout borrows objects from.

Its `config` file is wk's whole: written, never edited, so a wrong setting is found by comparing two strings."""

from wk.core.act import must
from wk.webkit import PROJECT

# gc.auto 0: a repack would delete objects the `--shared` checkouts borrow. No tags: every fetch would re-negotiate
# each tag upstream has. Two fetches writing the commit graph at once collide on its lock.
CONFIG = """[core]
\trepositoryformatversion = 0
\tbare = true
[gc]
\tauto = 0
\twriteCommitGraph = false
[fetch]
\twriteCommitGraph = false
[remote "origin"]
\turl = %s
\ttagOpt = --no-tags
%s""" % (PROJECT["upstream"], "".join("\tfetch = +refs/heads/%s:refs/heads/%s\n" % (b, b) for b in PROJECT["branches"]))


def where(home):
    return "%s/git/%s" % (home, PROJECT["mirror"])


def configure(m, home):
    """Make the mirror exist with exactly wk's configuration. True when that changed anything."""
    path, changed = where(home), False
    if not m.exists(path + "/HEAD"):
        must(m, ["git", "init", "--bare", "--quiet", path], "git must be installed on %s" % m.name)
        changed = True
    if m.read(path + "/config") != CONFIG:
        m.write(path + "/config", CONFIG)
        changed = True
    return changed


def fetched(m, home):
    """Whether the mirror holds every branch a checkout asks it for."""
    return all(m.run(["git", "-C", where(home), "rev-parse", "--verify", "--quiet", "refs/heads/" + b]).ok for b in PROJECT["branches"])


def fetch(m, home):
    path = where(home)
    must(m, ["git", "-C", path, "fetch", "origin"], "check that %s reaches github.com, then `wk sync` again" % m.name)
    # `git fetch` in a bare repository overwrites HEAD with the remote's default branch (measured, git 2.48.1).
    must(m, ["git", "-C", path, "symbolic-ref", "HEAD", "refs/heads/" + PROJECT["branches"][0]], "`wk sync` again")


def setup(m, home):
    """Setup's stage: the mirror exists, is configured and has been fetched once. True when that changed anything."""
    changed = configure(m, home)
    if not fetched(m, home):
        fetch(m, home)
        changed = True
    return changed
