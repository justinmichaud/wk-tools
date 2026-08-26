# Bound how many things LLVM links at once, so that its *compiles* can run wide.
#
# clang is the single most expensive recipe in this build -- 146 minutes of
# do_compile for clang-native, measured by buildstats, against 58 for the next
# one -- and it is expensive in the way that hurts most: it lands near the tail,
# where it is often the only recipe left. bitbake's parallelism is the product
# of BB_NUMBER_THREADS and PARALLEL_MAKE, which is right in the middle of a
# build and badly wrong at the ends: with one recipe left, a -j4 PARALLEL_MAKE
# leaves 76 of 80 cores idle for hours.
#
# image/yocto-build.sh raises PARALLEL_MAKE for this recipe to fix that. What
# makes raising it *safe* is this file, and the two halves are deliberately not
# in one place: the -j is a property of the machine doing the building, and this
# is a property of how LLVM builds, true on every machine.
#
# The distinction that matters: an LLVM compile is small and a link is not.
# Measured here, cc1plus on this recipe peaks around 334 MB, so even -j58 of
# them is under 20 GB. A link of libLLVM/clang/lld is gigabytes each, and
# meta-clang bounds neither -- links inherit PARALLEL_MAKE like everything else.
# So raising -j without this would put dozens of multi-gigabyte links in flight
# at once, which is how a build that used to fit in memory stops fitting.
#
# Derived from BB_NUMBER_THREADS rather than written as a constant, because a
# number that is safe on an 80-core workstation is not safe on a laptop. It is a
# proxy for machine size -- the honest input is free memory, which bitbake has
# no portable view of -- and it is capped, because past a handful of concurrent
# links the disk and the allocator are the limit rather than the core count.
#
# This is build-time only: LLVM_PARALLEL_LINK_JOBS changes how the image is
# built and nothing about what is in it, which is the rule conf/layer.conf
# keeps. Delete this file if meta-clang ever bounds its own link jobs.
LLVM_PARALLEL_LINK_JOBS ?= "${@min(8, max(1, int(d.getVar('BB_NUMBER_THREADS') or '1')))}"
EXTRA_OECMAKE:append = " -DLLVM_PARALLEL_LINK_JOBS=${LLVM_PARALLEL_LINK_JOBS}"
