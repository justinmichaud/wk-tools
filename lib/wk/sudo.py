"""`wk key sudo`: root costs a password with a 30-second window, through one drop-in; the verdict, its install, and the fleet's answers."""

import os
import re
import sys

from wk import act, fleet, priv, record
from wk.act import die, log
from wk.store import ws_name

DROPIN_FMT = "/etc/sudoers.d/zz-%s-passwd"
TIMEOUT_MIN = "0.5"   # sudo's own default is 5 minutes, long enough for an agent to ride a person's password
STATUS = ["key", "sudo", "status", "--quiet"]
QUIESCE_PRIV, CARD_PRIV, BOOT_PRIV = PRIV_HELPERS = tuple(priv.path(n) for n, _, _ in priv.HELPERS)
# sudoers takes the LAST match, so the drop-in sorts after the site's own and re-imposes a password over a NOPASSWD.
SUDOERS_BODY = ("# Installed by 'wk key sudo setup'. Do not edit; re-run the command instead.\n"
                "Defaults:%s timestamp_timeout=%s\n%s ALL=(ALL:ALL) PASSWD: ALL\n")
BLANKET = re.compile(r'(^|\s)(NO)?PASSWD:\s*ALL\s*$|\)\s*ALL\s*$')
# [0-9.-]: a bare [0-9-] would read '0.5' as '0', a different policy.
TIMEOUT = re.compile(r'timestamp_timeout=([0-9.-]*)')


def require_visudo(machine):
    if not machine.have("visudo"):
        die("visudo is required (part of the sudo package) but not on PATH -- refusing to write a sudoers file that cannot be validated")


def install(machine, dropin, text, linux):
    tmp = "/tmp/wk-sudoers.%d" % os.getpid()
    machine.write(tmp, text)
    if not act.dry_run() and not machine.run(["visudo", "-c", "-f", tmp]).ok:
        machine.remove_now(tmp)
        die("the sudoers file wk generated does not parse -- refusing to install it")
    r = machine.act_run(["sudo", "install", "-m", "0440", "-o", "root", "-g", "root" if linux else "wheel", tmp, dropin])
    machine.remove(tmp)
    if not r.ok:
        die("could not install %s" % dropin)
    if not machine.act_run(["sudo", "visudo", "-c"]).ok:
        act.warn("sudoers does not parse after the install -- removing what we added")
        machine.act_run(["sudo", "rm", "-f", dropin])
        die("sudoers was left as it was; nothing changed")


def seconds(minutes):
    try:
        return float(minutes) * 60
    except ValueError:
        return None


