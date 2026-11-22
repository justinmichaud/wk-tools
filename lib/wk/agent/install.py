"""What a server holds for agent sessions: Claude Code and pi under <wk_home>/cache/agents, the build-tool shims
under <wk_home>/shims, and Claude's own configuration under ~/.claude."""

import json
import os
import re

from wk.core.act import die, warn
from wk.core.hooks import MISSING, OK, UNKNOWN

CLAUDE_VERSION = "2.1.308"
CLAUDE_INSTALLER = "https://claude.ai/install.sh"
PI_PACKAGE = "@earendil-works/pi-coding-agent"
NODE = (22, 19)     # the oldest node pi runs on
TOOLS = ("build-webkit", "build-jsc", "cmake", "ninja", "make", "xcodebuild", "run-webkit-tests", "run-javascriptcore-tests")
MOVE = 'test -x "$1/.local/share/claude/versions/$3" && mv "$1" "$2"'


def wk_home(inv, m):
    return inv.wk_home if m is inv.here else m.home + "/wk"


def claude_bin(home):
    return "%s/cache/agents/claude/%s/.local/share/claude/versions/%s" % (home, CLAUDE_VERSION, CLAUDE_VERSION)


def pi_bin(home):
    return home + "/cache/agents/pi/node_modules/.bin/pi"


def source(name):
    with open(os.path.join(os.path.dirname(__file__), name)) as f:
        return f.read()


def claude(m, home, server):
    """Claude Code at the pinned version: the official installer run with a staging HOME, moved into place whole."""
    if m.exists(claude_bin(home)):
        return False
    final = "%s/cache/agents/claude/%s" % (home, CLAUDE_VERSION)
    stage = home + "/cache/agents/claude/.stage"
    m.remove(stage)
    for argv, env in ((["mkdir", "-p", stage], None),
                      (["curl", "-sSL", "--fail-with-body", "-o", stage + "/install.sh", CLAUDE_INSTALLER], None),
                      (["bash", stage + "/install.sh", CLAUDE_VERSION], {"HOME": stage}),
                      (["sh", "-c", MOVE, "sh", stage, final, CLAUDE_VERSION], None)):
        r = m.act(argv, env=env)
        if not r.ok:
            die("could not install Claude Code %s on %s: `%s` failed (exit %d): %s\n    claude.ai must answer from %s; "
                "then: wk machine setup %s" % (CLAUDE_VERSION, server, argv[0], r.rc, r.err.strip(), server, server))
    return True


def node(m):
    """(ok | missing | unknown, why) for the node pi needs."""
    r = m.run(["node", "--version"])
    found = re.match(r"v(\d+)\.(\d+)\.", r.out)
    if not r.ok:
        return MISSING, "no node runs"
    if not found:
        return UNKNOWN, "node --version printed %r" % r.out.strip()
    if tuple(map(int, found.groups())) < NODE:
        return MISSING, "node %s is older than %d.%d" % ((r.out.strip(),) + NODE)
    return OK, r.out.strip()


def pi(m, home, server):
    if m.exists(pi_bin(home)):
        return False
    status, why = node(m)
    if status != OK:
        warn("pi is not installed on %s: it needs node >= %d.%d and npm, and %s" % ((server,) + NODE + (why,)))
        return False
    r = m.act(["npm", "install", "--ignore-scripts", "--prefix", home + "/cache/agents/pi", PI_PACKAGE])
    if not r.ok:
        die("could not install pi on %s: npm failed (exit %d): %s\n    registry.npmjs.org must answer from %s; "
            "then: wk machine setup %s" % (server, r.rc, r.err.strip(), server, server))
    return True


def settings():
    """Claude's settings.json. The deny rules cover a build tool named by its path, which no shim on PATH meets."""
    return json.dumps({
        "permissions": {"allow": ["Bash(*)", "WebSearch", "WebFetch", "Read(//tmp/**)", "Read(//private/tmp/**)"],
                        "deny": ["Bash(%s%s *)" % (prefix, tool) for tool in TOOLS for prefix in ("", "*/")],
                        "additionalDirectories": ["/tmp", "/private/tmp"]},
        "theme": "dark", "autoCompactEnabled": False, "precomputeCompactionEnabled": False, "agentPushNotifEnabled": True,
    }, indent=2) + "\n"


def shims(home):
    return {"%s/shims/%s" % (home, tool): source("shim.sh") for tool in TOOLS}


def config(m):
    return {m.home + "/.claude/CLAUDE.md": source("CLAUDE.md"), m.home + "/.claude/settings.json": settings()}


def stale(m, files):
    return [path for path in sorted(files) if m.read(path) != files[path]]


def put(m, files, mode="644"):
    """Write each file that differs; True when one did."""
    changed = stale(m, files)
    for path in changed:
        m.write(path, files[path], mode)
    return bool(changed)


def answered(text, checkout):
    """~/.claude.json with onboarding, the Remote Control dialog and trust in `checkout` answered, or None when it
    already says so. `text` is the file as it is, None when there is none."""
    doc = json.loads(text or "{}")
    before = json.dumps(doc, sort_keys=True)
    doc.update(hasCompletedOnboarding=True, remoteDialogSeen=True)
    doc.setdefault("projects", {}).setdefault(checkout, {})["hasTrustDialogAccepted"] = True
    return None if json.dumps(doc, sort_keys=True) == before else json.dumps(doc, indent=2) + "\n"
