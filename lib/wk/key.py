"""`wk key`: what this machine holds of each credential, and one working one of each on every workstation. The rules are
lib/credcheck.py's rows, the files are lib/wk/secrets.py's, and every other workstation is asked through its own `wk key`."""

import contextlib
import io
import os
import re
import sys
import tarfile
import termios
import time
from concurrent.futures import ThreadPoolExecutor

from wk import act, record
from wk.act import Refused, debug, die, info, log, warn
from wk.secrets import Secrets, first_line
from wk.targets import Registry

LOCAL = "local"
LOGIN = "claude-login"
TITLE = "wk shared deploy key"
SAME_AS_HERE = "the one this machine holds"
LOGIN_FILES = (".credentials.json", ".claude.json")
STATES = {"ok": "ok", "wide": "wide", "unverified": "?", "absent": "none"}
RANKS = {"ok": 3, "wide": 2}
TOMBSTONES = {"register": "wk key deploy",
              "share": "wk key setup   (it elects the credential the fleet holds rather than pushing this machine's)",
              "claude": "wk key set claude   (the account login beside it is 'wk key set claude-login')",
              "tailnet": "wk key set tailnet", "tailnet-api": "wk key set tailnet-api"}


def verdict(line):
    return (line or "").split("\t", 1)[0]


def detail(line):
    return line.split("\t", 1)[1] if "\t" in (line or "") else (line or "")


def summary(line):
    return first_line(detail(line))


def fact(line, key):
    for text in (line or "").split("\n"):
        m = re.match(r"^ *%s: (.*)$" % re.escape(key), text)
        if m:
            return m.group(1)
    return ""


def table_row(state, label, text):
    return "    %-4s %-28s %s\n" % (state, label, text)


def changed(msg):
    info(msg)


def unchanged(msg):
    debug("ok: " + msg)


class Indented(io.TextIOBase):
    """What `ensure` says, indented under the run that asked for it."""

    def __init__(self, out):
        self.out, self.fresh = out, True

    def write(self, s):
        for part in s.splitlines(True):
            self.out.write(("  " if self.fresh else "") + part)
            self.fresh = part.endswith("\n")
        return len(s)


def prompt_secret(what, url, how, tty):
    """What a person pasted with the terminal's echo off, or None."""
    if not tty():
        warn("wk needs %s, and there is no terminal to ask on. Re-run interactively." % what)
        return None
    sys.stderr.write("\n")
    info("wk needs %s." % what)
    if url:
        log("  " + url)
    if how:
        log("  " + how)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    try:
        sys.stderr.write("  paste it (input hidden, empty to skip): ")
        sys.stderr.flush()
        val = sys.stdin.readline()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stderr.write("\n")
    return val.rstrip("\n") or None


class GitHub:
    """Every GitHub API call `wk key` makes: a repository's deploy keys, through `gh`."""

    def __init__(self, machine):
        self.machine = machine

    def _keys(self, repo, jq):
        r = self.machine.run(["gh", "api", "repos/%s/keys" % repo, "--jq", jq], input="")
        return r.out if r.ok else None

    def bodies(self, repo):
        return [" ".join(line.split()[:2]) for line in (self._keys(repo, ".[].key") or "").splitlines()]

    def read_only(self, repo, b64):
        return first_line(self._keys(repo, '.[] | select(.key | contains("%s")) | .read_only' % b64) or "")

    def titled(self, repo, title):
        return (self._keys(repo, '.[] | select(.title == "%s") | .id' % title) or "").split()

    def delete(self, repo, key_id):
        return self.machine.act_run(["gh", "api", "-X", "DELETE", "repos/%s/keys/%s" % (repo, key_id)], input="").ok

    def add(self, repo, title, pub):
        return self.machine.act_run(["gh", "api", "repos/%s/keys" % repo, "-f", "title=" + title, "-f", "key=" + pub,
                                     "-F", "read_only=false"], input="").ok


