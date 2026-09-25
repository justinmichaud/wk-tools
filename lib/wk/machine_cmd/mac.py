"""`wk machine setup` of a Mac: this tree pushed there, then its privileged helpers installed from a terminal."""

import shlex
import sys

from wk import act, tools
from wk.act import die, info


class MacMachines:
    def setup_mac(self, name, conf):
        dest = conf.get("NODE_SSH") or name
        ok, why = self.answers(name, conf)
        if not ok:
            if not act.dry_run():
                die("cannot ssh to '%s': %s" % (dest, why))
            sys.stderr.write("would push this tree to %s and install its privileged helpers, once it"
                             " answers (%s)\n" % (dest, why))
            return 0
        act.nothing_to_ask()
        m = self.board_machine(dest)
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
                "    answer it on. On %s itself:\n        cd %s && ./setup --stage quiesce" % (dest, dest, tools_dir))
        info("installing the privileged helpers on %s (it asks for a password once)" % dest)
        if not m.run_tty(["sh", "-c", script]).ok:
            die("./setup --stage quiesce did not finish on %s" % dest)
        info("%s is ready" % dest)
        return 0
