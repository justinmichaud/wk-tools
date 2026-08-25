# Build fixes for the buildroot this repository pins, and nothing else.
#
# Included by buildroot's top-level Makefile *after* every package/*/*.mk
# (2020.02 Makefile, line 533 vs line 547), which is what makes the two lines
# below able to change a package the tree already defined.
#
# --- host-python 2.7 cannot build on an arm64 build host ----------------------
#
# Building `raspberrypi3_wpe_2_38_cog_defconfig` in an ubuntu:20.04 container,
# the toolchain builds (host-gcc-final 9.2.0) and then host-python-2.7.17 dies
# at `sharedmods`, because its bundled 2013-era libffi's `aarch64/sysv.S` does
# not assemble. An *architecture* problem and not an
# old-distro one -- on x86_64 that file is never compiled, which is why this
# tree always built on moose and fails on this Mac.
#
# Buildroot ships a host-libffi package and gives the *target* python
# `--with-system-ffi`, and at the pin it simply never joins the two for the host
# build. That is not a local opinion: upstream buildroot made exactly this
# change for host-python between 2021.02 and 2021.08 --
#
#   HOST_PYTHON_CONF_OPTS  += --with-system-ffi
#   HOST_PYTHON_DEPENDENCIES = host-expat host-libffi host-zlib
#
# -- and the fork's `wpe` branch head carries it too. Tag 2020.02 does not, in
# either tree, so the difference is the pin and not the fork. The fix here is
# the upstream one applied from outside rather than a patch against somebody
# else's vendor branch.
#
# Both lines are idempotent: on a pin that already has the fix, the option is
# passed twice and the ordering is already guaranteed.

# Recipe-time expansion ($$($$(PKG)_CONF_OPTS) in pkg-autotools.mk), so
# appending after python.mk was read still reaches the configure line.
HOST_PYTHON_CONF_OPTS += --with-system-ffi

# Recipe-time as well, in prepare-per-package-directory (pkg-generic.mk), which
# is what copies a dependency's files into this package's tree when
# BR2_PER_PACKAGE_DIRECTORIES is on. It also fixes `make host-python-depends`.
HOST_PYTHON_DEPENDENCIES += host-libffi

# What the append above cannot do, and the reason it is not enough on its own:
# the rule
#
#   $(HOST_PYTHON_TARGET_CONFIGURE): | $(HOST_PYTHON_FINAL_DEPENDENCIES)
#
# had its prerequisite list expanded when pkg-generic.mk defined it, long before
# this file was read, so nothing appended here appears in it. A second rule for
# the same target with prerequisites and no recipe is how make is told to add
# one -- and it must be order-only, like the original, because a phony package
# target is always newer than a stamp file and would otherwise reconfigure
# python on every invocation.
$(HOST_PYTHON_TARGET_CONFIGURE): | host-libffi
