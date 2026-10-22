"""One credential on this machine: its rule, its file, its verdict, and `wk key set`."""

import os
import sys

import credcheck
from wk import act, project, repos, secrets
from wk.act import Refused, die, info, log, warn
from wk.key.common import cred_print, detail, verdict


class Creds:
    def push_keys(self):
        """Every repo's deploy keys, each GitHub repository resolved."""
        return repos.push_rows(self.machine, self.root)

    def key_names(self):
        return [k[0] for k in secrets.push_keys()]

    def settable(self):
        return [n for n in credcheck.rules() if n != "deploy-key"]

    def rule(self, name, field):
        return getattr(credcheck.rules()[name], field)

    def alone(self, name):
        return self.rule(name, "alone")

    def path(self, name):
        return self.sec.cred_path(name)

    def _secretfile(self, verb, path, input="", run=None):
        return (run or self.machine.run)(["python3", os.path.join(self.root, "lib", "secretfile.py"), verb, path], input=input)

    def present(self, name):
        return bool(self.sec.cred_stored(name))

    def fingerprint(self, name):
        p = self.path(name)
        return self._secretfile("fingerprint", p).out.strip() if p else ""

    def stored_verdict(self, name):
        return self.sec.cred_verdict(name)

    def verdict_text(self, name):
        fp = self.fingerprint(name)
        return self.stored_verdict(name) + ("\n    fingerprint: %s" % fp if fp else "")

    def private_dir(self, d):
        """0700 asserted on every run: a directory made by hand is as open as its umask left it."""
        self.machine.ensure_dir_mode(d, "0700")

    def mint(self, name):
        """In the foreground on this terminal, as a mint may need a person."""
        if act.dry_run():
            sys.stderr.write("would mint a %s credential\n" % name)
            return ""
        return credcheck.rules()[name].mint().rstrip("\n")

    def store(self, name, value):
        p = self.path(name)
        self.private_dir(os.path.dirname(p))
        r = self._secretfile("write", p, value + "\n", run=self.machine.act_run)
        sys.stderr.write(r.err)
        if not r.ok:
            return False
        return self.sec.publish_view("container")

    def clear(self, name):
        self.machine.act_run(["rm", "-f", self.path(name)])
        self.sec.publish_view("container")

    def cred_line(self, name, state, where):
        var = next((r[3] for r in self.sec.agent_secrets() if r[0] == name), "-")
        return "%-13s %-8s %s%s" % (name, state, where or "", "  ($%s in a workspace)" % var if var != "-" else "")

    def cred_report(self, name):
        log(self.cred_line(name, "stored", self.path(name)))
        return cred_print(name, self.stored_verdict(name))

    def deliver(self, name):
        if name in ("github-pat", "bugzilla-api-key") and not self.sec.push_deliver():
            warn("an injector on this machine did not take the %s, so a request from the workspaces it serves answers 401\n"
                 "    (or is refused) until it does. './setup' converges the one in the podman machine (the vmtools stage on\n"
                 "    macOS, sdk on Linux) and 'wk start <guest>' the one that serves the guests." % name)

    def set(self, name, replace=False, paste=False, value=None):
        """0 when what is stored can do its job."""
        names = self.settable()
        if not name:
            die("usage: wk key set <name> [--replace] [--paste]\n    names: %s " % " ".join(names))
        if name == "deploy-key":
            die("a deploy key is generated here and never pasted:  wk key deploy")
        if name not in names:
            die("there is no credential called '%s'.\n    This checkout knows: %s \n    Each is a row of lib/credcheck.py, which is "
                "also what says where to mint\n    one and what it must be able to do." % (name, " ".join(names)))
        mints = bool(self.rule(name, "mint"))
        path = self.path(name)
        if replace and not self.present(name):
            info("no %s is stored here (%s), so there is nothing to replace: setting one" % (name, path))
        elif replace:
            if not act.asked() and not act.confirm("remove the stored %s and replace it?" % name):
                die("not done -- nothing was changed")
            self.clear(name)
            self.deliver(name)
            info("removed the old %s credential -- revoke it too if it is still live" % name)
        if self.present(name) and not paste:
            return 0 if self.cred_report(name) else 1
        minted = False
        if paste:
            val = (value or "").rstrip("\n")
            if not val:
                die("nothing arrived on stdin, so nothing is stored.\n    To mint one here instead:  wk key set " + name)
        elif mints:
            val, minted = self.mint(name), True
            if act.dry_run():
                log("  and would store what it mints at %s" % path)
                return 0
            if not val:
                die(self.cred_line(name, "skipped", "nothing was minted (above), so nothing is stored"))
        else:
            val = self.prompt(self.rule(name, "what"), self.rule(name, "url"), self.rule(name, "remedy"), self.tty) or ""
            if not val:
                die(self.cred_line(name, "skipped", "nothing stored, so nothing here can " + self.rule(name, "needs")))
        line = self.sec.check_value(name, val)
        if verdict(line) == "bad":
            die(self.cred_line(name, "refused", detail(line)))
        if not self.store(name, val):
            die("could not write " + path)
        self.deliver(name)
        log(self.cred_line(name, "minted" if minted else "stored", path))
        if minted and not self.alone(name):
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
