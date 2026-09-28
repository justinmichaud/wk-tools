"""key=value: `kv` reads what a probe or marker writes, leniently; `conf` a conf file, strictly, or ConfError."""

import re
import shlex

ANSI = re.compile(r"\x1b\[[0-9;]*m")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def kv(text):
    out = {}
    for line in ANSI.sub("", text or "").replace("\r", "").splitlines():
        k, eq, v = line.partition("=")
        if eq and k not in out:
            out[k] = v
    return out


def kv_file(path):
    try:
        with open(path, errors="replace") as f:
            return kv(f.read())
    except OSError:
        return {}


class ConfError(ValueError):
    pass


def conf(text, path="<conf>", check=None):
    """`KEY=<one literal shell word>` lines, a quote spanning lines; `check(key)` returns why a key is refused, or None."""
    out, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        line, i = lines[i].strip(), i + 1
        if not line or line.startswith("#"):
            continue
        at = "%s:%d:" % (path, i)
        m = ASSIGN.match(line)
        if not m:
            raise ConfError("%s not a KEY=value line: %s" % (at, line))
        key, raw = m.groups()
        why = check(key) if check else None
        if why:
            raise ConfError("%s %s" % (at, why))
        while True:
            try:
                words = shlex.split(raw, comments=True)
                break
            except ValueError:
                if i >= len(lines):
                    raise ConfError("%s %s's quote is never closed" % (at, key))
                raw, i = raw + "\n" + lines[i], i + 1
        if "$" in raw or "`" in raw:
            raise ConfError("%s %s is not a literal (a default belongs in code)" % (at, key))
        if len(words) > 1:
            raise ConfError("%s %s is more than one word; quote it" % (at, key))
        out[key] = words[0] if words else ""
    return out


def conf_file(path, check=None):
    with open(path, errors="replace") as f:
        return conf(f.read(), path, check)
