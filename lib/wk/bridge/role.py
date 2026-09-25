"""`wk machine setup|tailnet|rm <bridge>`: this end renders the role (wk.bridge.plan) and ships it with
bridge/'s scripts, bridge/provision.sh applies it on the phone, and the tailnet join and the policy are
this end's again. `setup --disk` writes the phone's system first (wk.bridge.provision)."""

import os
import sys

from wk import act, tailnet
from wk.act import die, info, log, warn
from wk.bridge import ACCEPT_NEW, HEALTHCHECK, Bridge, NeedsPassword, Unreachable, devices, judge, kv, render
from wk.bridge import plan as plans
from wk.bridge import provision
from wk.bridge.plan import AUTHKEY, LIB
from wk.clock import Clock
from wk.machine import Machine, Ssh
from wk.shell import sh_quote

SHIP = "rm -rf %s && mkdir -p %s && base64 -d | tar xzf - -C %s" % (LIB, LIB, LIB)
# A password reader flushes the input pending when it starts, so the password reaches the ssh after a pause.
PAUSED = '(sleep 3; cat; sleep 3) | "$@"'
TAILSCALED_WAIT = 30
DEPROVISION = """set -u
for s in %(services)s; do
    [ -x "/etc/init.d/$s" ] || continue
    rc-service "$s" stop >/dev/null 2>&1 || true
    rc-update del "$s" default >/dev/null 2>&1 || true
    rm -f "/etc/init.d/$s"
    printf 'removed %%s\\n' "$s"
done
nft delete table inet wkbridge 2>/dev/null || true
rm -f %(paths)s /usr/local/sbin/wk-bridge-*
rm -rf %(lib)s
nmcli connection reload >/dev/null 2>&1 || true
rm -f %(sshd)s
if sshd -t 2>/dev/null; then
    rc-service sshd reload >/dev/null 2>&1 || true
else
    printf 'WARNING: sshd -t fails; NOT reloading sshd. Fix /etc/ssh/sshd_config by hand.\\n' >&2
fi
tailscale logout >/dev/null 2>&1 || printf 'note: tailscale logout failed (already logged out?)\\n'
printf 'the bridge role is gone; postmarketOS and the packages are untouched\\n'
"""
SSHD_DROPIN = "/etc/ssh/sshd_config.d/10-wk-bridge.conf"
POLICY = """
Remaining manual step -- the tailnet policy, in the admin console.

  "tagOwners": { "%(tag)s": ["autogroup:admin"] },

  "autoApprovers": {
    "routes": { "%(segment)s": ["%(tag)s"] }
  },

  "grants": [
    { "src": [%(src)s],
      "dst": ["%(segment)s"], "ip": ["*"] }
  ]

autoApprovers is what makes the route live without a console click; without it
the node is up, the segment is advertised, and nothing behind it is reachable.

The grant is the real access control for everything behind this bridge -- the
devices on the segment have none of their own worth relying on (a BMC reverts
to factory credentials on a power loss). Grant %(tag)s itself nothing: it
routes, it never initiates, and root on it is equivalent to whatever is behind
it.
"""


class Paused(Machine):
    """The `via` of an ssh whose far side reads a password from its tty: stdin reaches the ssh after a pause."""

    def __init__(self, via):
        self.via, self.name = via, via.name

    def run(self, argv, input=None, timeout=None):
        return self.via.run(["sh", "-c", PAUSED, "sh"] + list(argv), input=input, timeout=timeout)


