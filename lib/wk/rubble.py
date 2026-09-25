"""A piece of rubble: its size, `flag` ("" for a plain `wk gc`, a --purge flag, or the command elsewhere that takes it),
`why` nothing may take it now, and `take`, FAR when another machine's wk acts on it. gc and `wk disk` render these rows."""

from collections import namedtuple

Row = namedtuple("Row", "kind what kb flag take why")
FAR = "far"


def row(kind, what, kb, flag="", take=None, why=""):
    return Row(kind, what, kb, flag, take, why)


def du_kb(machine, path):
    words = machine.run(["du", "-sk", path]).out.split()
    try:
        return int(words[0])
    except (IndexError, ValueError):
        return None


def remover(machine, *paths):
    def take():
        for p in paths:
            machine.remove(p)
    return take


def takes(r, flags):
    return r.take is not None and not r.why and (r.flag == "" or r.flag in flags)


def size(kb):
    from wk.sysimage.ls import human_bytes
    return "??" if kb is None else human_bytes(kb * 1024)


def note(r, flags, taken):
    if r.why:
        return r.why
    if takes(r, flags):
        return taken
    if r.flag.startswith("--"):
        return "kept -- 'wk gc %s' takes it" % r.flag
    return "kept -- '%s' takes it" % r.flag if r.flag else "kept"


def line(r, flags, taken):
    return "  %7s  %-46s %s" % (size(r.kb), r.what, note(r, flags, taken))


def to_line(r):
    return "\t".join(("??" if r.kb is None else str(r.kb), r.kind, r.what, r.flag, r.why, "t" if r.take else "-"))


def from_line(text, label):
    parts = text.split("\t")
    if len(parts) != 6:
        return None
    kb, kind, what, flag, why, t = parts
    return Row(kind, "%s: %s" % (label, what), None if kb == "??" else int(kb), flag, FAR if t == "t" else None, why)
