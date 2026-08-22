# pseudo, new enough to work on this workstation's kernel.
#
# scarthgap pins pseudo at e11ae91 ("1.9.0+git", 2024). On this host -- aarch64,
# kernel 7.0.11 -- that pseudo cannot track the directory fd that `tar` hands to
# `mkdirat`, so every recipe whose do_package copies a tree dies:
#
#   got *at() syscall for unknown directory, fd 4
#   unknown base path for fd 4, path sbin
#   tar: ./usr/sbin: Cannot mkdir: Bad address
#
# That is reproducible in three lines with no bitbake involved --
# `pseudo bash -c 'tar -cf - . | tar -xf -'` -- and reproduces identically in a
# plain `podman run` with default seccomp and no sandbox, so it is a property of
# pseudo and this host and nothing to do with how wk builds. The four other
# candidates (mixed sstate, the host's tar, the overlay checkout, wk's sandbox)
# were each tested and refuted; docs/HANDOFF-yocto.md records how.
#
# Upstream has fixed this class of bug repeatedly since that pin, and two of the
# commits name our exact symptom:
#
#   c63f439  ports/linux/guts: Add __open64_2 wrapper
#            -- a *fortified* glibc open variant pseudo did not wrap. An fd
#            opened through it is invisible to pseudo, which is precisely
#            "unknown directory, fd N".
#   b3958b0  makewrappers: Avoid efault workaround if using AT_EMPTY_PATH
#            -- and EFAULT is the "Bad address" above.
#
# Also picked up on the way: openat2 and close_range wrappers, and several
# memory-handling fixes in pseudo_util.
#
# Why this does not weaken the 2.48 pin: pseudo is a build-time fakeroot and is
# never installed into the image. Bumping it changes how the image is *built*,
# not what it contains -- unlike moving poky, which would change the
# distribution itself. See conf/layer.conf for the rule this layer keeps.
#
# Delete this file when the pinned poky carries pseudo >= 1.9.11.

SRCREV = "ba8887e5f1e922f866681ec7dec1a00b602a9328"
PV = "1.9.11+git"

# scarthgap's own patches are in that revision already, so applying them again
# fails do_patch ("Hunk #1 FAILED ... rejects in file configure"). Each is
# upstream by name:
#
#   0001-configure-Prune-PIE-flags.patch  -> 6831273 configure: Prune PIE flags
#   glibc238.patch                        -> 865ca5b pseudo/pseudo_client: Add
#                                            wrapper functions to operate
#                                            correctly with glibc 2.38 onwards
#
# Removed by name rather than by clearing SRC_URI: the recipe also fetches the
# prebuilt sqlite tarball and two fallback passwd/group files from there, and
# they are still wanted.
# All three in one assignment, deliberately: `:remove` is a variable flag, so a
# second `SRC_URI:remove = ...` would *replace* this list rather than add to it,
# and the two already-upstream patches would come back and fail do_patch again.
SRC_URI:remove = "file://0001-configure-Prune-PIE-flags.patch \
                  file://glibc238.patch \
                  file://older-glibc-symbols.patch"

# And the third, which needs its own justification because it is not simply
# already-upstream. `older-glibc-symbols.patch` makes pseudo link against older
# glibc symbol versions, so that a binary built on a newer host still runs on an
# older one -- i.e. so sstate can travel between hosts. It no longer applies
# (upstream's Makefile.in has moved; upstream carries the same patch only as an
# unapplied reference, 137d7be).
#
# Dropping it is safe *here* because that portability is something this setup
# deliberately does not rely on: SSTATE_DIR is namespaced per build-host image
# (image/yocto-build.sh), so sstate is never handed to a different host in the
# first place. If that ever changes, this line has to be reconsidered before the
# namespacing is removed.
#
# Unconditional, and it was `:class-native` until 2026-08-21. pseudo is built
# once per variant, and the variant that matters here was the one nobody had
# reached yet: `--stage image` builds **pseudo-native**, `populate_sdk` builds
# **nativesdk-pseudo**, and a `:class-native` override does not touch the
# nativesdk one. So the toolchain stage died on its first run at
# `nativesdk-pseudo do_patch` -- the same patch, the same failure, in the one
# variant the override missed. The patch applies to no variant of 1.9.11, so
# the removal belongs everywhere rather than in a second override beside the
# first.
# (removed in the single assignment above)
