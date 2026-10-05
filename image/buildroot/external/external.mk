# Build fixes for the pinned buildroot, included after every package/*/*.mk.
#
# host-python 2.7's bundled libffi aarch64/sysv.S does not assemble, so the tree fails on an arm64
# build host. Upstream buildroot (2021.08) and the fork's wpe head give host-python the system libffi;
# this applies that fix from outside the 2020.02 pin. Both appends are idempotent and expand at
# recipe time, so they reach a package already defined.
HOST_PYTHON_CONF_OPTS += --with-system-ffi
HOST_PYTHON_DEPENDENCIES += host-libffi

# The configure rule's prerequisites were expanded before this file was read; a second, order-only
# rule adds one (a phony target is always newer than a stamp, so it must not be a normal prerequisite).
$(HOST_PYTHON_TARGET_CONFIGURE): | host-libffi
