"""wk session. The kernel driver tells the GPU from the BMC's `ast` chip, never the card number: card1 is
the ast on moose only because of PCI enumeration order."""

import os
import re
import sys

from wk import act
from wk.act import die, info, log, warn
from wk.quiet import COMMON, PRIV, lib_argv, said

SYS_DRM = "/sys/class/drm"
UNIT = "wk-session"
CARD = re.compile(r"^card[0-9]+$")
OUTPUT = re.compile(r"^\s*name: ([A-Za-z][A-Za-z0-9]*-[0-9]+)$", re.M)
MODE = re.compile(r"width: ([0-9]*) px, height: ([0-9]*) px, refresh: ([0-9.]*) Hz")


class Session:
    def __init__(self, root, machine, clock, env, uid=None):
        self.root, self.m, self.clock, self.env = str(root), machine, clock, env
        run = env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % (os.getuid() if uid is None else uid)
        self.socket = os.path.join(run, "wk", "display", "wayland-0")
        self.sudo = None

    def priv(self, verb):
        """Whether sudo needs a password is asked once, with a read-only verb, because session-on-bmc may fail."""
        if not self.m.exists(PRIV):
            die("%s is not installed -- run: ./setup --stage quiesce" % PRIV)
        if self.sudo is None:
            self.sudo = ["sudo", "-n"] if self.m.run(["sudo", "-n", PRIV, "session-status"]).ok else ["sudo"]
            if self.sudo == ["sudo"]:
                warn("passwordless sudo unavailable for the helper; re-run ./setup --stage quiesce")
        return said(self.m.act_run([*self.sudo, PRIV, verb])).ok

    def _text(self, path, default=""):
        try:
            return self.m.read(path).strip()
        except OSError:
            return default

    def driver(self, card):
        link = self.m.readlink("%s/%s/device/driver" % (SYS_DRM, card))
        return os.path.basename(link.rstrip("/")) if link else "none"

    def _conns(self, which):
        try:
            names = self.m.listdir(SYS_DRM)
        except OSError:
            return []
        out = []
        for card in (n for n in names if CARD.match(n)):
            drv = self.driver(card)
            if (which == "ast" and drv != "ast") or (which == "not-ast" and drv in ("ast", "none")):
                continue
            out += [("%s/%s" % (SYS_DRM, n), n[len(card) + 1:]) for n in names if n.startswith(card + "-")]
        return out

    def connectors(self, which):
        return ["%s %s" % (name, self._text(path + "/status", "unknown")) for path, name in self._conns(which)]

    def lit(self, which="all"):
        """Connected connectors still being driven: `dpms` alone reads On for a CRTC never enabled."""
        return [name for path, name in self._conns(which)
                if self._text(path + "/status") == "connected" and self._text(path + "/enabled") == "enabled"
                and self._text(path + "/dpms") == "On"]

    def _wayland_info(self):
        return self.m.run(["env", "WAYLAND_DISPLAY=" + self.socket, "wayland-info"]).out

    def outputs(self):
        """wlroots may drop a device it cannot render for, silently, so this is the only word on what it took."""
        return OUTPUT.findall(self._wayland_info())

    def display_mode(self):
        m = MODE.search(self._wayland_info())
        return "%sx%s @ %sHz" % m.groups() if m else ""

    def have(self, tool):
        return self.m.run(["sh", "-c", 'command -v "$1"', "sh", tool]).ok

    def active(self, unit):
        return self.m.run(["systemctl", "is-active", "--quiet", unit]).ok

    def state(self, unit):
        return (self.m.run(["systemctl", "is-active", unit]).out.splitlines() or [""])[0].strip()

    def mode(self):
        return self.m.run(lib_argv(self.root, COMMON, "session_mode")).out.strip() or "none"

    def mode_warn(self):
        sys.stderr.write(self.m.run(lib_argv(self.root, COMMON, "session_mode_warn")).err)

    def _sessions(self, on_seat0):
        if not self.have("loginctl"):
            return []
        rows = [line.split() for line in self.m.run(["loginctl", "list-sessions", "--no-legend"]).out.splitlines()]
        return [r[0] for r in rows if r and on_seat0(r)]

    def _prop(self, sid, prop):
        return self.m.run(["loginctl", "show-session", sid, "-p", prop, "--value"]).out.strip()

    def greeter(self, tries=40):
        for i in range(tries):
            for sid in self._sessions(lambda r: len(r) > 3 and r[3] == "seat0"):
                typ = self._prop(sid, "Type") if self._prop(sid, "Class") == "greeter" else ""
                if typ:
                    return typ
            if i + 1 < tries:
                self.clock.sleep(0.25)
        return ""

    def leader(self):   # the unit `exec`s cage, so its MainPID is the session leader
        pid = self.m.run(["systemctl", "show", UNIT, "-p", "MainPID", "--value"]).out.strip()
        return "" if pid in ("", "0") else pid

    def foreign(self):
        """Somebody's desktop on seat0: Class matters as much as Type, since the greeter is itself a graphical session."""
        ours = self.leader()
        for sid in self._sessions(lambda r: "seat0" in r):
            typ, cls = self._prop(sid, "Type"), self._prop(sid, "Class")
            if cls != "user" or (ours and self._prop(sid, "Leader") == ours):
                continue
            if typ in ("wayland", "x11"):
                return "%s %s" % (sid, typ)
        return ""

    def on(self, bmc):
        if self.m.exists(self.socket) and self.active(UNIT):
            # "already running" answered for a software session is an afternoon of numbers that measured llvmpipe.
            wanted, running = ("bmc" if bmc else "gpu"), self.mode()
            if wanted == running:
                info("wk session already running (%s, mode %s)" % (self.socket, running))
                self.mode_warn()
                return 0
            info("a '%s' session is running; restarting it as '%s'" % (running, wanted))
            self.priv("session-stop")
        elif self.m.exists(self.socket):
            warn("stale compositor socket at %s -- starting a new session" % self.socket)

        seated = self.foreign()
        if seated:
            info("a graphical session is already active on seat0 (session %s)" % seated)
            log("  leaving it alone; run 'wk session off' first if you want a clean")
            log("  benchmark compositor instead of a full desktop")
            return 0
        if not self.have("wayland-info"):
            die("wayland-info missing -- a session nobody can verify is real is not started; "
                "./setup --stage tools installs wayland-utils")

        if bmc:
            log("  the monitor goes dark: the session moves to the BMC, it is not duplicated")
            info("starting a BMC session on seat0 (software-rendered)")
        else:
            info("starting the benchmark compositor on seat0 (GPU only, BMC excluded)")
        if not self.priv("session-on-bmc" if bmc else "session-on"):
            die("the compositor did not start; journalctl -u wk-session")
        if act.dry_run():
            return 0
        if not self.m.exists(self.socket):
            die("no Wayland socket at %s after start" % self.socket)
        outs = self.outputs()
        # The compositor coming up is not proof it took the BMC's output, and a blank remote console is the failure this cannot miss.
        if bmc and not set(outs) & {c.split()[0] for c in self.connectors("ast")}:
            warn("the session is up but not on a BMC connector (outputs: %s)" % (" ".join(outs) or "none"))

        info("session up: %s (mode %s)" % (self.socket, self.mode()))
        log("  outputs: %s  display: %s" % (" ".join(outs), self.display_mode()))
        log("  workspaces see it at /run/wk/display/wayland-0")
        log("  launch a browser in it with:  wk gui <workspace> [url]")
        if bmc:
            log("  watch it on the BMC's KVM-over-IP console")
        self.mode_warn()
        return 0

    def gdm(self, bmc):
        seated = self.foreign()
        if seated:
            info("a graphical session is already active on seat0 (session %s)" % seated)
            log("  leaving it alone; run 'wk session off' first if you want a clean start")
            return 0
        if self.active(UNIT):
            info("stopping the benchmark compositor to start a desktop instead")
        if bmc:
            log("  the monitor goes dark: the GPU is hidden from seat0, not just idle")
            info("starting gdm on seat0 (BMC only, software-rendered)")
        else:
            info("starting gdm on seat0 (GPU only, the BMC's chip hidden from seat0)")
        if not self.priv("session-gdm-bmc" if bmc else "session-gdm"):
            die("gdm did not start; journalctl -u gdm")
        if act.dry_run():
            return 0

        # Enforced only for a greeter taking its devices from logind: NVIDIA's Xorg driver uses /dev/nvidia*, which has no seat tags.
        greeter = self.greeter()
        if greeter == "wayland":
            info("gdm is up on seat0 (wayland greeter) -- log in at the console")
        elif not greeter:
            warn("gdm was started but no greeter session appeared on seat0")
            log("  journalctl -u gdm")
        else:
            warn("the greeter came up on %s, not wayland -- the mode is not enforced" % greeter)
            log("  hiding a device from seat0 constrains a compositor that asks logind for\n"
                "  its devices. NVIDIA's Xorg driver doesn't: it drives the card through\n"
                "  /dev/nvidia0 and /dev/nvidia-modeset, character devices with no udev\n"
                "  properties and no seat tags for the hide to remove. So the desktop is\n"
                "  wherever that driver put it, whichever mode was asked for.\n"
                "  gdm chose Xorg via 61-gdm.rules (nvidia_drm + modeset=Y falls through\n"
                "  to gdm_prefer_xorg); wk overrides it, so this means the override\n"
                "  didn't take -- check: sudo %s session-status" % PRIV)
        if bmc:
            log("  or over the BMC's KVM-over-IP console")
        return 0

    def off(self):
        if not self.priv("session-off"):
            die("the helper failed to turn the screen off")
        if act.dry_run():
            return 0
        lit = self.lit("not-ast")
        if lit:
            warn("the screen is black but still lit: %s" % " ".join(lit))
            log("  a compositor holding an output and painting it black is not an\n"
                "  output that is off -- the CRTC keeps scanning out, so the monitor\n"
                "  keeps its signal. The modeset that darkens it needs wlr-randr:\n"
                "    ./setup --stage tools   (then: wk session off)")
        else:
            info("screen off -- outputs modeset off, placeholder compositor holding the seat")
            log("  both halves are load-bearing: disabling the outputs is what darkens\n"
                "  the monitor, holding the device is what stops fbcon repainting the\n"
                "  console over the top of it")
        bmc_lit = self.lit("ast")
        if bmc_lit:
            log("  still lit on the BMC's chip: %s (its console keeps its last frame)" % " ".join(bmc_lit))
        log("  a desktop back:  wk session gdm      a benchmark session:  wk session on")
        return 0

    def status(self, out=None):
        out = out or sys.stdout
        live = self.m.exists(self.socket)
        priv = {w[0]: w[1] for w in (line.split() for line in self.m.run(["sudo", "-n", PRIV, "session-status"]).out.splitlines())
                if len(w) > 1}
        rows = [("session", self.state(UNIT) or "unknown"),
                ("socket", self.socket if live else "none"),
                ("dm", self.state("gdm3") or self.state("gdm") or "unknown"),
                ("modeset", priv.get("modeset:", "unknown")),
                ("mode", self.mode()),
                ("lit", " ".join(self.lit()) or "none"),
                ("gpu", " ".join(self.connectors("not-ast"))),
                ("bmc", " ".join(self.connectors("ast"))),
                ("desktop", priv.get("desktop:", "none"))]
        greeter = self.greeter(1)
        if greeter:
            rows.append(("greeter", greeter + ("" if greeter == "wayland" else "  (mode NOT enforced -- see gdm notes)")))
        if priv.get("seat-hide:"):
            rows.append(("seat-hide", priv["seat-hide:"] + " (hidden from seat0)"))
        if live:
            known = self.have("wayland-info")
            rows.append(("outputs", " ".join(self.outputs()) if known else "unknown (wayland-utils missing)"))
            # MotionMark scores scale with surface size, so runs on different display modes are not comparable.
            shown = self.display_mode() if known else "unknown (wayland-utils missing)"
            if shown:
                rows.append(("display", shown))
        else:
            seated = self.foreign()
            if seated:
                rows.append(("foreign", "session " + seated))
        for key, value in rows:
            out.write("%-10s %s\n" % (key + ":", value))
        self.mode_warn()
        return 0
