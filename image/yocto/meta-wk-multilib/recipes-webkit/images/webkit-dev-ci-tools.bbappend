# A multilib image installs multilib packages.
#
# `MLPREFIX` is set for a multilib image recipe, and the recipes themselves
# gain their variants from multilib.conf's global BBCLASSEXTEND -- but nothing
# rewrites IMAGE_INSTALL. So `bitbake lib32-webkit-dev-ci-tools` would
# assemble the 64-bit rootfs under a 32-bit name: a whole image whose width is
# not the one it is named for, and no error anywhere. This maps the list.
#
# What must not take the prefix is what has one build per machine rather than
# one per userspace width -- the kernel and its modules, the firmware, the
# bootloader. WK_MULTILIB_KEEP names those, and every name in it is there
# because bitbake could not resolve its prefixed form: the list is derived
# from `bitbake -n`, not guessed.
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
