# One header, dropped from the non-primary width's -dev package.
#
# A multilib SDK holds both widths in one sysroot -- SDKTARGETSYSROOT names the
# base machine (cortexa76 here) and the lib32 packages are installed beside the
# 64-bit ones. Shared /usr/include is the point of that arrangement: headers are
# meant to be width-independent, and only ${baselib} differs.
#
# math-vector-fortran.h is not width-independent. glibc ships it at a shared
# path with arch-specific content, so libc6-dev and lib32-libc6-dev carry
# different bytes under one name and rpm refuses the transaction outright:
#
#   file /usr/include/finclude/math-vector-fortran.h from install of
#   libc6-dev...cortexa76 conflicts with file from package
#   lib32-libc6-dev...armv7vet2hf_neon_vfpv4
#
# That is a packaging bug upstream, and it stops the SDK being assembled at all
# -- which stops the WebKit slot the 32-bit image is measured with being built.
# Pruning TOOLCHAIN_TARGET_TASK does not reach it: the 64-bit -dev packages
# arrive through SDKIMAGE_FEATURES' complementary globbing (dev-pkgs dbg-pkgs
# src-pkgs) over everything already in the sysroot, not through that list.
#
# So the duplicate goes, and the primary width keeps the copy. Nothing this
# repo builds uses Fortran, and the file is a vector-ABI declaration for
# Fortran callers alone: dropping the 32-bit copy costs nothing here and is
# strictly narrower than dropping a package, a feature, or a width.
do_install:append() {
    if [ -n "${MLPREFIX}" ]; then
        rm -f ${D}${includedir}/finclude/math-vector-fortran.h
        rmdir --ignore-fail-on-non-empty ${D}${includedir}/finclude 2>/dev/null || true
    fi
}
