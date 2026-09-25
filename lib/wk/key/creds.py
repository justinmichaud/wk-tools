"""One credential on this machine: its rule, its file, its verdict, and `wk key set`."""

import os
import sys

from wk import act
from wk.act import Refused, die, info, log, warn
from wk.key.common import LOGIN, cred_print, detail, verdict


class Creds:
    def forks(self):
        if self._forks is None:
            self._forks = self.sec.forks()
        return self._forks

    def fork_names(self):
        return [f[0] for f in self.forks()]

    def repos(self):
        return "".join(f[1] + " " for f in self.forks())

    def agent_rows(self):
        if self._rows is None:
            self._rows = self.sec.agent_secrets()
        return self._rows

    def _cc(self, *args, input=""):
        r = self.machine.run(["python3", os.path.join(self.root, "lib", "credcheck.py")] + list(args), input=input)
        sys.stderr.write(r.err)
        return r

    def settable(self):
        if self._names is None:
            self._names = [n for n in self._cc("names").out.split() if n != "deploy-key"]
        return self._names

    def rule(self, name, field):
        for line in self._cc("rule", name, "--repos", self.repos()).out.splitlines():
            k, _, v = line.partition("\t")
            if k == field:
                return v
        return ""

    def check_value(self, name, value, *extra):
        return self.sec.check_value(name, value, *extra)

    # -- one credential on this machine

    def path(self, name):
        return self.sec.cred_path(name)

    def _secretfile(self, verb, path, input=""):
        return self.machine.run(["python3", os.path.join(self.root, "lib", "secretfile.py"), verb, path], input=input)

    def present(self, name):
        return bool(self.sec.cred_stored(name))

    def fingerprint(self, name):
        p = self.path(name)
        return self._secretfile("fingerprint", p).out.strip() if p else ""

    def stored_verdict(self, name):
        return self.sec.cred_verdict(name)

    def verdict_text(self, name):
        """The `verdict` subverb: what an election on another workstation compares."""
        fp = self.fingerprint(name)
        return self.stored_verdict(name) + ("\n    fingerprint: %s" % fp if fp else "")

    def kind(self, name):
        return next((r[4] for r in self.agent_rows() if r[0] == name), "")

    def private_dir(self, d):
        """0700 asserted on every run: a directory made by hand is as open as its umask left it."""
        if self.sec.made_dir(d):
            self.machine.act_run(["chmod", "0700", d])
        else:
            self.sec.ensure_dir(d, "0700")

    def store(self, name, value):
        p = self.path(name)
        self.private_dir(os.path.dirname(p))
        r = self.machine.act_run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "write", p], input=value + "\n")
        sys.stderr.write(r.err)
        if not r.ok:
            return False
        return self.sec.publish_view("container")

    def clear(self, name):
        """A file row's login takes its config home (record, lock, backups) with it."""
        p = self.path(name)
        self.machine.act_run(["rm", "-f", p])
        if self.kind(name) == "file":
            d = os.path.dirname(p)
            self.machine.act_run(["rm", "-rf", d + "/.claude.json", d + "/.claude.json.lock", d + "/backups"])
        self.sec.publish_view("container")

    def cred_line(self, name, state, where):
        var = next((r[3] for r in self.agent_rows() if r[0] == name), "-")
        return "%-13s %-8s %s%s" % (name, state, where or "", "  ($%s in a workspace)" % var if var != "-" else "")

    def cred_report(self, name):
        log(self.cred_line(name, "stored", self.path(name)))
        return cred_print(name, self.stored_verdict(name))

    def deliver(self, name):
        """Only the credential injector, on the machine that runs the workspaces, ever reads these two."""
        if name == "github-pat":
            target = self.sec.machine_pat()
            if not self.sec.pat_deliver():
                warn("an injector on this machine did not take the read token, so a read from\n    the workspaces it serves answers "
                     "401 until it does. './setup' converges the\n    one in the podman machine (the vmtools stage on macOS, sdk "
                     "on Linux) and\n    'wk start <guest>' the one that serves the guests.")
        elif name == "bugzilla-api-key":
            target = self.sec.machine_bugzilla_key()
        else:
            return
        if not self.sec.switch_cred_converge(self.sec.machine_sock(), target, name):
            warn("the injector on this machine is still writing with the %s stored\n    before this one, so 'git-webkit pr' in a "
                 "workspace spends that: 'wk push off'\n    then 'wk push on' hands it the one stored here." % name)

    def have(self, cmd):
        return self.machine.run(["sh", "-c", "command -v %s >/dev/null" % cmd], input="").ok

    def set(self, name, replace=False, paste=False, value=None):
        """`wk key set`: 0 when what is stored can do its job."""
        names = self.settable()
        if not name:
            die("usage: wk key set <name> [--replace] [--paste]\n    names: %s " % " ".join(names))
        if name == "deploy-key":
            die("a deploy key is generated here and never pasted:  wk key deploy")
        if name not in names:
            die("there is no credential called '%s'.\n    This checkout knows: %s \n    Each is a row of lib/credcheck.py, which is "
                "also what says where to mint\n    one and what it must be able to do." % (name, " ".join(names)))
        mints = name in self._cc("minted").out.split()
        if paste and name == LOGIN:
            die("--paste cannot carry a claude.ai login -- it is made in a browser:\n    wk key set claude-login here, or "
                "'wk key setup' from a workstation with one")
        path = self.path(name)
        if replace:
            if not self.present(name):
                die("there is no %s credential here to replace (%s).\n    'wk key set %s' stores one." % (name, path, name))
            if not act.asked() and not act.confirm("remove the stored %s and replace it?" % name):
                die("not done -- nothing was changed")
            self.clear(name)
            self.deliver(name)
            info("removed the old %s credential -- revoke it too if it is still live" % name)
        if self.present(name) and not paste:
            return 0 if self.cred_report(name) else 1
        if name == LOGIN:
            if not self.login_run(name):
                return 1
            if act.dry_run():
                return 0
            if not self.present(name):
                die(self.cred_line(name, "skipped", "'claude auth login' left nothing at %s" % path))
            return 0 if self.cred_report(name) else 1
        minted = False
        if paste:
            val = (value or "").rstrip("\n")
            if not val:
                die("nothing arrived on stdin, so nothing is stored.\n    To mint one here instead:  wk key set " + name)
        elif mints:
            val, minted = self._cc("mint", name).out.rstrip("\n"), True
        else:
            val = self.prompt(self.rule(name, "what"), self.rule(name, "url"), self.rule(name, "remedy"), self.tty) or ""
            if not val:
                die(self.cred_line(name, "skipped", "nothing stored, so nothing here can " + self.rule(name, "needs")))
        line = self.check_value(name, val)
        if verdict(line) == "bad":
            die(self.cred_line(name, "refused", detail(line)))
        if not self.store(name, val):
            die("could not write " + path)
        self.deliver(name)
        log(self.cred_line(name, "minted" if minted else "stored", path))
        if minted:
            # Every reader of a stored credential redacts it, so this is the only chance to point a phone at one wk mints.
            log("  printed once: %s%s" % (self.rule(name, "url"), val))
            log("  " + self.rule(name, "remedy"))
        cred_print(name, line)
        return 0

    def _set(self, name, **kw):
        try:
            return self.set(name, **kw) == 0
        except Refused:
            return False