class Fleet:
    """The other workstations and the build machines, each asked through its own `wk key`."""

    def __init__(self, reg, env):
        self.reg, self.env = reg, env
        self.peers, self.boxes, self.targets = [], [], {}

    def _load(self, name):
        try:
            t = self.reg.load(name)
        except LookupError:
            return None
        return t if t.kind == "remote" and t.has_wk() else None

    def resolve(self):
        """Each machine is probed over ssh, so they are asked at once."""
        names = self.reg.machines()
        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            loaded = list(pool.map(self._load, names))
        for name, t in zip(names, loaded):
            if t is not None:
                self.targets[name] = t
                (self.peers if t.peer else self.boxes).append(name)
        return self

    def _argv(self, name, args, env):
        return ["sh", "-c", self.targets[name].wk_cmd(["key"] + list(args), env)]

    def ask(self, name, *args):
        """(status, stdout): a read, so the far side is never told this run is a dry one."""
        env = {k: v for k, v in self.env.items() if k != "WK_DRY_RUN"}
        r = self.targets[name].machine.run(self._argv(name, args, env), input="")
        return r.rc, r.out.replace("\r", "")

    def tell(self, name, args, value):
        """The value on stdin, never an argument in `ps` over there."""
        r = self.targets[name].machine.act_run(self._argv(name, args, self.env), input=value)
        return r.ok, r.out.replace("\r", "")


