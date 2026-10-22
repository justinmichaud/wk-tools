import sys

from wk.machine import Local

LIBEXEC = "/usr/local/libexec"
HELPERS = (("wk-quiesce-priv", "any", "wk quiesce"),
           ("wk-card-priv", "linux", "wk sysimage (writing a card)"),
           ("wk-boot-priv", "any", "wk boot (arming the firmware, restarting a machine)"))


def path(name):
    return "%s/%s" % (LIBEXEC, name)


def sudoers(name):
    return "/etc/sudoers.d/zzz-wk-%s" % name.removeprefix("wk-").removesuffix("-priv")


def helpers():
    return [(n, w, what, path(n), sudoers(n)) for n, w, what in HELPERS]


def answers(helper_path, machine=None):
    # `sudo -l`, never a run: `sudo -n <helper>` succeeds for anything while ./setup holds a timestamp open.
    out = (machine or Local()).run(["sudo", "-n", "-l"]).out
    return any("NOPASSWD:" in line and helper_path in line.split() for line in out.splitlines())


def main(argv):
    verb, rest = (argv or [""])[0], argv[1:]
    if verb not in ("rows", "path", "sudoers", "answers") or len(rest) != (verb != "rows"):
        sys.stderr.write("usage: python3 -m wk.priv rows | path <name> | sudoers <name> | answers <helper path>\n")
        return 2
    if verb == "answers":
        return 0 if answers(rest[0]) else 1
    print("\n".join(" ".join(h) for h in HELPERS) if verb == "rows" else path(rest[0]) if verb == "path" else sudoers(rest[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
