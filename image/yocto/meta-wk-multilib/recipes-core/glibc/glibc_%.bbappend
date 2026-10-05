# math-vector-fortran.h ships at a shared /usr/include path with arch-specific bytes, so a multilib
# SDK's libc6-dev and lib32-libc6-dev conflict and rpm refuses the transaction (an upstream packaging
# bug). TOOLCHAIN_TARGET_TASK cannot reach it: the -dev packages arrive by SDKIMAGE_FEATURES' globbing.
# The primary width keeps its copy; nothing here uses Fortran.
do_install:append() {
    if [ -n "${MLPREFIX}" ]; then
        rm -f ${D}${includedir}/finclude/math-vector-fortran.h
        rmdir --ignore-fail-on-non-empty ${D}${includedir}/finclude 2>/dev/null || true
    fi
}
