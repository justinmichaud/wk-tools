import contextlib
import io
from concurrent.futures import ThreadPoolExecutor

from wk import act, record
from wk.act import Refused, info, log, warn
from wk.key.common import LOCAL, RANKS, Fleet, Indented, changed, fact, summary, unchanged, verdict


class Election:
    def resolve(self):
        if self.fleet is None:
            self.fleet = Fleet(self.reg, self.env).resolve()
        return self.fleet

    def workstations(self):
        return [LOCAL] + self.resolve().peers

    def label(self, m):
        return record.host_name(self.machine) if m == LOCAL else m

    def fork_verdict(self, m, fork, repo):
        """What that machine's key authenticates as, and whether its public half is registered with write access."""
        if m == LOCAL:
            pub = self.pub_of(fork)
        else:
            rc, pub = self.fleet.ask(m, "pub", fork)
            pub = pub.rstrip("\n")
            if rc not in (0, 1):
                return "unverified\t%s did not answer: unreachable, or an older wk-tools there (wk sync --tools %s)" % (m, m)
        if not pub:
            return "absent\tno key for this fork\n    fix: wk key deploy"
        b64 = (pub.split() + ["", ""])[1]
        ssh = self.sshtest(fork)[1] if m == LOCAL else self.fleet.ask(m, "sshtest", fork)[1]
        line = self.check_value("deploy-key", "", "--repos", repo, "--evidence", "ssh=" + ssh.rstrip("\n"),
                                "--evidence", "read_only=" + self.gh.read_only(repo, b64))
        return "%s\n    fingerprint: %s" % (line, self.pub_fingerprint(pub))

    def cred_verdict_of(self, m, name):
        """A peer that cannot be asked says so rather than looking empty."""
        if m == LOCAL:
            return self.verdict_text(name)
        line = self.fleet.ask(m, "verdict", name)[1].rstrip("\n")
        return line or ("unverified\t%s did not answer: unreachable, or an older wk-tools there without the verdict subverb "
                        "(wk sync --tools %s)" % (m, m))

    def elect(self, fn, *args):
        """(winner, held, fingerprint per machine): `ok` over `wide`, one that cannot do its job never travels, and a tie
        goes to this machine, so a fleet that already agrees moves nothing."""
        ms = self.workstations()
        with ThreadPoolExecutor(max_workers=len(ms)) as pool:
            lines = list(pool.map(lambda m: fn(m, *args), ms))
        winner, held, best, fps = None, False, 0, {}
        for m, line in zip(ms, lines):
            held = held or verdict(line) == "unverified"
            fps[m] = fact(line, "fingerprint")
            rank = RANKS.get(verdict(line), 0)
            if rank > best:
                best, winner = rank, m
        return winner, held, fps

    def confirm(self, question):
        """The one question, asked before anything another workstation holds is overwritten or a key leaves GitHub."""
        if (self.rotate or self.fleet_on) and act.confirm(question):
            return True
        # Unasked or declined, what runs past it touches this machine alone, which takes nothing another holds.
        act.nothing_to_ask()
        return not (self.rotate or self.fleet_on)

    def fleet_fork(self, fork, repo):
        winner, held, fps = None, False, {}
        if self.fleet_on and not self.rotate:
            winner, held, fps = self.elect(self.fork_verdict, fork, repo)
            if winner and winner != LOCAL:
                info("%s: %s holds the deploy key GitHub accepts -- taking it" % (repo, winner))
                if not self.sec.push_key_adopt(fork, self.fleet.ask(winner, "give", fork)[1]):
                    warn("%s: %s's deploy key did not arrive, so nothing was changed (an older wk-tools there has no 'give': "
                         "wk sync --tools %s)" % (repo, winner, winner))
                    return False
                log("  an ssh-agent already holding the old key keeps offering it:  wk push off && wk push on")
        ok = self.register_fork(fork, repo)
        if not self.fleet_on:
            return ok
        if not winner and held:
            warn("%s: no workstation's deploy key could be judged, so none was written over another's" % repo)
            return False
        return self.fan_out_fork(fork, repo, fps) and ok

    def fan_out_fork(self, fork, repo, fps):
        mine = self.pub_fingerprint(self.pub_of(fork))
        if not mine:
            return False
        ok = True
        for m in self.fleet.peers:
            if fps.get(m) == mine:
                unchanged("%s: already holds the %s deploy key" % (m, repo))
            elif self.fleet.tell(m, ["adopt", fork], self.push_key(fork) or "")[0]:
                changed("%s: took the %s deploy key" % (m, repo))
            else:
                warn("%s: could not take the %s deploy key" % (m, repo))
                ok = False
        return ok

    def fleet_cred(self, name):
        fps = {}
        if self.rotate:
            if not self.rotate_here(name):
                return False
        else:
            winner, held, fps = self.elect(self.cred_verdict_of, name)
            if winner and winner != LOCAL:
                info("%s: %s holds one its issuer accepts -- taking it" % (name, winner))
                with contextlib.redirect_stdout(io.StringIO()):
                    taken = self._set(name, paste=True, value=self.fleet.ask(winner, "give", name)[1])
                if not taken:
                    warn("%s: what %s sent was not stored, so nothing here was changed (an older wk-tools there has no 'give': "
                         "wk sync --tools %s)" % (name, winner, winner))
                    return False
                log(self.cred_line(name, "taken", self.path(name)))
            elif not winner:
                if held:
                    # Being offline is a state, not a verdict: nothing is known to be broken, so nothing is replaced.
                    if self.present(name):
                        log(self.cred_line(name, "stored", self.path(name)))
                    warn("%s: no workstation's could be judged, so none was written over another's" % name)
                    return False
                warn("%s: no workstation holds one its issuer accepts" % name)
                if not self.cred_refresh(name):
                    return False
            else:
                log(self.cred_line(name, "stored", self.path(name)))
        return self.fan_out_cred(name, fps)

    def fan_out_cred(self, name, fps):
        mine = self.fingerprint(name)
        if not mine:
            warn("%s: nothing is stored here, so the other workstations were left as they are" % name)
            return False
        ok = True
        for m in self.fleet.peers:
            if fps.get(m) == mine:
                unchanged("%s: already holds the %s the fleet uses" % (m, name))
            elif self.fleet.tell(m, ["set", name, "--paste"], self.sec.cred_read(name) or "")[0]:
                changed("%s: took the %s" % (m, name))
            else:
                warn("%s: could not take the %s" % (m, name))
                ok = False
        return ok

    def local_cred(self, name):
        if self.rotate:
            return self.rotate_here(name)
        if not self.present(name):
            return self._set(name)
        line = self.stored_verdict(name)
        if verdict(line) == "bad":
            warn("%s is stored here but refused: %s" % (name, summary(line)))
            return self.cred_refresh(name)
        log(self.cred_line(name, "stored", self.path(name)))
        return True

    def cred_refresh(self, name):
        """A fresh one typed at a prompt, so no terminal means it is named and left."""
        if not self.present(name):
            return self._set(name)
        if not self.tty():
            warn("a replacement is typed at a prompt, and there is no terminal here:  wk key set %s --replace" % name)
            return False
        return self._set(name, replace=True)

    def rotate_here(self, name):
        return self._set(name, replace=self.present(name))

    def converge_forks(self):
        if self.rotate:
            info("rotating: removing the old shared keys from GitHub")
            self.rotate_keys()
        with contextlib.redirect_stderr(Indented(self.out)):
            try:
                self.ensure()
            except Refused:
                return False
        ok = True
        for fork, repo in [f[:2] for f in self.forks()]:
            ok = self.fleet_fork(fork, repo) and ok
        return ok

    def start_fleet(self):
        peers = " ".join(self.resolve().peers)
        self.fleet_on = bool(peers)
        return peers
