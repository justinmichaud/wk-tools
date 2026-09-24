"""`wk machine`: set up and remove the build machines and peers in machines/, list the fleet, and probe for a
machine or sweep for every device. What a build machine needs is remote/deps.sh's table; remote/probe.sh
and remote/provision.sh run on the machine."""

import hashlib
import json
import os
import re
import shlex
import sys

from wk import act, fleet, reach, secrets, shell, sudo, targets
from wk.act import die, info, log, warn
from wk.machine import Local, Ssh
from wk.workspace import require_name

PACKAGES = {("debian", "ninja"): "ninja-build", ("fedora", "ninja"): "ninja-build"}
INSTALL = {"debian": "sudo apt-get update && sudo apt-get install -y %s", "fedora": "sudo dnf install -y %s",
           "arch": "sudo pacman -S --needed %s", "suse": "sudo zypper install -y %s"}
CONFS = {
    "build": "# %(name)s -- a shared build machine, reached through the ssh entry of the same name.\n"
             "# Written by 'wk machine setup'; commit it to give every device this machine.\n"
             "KIND=build\nWK_TARGET_KIND=remote\n",
    "peer": "# %(name)s -- a workstation with its own wk-tools, asked for its workspaces rather than driven.\n"
            "# Written by 'wk machine setup'; commit it to give every device this machine.\n"
            "KIND=peer\nWK_TARGET_KIND=remote\nWK_REMOTE_PEER=1\nWK_REMOTE_TOOLS=Development/wk-tools\n",
}
MOTD = "cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null"
OLD_TOOLS = 'for d in "$HOME"/Development/wk-tools "$HOME"/wk-tools; do [ -d "$d" ] && echo "$d"; done; true'
# Every rc file that sources this machine's wk-tools loses the line, and the marker goes.
DEPROVISION = r'''for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    [ -f "$rc" ] && grep -qF "$1" "$rc" || continue
    tmp=$(mktemp)
    grep -vF -e "$1" -e '# wk-tools shared shell configuration' "$rc" > "$tmp" || true
    mv "$tmp" "$rc"
    echo "$rc"
done
rm -f "$HOME/.wk-remote"'''


def digest(value):
    return hashlib.sha256((value + "\n").encode()).hexdigest()[:16]


ON_BOX = ("wk_remote_deps", "wk_remote_family", "wk_remote_build_env_vars")   # deps.sh's part that runs on the machine


