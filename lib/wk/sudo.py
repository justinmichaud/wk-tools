"""wk key sudo: the sudoers verdict, drop-in write and fan-out, through the Machine passed in so a Fake proves it without a real sudo or visudo."""

import os
import re
import sys

from wk import act, record, status
from wk.act import die, log

DROPIN_FMT = "/etc/sudoers.d/zz-%s-passwd"
DEFAULT_TIMEOUT_MIN = "0.5"
# A drop-in that out-ranks one of these three sudoers-allowlisted helpers costs 'wk quiesce'/'wk session' a password too.
PRIV_HELPERS = ("/usr/local/libexec/wk-quiesce-priv", "/usr/local/libexec/wk-card-priv",
                "/usr/local/libexec/wk-boot-priv")

_RULE_LINE = re.compile(r'^\s*\(.*\)')
_BLANKET = re.compile(r'(^|\s)(NO)?PASSWD:\s*ALL\s*$|\)\s*ALL\s*$')
_TIMEOUT = re.compile(r'timestamp_timeout=([0-9.-]*)')

SUDOERS_BODY = (
    "# Installed by 'wk key sudo setup'. Do not edit; re-run the command instead.\n"
    "#\n"
    "# Sorted after the site's own drop-ins on purpose: sudoers takes the LAST\n"
    "# match, so this re-imposes a password over a NOPASSWD granted elsewhere\n"
    "# without modifying a file somebody else owns.\n"
    "Defaults:%s timestamp_timeout=%s\n"
    "%s ALL=(ALL:ALL) PASSWD: ALL\n"
)


def timeout_desc(minutes):
    return "%g seconds" % (float(minutes) * 60)


def timeout_secs(minutes):
    return "%g" % (float(minutes) * 60)


def timeout_is_ours(value, wanted):
    if not value:
        return False
    try:
        return float(value) == float(wanted)
    except ValueError:
        return False


def _rule_lines(out):
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if "may run the following" in line:
            return [l for l in lines[i:] if _RULE_LINE.match(l)]
    return []


# sudoers takes the LAST matching rule, so only rules[-1] decides whether a blanket ALL is granted.
def _blanket(rules):
    matches = [l for l in rules if _BLANKET.search(l)]
    if not matches:
        return "none"
    return "nopasswd" if "NOPASSWD:" in matches[-1] else "passwd"


def _timeout_value(out):
    m = _TIMEOUT.search(out)  # [0-9.-] as the digit class: a bare [0-9-] would read '0.5' as '0', a different policy.
    return m.group(1) if m and m.group(1) else "unset"


class Sudo:
    def __init__(self, machine, env=None, linux=None):
        self.machine = machine
        self.env = os.environ if env is None else env
        self.timeout_min = self.env.get("WK_SUDO_TIMEOUT_MIN") or DEFAULT_TIMEOUT_MIN
        self.timeout_desc = timeout_desc(self.timeout_min)
        self._linux = linux

    def is_linux(self):
        if self._linux is None:
            from wk.machine import is_linux
            self._linux = is_linux()
        return self._linux

    def user_name(self):
        return self.machine.run(["id", "-un"]).out.strip()

    def dropin(self):
        return DROPIN_FMT % self.user_name()

    # free/readable/blanket/scoped/timeout: `sudo -n` never prompts, so this never blocks on a password.
    def probe(self):
        d = {"free": "yes" if self.machine.run(["sudo", "-n", "true"]).ok else "no"}
        r = self.machine.run(["sudo", "-n", "-l"])
        if not r.ok:
            d["readable"] = "no"
            return d
        d["readable"] = "yes"
        rules = _rule_lines(r.out)
        d["blanket"] = _blanket(rules)
        d["scoped"] = sum(1 for l in rules if "NOPASSWD:" in l)
        d["timeout"] = _timeout_value(r.out)
        return d

    def verdict(self):
        p = self.probe()
        dropin = self.dropin()

        if p["free"] == "yes":
            if p.get("blanket") == "nopasswd":
                return 1, "NOPASSWD: ALL is granted here -- root costs nothing at all"
            return 1, "sudo runs without a password right now -- a timestamp is cached"

        if p["readable"] == "no":
            prefix = ("a password is required right now (no timestamp cached).\n"
                      "  the timeout cannot be read without authenticating -- ")
            if self.machine.exists(dropin):
                return 0, prefix + "but %s is installed" % dropin
            return 1, prefix + "and %s is not installed, so it is sudo's default (a few minutes)" % dropin

        timeout = p["timeout"]
        if timeout_is_ours(timeout, self.timeout_min):
            msg = "a password is required, and the timestamp lasts %s" % self.timeout_desc
            if self.machine.exists(dropin):
                msg += " (%s)" % dropin
            return 0, msg

        if timeout in ("0", "0.0", ".0"):
            msg = ("a password is required, and no timestamp is kept at all (timeout 0)"
                   " -- stricter than the %s this installs, so nothing to do" % self.timeout_desc)
            if self.machine.exists(dropin):
                msg += " (%s)" % dropin
            return 0, msg

        if timeout == "unset":
            return 1, "a password is required, but sudo keeps a timestamp (timeout unset -- sudo's default)"

        return 1, ("a password is required, but sudo keeps a timestamp for %s minute(s) (%s seconds) -- wanted %s"
                    % (timeout, timeout_secs(timeout), self.timeout_desc))

    def _visudo_resolve(self):
        if not self.machine.run(["which", "visudo"]).ok:
            die("visudo is required (part of the sudo package) but not on PATH -- "
                "refusing to write a sudoers file that cannot be validated")

    def setup(self):
        self._visudo_resolve()
        rc, v = self.verdict()
        host = record.host_name(self.machine)
        if rc == 0:
            act.info("already set up on %s: %s" % (host, v))
            return 0

        user = self.user_name()
        dropin = self.dropin()
        tmp = "/tmp/wk-sudoers.%d" % os.getpid()
        self.machine.write(tmp, SUDOERS_BODY % (user, self.timeout_min, user))

        if not act.dry_run() and not self.machine.run(["visudo", "-c", "-f", tmp]).ok:
            self.machine.remove_now(tmp)
            die("the sudoers file wk generated does not parse -- refusing to install it")

        grp = "root" if self.is_linux() else "wheel"
        act.info("installing %s on %s -- sudo will ask for your password" % (dropin, host))
        act.log("  after this, every sudo on this machine asks. That is the point.")
        r = self.machine.act_run(["sudo", "install", "-m", "0440", "-o", "root", "-g", grp, tmp, dropin])
        self.machine.remove(tmp)
        if not r.ok:
            die("could not install %s" % dropin)

        r = self.machine.act_run(["sudo", "visudo", "-c"])
        if not r.ok:
            act.warn("sudoers does not parse after the install -- removing what we added")
            self.machine.act_run(["sudo", "rm", "-f", dropin])
            die("sudoers was left as it was; nothing changed")

        self.machine.act_run(["sudo", "-k"])
        if act.dry_run():
            return 0

        if self.machine.run(["sudo", "-n", "true"]).ok:
            act.warn("sudo still runs without a password after installing %s" % dropin)
            act.log("  something later in the include order is granting NOPASSWD -- 'sudo -l' shows the order")
            return 1
        act.info("sudo on %s now asks for a password, and forgets after %s" % (host, self.timeout_desc))

        broke = [h for h in PRIV_HELPERS
                 if self.machine.exists(h) and not self.machine.run(["sudo", "-n", h, "status"]).ok]
        if broke:
            act.warn("the drop-in is in force, but it now out-ranks a privileged helper:%s"
                     % "".join(" " + h for h in broke))
            act.log("  those grants exist so 'wk quiesce' and 'wk session' need no password.")
            act.log("  './setup --stage quiesce' reinstalls their rules under names that sort last.")
            return 1
        return 0