class Role:
    def __init__(self, root, env=None, here=None, phones=None, clock=None, transport=tailnet.urllib_transport):
        self.b = Bridge(root, env, here, phones)
        self.root, self.env, self.here = self.b.root, self.b.env, self.b.machine
        self.clock = clock or Clock()
        self.transport = transport

    def conf(self, name):
        try:
            bc = self.b.conf(name)
        except LookupError as e:
            die(str(e))
        path = self.b.fleet.path(name)
        for key in ("BR_DEVICE", "BR_SEGMENT", "BR_ROUTER"):
            if not bc.conf.get(key):
                die("%s sets no %s" % (path, key))
        known = devices(self.root)
        if bc.device not in known:
            die("%s names device '%s', which bridge/devices.tsv has never heard of: %s"
                % (path, bc.device, ", ".join(sorted(known))))
        return (bc,) + known[bc.device]

    def password(self, name):
        """PMO_PASSWORD is not a secret: pmbootstrap handles it in plain text, and it is the phone's console login."""
        _, p = provision.bridge_profile(name, dict(self.env, WK_ROOT=self.root))
        return p["PMO_PASSWORD"] if p else ""

    def bootstrap_root(self, bc, dest):
        pw = self.password(bc.name)
        if not pw:
            return None
        info("giving root the same ssh key, so provisioning needs no password")
        log("  pmbootstrap installs the key for '%s' only; this uses PMO_PASSWORD from its image profile." % bc.user())
        keys = "/home/%s/.ssh/authorized_keys" % bc.user()
        install = "install -d -o root -g root -m 700 /root/.ssh && install -o root -g root -m 600 %s /root/.ssh/authorized_keys" % keys
        # -tt: doas reads its password from a tty.
        Ssh(dest, opts=["-tt"] + ACCEPT_NEW, timeout=15, via=Paused(self.here)).act_run(["doas", "sh", "-c", install],
                                                                                       input=pw + "\n", timeout=60)
        root = "root@" + dest.split("@")[-1]
        if not self.b.reaches(root):
            return None
        info("root on %s now accepts the same key" % root.split("@")[1])
        return root

    def connect(self, bc, at):
        try:
            dest = self.b.resolve(bc.name, at=at)
            phone = self.b.phone(dest)
            return dest, phone, self.b.priv_prefix(bc.name, phone, dest)
        except NeedsPassword:
            root = self.bootstrap_root(bc, dest)
            if root:
                return root, self.b.phone(root), []
            die("root on %s could not be given the key, and its doas needs a password.\n"
                "    By hand on the phone, once:\n"
                "        doas install -d -o root -g root -m 700 /root/.ssh\n"
                "        doas install -o root -g root -m 600 ~/.ssh/authorized_keys /root/.ssh/authorized_keys" % dest)
        except Unreachable as e:
            die(str(e))

    def act(self, phone, argv, what, **kw):
        r = phone.act_run(argv, **kw)
        sys.stderr.write(r.out + r.err)
        if not r.ok:
            die(what)
        return r

    def ship(self, phone, prefix, plan, dest):
        self.act(phone, prefix + ["sh", "-c", SHIP], "could not copy the role to %s" % dest, input=plans.bundle(self.root, plan))

    # -- setup and tailnet

    def setup(self, name, at=None, no_tailnet=False, disk=None, image=None, rebuild=False):
        bc, note, kill = self.conf(name)
        if (image or rebuild) and not disk:
            die("--image and --rebuild say what to write, and only --disk writes: add --disk <machine>:<device>")
        if image and rebuild:
            die("--rebuild and --image contradict each other: one builds a new image, the other names a file")
        c = bc.conf
        info("%s -- %s, %s via %s" % (name, note, bc.segment, bc.iface))
        log("  tailnet:  %s, %s, advertising %s" % (bc.hostname(), c["BR_TAG"], bc.segment))
        log("  egress:   %s, camera: %s, battery cap %s%%" % (bc.egress, bc.camera, c["BR_BATTERY_LIMIT"]))
        if not disk:
            act.nothing_to_ask()
            return self.apply(bc, kill, self.connect(bc, at), no_tailnet)
        w = provision.Write(self)
        if not w.run(bc, kill, disk, image, rebuild):
            return 0
        w.wait(bc, at)
        return self.apply(bc, kill, self.connect(bc, at), no_tailnet)

    def tailnet(self, name, at=None):
        bc, _note, kill = self.conf(name)
        act.nothing_to_ask()
        dest, phone, prefix = there = self.connect(bc, at)
        if not phone.run(prefix + ["test", "-f", "/etc/wk-bridge.conf"], timeout=15).ok:
            die("%s has no bridge role on it yet.\n"
                "    'wk machine setup %s --no-tailnet' applies everything else first; this is only the tailnet half." % (dest, name))
        return self.apply(bc, kill, there, False)

    def apply(self, bc, kill, there, no_tailnet):
        dest, phone, prefix = there
        if not phone.run(["sh", "-c", "command -v apk"], timeout=15).ok:
            die("%s is not running postmarketOS (no apk); the bridge role is pmOS-only. Reflash the phone first -- 'wk help'." % dest)
        warn("power the phone from a wall charger, never from the machine it exists to rescue.")
        warn("kill switches: WiFi on. %s" % kill)
        info("copying the role to %s:%s" % (dest, LIB))
        self.ship(phone, prefix, plans.Plan(bc.name, bc.conf), dest)
        self.act(phone, prefix + ["sh", LIB + "/provision.sh", "base"], "provisioning failed on %s; re-run it" % dest, timeout=1800)
        facts = kv(phone.run(prefix + ["sh", "-s"], input=plans.FACTS, timeout=60).out)
        try:
            plan = plans.Plan(bc.name, bc.conf, facts)
        except LookupError as e:
            die("%s: %s" % (dest, e))
        for w in plan.warnings:
            warn(w)
        self.ship(phone, prefix, plan, dest)
        self.act(phone, prefix + ["sh", LIB + "/provision.sh", "role"], "provisioning failed on %s; re-run it" % dest, timeout=600)
        joined = self.join(bc, phone, prefix, no_tailnet)
        if joined:
            src = ", ".join('"%s"' % s.strip() for s in bc.conf.get("BR_REACHED_BY", "").split(",") if s.strip())
            sys.stderr.write(POLICY % {"tag": bc.conf["BR_TAG"], "segment": bc.segment, "src": src})
        info("health check")
        report = judge(kv(phone.run(prefix + [HEALTHCHECK], timeout=30).out), bc)
        render(report, sys.stdout)
        if not joined:
            info("%s has the whole role except the tailnet; 'wk machine tailnet %s' finishes it" % (bc.name, bc.name))
        return 0

    def tailscale_up(self, phone, prefix):
        r = phone.run(prefix + ["tailscale", "status"], timeout=15)
        return r.ok or "Logged out" in r.out + r.err or "NeedsLogin" in r.out + r.err

    def approved(self, phone, prefix, bc):
        return bc.segment in ((self.b.ts_status(phone, prefix).get("Self") or {}).get("PrimaryRoutes") or [])

    def join(self, bc, phone, prefix, no_tailnet):
        """True once the node is on the tailnet, tagged and advertising the segment."""
        self.clock.wait_until(lambda: self.tailscale_up(phone, prefix), TAILSCALED_WAIT, 1)
        ts = self.b.ts_status(phone, prefix)
        seg, tag = bc.segment, bc.conf["BR_TAG"]
        if ts.get("BackendState") == "Running" and (ts.get("Self") or {}).get("Tags"):
            info("already on the tailnet, tags %s" % ",".join(ts["Self"]["Tags"]))
            if not phone.act_run(prefix + ["tailscale", "set", "--advertise-routes=" + seg, "--accept-dns=false", "--ssh=true"]).ok:
                warn("could not re-assert the advertised route")
            phone.act_run(prefix + ["rm", "-f", AUTHKEY])
            if not self.approved(phone, prefix, bc):
                self.readvertise(bc, phone, prefix)
            return True
        if no_tailnet:
            info("--no-tailnet: the role is applied and the node has not joined the tailnet")
            return False
        key = self.authkey()
        if not key:
            warn("%s is not on the tailnet and there is no auth key here to join it with ('wk key set tailnet')" % bc.name)
            return False
        self.act(phone, prefix + ["sh", "-c", "umask 077; cat > " + AUTHKEY], "could not hand the auth key over", input=key + "\n")
        # --advertise-tags is settable only at login, and a tagged node never key-expires.
        r = phone.act_run(prefix + ["tailscale", "up", "--auth-key=file:" + AUTHKEY, "--advertise-routes=" + seg,
                                    "--advertise-tags=" + tag, "--hostname=" + bc.hostname(), "--accept-dns=false",
                                    "--accept-routes=false", "--ssh=true"], timeout=120)
        phone.act_run(prefix + ["rm", "-f", AUTHKEY])
        if not r.ok:
            die("tailscale up failed on the phone: %s" % (r.err.strip() or r.out.strip()))
        info("joined the tailnet as %s (%s)" % (bc.hostname(), tag))
        return True

    def readvertise(self, bc, phone, prefix):
        """autoApprovers is evaluated only when a route is advertised anew, so an unapproved one is withdrawn and re-advertised."""
        info("the route is advertised but not approved -- re-advertising so autoApprovers is evaluated")
        phone.act_run(prefix + ["tailscale", "set", "--advertise-routes="])
        self.clock.sleep(2)
        phone.act_run(prefix + ["tailscale", "set", "--advertise-routes=" + bc.segment])
        self.clock.sleep(5)
        if self.approved(phone, prefix, bc):
            info("approved: %s is live" % bc.segment)
        else:
            warn("still not approved: the policy below is not in place yet, or its autoApprovers names another tag")

    def authkey(self):
        return tailnet.Fleet(self.root, self.env, self.here, self.transport).key()

    # -- rm

    def deprovision(self, bc):
        paths = [p for p in plans.every_path(bc.name, bc.conf) if p != SSHD_DROPIN]
        services = sorted(os.listdir(os.path.join(self.root, "bridge", "init.d")))
        return DEPROVISION % {"services": " ".join(services), "paths": sh_quote(*paths), "lib": LIB, "sshd": SSHD_DROPIN}

    def rm(self, name, at=None):
        bc, _note, _kill = self.conf(name)
        log("this removes the bridge role from %s: its services, every file setup rendered, /usr/local/lib/wk-bridge,\n"
            "  and its tailnet login, so it stops advertising %s. postmarketOS, the packages and your ssh keys stay." % (name, bc.segment))
        if not act.confirm("remove the bridge role from %s?" % name):
            die("aborted -- nothing was changed")
        dest, phone, prefix = self.connect(bc, at)
        self.act(phone, prefix + ["sh", "-s"], "deprovisioning did not complete -- re-run it", input=self.deprovision(bc), timeout=120)
        info("removed the bridge role from %s" % dest)
        log("  the node is logged out but still listed in the admin console; delete it there if the phone is not coming back.")
        return 0
