"""The buildroot builder's driving half; lib/wk/sysimage/buildroot_target.py runs from the workspace's own copy of
this tree, so the halves cannot skew. The tree is a fork: the release-pinned `cog` defconfigs exist nowhere else.
TODO: whether the rpi3 defconfig compiles wpa_supplicant at all is unverified."""

import os

from wk import act, fleet, images, slot
from wk.act import die, info, log
from wk.sysimage import task
from wk.sysimage.write import wants_wifi

BASE_IMAGE = "docker.io/library/ubuntu:22.04"   # the host the wiki recipe was driven on (container/buildroot/Containerfile)
SPEC = os.path.join("container", "buildroot", "Containerfile")
IMAGE_JOBS = 16    # 2009-era tarballs are where broken parallel rules live
WEBKIT_JOBS = 64   # WebKit links large; capped where the link steps stop gaining
PATTERN = "*buildroot_target.py*"
DL_IN_WS = "/cache/buildroot/dl"
SHA_LEN = 40
BUILD_USAGE = "usage: wk sysimage build %s [--dry-run|--workspace <name>|--detach|--stop]"
WEBKIT_USAGE = "usage: wk sysimage webkit <profile> --commit <sha> --slot <name> [--detach] [--dry-run]"
ZIMAGE_MAGIC = "016f2818"   # at offset 36 of a 32-bit ARM zImage
PIN_TOOLS = ("dpkg-deb", "xz", "depmod", "tar")


def kernel_pin(m, deb, release, out):
    """BusyBox modprobe cannot load a .ko.xz, so the modules are decompressed and depmod re-run."""
    missing = m.run(["sh", "-c", 'for t in "$@"; do command -v "$t" >/dev/null || echo "$t"; done', "sh"] + list(PIN_TOOLS)).out.split()
    if missing:
        die("preparing a pinned kernel needs %s, and this machine has none of it" % " ".join(missing))
    tarball = os.path.join(out, "wk-kernel-%s.tar" % release)
    stamp, want = tarball + ".from", task.sha256(m, deb)
    if m.exists(tarball) and m.exists(stamp) and m.read(stamp).strip() == want:
        act.debug("pinned kernel %s already prepared" % release)
        return tarball
    work = tarball + ".work"
    m.remove(work)
    m.mkdir(work)

    def ok(argv, why):
        r = m.act_run(argv)
        if not r.ok:
            die("%s\n    %s" % (why, r.err.strip()))

    x, tree = os.path.join(work, "x"), os.path.join(work, "tree")
    ok(["dpkg-deb", "-x", deb, x], "could not unpack %s" % deb)
    k, dtbs, mods = (os.path.join(x, "boot", "vmlinuz-" + release), os.path.join(x, "usr", "lib", "linux-image-" + release),
                     os.path.join(x, "lib", "modules", release))
    for path, what in ((k, "no boot/vmlinuz-" + release), (dtbs, "no device trees for " + release), (mods, "no modules for " + release)):
        if not m.exists(path):
            die("%s carries %s" % (deb, what))
    magic = m.run(["od", "-An", "-tx4", "-j36", "-N4", k]).out.replace(" ", "").strip()
    if magic != ZIMAGE_MAGIC:
        die("%s is not a 32-bit ARM zImage (magic %s)" % (k, magic or "unreadable"))
    for d in ("boot", "lib/modules", "dtb"):
        m.mkdir(os.path.join(tree, d))
    ok(["cp", k, os.path.join(tree, "boot", "zImage")], "could not stage the kernel")
    ok(["cp", "-a", mods, os.path.join(tree, "lib", "modules") + "/"], "could not stage the modules")
    ok(["find", os.path.join(tree, "lib", "modules", release), "-name", "*.ko.xz", "-exec", "xz", "-d", "{}", "+"],
       "could not decompress the modules")
    ok(["depmod", "-b", tree, release], "depmod failed over %s" % release)
    if ".ko.xz" in m.read(os.path.join(tree, "lib", "modules", release, "modules.dep")):
        die("modules.dep still names .xz modules after depmod")
    blobs = [os.path.join(dtbs, n) for n in m.listdir(dtbs) if n.endswith(".dtb")]
    if blobs:
        ok(["cp"] + blobs + [os.path.join(tree, "dtb") + "/"], "could not stage the device trees")
    if m.isdir(os.path.join(dtbs, "overlays")):
        ok(["cp", "-a", os.path.join(dtbs, "overlays"), os.path.join(tree, "dtb", "overlays")], "could not stage the overlays")
    ok(["tar", "-C", tree, "-cf", tarball + ".new", "."], "could not pack %s" % tarball)
    ok(["mv", "-f", tarball + ".new", tarball], "could not keep %s" % tarball)
    m.write(stamp, want)
    m.remove(work)
    return tarball


