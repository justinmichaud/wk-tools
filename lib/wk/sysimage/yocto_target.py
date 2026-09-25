"""The yocto image build as it runs inside a workspace (lib/wk/sysimage/yocto.py is the host half), around
Tools/Scripts/cross-toolchain-helper, which stays the upstream interface for the build itself. Run as
`python3 /opt/wk-tools/lib/wk/sysimage/yocto_target.py`, under task.stage_main, which takes the wall off PATH."""

import argparse
import configparser
import io
import os
import re
import shlex
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from wk.clock import Clock  # noqa: E402
from wk.machine import here  # noqa: E402

TOOLS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
STAGES = ("layers", "fetch", "image", "toolchain", "webkit", "pgo-mix")
DEFAULT_IMAGE = "webkit-dev-ci-tools"
WEBKIT_MB_PER_JOB = 2560
# The wkdev SDK's dev environment hands host headers and libraries to a cross build and stops bitbake's sanity checker.
UNSET = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "OBJCPLUS_INCLUDE_PATH",
         "PKG_CONFIG_PATH", "PKG_CONFIG_LIBDIR", "LD_LIBRARY_PATH", "LD_PRELOAD")
# dotfiles/gitconfig's index format (v4, null checksum) is one the toolchains' libgit2 cannot open (cargo in rust-native).
GIT_PIN = (("index.version", "2"), ("index.skipHash", "false"))
PASSTHROUGH = "DL_DIR SSTATE_DIR GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_KEY_1 GIT_CONFIG_VALUE_1"
HOST_TOOLS = ("gawk", "chrpath", "diffstat", "cpio", "makeinfo", "socat", "file", "zstd", "xz", "bzip2", "gcc", "g++",
              "perl", "python3", "git", "patch", "which", "unzip", "wget")
HOST_MODULES = (("git", "python3-git"), ("jinja2", "python3-jinja2"), ("pexpect", "python3-pexpect"))
# Worth more than their quarter of the machine, since bitbake cannot rebalance PARALLEL_MAKE mid-build.
BIG_RECIPES = ("clang", "clang-native", "clang-cross-arm", "clang-cross-aarch64", "llvm", "llvm-native", "rust-llvm",
               "rust-llvm-native", "mozjs-115", "boost", "gdb", "linux-raspberrypi")
LAYERS = ("meta-wk", "meta-wk-tailnet", "meta-wk-wifi", "meta-wk-rescue")
MARKER = "# --- wk (image/yocto-build.sh) ---"
BB_MARKER = "# --- wk layers (image/yocto-build.sh) ---"
# wpe-2.46 defaults both on and CMake refuses the pair; run-benchmark's WPE driver wants the platform API.
WEBKIT_CMAKE = "-DENABLE_WPE_PLATFORM=ON -DENABLE_WPE_1_1_API=OFF"
MISSING = """the workspace image is missing Yocto host tooling:%s

    These are host-side build dependencies, so they belong in the workspace
    image (container/yocto/Containerfile) rather than apt-installed into a
    workspace that is thrown away."""


class Failed(Exception):
    pass


def fail(text):
    raise Failed(text)


def say(text):
    sys.stdout.write("wk-yocto: %s\n" % text)
    sys.stdout.flush()


def parse(argv):
    ap = argparse.ArgumentParser(prog="yocto_target.py")
    for flag in ("--target", "--image", "--jobs", "--mem-budget", "--commit", "--slot", "--profile", "--sstate-ns",
                 "--port-target-from", "--port-machine", "--board", "--multilib", "--multilib-tune", "--cross-cc",
                 "--cross-cxx", "--cross-cmake", "--pgo-dir", "--pgo-lib", "--webkit-jobs"):
        ap.add_argument(flag, default="")
    ap.add_argument("--stage", default="image", choices=STAGES)
    ap.add_argument("--src", default="/src/WebKit")
    ap.add_argument("--cross-config", default="wpe-cross")
    for flag, default in (("--rm-work", "1"), ("--chromium", "0"), ("--local-layer", "1"), ("--tailnet", "1")):
        ap.add_argument(flag, default=default, choices=("0", "1"))
    a = ap.parse_args(argv)
    if not a.target:
        ap.error("--target is required")
    return a


