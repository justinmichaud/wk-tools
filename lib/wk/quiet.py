"""wk quiesce: the privileged helper, the daemons, the raiser, and the readings a measured Mac is judged by."""

import os
import sys

from wk import act, status
from wk.act import die, info, log, warn
from wk.machine import TIMED_OUT, is_macos
from wk.store import Store

PRIV = "/usr/local/libexec/wk-quiesce-priv"
DESKTOP = "bench/mac-quiet-desktop.sh"
HOSTS = "bench/mac-quiet-hosts.sh"
RAISER = "bench/mac-raiser.sh"
COMMON = "lib/common.sh"
READ_SECS = 20   # bench/mac-quiet-desktop.sh's _WK_QD_READ_SECS: a paused daemon answers no XPC request
SETTLE = 30
SETUP = "./setup --stage quiesce"
RENDER = '_wk_render() { render_findings <<<"$1"; }; _wk_render'


def lib_argv(root, rel, fn, *args):
    return ["bash", "-c", '. "$0"; %s "$@"' % fn, os.path.join(root, rel), *args]


def said(r):
    sys.stdout.write(r.out)
    sys.stderr.write(r.err)
    return r


def bench_mode(machine, env):
    return machine.exists(env.get("WK_IMAGE_MARKER") or "/etc/wk-image")


