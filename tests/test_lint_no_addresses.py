"""lint.no_addresses: a node is reached by its tailnet name, and how to reach it
is written down nowhere. dotfiles/ssh/config, machines/*.conf and lib/ hold no
MAC address or `.local` name; the ssh config also holds no IP-address HostName,
HostKeyAlias or ProxyJump, except the stanzas of the two nodes the tailnet
cannot name: moose's BMC behind its bridge phone, and Igalia's build boxes
behind the gateway.

Run: python3 tests/run.py --lint -k test_lint_no_addresses
"""
TIER = "lint"
import re
import unittest

from tests.support import REPO, owed

# Host stanzas allowed to name a jump host or an address.
EXCEPTED_HOSTS = {"moosebmc", "buildbox4", "devbox-arm64-2", "devbox-armhf-2"}
MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
LOCAL_NAME = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\.local\b(?![/\w])")
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def is_placeholder(mac):
    return set(mac.replace(":", "")) <= {"0"} or mac.lower().startswith("00:00:00:00:00:")


def text_hits(text):
    """(line number, what) for each MAC (all-zero placeholders excepted) and each `.local` name."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        hits += [(n, "MAC " + m) for m in MAC.findall(line) if not is_placeholder(m)]
        hits += [(n, "name " + m.group(0)) for m in LOCAL_NAME.finditer(line)]
    return hits


def ssh_config_hits(text):
    """(line number, what) for each address, HostKeyAlias and ProxyJump outside the excepted stanzas."""
    hits, hosts = [], []
    for n, raw in enumerate(text.splitlines(), 1):
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        key, args = parts[0].lower(), parts[1:]
        if key == "host":
            hosts = args
        elif not set(hosts) <= EXCEPTED_HOSTS:
            if key in ("proxyjump", "hostkeyalias"):
                hits.append((n, "%s in Host %s" % (parts[0], " ".join(hosts))))
            elif key == "hostname" and args and IPV4.match(args[0]):
                hits.append((n, "address %s in Host %s" % (args[0], " ".join(hosts))))
    return hits


def lib_files():
    return [p for p in sorted((REPO / "lib").rglob("*")) if p.is_file() and "__pycache__" not in p.parts]


def tree_hits():
    hits = []
    conf = REPO / "dotfiles" / "ssh" / "config"
    hits += ["%s:%d: %s" % (conf.relative_to(REPO), n, w) for n, w in ssh_config_hits(conf.read_text())]
    hits += ["%s:%d: %s" % (conf.relative_to(REPO), n, w) for n, w in text_hits(conf.read_text())]
    for p in sorted((REPO / "machines").glob("*.conf")) + lib_files():
        text = p.read_text(errors="replace")
        hits += ["%s:%d: %s" % (p.relative_to(REPO), n, w) for n, w in text_hits(text)]
    return hits


class TestDetection(unittest.TestCase):
    def test_a_mac_is_found_and_an_all_zero_placeholder_is_not(self):
        self.assertEqual(text_hits("mac=d8:3a:dd:aa:42:8b\nlan=00:00:00:00:00:01\n"), [(1, "MAC d8:3a:dd:aa:42:8b")])

    def test_a_local_name_is_found_and_a_local_directory_is_not(self):
        self.assertEqual(text_hits("ssh pi.local\ncd ~/.local/bin\n$HOME/.local/state\n"), [(1, "name pi.local")])

    def test_a_jump_or_address_outside_the_excepted_stanzas_is_found(self):
        conf = ("Host moosebmc\n  HostName 10.99.0.2\n  ProxyJump l5\n"
                "Host rpi5\n  HostKeyAlias rpi5\n  HostName 10.0.0.9\n  ProxyJump gw # note\n"
                "Host box\n  HostName box.example\n")
        self.assertEqual(ssh_config_hits(conf), [(5, "HostKeyAlias in Host rpi5"), (6, "address 10.0.0.9 in Host rpi5"),
                                                 (7, "ProxyJump in Host rpi5")])


class TestNoAddressesInTheTree(unittest.TestCase):
    @owed("five board and bridge confs still carry a MAC and two tailnet-bridge ssh stanzas still carry "
          "HostKeyAlias: closed once both boards join the tailnet by image")
    def test_no_conf_or_library_names_an_address_by_which_a_node_is_reached(self):
        hits = tree_hits()
        self.assertEqual(hits, [], "%d addresses:\n%s" % (len(hits), "\n".join(hits)))


if __name__ == "__main__":
    unittest.main()
