"""The claude.ai login: made here by the Claude CLI, and made here for another workstation as a tar of its two files."""

import io
import os
import sys
import tarfile
import time

from wk import act
from wk.act import info, warn
from wk.key.common import LOGIN, LOGIN_FILES, changed, detail, summary, unchanged, verdict
from wk.secrets import first_line


class Login:
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