class Quiesce:
    def __init__(self, root, machine, clock, env, macos=None):
        self.root, self.m, self.clock, self.env = str(root), machine, clock, env
        self.macos = is_macos() if macos is None else macos
        self.state = status.quiesce_dir(Store(env))
        self.stale = os.path.join(env.get("TMPDIR") or "/tmp", "wk-quiesce")
        self.bench = bench_mode(machine, env)

    def _lib(self, rel, fn, *args):
        return lib_argv(self.root, rel, fn, *args)

    def _at(self, name):
        return os.path.join(self.state, name)

    def _priv_refusal(self, verb):
        """Two failures share the helper's exit status: a sudo that wanted a password, and a helper killed mid-verb."""
        if self.m.run(["sudo", "-n", "true"]).ok:
            return ("%s exited nonzero running '%s'. Passwordless sudo is\n"
                    "    in force, so this is the helper and not the grant -- its own output above says what\n"
                    "    it was doing." % (PRIV, verb))
        return "passwordless sudo is not set up for %s.\n    Remedy:  %s" % (PRIV, SETUP)

    def _not_installed(self):
        return ("%s is not installed, so the privileged half of quiesce\n    cannot run.\n    Remedy:  %s"
                % (PRIV, SETUP))

    def priv(self, verb):
        if not self.m.exists(PRIV):
            die(self._not_installed())
        if not said(self.m.act_run(["sudo", "-n", PRIV, verb])).ok:
            die(self._priv_refusal(verb))

    def priv_claim(self):
        if not self.m.exists(PRIV):
            return self._not_installed()
        r = self.m.run(["sudo", "-n", PRIV, "status"])
        text = (r.out + r.err).strip()
        return text if r.ok else "\n".join(t for t in (text, self._priv_refusal("status")) if t)

    def on(self):
        try:
            self._on()
        except KeyboardInterrupt:
            warn("interrupted -- undoing what 'quiesce on' already did")
            try:
                self.off()
            except act.Refused:
                pass
            raise
        return 0

    def _on(self):
        self.priv("on")
        self.m.mkdir(self.state)
        if self.macos:
            # SIGSTOP, so root, undone by `off` or a reboot; SIP withholds it for a platform binary, which the table names.
            if said(self.m.act_run(["sudo", "-n", *self._lib(DESKTOP, "wk_quiet_daemons_pause")])).ok:
                self.m.write(self._at("daemons_paused"), "")
                info("background daemons paused")
            else:
                warn("could not pause the background daemons: this needs passwordless root,\n"
                     "    which a benchmark install has and a workstation does not. 'wk quiesce status'\n"
                     "    names every one still running.")
            # A first boot's write to a protected domain does not survive the session starting (measured 2026-09-07); bench mode only.
            if self.bench and not said(self.m.act_run(self._lib(DESKTOP, "wk_quiet_desktop_user"))).ok:
                warn("some of this account's desktop settings did not take (above)")
            said(self.m.act_run(self._lib(RAISER, "mac_raiser_on", self.state)))
            info("measured, now that both halves have run:")
            if self.noise():
                warn("the machine is noisier than a benchmark install should be (above)")
        settle = int(self.env.get("WK_SETTLE_SECONDS") or SETTLE)
        if act.dry_run():
            sys.stderr.write("would settle for %ds\n" % settle)
        else:
            self.clock.sleep(settle)
        info("quiesced; settle time elapsed")

    def off(self):
        if self.macos and self.m.isdir(self.state):
            said(self.m.act_run(self._lib(RAISER, "mac_raiser_off", self.state)))
            if self.m.exists(self._at("daemons_paused")):
                if not said(self.m.act_run(["sudo", "-n", *self._lib(DESKTOP, "wk_quiet_daemons_resume")])).ok:
                    warn("could not resume the background daemons; a reboot does it")
                self.m.remove(self._at("daemons_paused"))
                info("background daemons resumed")
        self.priv("off")
        return 0

    def _running(self, pidfile):
        try:
            pid = int(self.m.read(self._at(pidfile)).strip())
        except (OSError, ValueError):
            return False
        return self.m.alive(pid)

    def status(self):
        claim = self.priv_claim()
        if claim:
            log("  the privileged half's own account:")
            log("\n".join("    " + line for line in claim.splitlines()))
        log("  measured here, now:")
        log("  caffeinate: running" if self._running("caffeinate.pid") else "  caffeinate: no")
        log("  daemons:    'quiesce on' paused them; each one's state is measured below"
            if self.m.exists(self._at("daemons_paused")) else "  daemons:    not paused")
        if self.m.isdir(self.stale):
            log("  stale: %s (an older wk-tools' state; not read -- rm -rf it)" % self.stale)
        if self.macos:
            log("  raiser:     running (MiniBrowser kept frontmost)" if self._running("raiser.pid") else "  raiser:     no")
            nap = self.m.run(["defaults", "read", "org.webkit.MiniBrowser", "NSAppSleepDisabled"]).out.strip()
            log("  app nap:    disabled for MiniBrowser" if nap == "1"
                else "  app nap:    default (rAF can be throttled when backgrounded)")
            self.noise()
        return 0

    def _findings(self, fn, *args):
        return self.m.run(self._lib(DESKTOP, fn, *args)).out

    def render(self, text):
        if not text.strip():
            return 0
        return self.m.run_tty(self._lib(COMMON, RENDER, text)).rc

    def noise(self):
        """Only the clock is judged on a workstation: the rest is what a bench install is and a workstation never will be."""
        probe = self._findings("wk_quiet_desktop_probe")
        bad = self.render(self._findings("wk_quiet_cpu_findings", probe))
        if self.bench:
            bad += self.render(self._findings("wk_quiet_desktop_findings", probe, "wk sysimage build perf-macos-tolken --provision")
                               + self._findings("wk_quiet_daemons_findings", probe, "wk quiesce on"))
        bad += self._timemachine() + self._updates()
        if self.bench:
            if self.m.run(self._lib(HOSTS, "wk_bench_hosts_present", "/etc/hosts")).ok:
                log("  hosts:      update endpoints denied")
            else:
                warn("  hosts:      NOT denied -- wk sysimage build perf-macos-tolken --provision")
                bad += 1
        return bad

    def _timemachine(self):
        r = self.m.run(["tmutil", "destinationinfo"], timeout=READ_SECS)
        first = ((r.out + r.err).strip().splitlines() or [""])[0]   # "No destinations configured" is said on stderr
        if r.rc == TIMED_OUT:
            warn("  timemachine: backupd did not answer inside its bound (this preflight holds it stopped), "
                 "so whether a backup can start mid-run is unknown")
        elif "No destinations" in first:
            log("  timemachine: no destination configured")
        else:
            warn("  timemachine: a destination is configured; a backup can start mid-run")
            return 1
        return 0

    def _updates(self):
        # Not `softwareupdate --schedule`, which says "on" with AutomaticCheckEnabled 0 in the same plist.
        r = self.m.run(["sudo", "-n", "defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate",
                        "AutomaticCheckEnabled"], timeout=READ_SECS)
        v = r.out.strip()
        if r.rc == TIMED_OUT:
            warn("  updates:    softwareupdated did not answer inside its bound (this preflight holds it stopped), "
                 "so whether automatic checking is on is unknown")
        elif v == "0":
            log("  updates:    automatic checking off")
        elif v == "1":
            warn("  updates:    automatic checking is on")
            return 1
        else:
            log("  updates:    AutomaticCheckEnabled unset, where softwareupdated leaves it -- a scan is stopped "
                "by the endpoint denial and the paused scanner, not by this key")
        return 0

