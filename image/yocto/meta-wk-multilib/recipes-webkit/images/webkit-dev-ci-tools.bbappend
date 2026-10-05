# Nothing rewrites IMAGE_INSTALL for a multilib image, so lib32-webkit-dev-ci-tools would be the 64-bit
# rootfs under a 32-bit name. WK_MULTILIB_KEEP names what has one build per machine (kernel, firmware,
# bootloader): the names whose prefixed form `bitbake -n` cannot resolve.
WK_MULTILIB_KEEP ?= ""

python () {
    ml = d.getVar('MLPREFIX')
    if not ml:
        return
    keep = set((d.getVar('WK_MULTILIB_KEEP') or '').split())
    keep |= set((d.getVar('NON_MULTILIB_RECIPES') or '').split())
    mapped = []
    for pkg in (d.getVar('IMAGE_INSTALL') or '').split():
        if pkg in keep or pkg.startswith(ml):
            mapped.append(pkg)
        else:
            mapped.append(ml + pkg)
    d.setVar('IMAGE_INSTALL', ' '.join(mapped))
}
