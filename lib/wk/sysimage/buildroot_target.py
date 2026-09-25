"""The buildroot image and slot builds as they run inside a workspace (lib/wk/sysimage/buildroot.py is the host
half). Run as `python3 /opt/wk-tools/lib/wk/sysimage/buildroot_target.py image|webkit ...`, under task.stage_main,
which takes the wall off PATH. Follows the wiki recipe "Building WPEWebKit for 32-bit Raspberry Pi 3 (Buildroot DRM
config)", the only one known to boot."""

import argparse
import fnmatch
import os
import re
import shlex
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wk.clock import Clock  # noqa: E402
from wk.machine import here  # noqa: E402

TOOLS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
MB_PER_JOB = 2048
SHA_LEN = 40
TS_REL = "image/yocto/meta-wk-tailnet/recipes-network/tailscale/tailscale-release.inc"
TS_JOIN = "image/yocto/meta-wk-tailnet/recipes-network/tailscale/files/wk-tailnet-join"
WIFI_JOIN = "image/yocto/meta-wk-wifi/recipes-connectivity/wk-wifi-join/files/wk-wifi-join"
MARKER = "# --- added by wk (lib/wk/sysimage/buildroot_target.py) ---"
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


class Failed(Exception):
    pass


def fail(text):
    raise Failed(text)


def parse(argv):
    ap = argparse.ArgumentParser(prog="buildroot_target.py")
    sub = ap.add_subparsers(dest="stage")
    img = sub.add_parser("image")
    for flag in ("--name", "--tree-url", "--tree-branch", "--tree-commit", "--defconfig", "--image", "--jobs",
                 "--kernel-tar", "--kernel-release", "--overlay-arch"):
        img.add_argument(flag, default="")
    for flag in ("--external", "--overlay-wifi"):
        img.add_argument(flag, default="0", choices=("0", "1"))
    wk = sub.add_parser("webkit")
    for flag in ("--name", "--commit", "--slot", "--jobs"):
        wk.add_argument(flag, default="")
    for p in (img, wk):
        p.add_argument("--src", default="/src/WebKit")
    a = ap.parse_args(argv)
    if not a.stage:
        ap.error("image or webkit")
    need = ("name", "tree_url", "defconfig") if a.stage == "image" else ("name", "commit", "slot")
    for n in need:
        if not getattr(a, n):
            ap.error("--%s is required" % n.replace("_", "-"))
    if a.stage == "webkit" and (len(a.commit) != SHA_LEN or any(c not in "0123456789abcdef" for c in a.commit)):
        ap.error("--commit takes a full 40-character sha, got '%s'" % a.commit)
    if a.stage == "image" and a.kernel_tar and not a.kernel_release:
        ap.error("--kernel-tar needs --kernel-release")
    return a


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


