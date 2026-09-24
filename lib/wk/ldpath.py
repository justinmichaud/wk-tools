"""The loader-library prelude `cmd/run` and `cmd/profile` share: prepended, not replacing, so the wkdev image's own jhbuild/libwpe prefix survives."""


def prelude(var, dir_):
    return 'export %s="%s${%s:+:${%s}}"' % (var, dir_, var, var)
