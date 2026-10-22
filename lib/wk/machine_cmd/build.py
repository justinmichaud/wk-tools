"""`wk machine setup|rm` of a build machine or a peer: the conf, the probe, the push, remote/provision.sh and the
credentials; an rm takes the rc lines, the marker and the root back out."""

import os
import re
import shlex
import sys

from wk import act, places, project, secrets, sudo
from wk.act import die, info, log, warn
from wk.kv import kv
from wk.machine_cmd.deps import inputs_hash, probe

CONFS = {
    "build": "# %(name)s -- a shared build machine, reached through the ssh entry of the same name.\n"
             "# Written by 'wk machine setup'; commit it to give every device this machine.\n"
             "kind=build\ndriver=remote\n",
    "peer": "# %(name)s -- a workstation with its own wk-tools, asked for its workspaces rather than driven.\n"
            "# Written by 'wk machine setup'; commit it to give every device this machine.\n"
            "kind=peer\ndriver=remote\ntools=Development/wk-tools\n",
}
MOTD = "cat /etc/motd /etc/motd.d/* /run/motd.dynamic 2>/dev/null"
RC_FILES = (".zshrc", ".bashrc", ".bash_profile")
RC_MARK = "# wk-tools shared shell configuration"

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
        log("  from here:  wk new <ws> --on %s" % name)
        return 0

    def setup_build(self, name, t, path, new):
        """Every refusal comes before the first change; then each step in turn, the sudo verdict a barrier."""
        host = t.label()
        self.check_build(name, t, host)
        act.nothing_to_ask()
        self.write_conf(name, path, new, "build")
        self.provision(name, t, host)
        self.credentials(t, host)
        rc, verdict = sudo.Sudo(t._far(), self.env).verdict()
        if rc != 0:
            warn(verdict)
            act.barrier("sudo on '%s' grants root without a password, and this machine\n"
                        "    runs unattended builds. 'wk key sudo setup --on %s' fixes it in one\n"
                        "    file that no sysadmin has to approve." % (name, name))
        self.install_dependencies(t, host, name)
        info("%s is ready" % name)
        log("  from here:   wk new <ws> --on %s" % name)
        log("  on the box:  ssh %s, then wk ls / wk build <ws> <preset>" % host)
        return 0

    def check_build(self, name, t, host):
        """The probe and a shared home: a refusal changes nothing."""
        info("probing %s" % host)
        text = probe(t, self.root)
        if not text:
            die("cannot reach %s" % host)
        p = kv(text)
        log("  %s: %s, %s, %s cores" % (p.get("host", "?"), p.get("os", "?"), p.get("arch", "?"), p.get("cores", "?")))
        log("  root: %s   jobs: derived per build from what is free" % t.root_there())
        if re.search(r"home *(dir|directory)?.*shared", t._sh(MOTD).out, re.I) and not t.conf_root:
            die("this machine says its home directory is shared with other boxes. Each needs a root of its\n"
                "    own, or they share one set of workspaces, key dirs and locks, and one build tree\n"
                "    cannot hold two architectures. Add root=<a directory only this box uses> to\n"
                "    machines/%s.conf (and hostname=<its `hostname -s`> if that is not %s), then re-run.\n"
                "    Nothing has been changed." % (name, name))

    def provision(self, name, t, host):
        tools = t.tools("")
        info("pushing wk-tools to %s" % tools)
        if not t.sync_tools(""):
            die("nothing was changed on %s: it has no wk-tools to provision from" % host)
        q = ["WK_REMOTE_MACHINE=" + name, "WK_REMOTE_ROOT=" + t.root_there(),
             "WK_REMOTE_INPUTS=" + inputs_hash(self.root)]
        r = t._far().act_run(["env"] + q + ["bash", tools + "/remote/provision.sh"])
        places.show(r)
        if not r.ok:
            die("remote/provision.sh failed on %s; what it said is above. Re-run 'wk machine setup %s' once it is fixed." % (host, name))
        if t._far().act_run(["env", "WK_ROOT=" + tools, "WK_CLAUDE_REMOTE=1", "bash", "-c",
                             'set -euo pipefail; . "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/claude/install.sh"']).ok:
            info("linked ~/.claude on %s (settings-host.json + Bash(wk *))" % host)

    def size(self, t, path):
        r = t._sh("du -sh %s 2>/dev/null | cut -f1" % shlex.quote(path))
        return r.out.strip() or "?"

    def credentials(self, t, host):
        secrets.deliver(t._far(), "remote", secrets.Secrets(self.root, self.env, self.here).cred_read, host)

    def install_dependencies(self, t, host, name):
        """Tools/*/install-dependencies prompts for sudo, so it runs only with a terminal."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            warn("no terminal here -- skipping Tools/*/install-dependencies on %s.\n"
                 "  it needs an interactive sudo prompt; re-run 'wk machine setup %s' from a terminal." % (host, name))
            return
        t._mirror_update(t.root_there())
        src = t.mirror_dir()
        dirs = " ".join(project.get("DEPS_DIRS")[:2 if t.env.get("WK_REMOTE_WPE") else 1])
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

    def rm_machine(self, name, conf, path):
        t = self.driver(name, conf)
        if t.peer:
            return self.forget(path, "forget the peer '%s' (%s)? Its workspaces stay its own." % (name, self.rel(path)))
        ok, why = t.answers()
        if not ok:
            return self.forget_unreached(t.label(), why, "the machine keeps whatever it has", path)
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
        far, line = t._far(), tools + "/shell/bashrc"
        for rc in (t.home() + "/" + f for f in RC_FILES):
            try:
                text = far.read(rc)
            except OSError:
                continue
            if line in text:
                far.write(rc, "".join(l for l in text.splitlines(True) if line not in l and RC_MARK not in l))
                info("removed the wk-tools line from %s" % rc)
        far.remove(t.home() + "/.wk-remote")
        info("removed the marker from %s" % t.label())
        if there and t._far().act_run(["rm", "-rf", root]).ok:
            info("removed %s" % root)
        info("%s is deprovisioned" % t.label())
        return self.still_named(name, path)