class Key:
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

    # -- the tables this run reads once

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
        args = ["check", name, "--repos", self.repos()] + list(extra)
        if name == "bugzilla-api-key":
            args += ["--evidence", "login=" + (self.sec.bugzilla_user() or "")]
        return self._cc(*args, input=value).out.rstrip("\n")

    # -- one credential on this machine

    def path(self, name):
        return self.sec.cred_path(name)

    def _secretfile(self, verb, path, input=""):
        return self.machine.run(["python3", os.path.join(self.root, "lib", "secretfile.py"), verb, path], input=input)

    def present(self, name):
        p = self.path(name)
        return bool(p) and self._secretfile("present", p).ok

    def fingerprint(self, name):
        p = self.path(name)
        return self._secretfile("fingerprint", p).out.strip() if p else ""

    def stored_verdict(self, name):
        p = self.path(name)
        value = self.sec.read(p)
        if value is None:
            return "bad\tthe file at %s could not be read; the refusal above says why" % p
        return self.check_value(name, value, "--path", p)

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
        if not self.machine.act_run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "write", p],
                                    input=value + "\n").ok:
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

    def cred_print(self, name, line):
        v, d = verdict(line), detail(line)
        if v == "ok":
            log("  " + d)
        elif v == "wide":
            warn("%s reaches further than wk spends it: %s" % (name, d))
        elif v == "unverified":
            warn("%s is unverified: %s" % (name, d))
        elif v == "bad":
            warn("%s cannot do its job: %s" % (name, d))
            return False
        return True

    def cred_report(self, name):
        log(self.cred_line(name, "stored", self.path(name)))
        return self.cred_print(name, self.stored_verdict(name))

    def deliver(self, name):
        """Only the credential injector, on the machine that runs the workspaces, ever reads these two."""
        if name == "github-pat":
            target = self.sec.machine_pat()
            if not self.sec.pat_deliver():
                warn("an injector on this machine did not take the read token, so a read from\n    the workspaces it serves answers "
                     "401 until it does. './setup' converges the\n    one in the podman machine (the vmtools stage on macOS, sdk "
                     "on Linux) and\n    'wk vm start <guest>' the one that serves the guests.")
        elif name == "bugzilla-api-key":
            target = self.sec.machine_bugzilla_key()
        else:
            return
        if not self.sec.switch_cred_converge(self.sec.machine_sock(), target, name):
            warn("the injector on this machine is still writing with the %s stored\n    before this one, so 'git-webkit pr' in a "
                 "workspace spends that: 'wk push off'\n    then 'wk push on' hands it the one stored here." % name)

    def have(self, cmd):
        return self.machine.run(["sh", "-c", "command -v %s >/dev/null" % cmd], input="").ok

    def login_run(self, name, d=None):
        """The shared directory is the CLI's config home, so the account record remote control reads lands beside the credential."""
        d = d or self.sec.store.agent_rw_dir()
        if not self.have("claude"):
            warn(self.cred_line(name, "skipped", "the Claude CLI makes this login and is not on PATH -- install it, then: wk key set " + name))
            return False
        if not self.tty():
            warn(self.cred_line(name, "skipped", "this login opens a browser and needs a terminal -- re-run interactively: wk key set " + name))
            return False
        self.private_dir(d)
        # lib/no-keychain/security answers as a locked Keychain does, so on a Mac the CLI writes the file every workspace reads.
        argv = ["env", "PATH=%s:%s" % (os.path.join(self.root, "lib", "no-keychain"), self.env.get("PATH", "")),
                "CLAUDE_CONFIG_DIR=" + d, "CLAUDE_SECURESTORAGE_CONFIG_DIR=" + d, "claude", "auth", "login"]
        if act.dry_run():
            sys.stderr.write("would run: %s\n" % " ".join(argv[3:]))
            return True
        if not self.machine.run_tty(argv).ok:
            warn(self.cred_line(name, "skipped", "'claude auth login' did not finish"))
            return False
        return True

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
        self.cred_print(name, line)
        return 0

    def _set(self, name, **kw):
        try:
            return self.set(name, **kw) == 0
        except Refused:
            return False

    # -- the deploy keys on this machine

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

    # -- the claude.ai login that crosses to another workstation as a tar of its two files

    def scratch(self, prefix):
        d = os.path.join(self.env.get("TMPDIR") or "/tmp", "%s.%s" % (prefix, os.urandom(8).hex()))
        self.private_dir(d)
        return d

    def login_made_in(self, d):
        p = os.path.join(d, LOGIN_FILES[0])
        text = self.sec.text(p)
        if not text:
            return "bad\tthe login left nothing in " + d
        return self.check_value(LOGIN, text, "--path", p)

    def login_pack(self, d):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name in LOGIN_FILES:
                data = (self.sec.text(os.path.join(d, name)) or "").encode()
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode, ti.mtime = len(data), 0o600, int(time.time())
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue().decode()

    def login_adopt(self, data):
        """(status, verdict line): a login made for this workstation, judged whole before anything here is replaced."""
        bad = (1, "bad\tnot a login bundle: a tar holding .credentials.json and .claude.json was expected on stdin")
        try:
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                files = {n: tar.extractfile(n).read().decode() for n in LOGIN_FILES}
        except (tarfile.TarError, KeyError, AttributeError, UnicodeDecodeError):
            return bad
        if not all(files.values()):
            return bad
        rw = self.sec.store.agent_rw_dir()
        self.private_dir(rw)
        d = self.scratch("wk-login-adopt")
        try:
            for n, text in files.items():
                self.machine.write(os.path.join(d, n), text)
                self.machine.act_run(["chmod", "0600", os.path.join(d, n)])
            line = self.check_value(LOGIN, files[LOGIN_FILES[0]], "--path", os.path.join(d, LOGIN_FILES[0]))
            if verdict(line) == "bad":
                return 1, line
            self.clear(LOGIN)
            for n in LOGIN_FILES:
                self.machine.act_run(["mv", "-f", os.path.join(d, n), os.path.join(rw, n)])
            return 0, line
        finally:
            self.machine.remove(d)

    # -- the other workstations

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

    def share_login(self, m):
        """A peer without a usable login is logged in for here, in a directory of its own, and sent only that one."""
        if verdict(self.cred_verdict_of(m, LOGIN)) in ("ok", "wide", "unverified"):
            unchanged("%s: holds a claude.ai login of its own" % m)
            return True
        if not self.have("claude"):
            warn("%s: has no usable claude.ai login, and the Claude CLI that makes one is not on PATH here" % m)
            return False
        if not self.tty():
            warn("%s: has no usable claude.ai login; making one is a browser flow, so from a terminal:  wk key setup" % m)
            return False
        if act.dry_run():
            sys.stderr.write("would run: claude auth login, for %s, and send it the login\n" % m)
            return True
        d = self.scratch("wk-login")
        try:
            info("%s: logging in for it -- the browser opens once; what it leaves goes to %s and stays nowhere else" % (m, m))
            if not self.login_run(LOGIN, d):
                return False
            line = self.login_made_in(d)
            if verdict(line) == "bad":
                warn("%s: the login made here is not one a workspace can use, so it was not sent: %s" % (m, summary(line)))
                return False
            ok, out = self.fleet.tell(m, ["adopt", LOGIN], self.login_pack(d))
            line = first_line(out)
            if ok and line and verdict(line) != "bad":
                changed("%s: logged in for it -- %s" % (m, detail(line)))
                return True
            warn("%s: did not take the login, so it is discarded here (an older wk-tools there has no 'adopt claude-login': "
                 "wk sync --tools %s)" % (m, m))
            return False
        finally:
            self.machine.remove(d)

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

    def converge_forks(self):
        if self.rotate:
            info("rotating: removing the old shared keys from GitHub")
            self.rotate_keys()
        with contextlib.redirect_stderr(Indented(self.out)):
            try:
                self.ensure()
            except Refused:
                pass
        ok = True
        for fork, repo in [f[:2] for f in self.forks()]:
            ok = self.fleet_fork(fork, repo) and ok
        return ok

    def start_fleet(self):
        peers = " ".join(self.resolve().peers)
        self.fleet_on = bool(peers)
        return peers

    def setup(self):
        peers = self.start_fleet()
        if self.rotate:
            if not self.confirm("turn every credential over -- the shared deploy keys removed from GitHub and minted afresh, every "
                                "credential stored here replaced%s?"
                                % (", and what %s hold(s) overwritten with the fresh ones" % peers if peers else "")):
                die("not done -- nothing was changed")
        else:
            if not self.confirm("put one working deploy key and one of every credential but the claude.ai login on this machine "
                                "and on %s, overwriting what differs, and log in here for any of them without a claude.ai login?"
                                % peers):
                self.fleet_on = False
            if peers and not self.fleet_on:
                warn("%s was left exactly as it is -- this machine's own credentials are still set up" % peers)
        left = "" if self.converge_forks() else "the deploy keys"
        for name in self.settable():
            ok = self.fleet_cred(name) if self.fleet_on and name != LOGIN else self.local_cred(name)
            if not ok:
                left += " " + name
        if self.fleet_on and not all([self.share_login(m) for m in self.fleet.peers]):
            left += " the other workstations' logins"
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

    # -- `wk key check`: every row asked at once, replayed in the table's order

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

    # -- the read verbs another workstation, or `wk status`, calls

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
                                                 "private half here ('wk push status')" if here else "no private half"))
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
        if what == LOGIN:
            die("a claude.ai login is never handed over: a copy is a second holder of one\n    refresh token, and the first refresh "
                "on either side locks the other out.\n    'wk key setup' on a workstation that has one logs in for this machine "
                "instead.")
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
            die("usage: wk key adopt <fork>|claude-login   (the private key, or a tar of the login's two files, on stdin)")
        if what != LOGIN:
            return self.adopt(what, stdin().decode(errors="replace"))
        rc, line = self.login_adopt(stdin())
        if rc:
            die("what arrived on stdin is not a claude.ai login a workspace here can use: " + summary(line))
        self.out.write(line + "\n")
        log("adopted a claude.ai login made for this workstation")
        return 0


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
    if verb in TOMBSTONES:
        die("'wk key %s' is now:  %s\n    'wk key setup' does every credential this machine has not got yet." % (verb, TOMBSTONES[verb]))
    die("'%s' is not a verb of wk key; see wk key -h" % verb)