def code_lines(text):
    """Shell as what it runs: no blank or comment lines, no indentation, no trailing `  # ...` note."""
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
    """remote/deps.sh's table as (tool, required|wanted, what it is for)."""
    text = open(os.path.join(str(root), "remote", "deps.sh")).read()
    body = text.split("cat <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    return [tuple(line.split(None, 2)) for line in body.splitlines() if line.strip()]


def package(tool, family):
    return PACKAGES.get((family, tool), tool)


def install_cmd(family, pkgs):
    """The one root command for a family this knows; None for one it does not, or nothing to install."""
    return INSTALL[family] % " ".join(pkgs) if pkgs and family in INSTALL else None


def probe_fields(text):
    out = {}
    for line in text.splitlines():
        k, eq, v = line.partition("=")
        if eq:
            out[k] = v
    return out


class Deps:
    """What is wrong with a build machine, from its probe: (state, what, remedy) with state ok | required | wanted | note."""

    def __init__(self, root, env=None, here=None):
        self.root, self.env, self.here = str(root), os.environ if env is None else env, here or Local()

    def want_identity(self, key):
        r = self.here.run(["git", "config", "--file", os.path.join(self.root, "dotfiles", "gitconfig"), "--get", "user." + key])
        return r.out.strip() if r.ok else ""

    def remote_rows(self):
        return [r for r in shell.agent_secrets(self.root, self.here) if "remote" in (r[5:6] or [""])[0].split(",")]

    def stored_digest(self, name):
        value = secrets.first_line(secrets.Secrets(self.root, self.env, self.here).cred_read(name) or "")
        return digest(value) if value else ""

    def findings(self, text):
        p, out = probe_fields(text), []
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
    """Why the machine's provisioning predates this tree's remote/provision.sh and remote/deps.sh, or None."""
    r = t._sh('cat "$HOME/.wk-remote" 2>/dev/null')
    marker = probe_fields(r.out) if r.ok else {}
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


class Machines:
    def __init__(self, root, env=None, here=None, far=None):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.here = here or Local()
        self.far = far
        self.fleet = fleet.Fleet(self.root, self.env)
        self.reach = reach.Reach(self.here, self.env, self.fleet)

    def conf(self, name):
        try:
            return self.fleet.load(name)
        except fleet.ConfError as e:
            die(str(e))

    def target(self, name, conf):
        env = dict(self.env)
        env.update({k: v for k, v in conf.items() if k != "KIND"})
        t = targets.Remote(name, self.root, env, self.here)
        if self.far is not None:
            t.machine = self.far
        return t

    def rel(self, path):
        return os.path.relpath(path, self.root) if path.startswith(self.root + os.sep) else path

    # -- setup

    def setup(self, name, kind=None):
        require_name(name)
        conf, path = self.conf(name), self.fleet.conf_path(name)
        new = conf is None
        if new:
            if not kind:
                die("'%s' has no conf yet (%s), so say what it is:\n"
                    "        wk machine setup %s --kind build   someone else's machine this one builds on\n"
                    "        wk machine setup %s --kind peer    a workstation with its own wk-tools" % (name, self.rel(path), name, name))
            if kind not in fleet.TARGET_KINDS:
                die("--kind %s: 'wk machine setup' makes a build machine or a peer (--kind build | peer)" % kind)
            conf = fleet.parse_text(CONFS[kind] % {"name": name})
        elif kind and kind != conf["KIND"]:
            die("%s says KIND=%s, and --kind %s disagrees. The conf is the answer:\n"
                "    edit it, or drop --kind." % (self.rel(self.fleet.path(name)), conf["KIND"], kind))
        if conf["KIND"] not in fleet.TARGET_KINDS:
            die("'%s' is a %s (%s): 'wk machine setup' sets up a build machine or a peer"
                % (name, conf["KIND"], self.rel(self.fleet.path(name))))
        t = self.target(name, conf)
        ok, why = t.answers()
        if not ok:
            die("cannot ssh to '%s' non-interactively: %s\n"
                "    A machine is named after an ssh destination that already works, keys and\n"
                "    ProxyJump included:  ssh -o BatchMode=yes %s true" % (t.label(), why, t.label()))
        if conf["KIND"] == "peer":
            return self.setup_peer(name, t, path, new)
        return self.setup_build(name, t, path, new)

    def write_conf(self, name, path, new, kind):
        if not new:
            return
        self.here.mkdir(os.path.dirname(path))
        self.here.write(path, CONFS[kind] % {"name": name})
        info("wrote %s" % self.rel(path))
        log("  a tracked file: 'git add %s' and commit it to give every device this machine." % self.rel(path))

    def setup_peer(self, name, t, path, new):
        if not t.has_wk():
            die("%s answers, but has no wk at %s. A peer is a workstation with its own\n"
                "    checkout; on it:  git clone https://github.com/justinmichaud/wk-tools ~/Development/wk-tools\n"
                "                    cd ~/Development/wk-tools && ./setup" % (t.label(), t.tools("") + "/wk"))
        act.nothing_to_ask()
        self.write_conf(name, path, new, "peer")
        info("%s is a peer: its workspaces are its own, and it is asked for them" % name)
        log("  from here:  wk new <ws> --target %s" % name)
        return 0

    def setup_build(self, name, t, path, new):
        host = t.label()
        info("probing %s" % host)
        text = probe(t, self.root)
        if not text:
            die("cannot reach %s" % host)
        p = probe_fields(text)
        log("  %s: %s, %s, %s cores" % (p.get("host", "?"), p.get("os", "?"), p.get("arch", "?"), p.get("cores", "?")))
        log("  root: %s   jobs: derived per build from what is free" % t.root_there())
        stop = False
        for state, what, remedy in Deps(self.root, self.env, self.here).findings(text):
            if state == "ok":
                log("  " + what)
            elif state in ("required", "wanted"):
                warn(what)
                stop = stop or state == "required"
            else:
                log("  " + what + ("\n      " + remedy if remedy else ""))
        if stop:
            die("%s is missing something a build cannot start without.\n"
                "    Run the root command above (or send it to the machine's administrators),\n"
                "    then re-run 'wk machine setup %s'. Nothing has been changed." % (host, name))
        ref = t.reference()
        if ref:
            info("this machine publishes a WebKit repository: %s" % ref)
            log("  workspaces will be cloned from it (hardlinked objects), not from a mirror of ours")
        else:
            log("  no shared WebKit repository advertised -- a mirror under the root will be kept instead")
        if re.search(r"home *(dir|directory)?.*shared", t._sh(MOTD).out, re.I):
            warn("this machine says its home directory is shared with other boxes.\n"
                 "  A workspace name is then the same directory on all of them, and one build\n"
                 "  tree cannot hold two architectures. Give each box its own WK_REMOTE_ROOT\n"
                 "  (in its conf) if you use more than one.")
        rubble = self.rubble(t, ref)
        clean = self.ask_rubble(host, rubble)
        self.write_conf(name, path, new, "build")
        tools = t.tools("")
        info("pushing wk-tools to %s" % tools)
        if not t.sync_tools(""):
            die("nothing was changed on %s: it has no wk-tools to provision from" % host)
        q = ["WK_REMOTE_TARGET=" + name, "WK_REMOTE_ROOT=" + t.root_there(), "WK_REMOTE_REFERENCE=" + ref,
             "WK_REMOTE_INPUTS=" + inputs_hash(self.root)]
        if not said(t._far().act_run(["env"] + q + ["bash", tools + "/remote/provision.sh"])).ok:
            die("remote/provision.sh failed on %s; what it said is above. Re-run 'wk machine setup %s' once it is fixed." % (host, name))
        if t._far().act_run(["env", "WK_ROOT=" + tools, "WK_CLAUDE_REMOTE=1", "bash", "-c",
                             'set -euo pipefail; . "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/claude/install.sh"']).ok:
            info("linked ~/.claude on %s (settings-host.json + Bash(wk *))" % host)
        self.credentials(t, host)
        rc, verdict = sudo.Sudo(t._far(), self.env).verdict()
        if rc != 0:
            warn(verdict)
            act.barrier("sudo on '%s' grants root without a password, and this machine\n"
                        "    runs unattended builds. 'wk key sudo setup --target %s' fixes it in one\n"
                        "    file that no sysadmin has to approve." % (name, name))
        self.install_dependencies(t, host, name, ref)
        for d, _size, _why in rubble if clean else ():
            if t._far().act_run(["rm", "-rf", d]).ok:
                info("removed %s" % d)
        info("%s is ready" % name)
        log("  from here:   wk new <ws> --target %s" % name)
        log("  on the box:  ssh %s, then wk ls / wk build <ws> <config>" % host)
        return 0

    def size(self, t, path):
        r = t._sh("du -sh %s 2>/dev/null | cut -f1" % shlex.quote(path))
        return r.out.strip() or "?"

    def rubble(self, t, ref):
        """(path, size, why) for what a setup would remove: an older wk-tools checkout, and the mirror a shared repository replaces."""
        tools, out = t.tools(""), []
        for d in t._sh(OLD_TOOLS).out.split():
            if d != tools:
                out.append((d, self.size(t, d), "an older wk-tools checkout; the live one is %s" % tools))
        mirror = t.root_there() + "/mirror"
        if ref and t._far().isdir(mirror):
            out.append((mirror, self.size(t, mirror), "redundant now that %s is used instead; a workspace cloned from it"
                        " borrows its objects (--shared) and would break" % ref))
        return out

    def ask_rubble(self, host, rubble):
        """The one question a setup asks; what it declines is left in place and named."""
        if not rubble:
            act.nothing_to_ask()
            return False
        for d, size, why in rubble:
            log("  %s (%s) -- %s" % (d, size, why))
        if act.confirm("remove %s from %s?" % (", ".join(d for d, _s, _w in rubble), host)):
            return True
        log("  left in place: %s" % ", ".join(d for d, _s, _w in rubble))
        act.nothing_to_ask()
        return False

    def credentials(self, t, host):
        """Copies of the rows delivered to a `remote`, refreshed on every setup and removed when this store has none;
        on stdin, never in `ps`. Not the claude.ai login, whose refresh token would rotate out from under every holder."""
        sec = secrets.Secrets(self.root, self.env, self.here)
        for row in Deps(self.root, self.env, self.here).remote_rows():
            name, home_path = row[0], row[2]
            value = secrets.first_line(sec.cred_read(name) or "")
            dest = '"$HOME"/%s' % shlex.quote(home_path)
            if value:
                if t._far().act_run(["sh", "-c", "umask 077 && cat > %s" % dest], input=value + "\n").ok:
                    info("wrote the %s credential to ~/%s on %s (0600)" % (name, home_path, host))
                else:
                    warn("could not write the %s credential to ~/%s on %s" % (name, home_path, host))
            else:
                t._far().act_run(["sh", "-c", "rm -f %s" % dest])
                info("no %s credential here, so none was placed on %s ('wk key set %s' stores one)" % (name, host, name))

    def install_dependencies(self, t, host, name, ref):
        """Tools/*/install-dependencies prompts for sudo, so it runs only with a terminal."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            warn("no terminal here -- skipping Tools/*/install-dependencies on %s.\n"
                 "  it needs an interactive sudo prompt; re-run 'wk machine setup %s' from a terminal." % (host, name))
            return
        src = ref
        if not src:
            t._mirror_update(t.root_there())
            src = t.mirror_dir()
        dirs = "Tools/gtk" + (" Tools/wpe" if self.env.get("WK_TARGET_WPE") else "")
        script = ('set -e; tmp=$(mktemp -d); trap \'rm -rf "$tmp"\' 0\n'
                  'git -C %s archive main -- %s | tar -x -C "$tmp"\n'
                  'for d in %s; do "$tmp/$d/install-dependencies"; done' % (shlex.quote(src), dirs, dirs))
        if act.dry_run():
            sys.stderr.write("would run on %s: %s\n" % (host, script))
            return
        if t._far().run_tty(["sh", "-c", script]).ok:
            info("ran Tools/*/install-dependencies on %s (%s)" % (host, dirs))
        else:
            warn("Tools/*/install-dependencies failed on %s -- re-run 'wk machine setup %s' once it's fixed" % (host, name))

    # -- rm

    def rm(self, name):
        require_name(name)
        conf = self.conf(name)
        if conf is None:
            die("no conf for '%s' (%s) -- nothing to remove" % (name, self.rel(self.fleet.conf_path(name))))
        path = self.fleet.path(name)
        if conf["KIND"] not in fleet.TARGET_KINDS:
            die("'%s' is a %s (%s): 'wk machine rm' removes a build machine or a peer" % (name, conf["KIND"], self.rel(path)))
        t = self.target(name, conf)
        if t.peer:
            if not act.confirm("forget the peer '%s' (%s)? Its workspaces stay its own." % (name, self.rel(path))):
                die("aborted -- nothing was changed")
            self.here.remove(path)
            info("removed %s" % self.rel(path))
            return 0
        ok, why = t.answers()
        if not ok:
            warn("cannot reach %s (%s) -- the machine keeps whatever it has." % (t.label(), why))
            log("  (re-run this when it is reachable to deprovision it properly)")
            if not act.confirm("remove the local conf %s anyway?" % self.rel(path)):
                die("aborted -- nothing was changed")
            self.here.remove(path)
            info("removed %s" % self.rel(path))
            return 0
        live = [n for n, _s in t.list()]
        if live:
            die("workspaces still live on '%s': %s\n"
                "    Remove them first (wk rm <name>) -- that deletes their remote checkouts,\n"
                "    which needs the machine this command was about to forget." % (name, " ".join(live)))
        root, tools = t.root_there(), t.tools("")
        there = t._far().isdir(root)
        what = "the wk-tools lines in its shell rc files, ~/.wk-remote" + (", and %s (%s)" % (root, self.size(t, root)) if there else "")
        if not act.confirm("deprovision %s: remove %s?" % (t.label(), what)):
            die("aborted -- nothing was changed")
        r = t._far().act_run(["sh", "-c", DEPROVISION, "sh", tools + "/shell/bashrc"])
        for rc in r.out.split():
            info("removed the wk-tools line from %s" % rc)
        if not r.ok:
            die("could not deprovision %s: %s" % (t.label(), targets.ssh_last_word(r)))
        info("removed the marker from %s" % t.label())
        if there and t._far().act_run(["rm", "-rf", root]).ok:
            info("removed %s" % root)
        info("%s is deprovisioned" % t.label())
        log("  it is still a machine here, because the registry names it:")
        log("      git rm %s && git commit" % self.rel(path))
        log("  that forgets it on every device. 'wk machine setup %s' brings it back." % name)
        return 0

    # -- ls and probe

    def ls(self, as_json=False, out=None):
        out = out or sys.stdout
        rows = []
        for name in self.fleet.names():
            c = self.conf(name)
            reached = "; ".join("%s %s" % (n, self.reach.tailnet(n) or "not a node") for n in self.reach.names(name))
            rows.append({"name": name, "kind": c["KIND"], "tailnet": reached, "note": c.get("NODE_NOTE") or c.get("BR_NOTE", ""),
                         "conf": self.rel(self.fleet.path(name))})
        if as_json:
            out.write(json.dumps({"machines": rows}) + "\n")
            return 0
        for r in rows:
            out.write("%-26s %-7s %s\n" % (r["name"], r["kind"], r["tailnet"]))
            if r["note"]:
                out.write("%-26s %-7s %s\n" % ("", "", r["note"]))
        return 0

    def answers(self, name, conf):
        """(True, "") or (False, why): a build machine or peer by its one probe, anything else by an ssh `true`, and
        a node the tailnet already reports down by that alone."""
        if conf["KIND"] in fleet.TARGET_KINDS:
            return self.target(name, conf).answers()
        dest = conf.get("NODE_SSH") or conf.get("BR_SSH") or name
        down = self.reach.offline(dest)
        if down:
            return False, down
        r = Ssh(dest, timeout=reach.ssh_timeout(self.env), via=self.here).run(["true"], timeout=reach.ssh_timeout(self.env) + 5)
        return (True, "") if r.ok else (False, targets.ssh_last_word(r))

    def probe_one(self, name, survey, as_json=False, out=None):
        out = out or sys.stdout
        conf = self.conf(name)
        if conf is None:
            die("'%s' is not a machine here. machines:\n%s" % (name, "".join("      %s\n" % n for n in self.fleet.names())))
        ok, why = self.answers(name, conf)
        doc = {"machine": name, "kind": conf["KIND"], "answers": ok, "why": why,
               "tailnet": {n: self.reach.tailnet(n) for n in self.reach.names(name)},
               "ssh": self.reach.ssh_path(conf.get("NODE_SSH") or conf.get("BR_SSH") or name)}
        mac = conf.get("NODE_MAC", "").lower()
        if not ok and mac:
            doc["sweep"] = survey.run(mac)
        if as_json:
            out.write(json.dumps(doc) + "\n")
        else:
            for n, where in doc["tailnet"].items():
                out.write("  %-16s %s\n" % ("tailnet " + n, where or "not a node"))
            out.write("  %-16s %s\n" % ("ssh", doc["ssh"] or "no route in the ssh config"))
            out.write("  %-16s %s\n" % ("answers", "yes" if ok else "no -- unreachable: %s" % why))
            if "sweep" in doc:
                render_survey(doc["sweep"], out)
                if doc["sweep"]["seen"]:
                    info("%s is up, at the address above (%s)" % (name, mac))
                else:
                    warn("%s (%s) is on none of the %d segment(s) that could be swept.\n"
                         "  Either it is powered off, or it is on a segment nothing here can see. A board is\n"
                         "  found by its hardware address, so this is a fact about the wire, not a name."
                         % (name, mac, doc["sweep"]["swept"]))
        return 0 if ok or doc.get("sweep", {}).get("seen") else 1

    def sweep(self, survey, as_json=False, out=None):
        out = out or sys.stdout
        doc = dict(survey.run(), want=None, want_mac=None)
        if as_json:
            out.write(json.dumps({k: v if k != "hits" else [{h: x[h] for h in reach.HIT_KEYS} for x in v]
                                  for k, v in doc.items()}) + "\n")
            return 0
        render_survey(doc, out)
        info("%d device(s) on %d segment(s)%s" % (doc["seen"], doc["swept"], ", %d unsweepable" % doc["blind"] if doc["blind"] else ""))
        return 0


def render_survey(doc, out):
    for v in doc["vantages"]:
        where = "as asked" if v["via"] == "given" else "from this machine" if v["vantage"] == "local" else "through " + v["via"]
        if not v["swept"]:
            warn("%s (%s): cannot be swept" % (v["segment"], where))
            if v["vantage"] == "local":
                log("  nmap is not installed here, and there is deliberately no second sweeper. './setup' installs it.")
            else:
                log("  %s is unreachable, or has no nmap. Every device on this segment is reached through it,\n"
                    "  so none of them is at fault for being absent here.  wk bridge status %s" % (v["vantage"], v["via"]))
            continue
        info("%s (%s)%s" % (v["segment"], where, "" if v["found"] else ": nothing answered"))
        for h in (x for x in doc["hits"] if x["ip"] in v["found"]):
            render_hit(h, out)


def render_hit(h, out):
    def row(k, v):
        out.write("    %-16s %s\n" % (k, v))
    out.write("  %-15s %-17s %-9s %s\n" % (h["ip"], h["mac"], h["state"], h["machine"] or h["bridge"] or h["hostname"] or h["vendor"]))
    if h["machine"]:
        row("fleet machine", "%s -- machines/%s.conf%s" % (h["machine"], h["machine"], ", " + h["vendor"] if h["vendor"] else ""))
    elif h["vendor"]:
        row("hardware", h["vendor"])
    if h["_lease"]:
        row("reserved as", h["_lease"])
    if h["bridge"]:
        row("tailnet bridge", "%s -- machines/%s.conf" % (h["bridge"], h["bridge"]))
    if h["machine"] or h["lease"] or h["bridge"] or h["hostname"]:
        row("on the tailnet", "as " + h["tailnet_peer"] if h["tailnet_peer"] else
            "not as any name this repo knows -- this address is how it is reached")
    if h["hostname"]:
        row("answers ssh as", h["hostname"] + (" (as %s)" % h["_account"] if h["_account"] else "") +
            ("  (%s)" % h["uname"] if h["uname"] else ""))
        if h["wk_image_id"]:
            row("running", h["wk_image_id"])
            row("  profile", (h["wk_profile"] or "not in its marker") + (", built by " + h["wk_builder"] if h["wk_builder"] else ""))
            row("  role", (h["wk_role"] or "not in its marker -- a card written before roles existed; rewrite it") +
                (" (the card carries the rescue marker)" if h["marker"] == "rescue" else ""))
        else:
            row("running", "no /etc/wk-image -- not a system wk wrote")
        if h["tailscale"] != "yes":
            row("tailscale", "not installed on it, so its name cannot be how it is reached")
