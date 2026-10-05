"""The benchmark install's tailnet identity, host half: tailscaled built from pinned source for darwin/arm64,
collected with this machine's auth key and the node's remembered state, and installed onto a volume root.
`collect` needs a network and credentials and no root; `install` needs root and neither, so the bench install
can install a collected payload onto itself. The join runs on the install (bench/mac-tailnet.sh)."""

import argparse
import os
import plistlib
import re
import sys

from wk import act, fleet, images, tailnet
from wk.act import Refused, die, info
from wk.store import Store
from wk.sysimage import task

# No darwin tailscaled is published, and the packaged clients tunnel through NetworkExtension, whose consent panel
# only a person answers. The tailscale version and its source checksum are REL's.
GO = {"version": "1.26.8",
      "darwin_arm64": "a012b25b571bd0138a03dcd25375ceba866fe5ca822f426d2c66a4de56fd3f4b",
      "linux_arm64": "211ffced9dcb9633a55eac6364816ec0ddd951389a740e88fa8b3337971bdda0"}
REL = "image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"
MACHO_ARM64 = "cf fa ed fe 0c 00 00 01"   # MH_MAGIC_64 and CPU_TYPE_ARM64, little-endian

TS_BIN = "/usr/local/bin"
TS_STATE_DIR = "/var/db/wk/tailscale"
TS_KEY = "/etc/wk/tailscale-authkey"
TS_CONF = "/etc/wk/tailnet.conf"
DAEMON_LABEL = "com.wk.tailscaled"
JOIN_LABEL = "com.wk.tailnet-join"
LAUNCHD = "/Library/LaunchDaemons"
PAYLOAD_TOOLS = "/usr/local/share/wk-bench/wk-tools"
PAYLOAD = ("tailscaled", "tailscale", "authkey", "tailnet.conf", DAEMON_LABEL + ".plist", JOIN_LABEL + ".plist")


def launchd_plist(label, argv, log, keep_alive=False):
    doc = {"Label": label, "ProgramArguments": argv, "RunAtLoad": True, "StandardOutPath": log, "StandardErrorPath": log}
    return plistlib.dumps(dict(doc, KeepAlive=True) if keep_alive else doc).decode()


DAEMON_PLIST = launchd_plist(DAEMON_LABEL, [TS_BIN + "/tailscaled", "--state=%s/tailscaled.state" % TS_STATE_DIR, "--tun=utun"],
                             "/var/log/wk-tailscaled.log", keep_alive=True)
JOIN_PLIST = launchd_plist(JOIN_LABEL, ["/bin/bash", PAYLOAD_TOOLS + "/bench/mac-tailnet.sh", "join"], "/var/log/wk-tailnet-join.log")


def volume_state(root):
    """/private/var, never /var: that is a symlink on a macOS volume, and a package payload under one is not laid down."""
    return "%s/private%s/tailscaled.state" % (root.rstrip("/"), TS_STATE_DIR)


