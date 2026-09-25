"""`wk key check`: every row asked at once, replayed in the table's order."""

import re
from concurrent.futures import ThreadPoolExecutor

from wk.key.common import LOCAL, LOGIN, SAME_AS_HERE, STATES, detail, fact, summary, table_row, verdict


class Check:
    def cred_row(self, label, line):
        rows = [("row", table_row(STATES.get(verdict(line), "FIX"), label, summary(line)))]
        for text in detail(line).split("\n"):
            m = re.match(r"^ *fix: (.*)$", text)
            if m:
                rows.append(("action", label, m.group(1)))
        return rows

    def row_agrees(self, fp, line):
        return bool(fp) and verdict(line) == "ok" and fact(line, "fingerprint") == fp

    def fork_row(self, m, fork, repo):
        line = self.fork_verdict(m, fork, repo)
        if m != LOCAL and self.row_agrees(self.pub_fingerprint(self.pub_of(fork)), line):
            return True, [("row", table_row("ok", repo, SAME_AS_HERE))]
        return verdict(line) == "ok", self.cred_row(repo, line)

    def local_row(self, name):
        line = self.stored_verdict(name)
        return verdict(line) != "bad", self.cred_row(name, line)

    def peer_row(self, m, name):
        """One command is the remedy for every fault here, because one command puts all of it there."""
        line = self.cred_verdict_of(m, name)
        v, label = verdict(line), "%s %s" % (m, name)
        if name == LOGIN and v == "absent":
            return False, [("row", table_row("FIX", label, "no claude.ai login of its own")),
                           ("action", label, "from a terminal here: wk key setup   (it logs in for %s)" % m)]
        if name != LOGIN and self.row_agrees(self.fingerprint(name), line):
            return True, [("row", table_row("ok", label, SAME_AS_HERE))]
        rows = self.cred_row(label, line)
        if v == "absent":
            return False, rows + [("action", label, "wk key setup   (it puts the fleet's %s there)" % name)]
        if v == "bad":
            return False, rows + [("action", label, "from a terminal here: wk key setup   (it replaces what %s holds)" % m)]
        fp = fact(line, "fingerprint")
        if name == LOGIN or not fp or fp == self.fingerprint(name):
            return True, rows
        rows.append(("action", label, "wk key setup   (it is not the one this machine holds; an election settles it)"))
        if v in ("ok", "wide"):
            rows.append(("held", name, m))
        return True, rows

    def check(self):
        peers, boxes, forks, creds = self.resolve().peers, self.fleet.boxes, self.forks(), self.settable()
        with ThreadPoolExecutor(max_workers=16) as pool:
            fork_jobs = [(m, [pool.submit(self.fork_row, m, f[0], f[1]) for f in forks]) for m in self.workstations()]
            cred_jobs = [pool.submit(self.local_row, c) for c in creds]
            peer_jobs = [pool.submit(self.peer_row, m, c) for m in peers for c in creds]
        ok, actions, held = True, [], []

        def replay(job):
            nonlocal ok
            good, rows = job.result()
            ok = ok and good
            for r in rows:
                if r[0] == "row":
                    self.out.write(r[1])
                elif r[0] == "action":
                    actions.append((r[1], r[2]))
                else:
                    held.append((r[1], r[2]))

        for m, jobs in fork_jobs:
            if jobs:
                self.out.write("  %s:\n" % self.label(m))
            for j in jobs:
                replay(j)
        if boxes:
            self.out.write("  build machines:\n")
            self.out.write(table_row("ok", ", ".join(boxes), "nothing at rest; 'wk push' forwards the elected key"))
        self.out.write("  credentials:\n")
        for j in cred_jobs:
            replay(j)
        if peers:
            self.out.write("  the other workstations (one of each, and each its own login):\n")
            for j in peer_jobs:
                replay(j)
        for name, holder in held:
            fix = "wk key setup   (%s holds one its issuer accepts)" % holder
            done, kept = False, []
            for label, f in actions:
                if label == name:
                    kept.append((name, fix))
                    done = True
                elif label.split(" ")[1:] != [name]:
                    kept.append((label, f))
            actions = kept + ([] if done else [(name, fix)])
        self.needs_you(actions, ok)
        return 0 if ok else 1

    def needs_you(self, actions, ok):
        """The command first, because that is the line a person acts on; credcheck writes `<where> -- then: <command>`."""
        if actions:
            self.out.write("\n  needs you:\n")
            for n, (label, fix) in enumerate(actions, 1):
                where, _, cmd = fix.rpartition(" -- then: ")
                if not _:
                    where, cmd = "", fix
                if where.startswith(cmd):
                    where = ""
                self.out.write("    %d. %-26s %s\n" % (n, label, cmd))
                if where:
                    self.out.write("       %-26s %s\n" % ("", where))
        elif ok:
            self.out.write("\n  nothing needs you.\n")
        else:
            # An issuer that did not answer leaves its verdict unestablished, which is not something to go and do.
            self.out.write("\n  nothing to do by hand: what is not marked `ok` above could not be\n  established just now, so ask again.\n")
