"""The credential wrapper: the argv that runs a command with the credential paths unreadable and a minimal environment.

    wrap(kind, argv, hidden, env)      # kind is the server's: linux | mac | borrowed
    minimal(variables)                 # the part of an environment an agent may inherit
    verdict(listings, dump, env)       # what a stand-in run under the wrapper saw: (holds | leaks | unknown, why)"""

PASS = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "TERM", "LANG", "TZ")   # and every LC_*
HOLDS, LEAKS, ABSENT, UNKNOWN = "holds", "leaks", "absent", "unknown"
TOOL = {"linux": "bwrap", "mac": "sandbox-exec", "borrowed": "env"}
SAYS = {"linux": "credentials are hidden by bwrap", "mac": "credentials are hidden by a Seatbelt profile",
        "borrowed": "no credential is delivered to a borrowed box, so the wrapper is the minimal environment alone"}
NOT_READABLE = ("Operation not permitted", "Permission denied", "No such file or directory")


def bare(argv, hidden, env):
    return ["env", "-i"] + ["%s=%s" % item for item in sorted(env.items())] + list(argv)


def bwrap(argv, hidden, env):
    out = ["bwrap", "--dev-bind", "/", "/"]
    for path in hidden:
        out += ["--tmpfs", path]
    out.append("--clearenv")
    for name in sorted(env):
        out += ["--setenv", name, env[name]]
    return out + ["--"] + list(argv)


def seatbelt(argv, hidden, env):
    deny = "".join('(deny file-read* (subpath "%s"))' % path.replace("\\", "\\\\").replace('"', '\\"') for path in hidden)
    return ["sandbox-exec", "-p", "(version 1)(allow default)" + deny] + bare(argv, hidden, env)


FORM = {"linux": bwrap, "mac": seatbelt, "borrowed": bare}


def wrap(kind, argv, hidden, env):
    """`argv` run with each directory in `hidden` unreadable and exactly the environment `env`."""
    return FORM[kind](argv, hidden, env)


def variables(dump):
    """What `env -0` printed, as a dict."""
    return dict(item.split("=", 1) for item in dump.split("\0") if "=" in item)


def minimal(env):
    return {name: value for name, value in env.items() if name in PASS or name.startswith("LC_")}


def verdict(listings, dump, env):
    """`listings` is [(path, the Result of `ls -A path`)] and `dump` the Result of `env -0`, both run under the wrapper
    that was given `env`. Only ls's own refusal counts as unreadable: a wrapper that failed to start also says
    "Permission denied"."""
    for path, r in listings:
        if r.ok and r.out.strip():
            return LEAKS, "%s is readable under the wrapper: it lists %s" % (path, ", ".join(r.out.split()))
        if not r.ok and not (r.err.startswith("ls: ") and r.err.strip().endswith(NOT_READABLE)):
            return UNKNOWN, "the stand-in could not list %s (exit %d): %s" % (path, r.rc, r.err.strip())
    if not dump.ok:
        return UNKNOWN, "the stand-in could not print its environment (exit %d): %s" % (dump.rc, dump.err.strip())
    # bwrap --clearenv keeps PWD (bwrap(1)).
    extra = sorted(set(variables(dump.out)) - set(env) - {"PWD"})
    if extra:
        return LEAKS, "the environment under the wrapper carries %s" % ", ".join(extra)
    return HOLDS, ""
