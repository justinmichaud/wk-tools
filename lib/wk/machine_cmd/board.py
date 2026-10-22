"""`wk machine setup|rm` of the machines that hold no places: a board gets its card helper (admin/wk-card-priv,
boot/check-boot-files.py) over the tailnet; a Mac gets this tree and its privileged helpers, installed from a terminal."""

import os
import shlex
import sys

from wk import act, tools
from wk.act import die, info, log, warn
from wk.sudo import CARD_PRIV

CARD_HELPER_FILES = (("admin/wk-card-priv", CARD_PRIV),
                      ("boot/check-boot-files.py", "/usr/local/libexec/wk-check-boot-files.py"))


class BoardMachines:
    def reached(self, name, conf, would, refusal):
        """(dest, its Machine), or (dest, None) once a dry run said what it `would` do there: unreached is `refusal`."""
        dest = conf.get("ssh") or name
        ok, why = self.answers(name, conf)
        if ok:
            act.nothing_to_ask()
            return dest, self.board_machine(dest)
        if not act.dry_run():
            die(refusal % {"dest": dest, "why": why, "name": name})
        sys.stderr.write("would %s on %s, once it answers (%s)\n" % (would, dest, why))
        return dest, None

    def setup_board(self, name, conf, *_):
        dest, m = self.reached(name, conf, "install the card helper (%s)" % CARD_HELPER_FILES[0][1],
                               "cannot reach '%(dest)s' over the tailnet (%(why)s).\n    A board never gets tailscale any other "
                               "way than the image it boots joining on its\n    own -- boot it, wait for it to join, then re-run "
                               "'wk machine setup %(name)s'.")
        if m is None:
            return 0
        for src, rdest in CARD_HELPER_FILES:
            m.copy_in(os.path.join(self.root, src), rdest)
            if not m.act_run(["chmod", "+x", rdest]).ok:
                warn("could not chmod +x %s on %s" % (rdest, dest))
        info("installed the card helper on %s (%s)" % (dest, CARD_HELPER_FILES[0][1]))
        log("  from here:  wk boot %s" % name)
        return 0

    def setup_mac(self, name, conf, *_):
        dest, m = self.reached(name, conf, "push this tree and install its privileged helpers", "cannot ssh to '%(dest)s': %(why)s")
        if m is None:
            return 0
        r = m.run(["sh", "-c", 'printf "%s" "$HOME"'])
        if not r.ok or not r.out.strip():
            die("could not read $HOME on %s, so there is nowhere to put this tree" % dest)
        tools_dir = r.out.strip() + "/Development/wk-tools"
        info("pushing this tree to %s:%s" % (dest, tools_dir))
        if not tools.push(self.root, self.here, m, tools_dir, self.env):
            die("nothing was changed on %s; what it said is above." % dest)
        script = "cd %s && ./setup --stage quiesce" % shlex.quote(tools_dir)
        if act.dry_run():
            sys.stderr.write("would run on %s: %s\n" % (dest, script))
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            die("the tree is in place on %s. Installing its privileged helpers puts a NOPASSWD rule\n"
                "    in /etc/sudoers.d, and that sudo authenticates once; this session has no terminal to\n"
                "    answer it on. On %s itself:\n        %s" % (dest, dest, script))
        info("installing the privileged helpers on %s (it asks for a password once)" % dest)
        if not m.run_tty(["sh", "-c", script]).ok:
            die("./setup --stage quiesce did not finish on %s" % dest)
        info("%s is ready" % dest)
        return 0

    def rm_board(self, name, conf, path):
        dest = conf.get("ssh") or name
        ok, why = self.answers(name, conf)
        if not ok:
            return self.forget_unreached(dest, why, "the card helper stays on it", path)
        helper = ", ".join(r for _s, r in CARD_HELPER_FILES)
        if not act.confirm("deprovision %s: remove the card helper (%s)?" % (dest, helper)):
            die("aborted -- nothing was changed")
        m = self.board_machine(dest)
        for _src, rdest in CARD_HELPER_FILES:
            m.act_run(["rm", "-f", rdest])
        info("removed the card helper from %s" % dest)
        self.here.remove(path)
        info("removed %s" % self.rel(path))
        return self.still_named(name, path)
