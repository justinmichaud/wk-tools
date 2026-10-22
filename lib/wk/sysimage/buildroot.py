"""The buildroot builder's host half: its data and its two stages (the image, a WebKit slot) over task.ContainerBuilder's
lifecycle, around buildroot_ws.py in the workspace. The tree is a fork whose release-pinned `cog` defconfigs exist
nowhere else, not even at its 2020.02 tag: BR_TREE_COMMIT is a commit.
TODO: whether the rpi3 defconfig compiles wpa_supplicant at all is unverified."""

import os

from wk import act, fleet, images
from wk.act import die, info, log
from wk.sysimage import task
from wk.sysimage.ls import slot_doc
from wk.sysimage.write import wants_wifi

IMAGE_JOBS = 16    # 2009-era tarballs are where broken parallel rules live
WEBKIT_JOBS = 64   # WebKit links large; capped where the link steps stop gaining
DL_IN_WS = "/cache/buildroot/dl"
ZIMAGE_MAGIC = "016f2818"
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
    KIND, TITLE, BASE_VAR, PATTERN = "buildroot", "buildroot", "WK_BUILDROOT_BASE", "*buildroot_ws.py*"
    BASE_IMAGE = "docker.io/library/ubuntu:22.04"   # the host the wiki recipe was driven on (container/buildroot/Containerfile)
    SPEC = os.path.join("container", "buildroot", "Containerfile")
    NEEDS = "a buildroot image builds in a container workspace"
    NOT_HERE = "Build it on a machine whose container place holds it:  wk sysimage build %(spec)s@<machine>"
    IMAGE_NOTE = "  22.04 is the host the wiki recipe was driven on; a 2020 buildroot\n  does not survive a much newer one (%(spec)s)."
    SURVIVES = "the buildroot downloads are in the store and survive"
    USAGE = "usage: wk sysimage build %s [--workspace <name>|--detach|--stop]"
    WEBKIT_USAGE = "usage: wk sysimage webkit %s --commit <sha> --slot <name> [--workspace <name>|--detach]"

    def webkit(self, rest):
        self.verb = "webkit"
        return self.build(rest)

    def parse(self, rest):
        p = self.p
        if self.verb == "build":
            if not p["BR_DEFCONFIG"]:
                die("'%s' names no defconfig, so there is nothing to\n    build. Its image preset is %s."
                    % (self.name, images.conf_path(self.name, self.env)))
            if p["BR_KERNEL_DEB_URL"] and not (p["BR_KERNEL_DEB_SHA256"] and p["BR_KERNEL_RELEASE"]):
                die("%s pins a kernel but not its sha256 and release\n    (BR_KERNEL_DEB_SHA256, BR_KERNEL_RELEASE): a kernel by URL alone is not pinned." % self.name)
            return super().parse(rest)
        o = task.options(rest, ("--detach",), ("--workspace", "--commit", "--slot"), self.WEBKIT_USAGE % self.name)
        if not o.get("--commit") or not o.get("--slot"):
            die(self.WEBKIT_USAGE % self.name + "; see wk sysimage -h")
        images.check_slot_name(o["--slot"])
        task.check_commit(o["--commit"])
        return dict(o, stage="webkit-" + o["--slot"])

    def image(self, driver, ws):
        return os.path.join(driver.store.ws_dir(ws), "build", "buildroot", self.name, "output", "images", self.p["BR_IMAGE"] or "sdcard.img")

    def refuse(self, driver, st, ws, o):
        if self.verb == "webkit":
            if driver.info(ws) == "absent":
                die("no workspace '%s', so there is no image to build against.\n    Build the image first:  wk sysimage build %s" % (ws, self.spec))
            if not self.here.exists(self.image(driver, ws)):
                die("'%s' has no finished image (%s) for a slot to build against:  wk sysimage build %s" % (ws, self.image(driver, ws), self.spec))
        st.refuse_busy()

    def what(self, o):
        return "%s of %s" % ("build" if self.verb == "build" else "slot build", self.name)

    def wifi(self):
        return wants_wifi(fleet.Fleet(images.root(self.env), self.env), self.p["IMG_MACHINE"])

    def kernel(self, into):
        """Fetched and prepared here, where the network and depmod are; its path in the download cache both sides share."""
        p = self.p
        dtb = (fleet.Fleet(images.root(self.env), self.env).load(p["IMG_MACHINE"]) or {}).get("dtb", "")
        if not dtb:
            die("%s pins a kernel but machines/%s.conf names no dtb=" % (self.name, p["IMG_MACHINE"]))
        cache = task.cache_dir(self.env)
        deb = os.path.join(cache, os.path.basename(p["BR_KERNEL_DEB_URL"]))
        self.here.mkdir(cache)
        why = task.fetch_pinned(self.here, p["BR_KERNEL_DEB_URL"], deb, p["BR_KERNEL_DEB_SHA256"])
        if why:
            die(why)
        tar = kernel_pin(self.here, deb, p["BR_KERNEL_RELEASE"], self.cache("dl"))
        into += ["--kernel-tar", DL_IN_WS + "/" + os.path.basename(tar), "--kernel-dts", os.path.splitext(dtb)[0]]

    def plan(self, driver, st, ws, o):
        tools = driver.tools(ws)
        if self.verb == "webkit":
            budget, running, jobs = st.size(WEBKIT_JOBS)
            commit, name = o["--commit"], o["--slot"]
            return dict(sized=(budget, running, jobs), steps=[lambda: self.sync(driver, ws)],
                        plan=["sync wk-tools into '%s'" % ws, "build WebKit %s into slot '%s' with -j%d" % (commit[:12], name, jobs)],
                        say="building WebKit %s into slot '%s' of %s in '%s' (tens of minutes; --detach returns instead)"
                            % (commit[:12], name, self.name, ws),
                        argv=lambda: ["python3", tools + "/lib/wk/sysimage/buildroot_ws.py", "webkit", self.name, "--commit", commit,
                                      "--slot", name, "--jobs", str(jobs)])
        budget, running, jobs = st.size(IMAGE_JOBS)
        argv = ["python3", tools + "/lib/wk/sysimage/buildroot_ws.py", "image", self.name, "--jobs", str(jobs)] \
            + (["--wifi"] if self.wifi() else [])
        pin = [lambda: self.kernel(argv)] if self.p["BR_KERNEL_DEB_URL"] else []
        return dict(sized=(budget, running, jobs), steps=pin + [lambda: self.ensure_ws(driver, ws), lambda: self.sync(driver, ws)],
                    plan=(["prepare the pinned kernel %s" % self.p["BR_KERNEL_RELEASE"]] if pin else [])
                    + ["the workspace '%s' on %s" % (ws, self.host_image()[1]), "sync wk-tools into '%s'" % ws,
                       "build %s with -j%d" % (self.name, jobs)],
                    say="building %s in '%s' (hours; --detach returns instead)" % (self.name, ws), argv=lambda: argv)

    def sync(self, driver, ws):
        if not driver.sync_tools(ws):
            die("pushing wk-tools into '%s' failed -- the reason is above" % ws)

    def done(self, driver, ws, o):
        if self.verb == "build":
            info("built %s in '%s'" % (self.name, ws))
            return 0
        slotdir = images.slot_dir(ws, o["--slot"], self.env)
        if not self.here.exists(os.path.join(slotdir, "slot.json")):
            die("the build reported done but left no %s/slot.json" % slotdir)
        info("slot '%s' of %s holds %s" % (o["--slot"], self.name, o["--commit"][:12]))
        log("  %s\n  next:  wk bench deploy %s <board> --slot %s" % (slotdir, ws, o["--slot"]))
        return 0

    def report(self, driver, st, ws, o):
        p = self.p
        rows = [("workspace", "%s (%s)" % (ws, driver.info(ws)))]
        if self.verb == "webkit":
            _, _, jobs = st.size(WEBKIT_JOBS)
            slotdir, image = images.slot_dir(ws, o["--slot"], self.env), self.image(driver, ws)
            try:
                held = "slot '%s' holds %s -- rebuilt incrementally in the same build directory" % (
                    o["--slot"], slot_doc(os.path.join(slotdir, "slot.json")).get("commit", "")[:12])
            except (OSError, ValueError):
                held = "none"
            return ("build a WebKit slot for image %s (builder: buildroot)" % self.name, rows + [
                ("commit", o["--commit"]), ("slot", "%s -> %s" % (o["--slot"], slotdir)),
                ("image", image if self.here.exists(image) else "NOT BUILT -- the real run refuses here (wk sysimage build %s)" % self.spec),
                ("toolchain", "the image's own, options from the tree's WPEWEBKIT_CONF_OPTS, plus --build-id per slot"),
                ("jobs", "-j%d (memory-sized at %d MB/job)" % (jobs, task.MB_PER_JOB)), ("existing", held)])
        _, _, jobs = st.size(IMAGE_JOBS)
        dl, cc = self.cache("dl"), self.cache("ccache")
        return ("build image %s (builder: buildroot)" % self.name, rows + [
            ("for machine", "%s (%s)" % (p["IMG_MACHINE"] or "none", p["IMG_ARCH"] or "?")),
            ("tree", "%s @ %s" % (p["BR_TREE_URL"], p["BR_TREE_COMMIT"] or p["BR_TREE_BRANCH"] or "HEAD")),
            ("defconfig", p["BR_DEFCONFIG"] + (" (plus this repo's BR2_EXTERNAL)" if p["BR_EXTERNAL"] == "1" else "")),
            ("host image", "%s + %s" % (self.host_image()[0], self.SPEC)),
            ("jobs", "-j%d (memory-sized at %d MB/job)" % (jobs, task.MB_PER_JOB)),
            ("overlay", p["BR_OVERLAY_TAILSCALE"] or "none -- this image would join no tailnet"),
            ("wifi", "wk-wifi-join; the card carries the credential" if self.wifi() else "none -- %s has a cable" % (p["IMG_MACHINE"] or "this board")),
            ("kernel", "%s, pinned (%s)" % (p["BR_KERNEL_RELEASE"], os.path.basename(p["BR_KERNEL_DEB_URL"]))
             if p["BR_KERNEL_DEB_URL"] else "built from the tree by buildroot"),
            ("DL_DIR", "%s (%s) -- BR2_DL_DIR in the container" % (dl, self.du(dl))),
            ("CCACHE_DIR", "%s (%s) -- BR2_CCACHE_DIR in the container" % (cc, self.du(cc))),
            ("output", "in the workspace, under output/images")])
