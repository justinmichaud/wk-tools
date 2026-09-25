"""key=value lines as every probe, marker and far-side report writes them; the first of a repeated key wins."""

import re

ANSI = re.compile(r"\x1b\[[0-9;]*m")


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
