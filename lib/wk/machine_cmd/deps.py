"""What a build machine needs (remote/deps.sh's table) against what it has (remote/probe.sh's answer, run there)."""

import hashlib
import os
import re
import sys

from wk import secrets
from wk.kv import kv
from wk.machine import Local

PACKAGES = {("debian", "ninja"): "ninja-build", ("fedora", "ninja"): "ninja-build"}
INSTALL = {"debian": "sudo apt-get update && sudo apt-get install -y %s", "fedora": "sudo dnf install -y %s",
           "arch": "sudo pacman -S --needed %s", "suse": "sudo zypper install -y %s"}


def digest(value):
    return hashlib.sha256((value + "\n").encode()).hexdigest()[:16]


ON_BOX = ("wk_remote_deps", "wk_remote_family", "wk_remote_build_env_vars")


def code_lines(text):
    out = []
    for line in text.splitlines():
        line = re.sub(r"\s{2,}#\s.*$", "", line).strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def shell_function(text, name):
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(name + "() {"))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip() == "}")
    return "\n".join(lines[start:end + 1])


def inputs_hash(root):
    """What provisions and probes a machine, recorded in its ~/.wk-remote: a comment or a reindent changes nothing."""
    read = lambda f: open(os.path.join(str(root), "remote", f)).read()
    deps_text = read("deps.sh")
    parts = [read("provision.sh"), read("probe.sh")] + [shell_function(deps_text, fn) for fn in ON_BOX]
    return hashlib.sha256("\n".join("\n".join(code_lines(p)) for p in parts).encode()).hexdigest()[:16]


def deps(root):
    text = open(os.path.join(str(root), "remote", "deps.sh")).read()
    body = text.split("cat <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    return [tuple(line.split(None, 2)) for line in body.splitlines() if line.strip()]


def package(tool, family):
    return PACKAGES.get((family, tool), tool)


def install_cmd(family, pkgs):
    return INSTALL[family] % " ".join(pkgs) if pkgs and family in INSTALL else None



class Deps:
    """What is wrong with a build machine, from its probe: (state, what, remedy) with state ok | required | wanted | note."""

    def __init__(self, root, env=None, here=None):
        self.root, self.env, self.here = str(root), os.environ if env is None else env, here or Local()

    def want_identity(self, key):
        r = self.here.run(["git", "config", "--file", os.path.join(self.root, "dotfiles", "gitconfig"), "--get", "user." + key])
        return r.out.strip() if r.ok else ""

    def remote_rows(self):
        return [r for r in secrets.agent_secrets() if "remote" in (r[5:6] or [""])[0].split(",")]

    def stored_digest(self, name):
        value = secrets.first_line(secrets.Secrets(self.root, self.env, self.here).cred_read(name) or "")
        return digest(value) if value else ""

    def findings(self, text):
        p, out = kv(text), []
        family, pkgs = p.get("family", ""), []
        for tool, need, why in deps(self.root):
            if p.get("tool." + tool):
                out.append(("ok", "%s (%s)" % (tool, p["tool." + tool]), ""))
                continue
            pkgs.append(package(tool, family))
            out.append((need, "%s -- %s" % (tool, why), ""))
        if pkgs:
            cmd = install_cmd(family, pkgs)
            if cmd:
                out.append(("note", "as root on %s:" % p.get("host", "?"), cmd))
            else:
                out.append(("note", "%s runs %s, whose package manager this does not know" % (p.get("host", "?"), p.get("os", "?")),
                            "install by hand: " + " ".join(pkgs)))
        # Against the identity dotfiles/gitconfig declares, not merely "is it set": one machine had `user.name = no`.
        for key in ("name", "email"):
            want, have = self.want_identity(key), p.get("git." + key, "")
            if want and have == want:
                out.append(("ok", "git user.%s = %s" % (key, have), ""))
            elif not have:
                out.append(("wanted", "git user.%s is not set there -- a commit would be refused" % key,
                            "wk machine setup <name>  (it writes the include)"))
            else:
                out.append(("wanted", "git user.%s there is '%s', not this repository's '%s' -- every commit made there carries it"
                            % (key, have, want), "wk machine setup <name>  (it removes the shadowing value)"))
        if (p.get("git.fsmonitor"), p.get("git.manyfiles")) == ("true", "true"):
            out.append(("ok", "git speed settings (fsmonitor, manyFiles)", ""))
        else:
            out.append(("wanted", "git there is unconfigured for a big checkout, so `git status` in WebKit walks the whole tree"
                        " -- which an editor over ssh asks on every keystroke",
                        "wk machine setup <name>  (dotfiles/gitconfig, through the include)"))
        for k, v in p.items():
            if k.startswith("env."):
                out.append(("note", "%s is set to '%s' in a login shell there" % (k[4:], v),
                            "wk's build sets its own %s and ignores that one (lib/wk/buildconf.py)" % k[4:]))
        for row in self.remote_rows():
            out.append(self.cred_finding(row[0], p.get("cred." + row[2], ""), self.stored_digest(row[0])))
        return out

    @staticmethod
    def cred_finding(name, there, here):
        """The copy on the machine against the one stored here, by digest: no value crosses back."""
        fix = "wk machine setup <name>  (it %s the copy)"
        if here and here == there:
            return ("ok", "%s credential: the copy there is the one stored here" % name, "")
        if there == "?":
            return ("note", "%s credential is there, and the machine has no sha256sum to compare it with" % name, fix % "rewrites")
        if here and not there:
            return ("wanted", "%s credential is not on the machine, so an agent there asks for /login" % name, fix % "writes")
        if here:
            return ("wanted", "%s credential there is not the one stored here: rotated since it was written" % name, fix % "rewrites")
        if there:
            return ("wanted", "%s credential is on the machine and no longer stored here" % name, fix % "removes")
        return ("note", "%s credential: none here and none there" % name, "wk key set %s, then wk machine setup <name>" % name)


def findings(root, probe_text, env=None, here=None):
    return Deps(root, env, here).findings(probe_text)


def findings_text(rows):
    return "".join("\t".join(r) + "\n" for r in rows)


def probe(t, root):
    """remote/deps.sh then remote/probe.sh, into a shell there, so the far side needs no wk-tools of its own; "" when it did not answer."""
    script = "".join(open(os.path.join(str(root), "remote", f)).read() for f in ("deps.sh", "probe.sh"))
    r = t._far().run(["bash", "-s"], input=script, timeout=t.probe_seconds)
    return r.out if r.ok else ""


def stale(t, root):
    r = t._sh('cat "$HOME/.wk-remote" 2>/dev/null')
    marker = kv(r.out) if r.ok else {}
    if not r.ok or not r.out.strip():
        return "no ~/.wk-remote there, so nothing has provisioned it"
    if not marker.get("inputs"):
        return "provisioned before this record existed"
    if marker["inputs"] == inputs_hash(root):
        return None
    return "remote/provision.sh or remote/deps.sh has changed since it ran"


def said(r):
    sys.stderr.write(r.out + r.err)
    return r
