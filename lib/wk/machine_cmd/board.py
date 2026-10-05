"""`wk machine setup|rm` of a board: its card helper (admin/wk-card-priv, boot/check-boot-files.py), over the tailnet."""

import os
import sys

from wk import act
from wk.act import die, info, log, warn
from wk.sudo import CARD_PRIV

CARD_HELPER_FILES = (("admin/wk-card-priv", CARD_PRIV),
                      ("boot/check-boot-files.py", "/usr/local/libexec/wk-check-boot-files.py"))


class BoardMachines:
    def setup_board(self, name, conf):
        dest = conf.get("ssh") or name
        ok, why = self.answers(name, conf)
        if not ok:
            if not act.dry_run():
                die("cannot reach '%s' over the tailnet (%s).\n"
                    "    A board never gets tailscale any other way than the image it boots joining on its\n"
                    "    own -- boot it, wait for it to join, then re-run 'wk machine setup %s'." % (dest, why, name))
            sys.stderr.write("would install the card helper (%s) on %s, once it answers on the tailnet (%s)\n"
                             % (CARD_HELPER_FILES[0][1], dest, why))
            return 0
        act.nothing_to_ask()
        m = self.board_machine(dest)
        for src, rdest in CARD_HELPER_FILES:
            m.copy_in(os.path.join(self.root, src), rdest)
            if not m.act_run(["chmod", "+x", rdest]).ok:
                warn("could not chmod +x %s on %s" % (rdest, dest))
        info("installed the card helper on %s (%s)" % (dest, CARD_HELPER_FILES[0][1]))
        log("  from here:  wk boot %s" % name)
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
