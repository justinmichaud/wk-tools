"""`wk machine setup|rm` of a build machine or a peer: the conf, the probe, the push, remote/provision.sh, the
credentials, and the one question about what a setup removes."""

import os
import re
import shlex
import sys

from wk import act, secrets, sudo, targets
from wk.act import die, info, log, warn
from wk.kv import kv
from wk.machine_cmd.deps import Deps, inputs_hash, probe, said

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


class BuildMachines:
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
        p = kv(text)
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

    def rm_target(self, name, conf, path):
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