class Sudo:
    def __init__(self, machine, env=None, linux=None):
        self.machine, self.env, self._linux = machine, os.environ if env is None else env, linux
        self.timeout_desc = "%g seconds" % seconds(TIMEOUT_MIN)

    def dropin(self):
        return DROPIN_FMT % self.machine.run(["id", "-un"]).out.strip()

    def verdict(self):
        """(0 when the policy holds, one line); `sudo -n` never prompts, so this never waits on a password."""
        listing = self.machine.run(["sudo", "-n", "-l"])
        lines = listing.out.splitlines()
        start = next((i for i, l in enumerate(lines) if "may run the following" in l), len(lines))
        blanket = [l for l in lines[start:] if re.match(r'^\s*\(.*\)', l) and BLANKET.search(l)]
        dropin = self.dropin()
        held = " (%s)" % dropin if self.machine.exists(dropin) else ""
        if self.machine.run(["sudo", "-n", "true"]).ok:
            if listing.ok and blanket and "NOPASSWD:" in blanket[-1]:
                return 1, "NOPASSWD: ALL is granted here -- root costs nothing at all"
            return 1, "sudo runs without a password right now -- a timestamp is cached"
        if not listing.ok:
            said = "a password is required right now (no timestamp cached).\n  the timeout cannot be read without authenticating -- "
            return (0, said + "but %s is installed" % dropin) if held else \
                (1, said + "and %s is not installed, so it is sudo's default (a few minutes)" % dropin)
        m = TIMEOUT.search(listing.out)
        timeout = m.group(1) if m and m.group(1) else "unset"
        secs = seconds(timeout)
        if secs is not None and 0 <= secs <= seconds(TIMEOUT_MIN):
            if not secs:
                return 0, "a password is required, and no timestamp is kept at all (timeout 0) -- stricter than the %s this installs, so nothing to do%s" % (self.timeout_desc, held)
            return 0, "a password is required, and the timestamp lasts %g seconds%s" % (secs, held)
        if timeout == "unset":
            return 1, "a password is required, but sudo keeps a timestamp (timeout unset -- sudo's default)"
        return 1, "a password is required, but sudo keeps a timestamp for %s minute(s) (%g seconds) -- wanted %s" % (timeout, secs, self.timeout_desc)

    def setup(self):
        """visudo -c before and after the install, then `sudo -k` and the property itself: `sudo -n true` refused."""
        require_visudo(self.machine)
        rc, v = self.verdict()
        host = record.host_name(self.machine)
        if rc == 0:
            act.info("already set up on %s: %s" % (host, v))
            return 0
        user, dropin = self.machine.run(["id", "-un"]).out.strip(), self.dropin()
        if self._linux is None:
            from wk.machine import is_linux
            self._linux = is_linux()
        act.info("installing %s on %s -- sudo will ask for your password; after this every sudo here asks" % (dropin, host))
        install(self.machine, dropin, SUDOERS_BODY % (user, TIMEOUT_MIN, user), self._linux)
        self.machine.act_run(["sudo", "-k"])
        if act.dry_run():
            return 0
        if self.machine.run(["sudo", "-n", "true"]).ok:
            act.warn("sudo still runs without a password after installing %s: something later in the include order grants "
                     "NOPASSWD ('sudo -l' shows the order)" % dropin)
            return 1
        act.info("sudo on %s now asks for a password, and forgets after %s" % (host, self.timeout_desc))
        broke = [h for h in PRIV_HELPERS if self.machine.exists(h) and not self.machine.run(["sudo", "-n", h, "status"]).ok]
        if broke:
            act.warn("the drop-in out-ranks a privileged helper:%s -- './setup --stage quiesce' reinstalls their rules under "
                     "names that sort last" % "".join(" " + h for h in broke))
            return 1
        return 0


def row(name, line, rc, fix):
    sys.stdout.write("%-22s %s\n" % (name, line))
    if rc != 0 and fix:
        log("  fix: " + fix)
    return rc


def status_row(a):
    if a.side != "answering":
        return row(a.name, a.why or fleet.reason(a.driver, a.side), 1, "")
    return row(a.name, a.out.strip(), a.rc, "wk key sudo setup --on %s" % a.name)


def on_machine(reg, action, name, env):
    try:
        driver = reg.load(name)
    except LookupError as e:
        die(str(e))
    if action != "setup":
        return status_row(fleet.ask(driver, STATUS, env))
    side, why = driver.probe()
    if side != "answering":
        return row(name, fleet.reason(driver, side, why), 1, "")
    return driver.machine.run_tty(["sh", "-c", driver.wk_cmd(["key", "sudo", "setup"], env)]).rc


def all_machines(reg, action, env):
    """This machine's status whatever `action` is (only a bare or --on setup sets one up), then `action` on every other."""
    rc = row(record.host_name(reg.machine), *Sudo(reg.machine, env).verdict()[::-1], "wk key sudo setup")
    if action == "setup":
        return max([rc] + [on_machine(reg, action, name, env) for name in reg.machines()])
    return max([rc] + [status_row(a) for a in fleet.each(reg, STATUS, reg.machines(), env)])


def status_here(sudo, env):
    rc, v = sudo.verdict()
    if act.quiet(env):
        sys.stdout.write(v + "\n")
        return rc
    return row(record.host_name(sudo.machine), v, rc, "wk key sudo setup\n  every other machine: wk key sudo status --all")


def main(words, on, all_flag, reg, env=None):
    env = os.environ if env is None else env
    if reg.in_workspace():
        die("'wk key sudo' hardens a machine you log into, and this is workspace '%s' -- a container's sudoers belong to "
            "the image and the workspace is the blast radius anyway. Run it on the host." % ws_name(env))
    action = words[0] if words else "status"
    if action not in ("status", "setup"):
        die("'%s' is not a verb of wk key sudo: status or setup; see wk key -h" % action)
    if on == "":
        die("--on needs a name")
    if on:
        return on_machine(reg, action, on, env)
    if all_flag:
        return all_machines(reg, action, env)
    sudo = Sudo(reg.machine, env)
    return sudo.setup() if action == "setup" else status_here(sudo, env)