def build_env(environ, a, locales):
    env = {k: v for k, v in environ.items() if k not in UNSET}
    env["GIT_CONFIG_COUNT"] = str(len(GIT_PIN))
    for i, (k, v) in enumerate(GIT_PIN):
        env["GIT_CONFIG_KEY_%d" % i], env["GIT_CONFIG_VALUE_%d" % i] = k, v
    # The helper wipes the workdir whenever the target's conf moves, and a slot's commit moves it: the image and SDK stay.
    env["WEBKIT_CROSS_WIPE_ON_CHANGE"] = "0"
    env["BB_ENV_PASSTHROUGH_ADDITIONS"] = (environ.get("BB_ENV_PASSTHROUGH_ADDITIONS", "") + " " + PASSTHROUGH).strip()
    utf8 = any(re.match(r"^en_US\.utf-?8$", l.strip(), re.I) for l in locales.splitlines())
    env["LANG"] = env["LC_ALL"] = "en_US.UTF-8" if utf8 else "C.UTF-8"
    if a.mem_budget:
        env["WK_MEM_BUDGET_MB"] = a.mem_budget
    if a.sstate_ns and env.get("SSTATE_DIR"):
        env["SSTATE_DIR"] = os.path.join(env["SSTATE_DIR"], a.sstate_ns)   # uninative on and off do not share sstate
    return env


