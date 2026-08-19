#!/usr/bin/env python3
"""Turn a machine's own netplan into a network-config the image can render.

Reads netplan documents on stdin (one per file, `---`-separated) and writes a
cloud-init network-config to stdout.

Why this exists, and why it is not a copy.  The credential has to come from the
board itself -- so it never travels through a log or an agent's context -- and
netplan is the right lever, because on this distro a NetworkManager keyfile is
not authoritative: NM rewrites a dropped-in profile under a new uuid and the
secret does not survive the round trip.  But the board's netplan says
`renderer: NetworkManager`, and the base image has no NetworkManager at all
(668 packages in its manifest; netplan.io, wpasupplicant and systemd, no
network-manager).  Rendered as written, the image would come up with no
network on a board that has no cable -- the exact failure the three earlier
attempts hit, arrived at from a new direction.

So the wifi credential is carried across and the renderer is not.  What is left
is the part both backends share: NM drives wpa_supplicant, and so does
networkd, so the association machinery on the far side is the same one that is
associating with this AP right now.

Everything NM-specific is dropped rather than translated -- uuids, the keyfile
passthrough blob, per-device renderers.  A key this does not understand is a
key the image should not be given.
"""

import sys
import yaml

# The only access-point keys worth carrying: what to join, and how.
AP_KEYS = ("auth", "password", "bssid", "band", "channel", "hidden", "mode")


def main():
    wifis = {}
    for doc in yaml.safe_load_all(sys.stdin.read()):
        if not isinstance(doc, dict):
            continue
        net = doc.get("network", doc)
        for dev, cfg in (net.get("wifis") or {}).items():
            if not isinstance(cfg, dict):
                continue
            # The device is named by what it matches, not by NM's uuid-shaped
            # key: `NM-315abc06-...` means nothing to networkd.
            name = (cfg.get("match") or {}).get("name") or dev
            if name.startswith("NM-"):
                name = "wlan0"
            aps = {}
            for ssid, ap in (cfg.get("access-points") or {}).items():
                if not isinstance(ap, dict):
                    ap = {}
                aps[ssid] = {k: ap[k] for k in AP_KEYS if k in ap}
            if not aps:
                continue
            wifis[name] = {
                "dhcp4": True,
                # Ask the DHCP server by MAC, which is what NetworkManager
                # does on the machine this credential came from. networkd's
                # default is a DUID, and a different client identifier means a
                # different lease -- the image came up on a different address
                # than the workstation and was invisible to a driving host
                # looking for the machine it knew.
                "dhcp-identifier": "mac",
                # Nothing may wait on this interface at boot: a machine that
                # blocks on the network is a machine that cannot be logged into
                # to find out why the network failed.
                "optional": True,
                "access-points": aps,
            }

    if not wifis:
        sys.exit("no wifi stanza found in the machine's netplan")

    out = {
        "network": {
            "version": 2,
            # The image's renderer, not the board's.
            "renderer": "networkd",
            "ethernets": {"eth0": {"dhcp4": True, "optional": True}},
            "wifis": wifis,
        }
    }
    yaml.safe_dump(out, sys.stdout, default_flow_style=False, sort_keys=True)


main()
