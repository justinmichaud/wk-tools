"""The buildroot image and slot builds inside a workspace, under task.stage_main (buildroot.py is the host half).
Follows the wiki recipe "Building WPEWebKit for 32-bit Raspberry Pi 3 (Buildroot DRM config)", the one known to boot."""

import argparse
import fnmatch
import os
import re
import shlex
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wk import slot as wkslot  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import here  # noqa: E402
from wk.sysimage import TOOLS, WsBuild, fail  # noqa: E402
from wk.sysimage.task import du, fetch_pinned  # noqa: E402

MB_PER_JOB = 2048
TS_REL = "image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"
TS_JOIN = "image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join"
WIFI_JOIN = "image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join"
MARKER = "# --- added by wk (lib/wk/sysimage/buildroot_ws.py) ---"
# WebKitBuild and the test trees are gigabytes the package build never reads.
RSYNC_EXCLUDE = "--exclude WebKitBuild --exclude LayoutTests --exclude JSTests --exclude ManualTests " \
                "--exclude WebDriverTests --exclude Websites"
# Written here, run by buildroot as BR2_ROOTFS_POST_IMAGE_SCRIPT with BINARIES_DIR as $1.
POST_IMAGE = """#!/bin/sh
set -e
b="$1"
cp {stage}/boot/zImage "$b/zImage"
cp {stage}/dtb/{dtb} "$b/"
mkdir -p "$b/rpi-firmware/overlays"
cp -f {stage}/dtb/overlays/*.dtbo "$b/rpi-firmware/overlays/"
echo "wk: installed the pinned kernel {release} and its device trees into $b"
"""


def parse(argv):
    ap = argparse.ArgumentParser(prog="buildroot_ws.py")
    sub = ap.add_subparsers(dest="stage", required=True)
    img, wk = sub.add_parser("image"), sub.add_parser("webkit")
    for p, need, rest in ((img, ("--name", "--tree-url", "--defconfig"), ("--tree-branch", "--tree-commit", "--image", "--kernel-tar",
                                                                          "--kernel-release", "--kernel-dts", "--overlay-arch")),
                          (wk, ("--name", "--commit", "--slot"), ())):
        for flag in need:
            p.add_argument(flag, required=True)
        for flag in rest + ("--jobs",):
            p.add_argument(flag, default="")
        p.add_argument("--src", default="/src/WebKit")
    for flag in ("--external", "--overlay-wifi"):
        img.add_argument(flag, default="0", choices=("0", "1"))
    return ap.parse_args(argv)


def config_value(text, key):
    vals = re.findall(r'(?m)^%s="(.*)"$' % re.escape(key), text)
    return vals[-1] if vals else ""


def config_block(a, env, jobs, overlay, post_image):
    out = ["", MARKER]
    if a.image.endswith(".img"):   # cog defconfigs emit a tarball only, and buildroot wants the size before the rootfs exists
        out += ["BR2_TARGET_ROOTFS_EXT2=y", "BR2_TARGET_ROOTFS_EXT2_4=y", 'BR2_TARGET_ROOTFS_EXT2_SIZE="1600M"']
    if post_image:
        out.append('BR2_ROOTFS_POST_IMAGE_SCRIPT="%s"' % post_image)
    out += ['BR2_PRIMARY_SITE="https://sources.buildroot.net"', 'BR2_DL_DIR="%s"' % env["BR2_DL_DIR"], "BR2_CCACHE=y",
            'BR2_CCACHE_DIR="%s"' % env["BR2_CCACHE_DIR"],
            # A kconfig symbol, so an environment value is inert; its default, nproc+1, is five times the 2 GB/job budget.
            "BR2_JLEVEL=%d" % jobs,
            'BR2_TARGET_LDFLAGS="-Wl,--build-id"']   # a build-id is how a slot is told apart in the running process
    if overlay:
        out.append('BR2_ROOTFS_OVERLAY="%s"' % " ".join(overlay))
    return "\n".join(out) + "\n"


def tailscale_pin(text, arch):
    def field(k):
        m = re.search(r'(?m)^%s = "(.*)"$' % re.escape(k), text)
        return m.group(1) if m else ""
    return field("TS_VERSION"), field("TS_SHA256_" + arch)