class Buildroot(task.ContainerBuilder):
    KIND, TITLE, SPEC, BASE_IMAGE, BASE_VAR = "buildroot", "buildroot", SPEC, BASE_IMAGE, "WK_BUILDROOT_BASE"
    NEEDS = "a buildroot image builds in a container workspace"
    NOT_HERE = "Build it on a machine whose container target holds it:  wk sysimage build %(spec)s@<machine>"
    IMAGE_NOTE = "  22.04 is the host the wiki recipe was driven on; a 2020 buildroot\n  does not survive a much newer one (%(spec)s)."
    SURVIVES = "the buildroot downloads are in the store and survive"

    def cache(self, what):
        return os.path.join(self.store.root(), "cache", "buildroot", what)

    def kill_cmd(self, ws):
        return "wk sysimage build %s%s --stop" % (self.spec, self.ws_flag(ws))

    def stage(self, target, ws, stage):
        return task.Stage(self.reg, target, ws, "buildroot", stage, self.kill_cmd(ws), self.clock, self.popen)

    def kernel(self):
        """Fetched and prepared here, where the network and depmod are; its path in the download cache both sides share."""
        fetched = task.fetch_base(self.here, self.p["BR_KERNEL_DEB_URL"], self.p["BR_KERNEL_DEB_SHA256"], self.env)
        return DL_IN_WS + "/" + os.path.basename(kernel_pin(self.here, fetched, self.p["BR_KERNEL_RELEASE"], self.cache("dl")))

    def image_argv(self, tools, jobs, wifi, kernel_tar):
        p = self.p

        def opt(flag, value):
            return [flag, value] if value else []

        return (["python3", tools + "/lib/wk/sysimage/buildroot_target.py", "image", "--name", self.name, "--tree-url", p["BR_TREE_URL"]]
                + opt("--tree-branch", p["BR_TREE_BRANCH"]) + opt("--tree-commit", p["BR_TREE_COMMIT"])
                + ["--defconfig", p["BR_DEFCONFIG"], "--external", p["BR_EXTERNAL"] or "0"] + opt("--image", p["BR_IMAGE"])
                + ["--jobs", str(jobs)] + opt("--overlay-arch", p["BR_OVERLAY_TAILSCALE"]) + opt("--overlay-wifi", "1" if wifi else "")
                + opt("--kernel-tar", kernel_tar) + opt("--kernel-release", p["BR_KERNEL_RELEASE"]))

    def du(self, path):
        words = self.here.run(["du", "-sh", path]).out.split()
        return words[0] if words else "not created yet"

    def build(self, rest):
        o = task.options(rest, ("--detach", "--stop", "--dry-run"), ("--workspace",), BUILD_USAGE % self.name)
        p = self.p
        if not p["BR_DEFCONFIG"]:
            die("'%s' names no defconfig, so there is nothing to\n    build. Its configuration is %s." % (self.name, images.conf_path(self.name, self.env)))
        if p["BR_KERNEL_DEB_URL"] and not (p["BR_KERNEL_DEB_SHA256"] and p["BR_KERNEL_RELEASE"]):
            die("%s pins a kernel but not its sha256 and release\n    (BR_KERNEL_DEB_SHA256, BR_KERNEL_RELEASE): a kernel by URL alone is not pinned." % self.name)
        target = self.target()
        ws = o.get("--workspace") or images.image_ws(self.name, self.env)
        st = self.stage(target, ws, "image")
        if o.get("--stop"):
            return st.stop()
        wifi = wants_wifi(fleet.Fleet(images.root(self.env), self.env), p["IMG_MACHINE"])
        base, tag = self.host_image()
        if act.dry_run():
            return self.build_report(target, ws, base, wifi)
        st.refuse_busy()
        if o.get("--detach"):
            return st.detach([os.path.join(self.root, "wk"), "sysimage", "build", self.spec] + [a for a in rest if a != "--detach"],
                             "build of %s" % self.name)
        budget, running, jobs = st.size(IMAGE_JOBS)
        lock = st.admit(budget, running, jobs)
        try:
            plan = (["prepare the pinned kernel %s" % p["BR_KERNEL_RELEASE"]] if p["BR_KERNEL_DEB_URL"] else []) + [
                "the workspace '%s' on %s" % (ws, tag), "sync wk-tools into '%s'" % ws, "build %s with -j%d" % (self.name, jobs)]
            t = st.begin(plan)
            n, kernel_tar = 0, ""
            try:
                if p["BR_KERNEL_DEB_URL"]:
                    n += 1
                    st.step(t, n)
                    kernel_tar = self.kernel()
                n += 1
                st.step(t, n)
                self.ensure_ws(target, ws, base, tag)
                n += 1
                st.step(t, n)
                if not target.sync_tools(ws):
                    die("pushing wk-tools into '%s' failed -- the reason is above" % ws)
            except act.Refused as e:
                t.end(e.status)
                raise
            st.step(t, n + 1)
            # BR_TREE_COMMIT is a commit, never the 2020.02 tag: the cog defconfig is absent there.
            info("building %s in '%s' (hours; --detach returns instead)" % (self.name, ws))
            st.run(t, budget, jobs, self.image_argv(target.tools(ws), jobs, wifi, kernel_tar), PATTERN)
        finally:
            lock.release_all()
        info("built %s in '%s'" % (self.name, ws))
        return 0

    def build_report(self, target, ws, base, wifi):
        p = self.p
        _, _, jobs = self.stage(target, ws, "image").size(IMAGE_JOBS)
        kernel = ("%s, pinned (%s)" % (p["BR_KERNEL_RELEASE"], os.path.basename(p["BR_KERNEL_DEB_URL"]))
                  if p["BR_KERNEL_DEB_URL"] else "built from the tree by buildroot")
        machine = p["IMG_MACHINE"] or "this board"
        dl, cc = self.cache("dl"), self.cache("ccache")
        log("would build image %s (builder: buildroot)" % self.name)
        log("  for machine  %s (%s)" % (p["IMG_MACHINE"] or "none", p["IMG_ARCH"] or "?"))
        log("  tree         %s @ %s" % (p["BR_TREE_URL"], p["BR_TREE_COMMIT"] or p["BR_TREE_BRANCH"] or "HEAD"))
        log("  defconfig    %s%s" % (p["BR_DEFCONFIG"], " (plus this repo's BR2_EXTERNAL)" if p["BR_EXTERNAL"] == "1" else ""))
        log("  workspace    %s (%s)" % (ws, target.info(ws)))
        log("  host image   %s + %s" % (base, SPEC))
        log("  jobs         -j%d (memory-sized at %d MB/job)" % (jobs, task.MB_PER_JOB))
        log("  overlay      %s" % (p["BR_OVERLAY_TAILSCALE"] or "none -- this image would join no tailnet"))
        log("  wifi overlay %s" % ("wk-wifi-join (lib/wk/sysimage/buildroot_target.py); the card carries the credential" if wifi
                                  else "none -- %s has a cable" % machine))
        log("  kernel       %s" % kernel)
        log("  DL_DIR       %s (%s) -- BR2_DL_DIR in the container" % (dl, self.du(dl)))
        log("  CCACHE_DIR   %s (%s) -- BR2_CCACHE_DIR in the container" % (cc, self.du(cc)))
        log("  output       in the workspace, under output/images (there is no store)")
        log("dry run -- nothing was built.")
        return 0

    def webkit(self, rest):
        """One commit built with the image's own wpewebkit package, so `wk bench run --ab` alternates two with no reflash."""
        o = task.options(rest, ("--detach", "--dry-run"), ("--workspace", "--commit", "--slot"), WEBKIT_USAGE)
        commit, name = o.get("--commit") or "", o.get("--slot") or ""
        if not commit or not name:
            die(WEBKIT_USAGE + "; see wk sysimage -h")
        images.check_slot_name(name)
        if len(commit) != SHA_LEN or any(c not in "0123456789abcdef" for c in commit):
            die("--commit takes a full sha (40 hex digits), got '%s'.\n    'git rev-parse' in the mirror or the workspace expands a short one." % commit)
        target = self.target()
        ws = o.get("--workspace") or images.image_ws(self.name, self.env)
        image = os.path.join(target.store.ws_dir(ws), "build", "buildroot", self.name, "output", "images", self.p["BR_IMAGE"] or "sdcard.img")
        slotdir = images.slot_dir(ws, name, self.env)
        st = self.stage(target, ws, "webkit-" + name)
        if act.dry_run():
            return self.webkit_report(target, st, ws, commit, name, image, slotdir)
        if target.info(ws) == "absent":
            die("no workspace '%s', so there is no image to build against.\n    Build the image first:  wk sysimage build %s" % (ws, self.spec))
        if not self.here.exists(image):
            die("'%s' has no finished image (%s).\n    A slot is built against the image's toolchain, so the image comes first:\n"
                "        wk sysimage build %s" % (ws, image, self.spec))
        st.refuse_busy()
        if o.get("--detach"):
            return st.detach([os.path.join(self.root, "wk"), "sysimage", "webkit", self.spec] + [a for a in rest if a != "--detach"],
                             "slot build of %s" % self.name)
        budget, running, jobs = st.size(WEBKIT_JOBS)
        lock = st.admit(budget, running, jobs)
        try:
            t = st.begin(["sync wk-tools into '%s'" % ws, "build WebKit %s into slot '%s' with -j%d" % (commit[:12], name, jobs)])
            st.step(t, 1)
            if not target.sync_tools(ws):
                t.end(1)
                die("pushing wk-tools into '%s' failed -- the reason is above" % ws)
            st.step(t, 2)
            info("building WebKit %s into slot '%s' of %s in '%s' (tens of minutes; --detach returns instead)" % (commit[:12], name, self.name, ws))
            st.run(t, budget, jobs, ["python3", target.tools(ws) + "/lib/wk/sysimage/buildroot_target.py", "webkit", "--name", self.name, "--commit", commit,
                                     "--slot", name, "--jobs", str(jobs)], PATTERN)
        finally:
            lock.release_all()
        if not self.here.exists(os.path.join(slotdir, "slot.json")):
            die("the build reported done but left no %s/slot.json" % slotdir)
        info("slot '%s' of %s holds %s" % (name, self.name, commit[:12]))
        log("  %s" % slotdir)
        log("  next:  wk bench deploy %s <board> --slot %s" % (ws, name))
        return 0

    def webkit_report(self, target, st, ws, commit, name, image, slotdir):
        _, _, jobs = st.size(WEBKIT_JOBS)
        sj = os.path.join(slotdir, "slot.json")
        try:
            held = "slot '%s' holds %s -- rebuilt incrementally in the same build directory" % (name, slot.load(sj).get("commit", "")[:12])
        except (OSError, ValueError):
            held = "none"
        log("would build a WebKit slot for image %s (builder: buildroot)" % self.name)
        log("  commit       %s" % commit)
        log("  slot         %s -> %s" % (name, slotdir))
        log("  workspace    %s (%s)" % (ws, target.info(ws)))
        log("  image        %s" % (image if self.here.exists(image) else
                                    "NOT BUILT -- the real run refuses here (wk sysimage build %s)" % self.spec))
        log("  toolchain    the image's own (output/host/share/buildroot/toolchainfile.cmake),")
        log("               options from the tree's WPEWEBKIT_CONF_OPTS, plus --build-id per slot")
        log("  jobs         -j%d (memory-sized at %d MB/job)" % (jobs, task.MB_PER_JOB))
        log("  existing     %s" % held)
        log("dry run -- nothing was built.")
        return 0