def machine_answers(target, name):
    """One probe: whether `target` answers at all, printing the fan-out row when it does not."""
    side, why = target.probe()
    if side == "answering":
        return True
    sys.stdout.write("%-22s %s\n" % (name, status.far_side_reason(target, side, why)))
    return False


def wk_tty(target, *args, env=None):
    """With a terminal, for far-side commands that prompt a human -- 'wk key sudo setup' over ssh -t."""
    env = os.environ if env is None else env
    if hasattr(target, "wk_cmd"):
        return target.machine.run_tty(["sh", "-c", target.wk_cmd(list(args), env)]).rc
    rc, out = target.wk(*args, env=env)
    sys.stderr.write(out)
    return rc


def on_target(reg, action, name, env):
    try:
        target = reg.load(name)
    except LookupError as e:
        die(str(e))
    if not machine_answers(target, name):
        return 1
    if action == "setup":
        return wk_tty(target, "key", "sudo", "setup", env=env)
    rc, out = target.wk("key", "sudo", "status", "--quiet", env=env)
    sys.stdout.write("%-22s %s\n" % (name, out.strip()))
    if rc != 0:
        log("  fix: wk key sudo setup --target %s" % name)
    return rc


def all_machines(reg, action, env):
    """This machine's own status whatever `action` is (only a bare or --target setup sets a machine up), then `action`
    on every other one."""
    rc, v = Sudo(reg.machine, env).verdict()
    sys.stdout.write("%-22s %s\n" % (record.host_name(reg.machine), v))
    if rc != 0:
        log("  fix: wk key sudo setup")
    worst = rc
    for name in reg.machines():
        worst = max(worst, on_target(reg, action, name, env))
    return worst


def status_here(sudo, env):
    rc, v = sudo.verdict()
    if env.get("WK_QUIET"):
        sys.stdout.write(v + "\n")
        return rc
    sys.stdout.write("%-22s %s\n" % (record.host_name(sudo.machine), v))
    if rc != 0:
        log("  fix: wk key sudo setup")
        log("  every other machine: wk key sudo status --all")
    return rc


def main(words, target, all_flag, reg, env=None):
    """`wk key sudo [status|setup] [--target <t>|--all]`: `words` are the positionals after `sudo`, `target` is None
    when --target was not given."""
    env = os.environ if env is None else env
    if reg.in_workspace():
        die("'wk key sudo' hardens a machine you log into, and this is workspace\n"
            "    '%s' -- a container's sudoers belong to the image and the\n"
            "    workspace is the blast radius anyway. Run it on the host." % env.get("WK_NAME", ""))
    action = words[0] if words else "status"
    if action not in ("status", "setup"):
        die("'%s' is not a verb of wk key sudo: status or setup; see wk key -h" % action)
    if target == "":
        die("--target needs a name")
    if target:
        return on_target(reg, action, target, env)
    if all_flag:
        return all_machines(reg, action, env)
    sudo = Sudo(reg.machine, env)
    return sudo.setup() if action == "setup" else status_here(sudo, env)