class Build:
    def __init__(self, a, machine, env, clock, tools=TOOLS):
        self.a, self.m, self.env, self.clock, self.tools = a, machine, env, clock, tools
        self.jobs = int(a.jobs) if a.jobs else int(machine.run(["nproc"]).out.strip() or 4)
        self.workdir = os.path.join(a.src, "WebKitBuild", "buildroot", a.name)
        self.out = os.path.join(self.workdir, "output")
        self.tcf = os.path.join(self.out, "host", "share", "buildroot", "toolchainfile.cmake")
        self.prefix = "wk-buildroot" if a.stage == "image" else "wk-buildroot-webkit"

    def say(self, text):
        sys.stdout.write("%s: %s\n" % (self.prefix, text))
        sys.stdout.flush()

    def ok(self, argv, why):
        r = self.m.act_run(argv)
        if not r.ok:
            fail("%s\n    %s" % (why, (r.err or r.out).strip()))
        return r

    def git(self, *args):
        return self.m.act_run(["git", "-C", self.workdir] + list(args))

    def make(self, br_ext, *args, quiet=False):
        argv = ["make", "-C", self.workdir] + br_ext + list(args)
        if not quiet:
            return self.m.run_tty(argv, cwd=self.workdir).ok
        r = self.m.act_run(argv)
        sys.stderr.write("" if r.ok else r.err)
        return r.ok

    def guarded(self, argv):
        return ["env", "WK_MB_PER_JOB=%d" % MB_PER_JOB, "bash", "-c",
                '. %s/build/guard.sh && guard_run "$0" -- "$@"' % self.tools, str(self.jobs)] + list(argv)

    def br_ext(self, wanted):
        """On every make: buildroot records BR2_EXTERNAL in output/.br-external.mk, and a make without it then fails."""
        return ["BR2_EXTERNAL=%s/image/buildroot/external" % self.tools] if wanted else []

    def check_caches(self):
        for name in ("BR2_DL_DIR", "BR2_CCACHE_DIR"):
            d = self.env.get(name)
            if not d:
                fail("BR2_DL_DIR/BR2_CCACHE_DIR are not set in this workspace.\n    They come from the container's "
                     "store-backed cache mount (targets/container.sh); without them buildroot's download and ccache\n"
                     "    caches would land in the workspace and die with it.")
            self.m.mkdir(d)
            if not self.m.run(["test", "-w", d]).ok:
                fail("%s is not writable" % d)

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
        """An output/ tree is tens of gigabytes: a second run at the pin fetches rather than re-cloning."""
        a = self.a
        if self.m.isdir(os.path.join(self.workdir, ".git")):
            self.say("tree already present; fetching the pin")
            if not self.git("fetch", "--tags", "origin", a.tree_branch or "HEAD").ok:
                fail("could not fetch %s in %s" % (a.tree_url, self.workdir))
        else:
            self.m.mkdir(os.path.dirname(self.workdir))
            self.say("cloning (this is somebody else's vendor branch; shallow would lose the tag)")
            if not self.m.act_run(["git", "clone"] + (["--branch", a.tree_branch] if a.tree_branch else [])
                                  + [a.tree_url, self.workdir]).ok:
                fail("could not clone %s" % a.tree_url)
        if a.tree_commit:
            self.git("fetch", "origin", a.tree_commit)
            if not self.git("checkout", "--detach", a.tree_commit).ok:
                fail("%s has no commit %s.\n    The configuration pins one deliberately: a branch moves and the 2020.02 "
                     "tag\n    predates the cog defconfigs." % (a.tree_url, a.tree_commit))
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
            if not self.git("apply", p).ok:
                fail("tree patch does not apply: %s\n    The pin moved out from under it (BR_TREE_COMMIT); rederive the patch." % n)
            self.say("tree patch applied: %s" % n)

    def sha_ok(self, sha, path):
        words = self.m.run(["sha256sum", path]).out.split()
        return bool(words) and words[0] == sha

    def tailnet_overlay(self, stage):
        """The tailscale release the yocto layer pins, and no credential: the key arrives with the card."""
        arch = self.a.overlay_arch
        rel = os.path.join(self.tools, TS_REL)
        ver, sha = tailscale_pin(self.m.read(rel), arch)
        if not ver:
            fail("no TS_VERSION in %s" % rel)
        if not sha:
            fail("%s declares no TS_SHA256_%s" % (rel, arch))
        base = "tailscale_%s_%s" % (ver, arch)
        tgz = os.path.join(self.workdir, base + ".tgz")
        if not (self.m.exists(tgz) and self.sha_ok(sha, tgz)):
            self.say("fetching tailscale %s (%s)" % (ver, arch))
            self.ok(["curl", "-fsSL", "-o", tgz + ".part", "https://pkgs.tailscale.com/stable/%s.tgz" % base],
                    "could not fetch the tailscale release")
            if not self.sha_ok(sha, tgz + ".part"):
                self.m.remove(tgz + ".part")
                fail("%s.tgz does not match the pinned sha256.\n    Refusing to put unverified bytes in an image -- %s "
                     "is what says which bytes are right." % (base, rel))
            self.ok(["mv", "-f", tgz + ".part", tgz], "could not keep %s" % tgz)
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
            fail("the pinned kernel is not at %s.\n    It is fetched and prepared on the driving machine and handed over "
                 "through\n    the download cache both sides share (lib/wk/sysimage/buildroot.py); this build does\n"
                 "    not fetch it itself." % a.kernel_tar)
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
        conf = self.m.read(os.path.join(self.workdir, ".config"))
        orig = config_value(conf, "BR2_ROOTFS_POST_IMAGE_SCRIPT")
        dts = config_value(conf, "BR2_LINUX_KERNEL_INTREE_DTS_NAME")
        if not dts:
            fail("%s names no BR2_LINUX_KERNEL_INTREE_DTS_NAME, so there is no\n    device tree name to install the pinned "
                 "kernel's copy of." % self.a.defconfig)
        dtb = dts + ".dtb"
        if not self.m.exists(os.path.join(stage, "dtb", dtb)):
            fail("the pinned kernel carries no %s" % dtb)
        script = os.path.join(self.workdir, "wk-kernel-post-image.sh")
        self.m.write(script, POST_IMAGE.format(stage=shlex.quote(stage), dtb=shlex.quote(dtb), release=self.a.kernel_release))
        self.ok(["chmod", "+x", script], "could not make %s executable" % script)
        return (script + " " + orig).strip()

    def verify_fresh(self, img, start):
        """make exits 0 on a tree with nothing left to do, so the image must be newer than the build."""
        if not self.m.exists(img):
            fail("the configuration names '%s' and make\n    reported success, but %s does not exist. A cog defconfig whose\n"
                 "    filesystem output is tar-only builds no card image -- a defconfig question, not\n"
                 "    something this stage can call done." % (os.path.basename(img), img))
        r = self.m.run(["stat", "-c", "%Y", img])
        if not r.ok or not r.out.strip().isdigit():
            fail("could not read the mtime of %s" % img)
        if int(r.out.strip()) < int(start):
            fail("make reported success but %s is older\n    than this build started: something upstream of genimage "
                 "skipped work\n    it always does, the trap verify_fresh (yocto_target.py) guards against for bitbake." % img)

    def image(self):
        a = self.a
        self.check_caches()
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
            fail("no such defconfig: %s\n    'make list-defconfigs' in %s shows what the tree has, and the external\n"
                 "    tree adds this repository's own (image/buildroot/external/configs)." % (a.defconfig, self.workdir))
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
        if self.m.exists(local):   # a slot's source override; an image is the pinned tarball and nothing else
            self.say("dropping local.mk (a WebKit slot's source override) and rebuilding wpewebkit from the pinned tarball")
            self.m.remove(local)
            if not self.make(ext, "wpewebkit-dirclean", quiet=True):
                fail("wpewebkit-dirclean failed")
        start = self.clock.now()
        self.say("building (this is hours, and the log below is the whole account of it)")
        # FORCE_UNSAFE_CONFIGURE=1: 2009-era configure scripts refuse to run as root.
        if not self.m.run_tty(self.guarded(["env", "FORCE_UNSAFE_CONFIGURE=1", "make", "-C", self.workdir] + ext
                                           + ["-j%d" % self.jobs]), cwd=self.workdir).ok:
            fail("buildroot failed. The last lines above are the failing package.")
        images = os.path.join(self.out, "images")
        if not self.m.isdir(images):
            fail("the build reported success but produced no %s" % images)
        self.say("images built at:")
        for n in self.m.listdir(images):
            self.say("  -   %s" % n)
        if a.image:
            self.verify_fresh(os.path.join(images, a.image), start)
            self.say("%s is fresh" % a.image)
        self.say("stage 'image' done")

    def check_image(self):
        n = self.a.name
        if not self.m.isdir(os.path.join(self.workdir, "package")):
            fail("no buildroot tree at %s.\n    A slot is built beside a finished image; build the image first:\n"
                 "        wk sysimage build %s" % (self.workdir, n))
        if not self.m.exists(os.path.join(self.out, "build", "packages-file-list.txt")):
            fail("the image in %s was never built to the end\n    (no output/build/packages-file-list.txt); "
                 "'wk sysimage build %s' first." % (self.workdir, n))
        if not (self.m.exists(self.tcf) and "--build-id" in self.m.read(self.tcf)):
            fail("the image's toolchain file carries no --build-id\n    (%s). Every slot binary must carry the identifier "
                 "the board is checked\n    against; the image build sets it (BR2_TARGET_LDFLAGS) and regenerates the\n"
                 "    file:  wk sysimage build %s   (incremental)" % (self.tcf, n))

    def checkout(self):
        a, src = self.a, self.a.src
        mirror = self.env.get("WK_MIRROR")
        if not mirror:
            fail("WK_MIRROR names the mirror this container mounts, set by targets/container.sh")
        dirty = [l for l in self.m.run(["git", "-C", src, "status", "--porcelain"]).out.splitlines() if l.strip()]
        if dirty:
            fail("%s has %d uncommitted change(s); a slot is built from a\n    commit and nothing else. Commit or discard "
                 "them in the workspace first." % (src, len(dirty)))
        if not self.m.run(["git", "-C", src, "cat-file", "-e", a.commit + "^{commit}"]).ok:
            self.say("fetching %s from the mirror" % a.commit)
            if not self.m.act_run(["git", "-C", src, "fetch", "--quiet", mirror, a.commit]).ok:
                fail("%s is not in this machine's mirror (%s).\n    'wk bench ab' and 'wk pr' fetch a PR head into the mirror "
                     "first; a bare sha has\n    to be reachable from a branch the mirror carries." % (a.commit, mirror))
        if not self.m.act_run(["git", "-C", src, "checkout", "--detach", "--quiet", a.commit]).ok:
            fail("could not check out %s in %s" % (a.commit, src))
        self.say("source      %s @ %s" % (src, self.m.run(["git", "-C", src, "log", "-1", "--format=%h (%s)"]).out.strip()[:80]))

    def find_one(self, d, pattern, want_dir):
        for n in self.m.listdir(d) if self.m.isdir(d) else []:
            p = os.path.join(d, n)
            if fnmatch.fnmatchcase(n, pattern) and self.m.isdir(p) == want_dir:
                return p
        return ""

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
        if not self.m.run_tty(self.guarded(["env", "FORCE_UNSAFE_CONFIGURE=1", "make", "-C", self.workdir] + ext
                                           + ["BR2_JLEVEL=%d" % self.jobs, "wpewebkit-rebuild"]), cwd=self.workdir).ok:
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
        lib = self.find_one(os.path.join(root, "usr", "lib"), "libWPEWebKit-*.so.*.*.*", False)
        if not lib:
            fail("the slot has no libWPEWebKit under %s/usr/lib" % root)
        execdir = self.find_one(os.path.join(root, "usr", "libexec"), "wpe-webkit-*", True)
        if not execdir or not self.m.run(["test", "-x", os.path.join(execdir, "WPEWebProcess")]).ok:
            fail("no WPEWebProcess under %s/usr/libexec" % root)
        libdir = os.path.join(root, "usr", "lib")
        bundle = next((os.path.join(libdir, n, "injected-bundle") for n in self.m.listdir(libdir)
                       if self.m.isdir(os.path.join(libdir, n, "injected-bundle"))), "")
        if not bundle or not self.m.exists(os.path.join(bundle, "libWPEInjectedBundle.so")):
            fail("no injected bundle under %s/usr/lib" % root)
        cc = re.search(r'(?m)^set\(CMAKE_C_COMPILER .*bin/(.*)-gcc"\)$', self.m.read(self.tcf))
        if not cc:
            fail("%s names no cross gcc, so there is no readelf to read the build-id with" % self.tcf)
        rev = self.m.run(["git", "-C", self.tools, "rev-parse", "--short", "HEAD"])
        fields = dict(slot=a.slot, profile=a.name, commit=a.commit, browser="cog", lib_dir="usr/lib",
                      exec_dir=os.path.relpath(execdir, root), bundle_dir=os.path.relpath(bundle, root), jobs=str(self.jobs),
                      built_at=self.clock.iso(), wk_tools=rev.out.strip() if rev.ok else "unknown")
        slot_py = os.path.join(self.tools, "lib", "wkslot.py")
        sj = os.path.join(slotdir, "slot.json")
        self.ok(["python3", slot_py, "manifest", "--readelf", os.path.join(self.out, "host", "bin", cc.group(1) + "-readelf"),
                 root, sj] + ["%s=%s" % kv for kv in sorted(fields.items())], "could not write %s" % sj)
        bid = self.m.run(["python3", slot_py, "get", sj, "build_id"]).out.strip()
        du = self.m.run(["du", "-sh", root]).out.split()
        self.say("slot ready: %s" % slotdir)
        self.say("  %s in root/, %s build-id %s" % (du[0] if du else "?", os.path.basename(lib), bid))
        self.say("stage 'webkit-%s' done" % a.slot)

    def run(self):
        if self.a.stage == "image":
            self.image()
        else:
            self.webkit()


def main(argv, environ=None, machine=None, clock=None, tools=TOOLS):
    a = parse(argv)
    b = Build(a, machine or here(), dict(os.environ if environ is None else environ), clock or Clock(), tools)
    try:
        b.run()
    except Failed as e:
        sys.stderr.write("%s: error: %s\n" % (b.prefix, e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
