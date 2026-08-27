# HANDOFF — fleet-wide state: boards, images, bridges

What is owed across more than one board or bridge; board- and bridge-specific
work lives in the other `docs/HANDOFF-*.md`.

- [ ] run the card edits against a real card on rpi5: `retarget`, `cmdline-append`, `config-append`, `boot-id`, `units`, `boot-check`, `parts`, `root-spec` — every one is written and none has met a reader [needs a card in rpi5]
- [ ] boot a board off a card those verbs wrote: the units start, the root resolves by PARTUUID, `wk-image.id` names the system [needs a Pi card in hand]
- [ ] install `boot/check-boot-files.py` beside the card helper as `/usr/local/libexec/wk-check-boot-files.py`, root-owned, in `admin/install.sh` — `boot-check` refuses without it, and root may not run a path a caller names [code]
- [ ] `wk sysimage write` seeding the tailnet key onto a card, and the board then joining the tailnet on first boot [needs a Pi card in hand]
- [ ] once two boards have joined the tailnet by image, delete in this order — build, write, boot, confirm the board answers to its own tailnet name, then remove: `image_addr`'s MAC→ARP→mDNS ladder, `MACH_MAC`, `.local` HostNames, the `10.99.1.10` stanza, the `ProxyJump` [waits on the two items above]
- [ ] the `HostKeyAlias`/`accept-new` bridge stanzas become dead config once both boards are on the tailnet (Tailscale SSH needs no host-key acceptance) [waits on the item above]
- [ ] the rpi3 end to end: provision it, `wk sysimage write` its SD, boot it (the OTP USB-boot fuse stays blown, so this is hands-on until the two-slot card exists) [needs a person at the board]
- [ ] first contact with an unreachable Pi is physical: `wk pi boot-order` needs the board already running over ssh [needs a person at the board]
- [ ] `wk find <machine>` against a board that is powered on and answering — both boards were off when this was last run [needs a board that is on]
- [ ] tell a bridge that is on the tailnet but whose *segment* is down from one that is simply off [needs a bridge]
- [ ] a listing for pmos builds: their output lives on the build host (`pmos_out`, image/pmos.sh) where no local scan reaches [code]
- [ ] two *unmarked* disks of the same transport: `disk_resolve_own` refuses and lists rather than picking, which resolves itself the moment either is written [needs two unmarked disks in one reader]
- [ ] the rpi4 has two wk-written media — a rescue USB and a bench SD — and both join as `rpi4-bench` (MACH_BENCH_SSH), so the second write is refused while the first's node exists: per-role names, or one shared node identity the two take turns holding [decision]
- [ ] one netplan WiFi reader: admin/wk-card-priv (`wifi-host`, `wifi-from-host`) embeds it; image/pmos-build.sh and image/pmos.sh (`pmos_uplink_ssid`) each carry their own copy, and pmos-build.sh's `sudo -n cat` is refused on rpi5 now that `wk sudo setup` closed NOPASSWD there [decision]
- [ ] the disk-verb completeness check (`wk selftest` grepping command-position calls against definitions) against a real card write [needs a card in rpi5]
- [ ] a machine whose card helper is missing or out-ranked names the remedy — checked at a terminal, never against a machine actually missing it [needs a machine without the helper]
- [ ] the `joins`/`wifi-joins` probes and the fleet, tailnet and role edits being read back before the card is unmounted [needs a card in rpi5]
- [ ] "the role is a property of the card, not the build": the marker lands, `systemctl show wk-self-return` reports the condition unmet, and the board does not reboot after `IMG_WATCHDOG` seconds [needs a Pi card in hand]
- [ ] confirm why the rpi4 recovery USB does not join the tailnet: `wk find rpi4` finding the MAC on the cable while `wk status` shows it off the tailnet means no key was seeded [needs the rpi4 on]
- [ ] re-write the rpi4 card with `--machine rpi4` and confirm it joins [waits on the item above]
- [ ] nothing in this fleet can power a Pi on (moose has a BMC; the boards do not), so "never touch the boards" is not literally met [hardware]
