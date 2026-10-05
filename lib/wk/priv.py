import sys

from wk.machine import Local

LIBEXEC = "/usr/local/libexec"
HELPERS = (("wk-quiesce-priv", "any", "wk quiesce / wk session"),
           ("wk-card-priv", "linux", "wk sysimage (writing a card)"),
           ("wk-boot-priv", "any", "wk boot (arming the firmware, restarting a machine)"))


def path(name):
    return "%s/%s" % (LIBEXEC, name)


def sudoers(name):
    n = name[3:] if name.startswith("wk-") else name
    return "/etc/sudoers.d/zzz-wk-%s" % (n[:-5] if n.endswith("-priv") else n)


def helpers():
    return [(n, w, what, path(n), sudoers(n)) for n, w, what in HELPERS]


def answers(helper_path, machine=None):
    # `sudo -l`, never a run: `sudo -n <helper>` succeeds for anything while ./setup holds a timestamp open.
    out = (machine or Local()).run(["sudo", "-n", "-l"]).out
    return any("NOPASSWD:" in line and helper_path in line.split() for line in out.splitlines())


def main(argv):
    verb, rest = argv[0], argv[1:]
    if verb == "rows":
        for n, w, what in HELPERS:
            print(n, w, what)
    elif verb == "path":
        print(path(rest[0]))
    elif verb == "sudoers":
        print(sudoers(rest[0]))
    elif verb == "answers":
        return 0 if answers(rest[0]) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
