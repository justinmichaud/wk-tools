"""`wk machine setup|rm` of a board: its card helper (admin/wk-card-priv, boot/check-boot-files.py), over the tailnet."""

import os
import sys

from wk import act
from wk.act import die, info, log, warn

# admin/wk-card-priv's own destination convention.
CARD_HELPER_FILES = (("admin/wk-card-priv", "/usr/local/libexec/wk-card-priv"),
                      ("boot/check-boot-files.py", "/usr/local/libexec/wk-check-boot-files.py"))


class BoardMachines:
    def setup_board(self, name, conf):
        dest = conf.get("NODE_SSH") or name
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
        dest = conf.get("NODE_SSH") or name
        ok, why = self.answers(name, conf)
        if not ok:
            warn("cannot reach %s (%s) -- the card helper stays on it." % (dest, why))
            log("  (re-run this when it is reachable to remove it properly)")
            if not act.confirm("remove the local conf %s anyway?" % self.rel(path)):
                die("aborted -- nothing was changed")
            self.here.remove(path)
            info("removed %s" % self.rel(path))
            return 0
        helper = ", ".join(r for _s, r in CARD_HELPER_FILES)
        if not act.confirm("deprovision %s: remove the card helper (%s)?" % (dest, helper)):
            die("aborted -- nothing was changed")
        m = self.board_machine(dest)
        for _src, rdest in CARD_HELPER_FILES:
            m.act_run(["rm", "-f", rdest])
        info("removed the card helper from %s" % dest)
        self.here.remove(path)
        info("removed %s" % self.rel(path))
        log("  it is still a machine here, because the registry names it:")
        log("      git rm %s && git commit" % self.rel(path))
        log("  that forgets it on every device. 'wk machine setup %s' brings it back." % name)
        return 0