class Build(WsBuild):
    PREFIX = "wk-buildroot"

    def __init__(self, a, machine, env, clock, tools=TOOLS):
        super().__init__(a, machine, env, clock, tools)
        self.workdir = os.path.join(a.src, "WebKitBuild", "buildroot", a.name)
        self.out = os.path.join(self.workdir, "output")
        self.tcf = os.path.join(self.out, "host", "share", "buildroot", "toolchainfile.cmake")

    def make(self, br_ext, *args, quiet=False):
        argv = ["make", "-C", self.workdir] + br_ext + list(args)
        if not quiet:
            return self.m.run_tty(argv, cwd=self.workdir).ok
        r = self.m.act_run(argv)
        sys.stderr.write("" if r.ok else r.err)
        return r.ok

    def make_all(self, ext, *args):
        # FORCE_UNSAFE_CONFIGURE=1: 2009-era configure scripts refuse to run as root.
        return self.m.run_tty(self.guarded(self.jobs, ["env", "FORCE_UNSAFE_CONFIGURE=1", "make", "-C", self.workdir] + ext + list(args),
                                           MB_PER_JOB), cwd=self.workdir).ok

    def br_ext(self, wanted):
        """On every make: buildroot records BR2_EXTERNAL in output/.br-external.mk, and a make without it then fails."""
        return ["BR2_EXTERNAL=%s/image/buildroot/external" % self.tools] if wanted else []

    def summary(self):
        a = self.a
        osr = self.m.read("/etc/os-release") if self.m.exists("/etc/os-release") else ""
        pretty = re.search(r'(?m)^PRETTY_NAME="?([^"\n]*)"?$', osr)
        gcc = self.m.run(["gcc", "-dumpversion"])
        self.say("tree        %s @ %s" % (a.tree_url, a.tree_commit or a.tree_branch or "HEAD"))
        self.say("defconfig   %s" % a.defconfig)
        self.say("workdir     %s" % self.workdir)
        self.say("jobs        -j%d" % self.jobs)
        self.say("host        %s, gcc %s" % (pretty.group(1) if pretty else "?", gcc.out.strip() if gcc.ok else "?"))

    def tree(self):
        a = self.a
        if self.m.isdir(os.path.join(self.workdir, ".git")):
            self.say("tree already present; fetching the pin")
            if not self.git(self.workdir, "fetch", "--tags", "origin", a.tree_branch or "HEAD").ok:
                fail("could not fetch %s in %s" % (a.tree_url, self.workdir))
        else:
            self.m.mkdir(os.path.dirname(self.workdir))
            self.say("cloning (this is somebody else's vendor branch; shallow would lose the tag)")
            if not self.m.act_run(["git", "clone"] + (["--branch", a.tree_branch] if a.tree_branch else [])
                                  + [a.tree_url, self.workdir]).ok:
                fail("could not clone %s" % a.tree_url)
        if a.tree_commit:
            self.git(self.workdir, "fetch", "origin", a.tree_commit)
            if not self.git(self.workdir, "checkout", "--detach", a.tree_commit).ok:
                fail("%s has no commit %s" % (a.tree_url, a.tree_commit))
            self.say("pinned at %s" % self.m.run(["git", "-C", self.workdir, "rev-parse", "--short", "HEAD"]).out.strip())

    def tree_patches(self):
        """The fork pins versions it never built: wpebackend-fdo 1.14 is meson, its package calls cmake."""
        d = os.path.join(self.tools, "image", "buildroot", "tree-patches")
        for n in sorted(self.m.listdir(d)) if self.m.isdir(d) else []:
            if not n.endswith(".patch"):
                continue
            p = os.path.join(d, n)
            if self.m.run(["git", "-C", self.workdir, "apply", "--reverse", "--check", p]).ok:
                self.say("tree patch already applied: %s" % n)
                continue
            if not self.git(self.workdir, "apply", p).ok:
                fail("tree patch does not apply: %s\n    The pin moved out from under it (BR_TREE_COMMIT); rederive the patch." % n)
            self.say("tree patch applied: %s" % n)

    def tailnet_overlay(self, stage):
        arch = self.a.overlay_arch
        rel = os.path.join(self.tools, TS_REL)
        ver, sha = tailscale_pin(self.m.read(rel), arch)
        if not ver:
            fail("no TS_VERSION in %s" % rel)
        if not sha:
            fail("%s declares no TS_SHA256_%s" % (rel, arch))
        base = "tailscale_%s_%s" % (ver, arch)
        tgz = os.path.join(self.workdir, base + ".tgz")
        why = fetch_pinned(self.m, "https://pkgs.tailscale.com/stable/%s.tgz" % base, tgz, sha)
        if why:
            fail(why)
        self.say("assembling the tailnet overlay (%s)" % arch)
        self.fresh_dirs(stage, ("usr/bin", "usr/sbin", "etc/init.d"))
        for into, member in (("usr/bin", "tailscale"), ("usr/sbin", "tailscaled")):
            self.ok(["tar", "xzf", tgz, "-C", os.path.join(stage, into), "--strip-components=1", base + "/" + member],
                    "could not unpack %s from %s" % (member, tgz))
        self.install(os.path.join(self.tools, TS_JOIN), os.path.join(stage, "usr/sbin/wk-tailnet-join"))
        self.install(os.path.join(self.tools, "image/buildroot/overlay/etc/init.d/S99tailscale"),
                     os.path.join(stage, "etc/init.d/S99tailscale"))

    def fresh_dirs(self, stage, subs):
        """Made, never mktemp'd: rsync copies an overlay in as the build user, and a mode-0700 directory dies with error 23."""
        self.m.remove(stage)
        for s in subs:
            self.m.mkdir(os.path.join(stage, s))

    def install(self, src, dest):
        self.ok(["install", "-m", "0755", src, dest], "could not install %s" % dest)

    def wifi_overlay(self, stage):
        self.say("assembling the wifi overlay")
        self.fresh_dirs(stage, ("usr/sbin", "etc/init.d"))
        self.install(os.path.join(self.tools, WIFI_JOIN), os.path.join(stage, "usr/sbin/wk-wifi-join"))
        self.install(os.path.join(self.tools, "image/buildroot/overlay/etc/init.d/S41wifi"), os.path.join(stage, "etc/init.d/S41wifi"))

    def kernel(self):
        """The modules go in as an overlay; the boot files through a post-image hook ahead of the board's own."""
        a = self.a
        if not self.m.exists(a.kernel_tar):
            fail("the pinned kernel is not at %s; the driving machine prepares it (lib/wk/sysimage/buildroot.py)" % a.kernel_tar)
        stage = os.path.join(self.workdir, "wk-kernel")
        self.m.remove(stage)
        self.m.mkdir(stage)
        self.say("unpacking the pinned kernel %s" % a.kernel_release)
        self.ok(["tar", "-C", stage, "-xf", a.kernel_tar], "could not unpack %s" % a.kernel_tar)
        if not (self.m.exists(os.path.join(stage, "boot", "zImage"))
                and self.m.isdir(os.path.join(stage, "lib", "modules", a.kernel_release))):
            fail("%s does not hold a kernel and modules for %s" % (a.kernel_tar, a.kernel_release))
        overlay = os.path.join(self.workdir, "wk-overlay-kernel")
        self.m.remove(overlay)
        self.m.mkdir(overlay)
        self.ok(["cp", "-a", os.path.join(stage, "lib"), os.path.join(overlay, "lib")], "could not stage the kernel modules")
        n = self.m.run(["find", os.path.join(overlay, "lib", "modules", a.kernel_release), "-name", "*.ko"]).out.split()
        self.say("  modules      %d in the overlay" % len(n))
        return stage, overlay

    def post_image(self, stage):
        # No BR2_LINUX_KERNEL builds here, so the device tree name is --kernel-dts, not a buildroot config key.
        orig = config_value(self.m.read(os.path.join(self.workdir, ".config")), "BR2_ROOTFS_POST_IMAGE_SCRIPT")
        dtb = self.a.kernel_dts + ".dtb"
        if not self.m.exists(os.path.join(stage, "dtb", dtb)):
            fail("the pinned kernel carries no %s" % dtb)
        script = os.path.join(self.workdir, "wk-kernel-post-image.sh")
        self.m.write(script, POST_IMAGE.format(stage=shlex.quote(stage), dtb=shlex.quote(dtb), release=self.a.kernel_release))
        self.ok(["chmod", "+x", script], "could not make %s executable" % script)
        return (script + " " + orig).strip()

    def image(self):
        a = self.a
        self.need_caches(("BR2_DL_DIR", "BR2_CCACHE_DIR"))
        self.summary()
        self.tree()
        self.tree_patches()
        overlay = []
        if a.overlay_arch:
            overlay.append(os.path.join(self.workdir, "wk-overlay-tailnet"))
            self.tailnet_overlay(overlay[-1])
        if a.overlay_wifi == "1":
            overlay.append(os.path.join(self.workdir, "wk-overlay-wifi"))
            self.wifi_overlay(overlay[-1])
        stage = ""
        if a.kernel_tar:
            stage, kover = self.kernel()
            overlay.append(kover)
        ext = self.br_ext(a.external == "1")
        self.say("applying %s" % a.defconfig)
        if not self.make(ext, a.defconfig):
            fail("no such defconfig: %s ('make list-defconfigs' in %s)" % (a.defconfig, self.workdir))
        post = self.post_image(stage) if stage else ""
        conf = os.path.join(self.workdir, ".config")
        # In .config because only .config survives buildroot's recursive makes; olddefconfig resolves the later line.
        self.m.write(conf, self.m.read(conf) + config_block(a, self.env, self.jobs, overlay, post))
        if not self.make(ext, "olddefconfig", quiet=True):
            fail("the configuration would not resolve after wk's additions")
        text = self.m.read(conf)
        for k in ("BR2_DL_DIR", "BR2_CCACHE_DIR", "BR2_ROOTFS_OVERLAY"):
            v = config_value(text, k)
            if v:
                self.say('  %s="%s"' % (k, v))
        if self.m.exists(self.tcf) and "--build-id" not in self.m.read(self.tcf):
            self.say("the toolchain file predates BR2_TARGET_LDFLAGS; reinstalling the toolchain to regenerate it")
            if not self.make(ext, "toolchain-reinstall", quiet=True):
                fail("toolchain-reinstall failed")
        local = os.path.join(self.workdir, "local.mk")
        if self.m.exists(local):
            self.say("dropping local.mk (a WebKit slot's source override) and rebuilding wpewebkit from the pinned tarball")
            self.m.remove(local)
            if not self.make(ext, "wpewebkit-dirclean", quiet=True):
                fail("wpewebkit-dirclean failed")
        start = self.clock.now()
        self.say("building (this is hours, and the log below is the whole account of it)")
        if not self.make_all(ext, "-j%d" % self.jobs):
            fail("buildroot failed. The last lines above are the failing package.")
        images = os.path.join(self.out, "images")
        if not self.m.isdir(images):
            fail("the build reported success but produced no %s" % images)
        self.say("images built at:")
        for n in self.m.listdir(images):
            self.say("  -   %s" % n)
        if a.image:
            self.verify_fresh(os.path.join(images, a.image), start)
        self.say("stage 'image' done")

    def check_image(self):
        n = self.a.name
        if not self.m.isdir(os.path.join(self.workdir, "package")):
            fail("no buildroot tree at %s; build the image first:  wk sysimage build %s" % (self.workdir, n))
        if not self.m.exists(os.path.join(self.out, "build", "packages-file-list.txt")):
            fail("the image in %s was never built to the end; 'wk sysimage build %s' first" % (self.workdir, n))
        if not (self.m.exists(self.tcf) and "--build-id" in self.m.read(self.tcf)):
            fail("the image's toolchain file %s carries no --build-id, which every slot binary needs;\n"
                 "    'wk sysimage build %s' regenerates it (incremental)" % (self.tcf, n))

    def checkout(self):
        a, src = self.a, self.a.src
        dirty = [l for l in self.m.run(["git", "-C", src, "status", "--porcelain"]).out.splitlines() if l.strip()]
        if dirty:
            fail("%s has %d uncommitted change(s); a slot is built from a commit and nothing else" % (src, len(dirty)))
        self.fetch_commit(src, a.commit)
        if not self.m.act_run(["git", "-C", src, "checkout", "--detach", "--quiet", a.commit]).ok:
            fail("could not check out %s in %s" % (a.commit, src))
        self.say_source(src)

    def find_one(self, d, pattern):
        return next((os.path.join(d, n) for n in (self.m.listdir(d) if self.m.isdir(d) else [])
                     if fnmatch.fnmatchcase(n, pattern) and self.m.isdir(os.path.join(d, n))), "")

    def copy_slot(self, root, slotdir):
        listing = self.m.read(os.path.join(self.out, "build", "packages-file-list.txt"))
        files = [l[len("wpewebkit,"):] for l in listing.splitlines() if l.startswith("wpewebkit,")]
        if not files:
            fail("packages-file-list.txt records nothing for wpewebkit")
        self.m.write(os.path.join(slotdir, "files.txt"), "".join(f + "\n" for f in files))
        target = os.path.join(self.out, "target")
        present = [f for f in files if self.m.exists(os.path.join(target, f))]
        nul = os.path.join(slotdir, "files.nul")
        tar = os.path.join(slotdir, "files.tar")
        self.m.write(nul, "".join(f + "\0" for f in present))
        self.ok(["tar", "-C", target, "--null", "-T", nul, "-cf", tar], "could not collect wpewebkit's installed files")
        self.ok(["tar", "-C", root, "-xf", tar], "could not copy wpewebkit's installed files into %s" % root)
        self.m.remove(tar)
        self.m.remove(nul)

    def webkit(self):
        a = self.a
        self.check_image()
        ext = self.br_ext(self.m.exists(os.path.join(self.tools, "image", "buildroot", "external", "external.desc")))
        self.checkout()
        slotdir = os.path.join(self.out, "wk-slots", a.slot)
        root = os.path.join(slotdir, "root")
        self.say("slot        %s -> %s" % (a.slot, slotdir))
        self.say("jobs        -j%d" % self.jobs)
        self.m.write(os.path.join(self.workdir, "local.mk"),
                     "# Written by wk for slot '%s'; dropped by the next image build.\nWPEWEBKIT_OVERRIDE_SRCDIR = %s\n"
                     "WPEWEBKIT_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = %s\n" % (a.slot, a.src, RSYNC_EXCLUDE))
        start = self.clock.now()
        self.say("building (make wpewebkit-rebuild; the output below is the whole account of it)")
        # BR2_JLEVEL is a kconfig symbol, so it goes on the command line, not the environment.
        if not self.make_all(ext, "BR2_JLEVEL=%d" % self.jobs, "wpewebkit-rebuild"):
            fail("the WebKit build failed. The last lines above are the failing step.")
        self.say("built in %d min" % ((self.clock.now() - start) // 60))
        self.say("finalising the root filesystem (strip, development files) the way an image build does")
        if not self.make(ext, "target-finalize", quiet=True):
            fail("target-finalize failed")
        self.m.remove(root)
        self.m.mkdir(root)
        self.copy_slot(root, slotdir)
        self.slot(root, slotdir)

    def slot(self, root, slotdir):
        a = self.a
        execdir = self.find_one(os.path.join(root, "usr", "libexec"), "wpe-webkit-*")
        if not execdir or not self.m.run(["test", "-x", os.path.join(execdir, "WPEWebProcess")]).ok:
            fail("no WPEWebProcess under %s/usr/libexec" % root)
        bundle = self.find_one(os.path.join(root, "usr", "lib"), "wpe-webkit-*")
        bundle = bundle and os.path.join(bundle, "injected-bundle")
        if not bundle or not self.m.exists(os.path.join(bundle, "libWPEInjectedBundle.so")):
            fail("no injected bundle under %s/usr/lib" % root)
        cc = re.search(r'(?m)^set\(CMAKE_C_COMPILER .*bin/(.*)-gcc"\)$', self.m.read(self.tcf))
        if not cc:
            fail("%s names no cross gcc, so there is no readelf to read the build-id with" % self.tcf)
        bid = wkslot.write_manifest(self, root, slotdir, dict(slot=a.slot, profile=a.name, commit=a.commit, browser="cog", lib_dir="usr/lib",
                                                exec_dir=os.path.relpath(execdir, root), bundle_dir=os.path.relpath(bundle, root),
                                                jobs=str(self.jobs)),
                            ["--readelf", os.path.join(self.out, "host", "bin", cc.group(1) + "-readelf")])
        size = du(self.m, root)
        self.say("slot ready: %s" % slotdir)
        self.say("  %s in root/, build-id %s" % (size, bid))
        self.say("stage 'webkit-%s' done" % a.slot)

    def run(self):
        if self.a.stage == "image":
            self.image()
        else:
            self.webkit()


def main(argv, environ=None, machine=None, clock=None, tools=TOOLS):
    return Build(parse(argv), machine or here(), dict(os.environ if environ is None else environ), clock or Clock(), tools).main()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
