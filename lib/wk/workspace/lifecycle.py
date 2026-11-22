"""A workspace's state, read from its directory, `.ready` and the driver's word; and the steps that make, fetch and
remove one. Nothing is stored: `.ready` is written last and removed first, so its absence is a half-made workspace."""

import dataclasses
import re

from wk.core.act import barrier, die, info, must
from wk.core.seams import ABSENT, BROKEN, CREATING, READY, Workspace
from wk.webkit import PROJECT
from wk.workspace import mirror
from wk.workspace.place import place

NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
NO_PUSH = "no-push://push-to-the-fork-remote"


def check(inv, name, on):
    """Refuse a name that cannot be a directory and a container, and a server command sent to the wrong server."""
    if not NAME.fullmatch(name):
        die("%s cannot name a workspace: use lowercase letters, digits and hyphens, starting with a letter or digit" % name)
    if inv.fleet.here.kind != "client" and on not in (None, inv.fleet.hostname):
        die("this is %s, not %s: type it on the client, which hands it to %s" % (inv.fleet.hostname, on, on))


def made(m, ws):
    return m.exists(ws.path + "/.ready")


def state(inv, m, ws):
    if not m.exists(ws.path):
        return ABSENT
    if made(m, ws):
        part = inv.driver(ws.driver, m).state(ws)
        return BROKEN if part == ABSENT else part
    # The bracket keeps the pattern from matching the shell that carries this very command over ssh.
    r = m.run(["pgrep", "-f", "wk new (.* )?[%s]%s( |$)" % (ws.name[0], ws.name[1:])])
    if r.rc > 1:
        barrier("cannot tell whether a `wk new %s` is running on %s: pgrep failed (exit %d): %s\n    pgrep must be installed there"
                % (ws.name, m.name, r.rc, r.err.strip()))
    return CREATING if r.ok else BROKEN


def at(inv, m, name, state):
    conf, home = place(inv, m)
    return Workspace(name, conf.name, "%s/ws/%s" % (home, name), conf.driver, state)


def find(inv, m, name):
    """The Workspace `name` on the machine `m`, whatever its state."""
    ws = at(inv, m, name, ABSENT)
    return dataclasses.replace(ws, state=state(inv, m, ws))


def listing(inv, m):
    return [find(inv, m, name) for name in m.listdir(place(inv, m)[1] + "/ws")]


def need(inv, name):
    """The made workspace `name` on this server; at worst its driver's part is not up."""
    ws = find(inv, inv.here, name)
    if ws.state == ABSENT:
        die("no workspace named %s on %s: `wk ls` lists them, `wk new %s` makes it" % (name, ws.server, name))
    if not made(inv.here, ws):
        die("workspace %s on %s is not made (%s): wait for its `wk new`, or `wk new %s` remakes a broken one"
            % (name, ws.server, ws.state, name))
    return ws


def ready(inv, name):
    """The ready workspace `name` on this server: what a build, a run and an agent session start from."""
    ws = need(inv, name)
    if ws.state != READY:
        barrier("workspace %s on %s is %s, not ready\n    `wk enter %s`, typed on the client, starts its container" % (name, ws.server, ws.state, name))
    return ws


def wipe(inv, m, ws):
    m.remove(ws.path + "/.ready")
    inv.driver(ws.driver, m).destroy(ws)
    m.remove(ws.path)


def make(inv, name):
    """Converge this server on a ready workspace `name`. A half-made one is wiped and remade."""
    m, home = inv.here, inv.wk_home
    inv.fleet.server(inv.fleet.hostname)
    ws = at(inv, m, name, CREATING)
    store, src, again = mirror.where(home), ws.path + "/src", "`wk new %s` again remakes it" % name
    if made(m, ws):
        die("workspace %s is already on %s: `wk enter %s` uses it, `wk rm %s` removes it" % (name, ws.server, name, name))
    if not mirror.fetched(m, home):
        die("the mirror on %s lacks a branch of %s: `wk machine setup %s` makes it" % (ws.server, ", ".join(PROJECT["branches"]), ws.server))
    if m.exists(ws.path):
        info("wiping the half-made %s" % ws.path)
        wipe(inv, m, ws)
    info("cloning %s from the mirror: this takes minutes" % src)
    must(m, ["git", "clone", "--shared", "--no-tags", "-c", "fetch.writeCommitGraph=false", "-c", "gc.writeCommitGraph=false",
             store, src], again)
    # remote.origin.url stays GitHub's, which is where the PR tool reads the project from; fetches are rewritten to the mirror.
    for words in (["remote", "set-url", "origin", PROJECT["upstream"]], ["remote", "set-url", "--push", "origin", NO_PUSH],
                  ["config", "url.%s.insteadOf" % store, PROJECT["upstream"]], ["remote", "add", "fork", PROJECT["fork"]],
                  ["config", "remote.fork.tagOpt", "--no-tags"]):
        must(m, ["git", "-C", src] + words, again)
    must(m, ["mkdir", "-p", ws.path + "/build"], again)
    inv.driver(ws.driver, m).create(ws)
    m.write(ws.path + "/.ready", "")
    info("workspace %s is ready on %s: `wk enter %s`" % (name, ws.server, name))


def fetch(m, ws):
    must(m, ["git", "-C", ws.path + "/src", "fetch", "origin"], "`wk sync %s` again" % ws.name)
