"""The `Key` object `cmd/key` drives: setup and deploy, the read verbs another workstation calls, and the verb table."""

import os
import sys

from wk.act import die, warn
from wk.key.check import Check
from wk.key.common import GitHub, prompt_secret
from wk.key.creds import Creds
from wk.key.deploy import DeployKeys
from wk.key.election import Election
from wk.secrets import Secrets
from wk.places import Registry


class Key(Creds, DeployKeys, Election, Check):
    def __init__(self, root, env=None, machine=None, reg=None, sec=None, tty=None, prompt=None, out=None, rotate=False):
        self.root = str(root)
        self.env = os.environ if env is None else env
        self.reg = reg or Registry(self.root, self.env, machine)
        self.machine = machine or self.reg.machine
        self.sec = sec or Secrets(self.root, self.env, self.machine)
        self.gh = GitHub(self.machine)
        self.tty = tty or (lambda: os.isatty(0))
        self.prompt = prompt or prompt_secret
        self.out = out or sys.stdout
        self.rotate = rotate
        self.fleet = None
        self.fleet_on = False
        self._forks = self._rows = self._names = None

    def setup(self):
        peers = self.start_fleet()
        if self.rotate:
            if not self.confirm("turn every credential over -- the shared deploy keys removed from GitHub and minted afresh, every "
                                "credential stored here replaced%s?"
                                % (", and what %s hold(s) overwritten with the fresh ones" % peers if peers else "")):
                die("not done -- nothing was changed")
        else:
            if not self.confirm("put one working deploy key and one of every credential on this machine and on %s, "
                                "overwriting what differs?" % peers):
                self.fleet_on = False
            if peers and not self.fleet_on:
                warn("%s was left exactly as it is -- this machine's own credentials are still set up" % peers)
        left = "" if self.converge_forks() else "the deploy keys"
        for name in self.settable():
            ok = self.fleet_cred(name) if self.fleet_on else self.local_cred(name)
            if not ok:
                left += " " + name
        if left:
            warn("not settled:" + left)
        return self.check()

    def deploy(self):
        peers = self.start_fleet()
        if self.rotate:
            q = "remove the shared deploy keys from GitHub, mint fresh ones and register them%s?" % (
                ", and put them on %s" % peers if peers else "")
        else:
            q = "register the shared deploy keys on GitHub and put the elected one on %s?" % peers
        if not self.confirm(q):
            die("not done -- nothing was changed")
        self.converge_forks()
        sys.stderr.write("\n")
        return self.check()

    def fork_arg(self, verb, fork):
        if not fork:
            die("wk key %s needs a fork: %s " % (verb, " ".join(self.fork_names())))
        return fork

    def pub(self, fork):
        out = self.pub_of(self.fork_arg("pub", fork))
        if not out:
            return 1
        self.out.write(out + "\n")
        return 0

    def fingerprints(self):
        for fork in self.fork_names():
            pub = self.pub_of(fork)
            if not pub:
                self.out.write("%-9s %-50s %s\n" % (fork, "-", "no key"))
                continue
            here = self.machine.exists(self.sec.push_key_path(fork))
            self.out.write("%-9s %-50s %s\n" % (fork, self.pub_fingerprint(pub) or "unreadable",
                                                 "private half here ('wk key push status')" if here else "no private half"))
        return 0

    def show(self):
        for fork, repo in [f[:2] for f in self.forks()]:
            self.out.write("# %s\n%s\n" % (repo, self.pub_of(fork)))
        # The topic name is the whole ntfy credential, so this is the one reader that prints it: a second phone joins the topic already minted.
        if self.present("ntfy"):
            self.out.write("# ntfy -- %s\n%s%s\n" % (self.rule("ntfy", "remedy"), self.rule("ntfy", "url"),
                                                      (self.sec.cred_read("ntfy") or "").rstrip("\n")))
        else:
            self.out.write("# ntfy -- no topic on this machine: wk key set ntfy\n")
        return 0

    def give(self, what):
        if not what:
            die("usage: wk key give <fork>|<name>   (it prints on stdout, for the workstation electing one to take)")
        if what in self.fork_names():
            val = self.push_key(what)
        elif what in self.settable():
            val = self.sec.cred_read(what)
        else:
            die("there is no fork or credential called '%s'.\n    This checkout knows: %s %s " % (
                what, " ".join(self.fork_names()), " ".join(self.settable())))
        if val is None:
            return 2
        self.out.write(val)
        return 0

    def verdict(self, name):
        if name not in self.settable():
            die("usage: wk key verdict <name>\n    names: %s " % " ".join(self.settable()))
        self.out.write(self.verdict_text(name) + "\n")
        return 0

    def adopt_verb(self, what, stdin):
        if not what:
            die("usage: wk key adopt <fork>   (the private key on stdin)")
        return self.adopt(what, stdin().decode(errors="replace"))

def main(root, verb, arg="", rotate=False, replace=False, paste=False, env=None, stdin=None, **kw):
    """`stdin()` is every byte on it, read only by the verbs that take a value there."""
    stdin = stdin or (lambda: sys.stdin.buffer.read())
    k = Key(root, env, rotate=rotate, **kw)
    if verb in ("ensure", "setup", "deploy", "check", "show", "fingerprints"):
        return getattr(k, verb)()
    if verb == "set":
        return k.set(arg, replace=replace, paste=paste, value=stdin().decode(errors="replace") if paste else None)
    if verb in ("pub", "give", "verdict"):
        return getattr(k, verb)(arg)
    if verb == "sshtest":
        rc, text = k.sshtest(k.fork_arg("sshtest", arg))
        k.out.write(text)
        return rc
    if verb == "adopt":
        return k.adopt_verb(arg, stdin)