class Tailnet:
    def __init__(self, machine, env, root=None, go=None):
        self.m, self.env = machine, env
        self.root = str(root or images.root(env))
        self.go_pin = GO if go is None else go

    def run(self, argv, what):
        if not self.m.act_run(argv).ok:
            die(what)

    def artifacts(self):
        return os.path.join(task.image_root(self.env), "cache", "mac-tailnet")

    def release(self):
        text = self.m.read(os.path.join(self.root, REL))
        got = dict(re.findall(r'^(TS_VERSION|TS_SRC_SHA256) = "(.*)"$', text, re.M))
        if len(got) != 2:
            die("%s declares no TS_VERSION or no TS_SRC_SHA256" % REL)
        return got["TS_VERSION"], got["TS_SRC_SHA256"]

    def host(self):
        system = self.m.run(["uname", "-s"]).out.strip().lower()
        arch = self.m.run(["uname", "-m"]).out.strip()
        return "%s_%s" % (system, {"aarch64": "arm64"}.get(arch, arch))

    def runnable(self, d):
        return all(self.m.run(["od", "-An", "-tx1", "-N8", d + "/" + n]).out.split() == MACHO_ARM64.split()
                   for n in ("tailscaled", "tailscale"))

    def fresh(self, path):
        self.m.act_run(["rm", "-rf", path])
        self.m.mkdir(path)

    def fetch(self, url, dest, sha, what):
        why = task.fetch_pinned(self.m, url, dest, sha)
        if why:
            die("%s: %s\n    No daemon is built out of unverified bytes." % (what, why))

    def unpack(self, argv, final, what):
        self.m.act_run(["rm", "-rf", final])
        self.fresh(final + ".part")
        self.run(argv + [final + ".part"], "could not unpack %s" % what)
        self.run(["mv", final + ".part", final], "could not keep %s" % final)

    def go(self):
        ver, host = self.go_pin["version"], self.host()
        sha = self.go_pin.get(host)
        if not sha:
            die("lib/wk/sysimage/mactailnet.py pins no Go toolchain for %s.\n    Remedy: add GO[\"%s\"] with go.dev's "
                "published checksum for go%s.%s.tar.gz" % (host, host, ver, host.replace("_", "-")))
        art = self.artifacts()
        go = os.path.join(art, "go-" + ver, "go", "bin", "go")
        if self.m.exists(go):
            return go
        self.m.mkdir(art)
        name = "go%s.%s.tar.gz" % (ver, host.replace("_", "-"))
        tgz = os.path.join(art, name)
        self.fetch("https://go.dev/dl/" + name, tgz, sha, "the Go %s toolchain (%s)" % (ver, host))
        self.unpack(["tar", "xzf", tgz, "-C"], os.path.join(art, "go-" + ver), tgz)
        return go

    def source(self, ver, sha):
        art = self.artifacts()
        src = os.path.join(art, "src-" + ver, "tailscale.com@v" + ver)
        if self.m.exists(os.path.join(src, "go.mod")):
            return src
        self.m.mkdir(art)
        zipf = os.path.join(art, "tailscale-%s.zip" % ver)
        self.fetch("https://proxy.golang.org/tailscale.com/@v/v%s.zip" % ver, zipf, sha, "the tailscale %s source" % ver)
        self.unpack(["python3", "-m", "zipfile", "-e", zipf], os.path.join(art, "src-" + ver), zipf)
        return src

    def build(self):
        ver, sha = self.release()
        art = self.artifacts()
        out = os.path.join(art, "darwin-arm64-" + ver)
        if self.runnable(out):
            return out
        go, src = self.go(), self.source(ver, sha)
        info("building tailscaled %s for darwin/arm64" % ver)
        part = out + ".part"
        self.fresh(part)
        stamp = "-X tailscale.com/version.longStamp=%s -X tailscale.com/version.shortStamp=%s" % (ver, ver)
        self.run(["env", "GOPATH=%s/gopath" % art, "GOMODCACHE=%s/gopath/pkg/mod" % art, "GOCACHE=%s/gocache" % art,
                  "GOTOOLCHAIN=local", "CGO_ENABLED=0", "GOOS=darwin", "GOARCH=arm64",
                  go, "-C", src, "build", "-trimpath", "-ldflags", stamp, "-o", part + "/", "./cmd/tailscaled",
                  "./cmd/tailscale"], "could not build tailscaled for darwin/arm64")
        self.refuse_unrunnable(part)
        self.run(["chmod", "0755", part + "/tailscaled", part + "/tailscale"], "could not mark %s executable" % part)
        self.m.act_run(["rm", "-rf", out])
        self.run(["mv", part, out], "could not keep %s" % out)
        return out

    def refuse_unrunnable(self, d):
        if not act.dry_run() and not self.runnable(d):
            die("%s holds something that is not a Mach-O arm64 executable.\n    Refusing it: a binary the benchmark "
                "install cannot run is a\n    machine that comes back unreachable." % d)

    def node_name(self, machine):
        name = (fleet.Fleet(self.root, self.env).load(machine) or {}).get("bench_ssh", "")
        if not name:
            die("machines/%s.conf declares no bench_ssh,\n    so there is no name for the benchmark install to "
                "join the tailnet under.\n    Every phase of the Mac bench path reaches it by that name." % machine)
        return name

    def remembered(self, name):
        return os.path.join(Store(self.env).state_dir(), "mac-tailnet", name + ".state")

    def authkey(self):
        if act.dry_run():
            return "<the tailnet auth key>" if tailnet.Fleet(self.root, self.env, self.m).key_present() else ""
        return tailnet.Fleet(self.root, self.env, self.m).authkey()

    def collect(self, machine, dest):
        name = self.node_name(machine)
        keyfile = self.authkey()
        if not keyfile:
            die("there is no tailnet auth key on this machine, so the benchmark\n    install staged here would come up "
                "with no tailnet identity -- unobservable\n    from the moment it reboots until it powers itself off.\n"
                "    Set one first:  wk key set tailnet")
        out = self.build()
        part = dest + ".part"
        self.fresh(part)
        self.run(["install", "-m", "0755", out + "/tailscaled", out + "/tailscale", part + "/"], "could not collect the daemon")
        self.run(["install", "-m", "0600", keyfile, part + "/authkey"], "could not collect the auth key")
        self.m.write(part + "/tailnet.conf", "hostname=%s\ntag=%s\n" % (name, tailnet.fleet_tag(self.env)))
        self.m.write(part + "/%s.plist" % DAEMON_LABEL, DAEMON_PLIST)
        self.m.write(part + "/%s.plist" % JOIN_LABEL, JOIN_PLIST)
        kept = self.remembered(name)
        if self.m.exists(kept) and self.m.read(kept):
            self.run(["install", "-m", "0600", kept, part + "/tailscaled.state"], "could not collect %s" % kept)
            info("  tailnet: '%s' will rejoin as the node this machine remembers" % name)
        else:
            info("  tailnet: '%s' will join fresh; its identity is kept from then on" % name)
        self.m.act_run(["rm", "-rf", dest])
        self.run(["mv", part, dest], "could not keep %s" % dest)
        return dest

    def install(self, root, src, sudo=False):
        root = root.rstrip("/")
        for f in () if act.dry_run() else PAYLOAD:
            if not self.m.exists(os.path.join(src, f)):
                die("%s carries no %s, so it is not a collected tailnet payload" % (src, f))
        self.refuse_unrunnable(src)
        pre = ["sudo"] if sudo else []
        # BSD install -d applies -m to every directory it creates, so the parents come first at 0755.
        steps = [["-d", "-m", "0755", root + TS_BIN, root + LAUNCHD],
                 ["-m", "0755", src + "/tailscaled", src + "/tailscale", root + TS_BIN + "/"],
                 ["-d", "-m", "0755", root + "/private" + os.path.dirname(TS_STATE_DIR), root + "/private/etc/wk"],
                 ["-d", "-m", "0700", root + "/private" + TS_STATE_DIR],
                 ["-m", "0600", src + "/authkey", root + "/private" + TS_KEY],
                 ["-m", "0644", src + "/tailnet.conf", root + "/private" + TS_CONF],
                 ["-m", "0644", src + "/%s.plist" % DAEMON_LABEL, src + "/%s.plist" % JOIN_LABEL, root + LAUNCHD + "/"]]
        state = src + "/tailscaled.state"
        if self.m.exists(state) and self.m.read(state):
            steps.append(["-m", "0600", state, volume_state(root)])
        for s in steps:
            self.run(pre + ["install"] + s, "could not install the tailnet payload onto '%s'" % (root or "/"))
        info("  tailnet: tailscaled installed on '%s'" % (root or "/"))

    def stage(self, root, machine, sudo=False):
        self.install(root, self.collect(machine, os.path.join(self.artifacts(), "collected")), sudo)

    def remember(self, root, machine, sudo=False):
        """Before a volume is erased: without its node identity a reinstall joins as '<name>-1'."""
        name = self.node_name(machine)
        if act.dry_run():
            act.log("would keep '%s's tailnet node identity aside, if the volume holds one" % name)
            return
        pre = ["sudo"] if sudo else []
        state = volume_state(root)
        if not self.m.run(pre + ["test", "-s", state]).ok:
            info("  tailnet: '%s' holds no node identity to keep" % root)
            return
        r = self.m.run(pre + ["cat", state])
        if not r.ok:
            die("could not read '%s's tailnet node identity" % root)
        kept = self.remembered(name)
        self.m.mkdir(os.path.dirname(kept))
        self.run(["chmod", "0700", os.path.dirname(kept)], "could not keep %s private" % os.path.dirname(kept))
        self.m.write(kept + ".part", r.out)
        self.run(["chmod", "0600", kept + ".part"], "could not keep %s private" % kept)
        self.run(["mv", kept + ".part", kept], "could not keep %s" % kept)
        info("  tailnet: kept '%s' aside; the next volume rejoins as it" % name)


def main(argv, env=None, machine=None):
    from wk.machine import here
    parser = argparse.ArgumentParser(prog="python3 -m wk.sysimage.mactailnet")
    p = parser.add_subparsers(dest="verb", required=True).add_parser("install")
    p.add_argument("volume_root")
    p.add_argument("collected_dir")
    a = parser.parse_args(argv)
    try:
        Tailnet(machine or here(), os.environ if env is None else env).install(a.volume_root, a.collected_dir)
    except Refused as e:
        return e.status
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
