"""wk key backup: the dconf filter, the write-if-changed writer and the --candidates scanner."""

import os
import plistlib
import re
import sys
from xml.parsers.expat import ExpatError

from wk.act import debug, die, info, warn

DEFAULTS_CONF = os.path.join("host", "macos", "defaults.conf")
SYMBOLICHOTKEYS = os.path.join("host", "macos", "symbolichotkeys.plist")
DCONF_CONF = os.path.join("host", "linux", "config.dconf")

# Dropped from a live `dconf dump /`: WiFi 802.1X UUIDs, a weather location, terminal profile UUIDs, a GTK last-folder path -- all differ on every run.
_DCONF_SECTION_NOISE = re.compile(r"nm-applet|Ptyxis.*Profiles|org/gnome/shell/weather")
_DCONF_LINE_NOISE = ("last-folder-path=", "welcome-dialog-last-shown-version=", "last-selected-power-profile=",
                     "looking-glass-history=", "command-history=")


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
            skip = bool(_DCONF_SECTION_NOISE.search(line))
        if not skip and not line.startswith(_DCONF_LINE_NOISE):
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
    reason = line.split(None, 4)[4:]
    r = machine.run(["defaults", "read", domain, key])
    cur = r.out.strip() if r.ok else ""
    if not cur:
        warn("%s %s is no longer set; keeping the recorded value" % (domain, key))
        return line
    if kind == "bool":
        cur = {"1": "true", "0": "false"}.get(cur, cur)
    return " ".join([domain, key, kind, cur] + reason)


def macos_backup(machine, root):
    conf = os.path.join(root, DEFAULTS_CONF)
    lines = [l if not l.strip() or l.strip().startswith("#") else _refreshed_defaults_line(machine, l)
             for l in read_or_empty(machine, conf).splitlines()]
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
    return {tuple(l.split()[:2]) for l in conf_text.splitlines() if len(l.split()) >= 2 and not l.strip().startswith("#")}


def _is_candidate_noise(k):
    """Window, toolbar and panel state, timestamps and identifiers; case-sensitive, since "date" would catch "Update"."""
    return (k == "SUEnableAutomaticChecks"
            or k.startswith(("NSWindow Frame", "TB Default Item Identifiers", "NSToolbar", "NSSplitView", "NSStatusItem", "NSTableView"))
            or any(w in k for w in ("NSNavLastRootDirectory", "Recent", "Date", "UUID", "WindowFrame", "Position", "Bounds")))


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
        except (ValueError, ExpatError) as e:
            warn("defaults domain %s does not parse as a plist (%s); its keys are not listed" % (domain, e))
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
