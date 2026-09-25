from wk import act
from wk.act import die, info, log, warn
from wk.key.common import TITLE, changed, unchanged


class DeployKeys:
    def pub_of(self, fork):
        return (self.sec.text(self.sec.pub_path(fork)) or "").rstrip("\n")

    def pub_fingerprint(self, pub):
        if not pub:
            return ""
        r = self.machine.run(["ssh-keygen", "-lf", "-"], input=pub + "\n")
        return (r.out.split() + ["", ""])[1] if r.ok else ""

    def push_key(self, fork):
        return self.sec.read(self.sec.push_key_path(fork))

    def ensure(self):
        self.private_dir(self.sec.secrets_dir())
        self.private_dir(self.sec.held_dir())
        for fork, repo in [f[:2] for f in self.forks()]:
            key = self.sec.push_key_path(fork)
            if self.machine.exists(key):
                unchanged("key for " + repo)
            else:
                # No passphrase: agents use it unattended, and per-repo scoping is the control.
                self.machine.act_run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wk deploy key for " + repo, "-f", key],
                                     input="")
                self.machine.act_run(["chmod", "0600", key])
                changed("generated key for " + repo)
                if act.dry_run():
                    continue
            if not self.sec.pub_publish(fork):
                die("%s is not a usable private key, so no public half was published for %s" % (key, repo))
        return 0

    def sshtest(self, fork):
        key = self.sec.push_key_path(fork)
        if not self.machine.exists(key):
            return 1, "no key\n"
        r = self.machine.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                              "-i", key, "-T", "git@github.com"], input="")
        return 0, r.out + r.err

    def adopt(self, fork, value):
        if fork not in self.fork_names():
            die("no fork called '%s'; this checkout knows: %s " % (fork, " ".join(self.fork_names())))
        if not self.sec.push_key_adopt(fork, value):
            die("what arrived on stdin is not a usable private key for " + fork)
        log("adopted the shared deploy key for " + fork)
        return 0

    def register_fork(self, fork, repo):
        pub = self.pub_of(fork)
        if not pub:
            warn("no key for %s here -- 'wk key ensure' makes it" % repo)
            return False
        if " ".join(pub.split()[:2]) in self.gh.bodies(repo):
            unchanged("%s: the shared deploy key is already registered" % repo)
            return True
        info("registering the shared deploy key on " + repo)
        # read_only=false: a key registered read-only looks fine until the first push fails.
        if self.gh.add(repo, TITLE, pub):
            changed("%s: the shared deploy key is registered with write access" % repo)
            return True
        warn("%s: could not register the shared key automatically" % repo)
        log("  add it at https://github.com/%s/settings/keys/new, ticking 'Allow write access':" % repo)
        self.out.write(pub + "\n")
        return False

    def rotate_keys(self):
        """The old keys off GitHub first: one left there keeps write access."""
        for fork, repo in [f[:2] for f in self.forks()]:
            for key_id in self.gh.titled(repo, TITLE):
                if self.gh.delete(repo, key_id):
                    changed("%s: removed the old shared deploy key (%s)" % (repo, key_id))
                else:
                    warn("%s: could not remove the old shared deploy key (%s)" % (repo, key_id))
            self.machine.act_run(["rm", "-f", self.sec.push_key_path(fork), self.sec.pub_path(fork)])
