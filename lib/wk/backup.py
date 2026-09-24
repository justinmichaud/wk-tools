"""wk key backup: the dconf filter, the atomic writer and the --candidates scanner, through the Machine passed in so a Fake proves it without the real tools."""

import os
import plistlib
import re
import sys

from wk.act import debug, die, info, warn

DEFAULTS_CONF = os.path.join("host", "macos", "defaults.conf")
SYMBOLICHOTKEYS = os.path.join("host", "macos", "symbolichotkeys.plist")
DCONF_CONF = os.path.join("host", "linux", "config.dconf")

# Dropped from a live `dconf dump /`: WiFi 802.1X UUIDs, a weather location, terminal profile UUIDs, a GTK last-folder path -- all differ on every run.
_DCONF_SECTION_NOISE = (
    re.compile(r"nm-applet"),
    re.compile(r"Ptyxis.*Profiles"),
    re.compile(r"org/gnome/shell/weather"),
)
_DCONF_LINE_NOISE = (
    "last-folder-path=",
    "welcome-dialog-last-shown-version=",
    "last-selected-power-profile=",
    "looking-glass-history=",
    "command-history=",
)

# Matched case-sensitively against Apple's own PascalCase naming: a case-insensitive "date" would also catch "Update".
_CANDIDATE_NOISE = (
    (lambda k: k.startswith("NSWindow Frame"), "a saved window position/size"),
    (lambda k: "NSNavLastRootDirectory" in k, "the last folder used in an Open/Save panel"),
    (lambda k: "Recent" in k, "a recently-used-items list"),
    (lambda k: "Date" in k, "a timestamp, not a choice"),
    (lambda k: "UUID" in k, "a machine- or install-specific identifier"),
    (lambda k: k.startswith("TB Default Item Identifiers"), "a toolbar customization snapshot"),
    (lambda k: k.startswith("NSToolbar"), "toolbar layout state"),
    (lambda k: k == "SUEnableAutomaticChecks", "Sparkle's own per-app update-check toggle"),
    (lambda k: "WindowFrame" in k or "Position" in k or "Bounds" in k,
     "a window, dock or Finder position"),
    (lambda k: k.startswith(("NSSplitView", "NSStatusItem", "NSTableView")),
     "AppKit-restored UI layout state"),
)


def read_or_empty(machine, path):
    try:
        return machine.read(path)
    except OSError:
        return ""


def atomic_update(machine, path, content, label):
    if read_or_empty(machine, path) == content:
        debug("ok: %s" % label)
        return False
    machine.write(path, content)
    info("%s updated" % label)
    return True


def dconf_filter(text):
    out = []
    skip = False
    for line in text.splitlines():
        if line.startswith("["):
            skip = any(p.search(line) for p in _DCONF_SECTION_NOISE)
        if skip:
            continue
        if line.startswith(_DCONF_LINE_NOISE):
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def _dconf_header(machine, path):
    lines = []
    for line in read_or_empty(machine, path).splitlines():
        if line.startswith("["):
            break
        lines.append(line)
    return "\n".join(lines) + "\n" if lines else ""


def linux_backup(machine, root):
    out = os.path.join(root, DCONF_CONF)
    header = _dconf_header(machine, out)
    r = machine.run(["dconf", "dump", "/"])
    if not r.ok:
        die("could not dump dconf: %s" % r.err.strip())
    content = header + dconf_filter(r.out)
    return 1 if atomic_update(machine, out, content, "config.dconf") else 0


def _refreshed_defaults_line(machine, line):
    domain, key, kind = line.split()[:3]
    r = machine.run(["defaults", "read", domain, key])
    cur = r.out.strip() if r.ok else ""
    if not cur:
        warn("%s %s is no longer set; keeping the recorded value" % (domain, key))
        return line
    if kind == "bool":
        cur = {"1": "true", "0": "false"}.get(cur, cur)
    return "%s %s %s %s" % (domain, key, kind, cur)


def macos_backup(machine, root):
    conf = os.path.join(root, DEFAULTS_CONF)
    lines = []
    for line in read_or_empty(machine, conf).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
        else:
            lines.append(_refreshed_defaults_line(machine, line))
    new_conf = "\n".join(lines) + ("\n" if lines else "")
    n = 1 if atomic_update(machine, conf, new_conf, "defaults.conf") else 0

    hk = os.path.join(root, SYMBOLICHOTKEYS)
    tmp = "/tmp/wk-backup-hotkeys.%d" % os.getpid()
    if not machine.run(["defaults", "export", "com.apple.symbolichotkeys", tmp]).ok:
        die("could not export com.apple.symbolichotkeys")
    if not machine.run(["plutil", "-convert", "xml1", tmp]).ok:
        machine.remove_now(tmp)
        die("could not convert the symbolichotkeys export to xml1")
    xml = machine.read(tmp)
    machine.remove_now(tmp)
    n += 1 if atomic_update(machine, hk, xml, "symbolichotkeys.plist") else 0
    return n


def _known_pairs(conf_text):
    known = set()
    for line in conf_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            known.add((parts[0], parts[1]))
    return known


def _is_candidate_noise(key):
    return any(match(key) for match, _why in _CANDIDATE_NOISE)


def candidates(machine, conf_text):
    known = _known_pairs(conf_text)
    listing = machine.run(["defaults", "domains"])
    domains = {d.strip() for d in listing.out.split(",") if d.strip()}
    domains.add("NSGlobalDomain")  # `defaults domains` never lists it

    results = []
    for domain in sorted(domains):
        export = machine.run(["defaults", "export", domain, "-"])
        if not export.ok or not export.out:
            continue
        try:
            data = plistlib.loads(export.out.encode("utf-8", errors="surrogateescape"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for key, value in data.items():
            if (domain, key) in known or _is_candidate_noise(key):
                continue
            if isinstance(value, (dict, list, bytes)):
                continue  # a nested structure is app state, not a scalar choice
            results.append((domain, key, value))
    results.sort()
    return results


def main(root, candidates_only, machine, macos):
    if candidates_only:
        if not macos:
            die("wk key backup --candidates: macOS only (defaults(1) has no Linux equivalent)")
        for domain, key, value in candidates(machine, read_or_empty(machine, os.path.join(root, DEFAULTS_CONF))):
            print(domain, key, value)
        return 0
    n = macos_backup(machine, root) if macos else linux_backup(machine, root)
    sys.stderr.write("\n")
    if n == 0:
        info("no settings changed since the last backup")
    else:
        info("%d file(s) updated -- review with 'git diff' before committing" % n)
    return 0