def threads(jobs):
    bb = max(1, jobs // 4)
    return bb, jobs // bb


def strip_block(text, marker):
    lines = text.split("\n")
    return "\n".join(lines[:lines.index(marker)]) if marker in lines else text


def local_conf(a, env, jobs, board_append):
    bb, par = threads(jobs)
    out = ["", MARKER, "# Written by lib/wk/sysimage/yocto_target.py; the workdir is wiped whenever the target config changes.", "",
           'DL_DIR = "%s"' % env["DL_DIR"], 'SSTATE_DIR = "%s"' % env["SSTATE_DIR"], ""]
    if a.multilib:
        out += ["require conf/multilib.conf", 'MULTILIBS = "multilib:%s"' % a.multilib,
                'DEFAULTTUNE:virtclass-multilib-%s = "%s"' % (a.multilib, a.multilib_tune), ""]
    out += ['BB_NUMBER_THREADS = "%d"' % bb, 'PARALLEL_MAKE = "-j %d"' % par, ""]
    out += ['PARALLEL_MAKE:pn-%s = "-j %d"' % (r, jobs) for r in BIG_RECIPES]
    out += ["", 'BB_PRESSURE_MAX_MEMORY = "10000"', ""]
    # Not PREMIRRORONLY: meta-webkit, meta-clang and meta-raspberrypi fetch from github, which the mirror lacks.
    out += ['INHERIT += "own-mirrors"', 'SOURCE_MIRROR_URL = "https://downloads.yoctoproject.org/mirror/sources/"',
            'BB_GENERATE_MIRROR_TARBALLS = "1"', ""]
    out += ['BB_DISKMON_DIRS = "\\', "    STOPTASKS,${TMPDIR},10G,100K \\", "    STOPTASKS,${DL_DIR},5G,100K \\",
            "    STOPTASKS,${SSTATE_DIR},5G,100K \\", "    HALT,${TMPDIR},5G,1K \\", "    HALT,${DL_DIR},2G,1K \\",
            '    HALT,${SSTATE_DIR},2G,1K"', ""]
    out += (["# tailnet: off. This image joins nothing and is reachable only over whatever LAN it lands on.", ""]
            if a.tailnet == "0" else ['IMAGE_INSTALL:append = " tailscale"', ""])
    out += ['IMAGE_INSTALL:append = " wk-wifi-join"', "", 'IMAGE_INSTALL:append = " wk-card-priv"', ""]
    if a.chromium == "0":
        out += ["# Chromium dropped: about half the build. --chromium puts it back.", 'IMAGE_INSTALL:remove = "chromium-ozone-wayland"', ""]
    if a.rm_work == "1":
        out += ['INHERIT += "rm_work"', 'RM_WORK_EXCLUDE += "%s"' % (a.image or DEFAULT_IMAGE), ""]
    if board_append is not None:
        out += ["# --- image/boards/%s/local.conf.append ---" % a.board, board_append.rstrip("\n")]
    return "\n".join(out) + "\n"


def webkit_args(targets_conf, target):
    c = configparser.ConfigParser()
    c.read_string(targets_conf)
    toks = shlex.split(c[target].get("environment[BUILD_WEBKIT_ARGS]", ""))
    cmake = [t[len("--cmakeargs="):] for t in toks if t.startswith("--cmakeargs=")]
    return [t for t in toks if not t.startswith("--cmakeargs=")], cmake[-1] if cmake else ""


def port_target(conf_text, local_texts, target, frm, machine, image):
    cp = configparser.ConfigParser()
    cp.read_string(conf_text)
    if cp.has_section(target):
        return None
    if not cp.has_section(frm):
        fail("this branch has no [%s] to derive [%s] from; its sections are: %s" % (frm, target, ", ".join(cp.sections())))
    src = dict(cp.items(frm))
    src_local = src.get("conf_local_path")
    if not src_local:
        fail("[%s] names no conf_local_path, so there is nothing to derive" % frm)
    text = local_texts(src_local)
    if text is None:
        fail("[%s] names %s, which is not in this checkout" % (frm, src_local))
    swapped, n = re.subn(r"(?m)^\s*MACHINE\s*=.*$", 'MACHINE = "%s"' % machine, text)
    if n != 1:
        fail("%s sets MACHINE %d times; expected exactly one line to change" % (src_local, n))
    new_local = os.path.join(os.path.dirname(src_local), "local-%s.conf" % target)
    cp[target] = dict(src, conf_local_path=new_local, **({"image_basename": image} if image else {}))
    out = io.StringIO()
    cp.write(out)
    return out.getvalue(), {new_local: swapped}


class Build:
    def __init__(self, a, machine, env, clock, tools=TOOLS):
        self.a, self.m, self.env, self.clock, self.tools = a, machine, env, clock, tools
        self.jobs = int(a.jobs) if a.jobs else int(machine.run(["nproc"]).out.strip() or 4)
        self.src = a.src
        self.workdir = os.path.join(a.src, "WebKitBuild", "CrossToolChains", a.target)
        self.conf = os.path.join(self.workdir, "build", "conf", "local.conf")
        self.helper = os.path.join(a.src, "Tools", "Scripts", "cross-toolchain-helper")
        self.image_dir = os.path.join(self.workdir, "build", "image")

    def git(self, d, *args):
        return self.m.act_run(["git", "-C", d] + list(args))

    def guarded(self, jobs, argv, mb_per_job=None):
        pre = ["env", "WK_MB_PER_JOB=%d" % mb_per_job] if mb_per_job else []
        return pre + ["bash", "-c", '. %s/build/guard.sh && guard_run "$0" -- "$@"' % self.tools, str(jobs)] + list(argv)

    def check_host(self):
        script = ("for t in %s; do command -v \"$t\" >/dev/null 2>&1 || echo \"$t\"; done; "
                  "command -v lz4c >/dev/null 2>&1 || command -v lz4 >/dev/null 2>&1 || echo lz4; " % " ".join(HOST_TOOLS)
                  + "".join("python3 -c 'import %s' 2>/dev/null || echo %s; " % mp for mp in HOST_MODULES))
        missing = self.m.run(["sh", "-c", script]).out.split()
        if missing:
            fail(MISSING % "".join(" " + t for t in missing))
        if self.m.run(["id", "-u"]).out.strip() == "0":
            fail("bitbake refuses to run as root, and this shell is root.\n    A workspace's builds run as the workspace "
                 "user; something started this one\n    with 'podman exec -u root' or similar.")
        for name in ("DL_DIR", "SSTATE_DIR"):
            d = self.env.get(name)
            if not d:
                fail("DL_DIR/SSTATE_DIR are not set in this workspace. They come from the container's\n    store-backed "
                     "cache mount (targets/container.sh); without them the caches would die with it.")
            self.m.mkdir(d)

    def refresh_git_index(self, d):
        if not self.m.exists(os.path.join(d, ".git")):
            return
        for k, v in GIT_PIN:
            self.git(d, "config", k, v)
        if not self.m.run(["git", "-C", d, "ls-files", "--", ".wk-no-such-path"]).ok:   # a killed stage can leave a 0-byte index
            gd = self.m.run(["git", "-C", d, "rev-parse", "--absolute-git-dir"]).out.strip()
            if gd:
                self.m.remove(os.path.join(gd, "index"))
            self.git(d, "read-tree", "HEAD")
            say("rebuilt an unreadable git index in %s from HEAD" % d)
        self.git(d, "update-index", "--really-refresh")

    def checkout_slot_commit(self):
        """Forced and cleaned: a killed webkit stage leaves the checkout mid-checkout. `-fd`, never `-fdx`: WebKitBuild holds the lane."""
        c, mirror = self.a.commit, self.env.get("WK_MIRROR")
        if not mirror:
            fail("WK_MIRROR names the mirror this container mounts (targets/container.sh), and it is not set")
        if not self.m.run(["git", "-C", self.src, "cat-file", "-e", c + "^{commit}"]).ok \
                and not self.git(self.src, "fetch", "--quiet", mirror, c).ok:
            fail("%s is not in this machine's mirror; 'wk bench ab' and 'wk pr' fetch a PR head into it first" % c)
        dirty = len([l for l in self.m.run(["git", "-C", self.src, "status", "--porcelain"]).out.splitlines() if l.strip()])
        if dirty:
            say("discarding %d uncommitted path(s) in %s -- a slot is built from a commit and nothing else" % (dirty, self.src))
        if not self.git(self.src, "checkout", "--force", "--detach", "--quiet", c).ok:
            fail("could not check out %s in %s" % (c, self.src))
        if not self.git(self.src, "clean", "-qfd").ok:
            fail("could not clean %s after checking out %s" % (self.src, c))
        self.refresh_git_index(self.src)
        say("source        %s @ %s" % (self.src, self.m.run(["git", "-C", self.src, "log", "-1", "--format=%h (%s)"]).out.strip()[:80]))

    def port(self):
        a, ydir = self.a, os.path.join(self.src, "Tools", "yocto")
        if not a.port_target_from:
            return
        if not a.port_machine:
            fail("this profile names a target to derive [%s] from but no YOC_MACHINE for\n    it to select, so the derived "
                 "local.conf would name the wrong machine." % a.target)
        say("porting       [%s] from [%s] (MACHINE=%s) -- this branch has no such section" % (a.target, a.port_target_from, a.port_machine))

        def local(rel):
            p = os.path.join(ydir, rel)
            return self.m.read(p) if self.m.exists(p) else None

        conf = os.path.join(ydir, "targets.conf")
        if not self.m.exists(conf):
            fail("no targets.conf in %s" % ydir)
        got = port_target(self.m.read(conf), local, a.target, a.port_target_from, a.port_machine, a.image)
        if got is None:
            say("targets.conf already has [%s]; nothing to port" % a.target)
            return
        text, locals_ = got
        for rel, body in locals_.items():
            self.m.write(os.path.join(ydir, rel), body)
        self.m.write(conf, text)

    def check_available(self):
        r = self.m.run([self.helper, "--print-available-targets", "--log-level", "quiet"])
        if self.a.target not in r.out.split():
            fail("'%s' is not a cross-target in this checkout.\n    Available here:\n%s"
                 % (self.a.target, "".join("      %s\n" % t for t in r.out.split())))

    def init_workdir(self):
        if self.m.exists(os.path.join(self.workdir, ".target-info-version")) and self.m.exists(self.conf):
            say("layers already synced at %s" % self.workdir)
        else:
            say("syncing Yocto layers (repo sync -- this is the network-bound part)")
            if not self.m.act_run([self.helper, "--cross-target=" + self.a.target, "--bitbake-dev-shell"], input="").ok:
                fail("the layer sync failed. It is almost always egress: 'repo' fetches from\n    git.yoctoproject.org, github.com "
                     "and gerrit.googlesource.com, and a workspace\n    reaches them only through the proxy allowlist "
                     "(container/proxy/wk-proxy.py). Its log\n    names what was refused.")
            if not self.m.exists(self.conf):
                fail("the layer sync reported success but wrote no %s" % self.conf)
        self.refresh_git_index(self.workdir)

    def configure_local_conf(self):
        if not self.m.exists(self.conf):
            fail("no %s -- the layer sync did not run" % self.conf)
        text = self.m.read(self.conf)
        say("refreshing the wk additions in local.conf" if MARKER in text.split("\n") else "appending the wk additions to local.conf")
        board = os.path.join(self.tools, "image", "boards", self.a.board, "local.conf.append") if self.a.board else ""
        append = self.m.read(board) if board and self.m.exists(board) else None
        self.m.write(self.conf, strip_block(text, MARKER).rstrip("\n") + "\n" + local_conf(self.a, self.env, self.jobs, append))

    def configure_bblayers(self):
        f = os.path.join(self.workdir, "build", "conf", "bblayers.conf")
        if not self.m.exists(f):
            fail("no %s -- the layer sync did not run" % f)
        text = strip_block(self.m.read(f), BB_MARKER).rstrip("\n") + "\n"
        if self.a.local_layer == "0":
            self.m.write(f, text)
            say("no local layer: this profile builds the branch's own configuration unmodified")
            return
        lines = ["", BB_MARKER]
        for name in LAYERS + (("meta-wk-multilib",) if self.a.multilib else ()):
            layer = os.path.join(self.tools, "image", "yocto", name)
            if not self.m.exists(os.path.join(layer, "conf", "layer.conf")):
                fail("no layer at %s" % layer)
            lines.append('BBLAYERS += "%s"' % layer)
            say("layer added: %s" % layer)
        self.m.write(f, text + "\n".join(lines) + "\n")

    def clear_hosttools(self):
        d = os.path.join(self.workdir, "build", "tmp", "hosttools")
        if self.m.isdir(d):
            say("clearing tmp/hosttools so bitbake re-resolves the host tools")
            self.m.remove(d)

    def bitbake(self, args):
        """WEBKIT_CROSS_TARGET/VERSION from the file the helper wrote: without them meta-webkit's distro conf falls back
        to the machine name and today's date, and sstate is invalidated."""
        info = os.path.join(self.workdir, ".target-info-version")
        if not self.m.exists(info):
            fail("no %s -- the layer sync did not finish" % info)
        words = self.m.read(info).split()
        line = ('cd %s && set +u && . ./oe-init-build-env %s >/dev/null && . %s/build/guard.sh && guard_run %d -- bitbake "$@"'
                % (shlex.quote(os.path.join(self.workdir, "sources", "poky")), shlex.quote(os.path.join(self.workdir, "build")),
                   self.tools, self.jobs))
        return self.m.run_tty(["env", "WEBKIT_CROSS_TARGET=" + words[0], "WEBKIT_CROSS_VERSION=" + (words[1] if len(words) > 1 else ""),
                               "bash", "-c", line, "bitbake"] + list(args), cwd=self.src).ok

    def helper_run(self, what, args):
        say(what)
        if not self.m.run_tty(self.guarded(self.jobs, [self.helper, "--cross-target=" + self.a.target] + list(args)), cwd=self.src).ok:
            fail("%s failed" % what)

    def require_toolchain(self):
        """Asked here: build-webkit bitbakes the whole nativesdk stack when the SDK is missing, under a budget booked for one compile."""
        d = os.path.join(self.workdir, "build", "toolchain")
        setup = ""
        if self.m.exists(os.path.join(d, ".toolchain_path_configured")):
            setup = next((os.path.join(d, n) for n in self.m.listdir(d) if n.startswith("environment-setup-")), "")
        if not setup:
            fail("this lane has no cross toolchain installed, so cross-building WebKit here\n    would bitbake the whole nativesdk "
                 "stack under a budget sized for one\n    WebKit compile. Build it as its own stage first, which books the "
                 "whole machine:\n\n        wk sysimage build <profile> --stage toolchain\n\n    What is looked for is "
                 "%s/.toolchain_path_configured\n    and an environment-setup script beside it." % d)
        say("SDK           %s" % setup)

    def move(self, src, dst):
        if not self.m.act_run(["mv", "-T", src, dst]).ok:
            fail("could not move %s to %s" % (src, dst))

    def copies_aside(self):
        """The helper takes a file at build/image as proof the image is current, so it is moved aside, not deleted:
        a killed stage must leave the lane its last image."""
        prev = self.image_dir + ".previous"
        self.m.remove(prev)
        if self.m.isdir(self.image_dir):
            say("moving %s aside to %s, so the helper cannot report a previous run's image as this one's" % (self.image_dir, prev))
            self.move(self.image_dir, prev)

    def copies_back(self):
        prev = self.image_dir + ".previous"
        if not self.m.isdir(prev):
            return
        if self.m.isdir(self.image_dir) and any(not self.m.isdir(os.path.join(self.image_dir, n)) for n in self.m.listdir(self.image_dir)):
            self.m.remove(prev)
            return
        say("putting the image an earlier stage moved aside back at %s (it built no replacement)" % self.image_dir)
        self.m.remove(self.image_dir)
        self.move(prev, self.image_dir)

    def verify_fresh(self, start):
        r = self.m.run(["find", self.image_dir, "-maxdepth", "1", "-type", "f", "-printf", "%T@\n"])
        stamps = [float(x) for x in r.out.split()] if r.ok else []
        if not stamps:
            fail("bitbake produced no image directory at %s; the helper reported\n    success but left nothing behind." % self.image_dir)
        if int(max(stamps)) < int(start):
            fail("bitbake produced no new image; the helper reported a stale one. The newest\n    file in %s predates this "
                 "stage, which the move aside makes\n    impossible -- so cross-toolchain-helper changed how it decides an "
                 "image is\n    built, and copies_aside (lib/wk/sysimage/yocto_target.py) must change with it." % self.image_dir)

    def summary(self):
        a = self.a
        bb, par = threads(self.jobs)
        say("target        %s" % a.target)
        say("image recipe  %s" % (a.image or "<from targets.conf>"))
        if a.multilib:
            say("multilib      %s at %s -- the userspace width, not the machine's" % (a.multilib, a.multilib_tune))
        say("stage         %s" % a.stage)
        say("chromium      %s" % ("dropped (about half the build; --chromium puts it back)" if a.chromium == "0" else "in the image (--chromium)"))
        say("jobs          BB_NUMBER_THREADS=%d PARALLEL_MAKE=-j%d (from %d); -j%d for %d named recipes"
            % (bb, par, self.jobs, self.jobs, len(BIG_RECIPES)))
        say("DL_DIR        %s" % self.env.get("DL_DIR", ""))
        say("SSTATE_DIR    %s" % self.env.get("SSTATE_DIR", ""))
        say("locale        %s" % self.env.get("LANG", ""))

    def prepare(self):
        self.init_workdir()
        self.configure_local_conf()
        self.configure_bblayers()

    def webkit(self):
        a = self.a
        self.prepare()
        self.clear_hosttools()
        self.require_toolchain()
        say("cross-building WebKit (WPE, Release) for %s" % a.target)
        try:
            flags, cmake = webkit_args(self.m.read(os.path.join(self.src, "Tools", "yocto", "targets.conf")), a.target)
        except (OSError, KeyError, configparser.Error, ValueError):
            fail("could not read BUILD_WEBKIT_ARGS for %s out of Tools/yocto/targets.conf" % a.target)
        cmake = " ".join(x for x in (cmake, WEBKIT_CMAKE, a.cross_cmake) if x)
        jobs = int(a.webkit_jobs or 8)
        say("  config:       %s" % a.cross_config)
        say("  target flags: %s" % (" ".join(flags) or "none"))
        say("  cmakeargs:    %s" % cmake)
        pre = ["env", "CC=" + a.cross_cc, "CXX=" + a.cross_cxx] if a.cross_cc else []   # the SDK's setup exports its clang only when named
        # build-webkit's own -j$(nproc) OOMs on WebCore's unified sources, and bitbake's PARALLEL_MAKE does not reach it.
        argv = pre + [os.path.join(self.src, "Tools", "Scripts", "build-webkit"), "--wpe", "--release", "--cross-target=" + a.target] \
            + flags + ["--makeargs=-j%d" % jobs, "--cmakeargs=" + cmake]
        if not self.m.run_tty(self.guarded(jobs, argv, WEBKIT_MB_PER_JOB), cwd=self.src).ok:
            fail("the cross build of WebKit failed")
        if a.slot:
            self.slot(jobs)

    def slot(self, jobs):
        a = self.a
        b = os.path.join(self.src, "WebKitBuild", "WPE", "Release_" + a.target)
        slotdir = os.path.join(self.src, "WebKitBuild", "wk-slots", a.slot)
        if not self.m.exists(os.path.join(b, "bin", "MiniBrowser")):
            fail("the cross build left no %s/bin/MiniBrowser" % b)
        root = os.path.join(slotdir, "root")
        self.m.remove(root)
        self.m.mkdir(root)
        if not self.m.act_run(["cp", "-a", os.path.join(b, "bin"), os.path.join(b, "lib"), root + "/"]).ok:
            fail("could not copy the build into %s" % slotdir)
        rev = self.m.run(["git", "-C", self.tools, "rev-parse", "--short", "HEAD"])
        fields = dict(slot=a.slot, profile=a.profile, commit=a.commit, target=a.target, build_config=a.cross_config,
                      browser="minibrowser", lib_dir="lib", exec_dir="bin", bundle_dir="lib", jobs=str(jobs),
                      built_at=self.clock.iso(), wk_tools=rev.out.strip() if rev.ok else "unknown")
        slot_py = os.path.join(self.tools, "lib", "wkslot.py")
        if not self.m.act_run(["python3", slot_py, "manifest", root, os.path.join(slotdir, "slot.json")]
                              + ["%s=%s" % kv for kv in sorted(fields.items())]).ok:
            fail("could not describe the build as a slot; a cross build with no build-id\n    note cannot be told apart on the board")
        bid = self.m.run(["python3", slot_py, "get", os.path.join(slotdir, "slot.json"), "build_id"]).out.strip()
        say("slot ready: %s (build-id %s)" % (slotdir, bid))

    def pgo_mix(self):
        a = self.a
        if not (a.pgo_dir and a.pgo_lib):
            fail("the pgo-mix stage needs --pgo-dir and --pgo-lib")
        if not self.m.isdir(a.pgo_dir):
            fail("no collection at %s; a board's PGO collection puts one there" % a.pgo_dir)
        self.init_workdir()
        say("mixing %s profiles from %s at WebKit's own weights" % (a.pgo_lib, a.pgo_dir))
        pgo = ["env", "PYTHONPATH=" + os.path.join(self.tools, "lib"), "python3", "-m", "wk.pgo"]
        common = ["--scripts", os.path.join(self.src, "Tools", "Scripts"), "--dir", a.pgo_dir, "--lib", a.pgo_lib]
        self.helper_run("mix the collected profiles", ["--cross-toolchain-run-cmd"] + pgo + ["mix"] + common)
        self.helper_run("read the mixed profile back", ["--cross-toolchain-run-cmd"] + pgo + ["check"] + common
                        + ["--json", os.path.join(a.pgo_dir, "profile-check.json")])

    def run(self):
        a = self.a
        if not self.m.isdir(self.src):
            fail("no checkout at %s" % self.src)
        if a.multilib and not a.multilib_tune:
            fail("this profile asks for the '%s' multilib but names no YOC_MULTILIB_TUNE for it,\n    so the image would "
                 "not be the width it is named for." % a.multilib)
        self.check_host()
        self.refresh_git_index(self.src)
        if a.stage == "webkit" and a.commit:
            self.checkout_slot_commit()
        self.summary()
        self.port()
        self.check_available()
        self.copies_back()
        if a.stage == "layers":
            self.prepare()
            say("layers ready at %s" % self.workdir)
        elif a.stage == "fetch":
            self.prepare()
            self.clear_hosttools()
            say("fetching every source for %s (--runall=fetch -k)" % (a.image or "the image"))
            if not self.bitbake(["--runall=fetch", "-k", a.image or DEFAULT_IMAGE]):
                say("some fetches failed; the ERROR lines above name the recipes and the proxy's log")
                say("names the hosts it refused:  journalctl --user -u wk-proxy -g DENY")
                fail("not every source could be fetched")
        elif a.stage == "image":
            self.prepare()
            self.clear_hosttools()
            self.copies_aside()
            start = self.clock.now()
            self.helper_run("bitbake %s (rootfs + kernel + wic)" % (a.image or "the image"), ["--build-image"])
            self.verify_fresh(start)
            self.m.remove(self.image_dir + ".previous")
        elif a.stage == "toolchain":
            self.prepare()
            self.clear_hosttools()
            self.helper_run("bitbake populate_sdk (the cross toolchain)", ["--build-toolchain"])
        elif a.stage == "webkit":
            self.webkit()
        else:
            self.pgo_mix()
        say("stage '%s' done" % a.stage)


def main(argv, environ=None, machine=None, clock=None, tools=TOOLS):
    a = parse(argv)
    m = machine or here()
    env = build_env(os.environ if environ is None else environ, a, m.run(["locale", "-a"]).out)
    if environ is None:
        os.environ.clear()
        os.environ.update(env)
    try:
        Build(a, m, env, clock or Clock(), tools).run()
    except Failed as e:
        sys.stderr.write("wk-yocto: error: %s\n" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
