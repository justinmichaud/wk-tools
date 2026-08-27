# HANDOFF — fleet-wide state: boards, images, bridges

Items that span more than one board or bridge, or belong to no single one.
Board-specific and bridge-specific work lives in `docs/HANDOFF-boot.md`,
`docs/HANDOFF-sdcard.md`, `docs/HANDOFF-bmc.md`, `docs/HANDOFF-ab-bench.md`,
`docs/HANDOFF-pi-deploy.md`, `docs/HANDOFF-linux-pi.md` and
`docs/HANDOFF-yocto.md`.

## Remaining

- [ ] `wk sysimage write` seeding the tailnet key onto a card, and the board
      then joining the tailnet on first boot — written, never run against
      real hardware
- [ ] once two boards have confirmed joining the tailnet by image: delete
      `image_addr`'s MAC→ARP→mDNS ladder, `MACH_MAC`, `.local` HostNames, the
      `10.99.1.10` stanza and the `ProxyJump` — only then, in that order
- [ ] once both boards are on the tailnet, the `HostKeyAlias`/`accept-new`
      bridge stanzas become dead config to remove (Tailscale SSH needs no
      host-key acceptance)
- [ ] the rpi3 end to end: provision it, `wk sysimage write` its SD, boot it
      (the OTP USB-boot fuse stays blown, so this is hands-on until the
      two-slot card exists)
- [ ] **first contact with an unreachable Pi is physical** — `wk pi
      boot-order` needs the board already running over ssh; an unresponsive
      Pi has to be met once with a card write
- [ ] `wk find <machine>` against a board that is actually powered on and
      answering — both boards were off when this was last run, which is what
      the command correctly reported
- [ ] a bridge that is on the tailnet but whose *segment* is down needs to be
      distinguishable from one that is simply off

## Images without a store

`wk help images` has the model; `wk sysimage ls` scans where each builder
leaves output and `wk sysimage write --from <path>` is the one write.

- [ ] every image edit moves onto the card, on the machine holding the reader
      (a new `admin/wk-card-priv` verb mounting the boot partition and doing
      the mtools-equivalent edits there), so a macOS driver needs no Linux
      tooling; today `wk sysimage write` of a yocto/buildroot image edits a
      local scratch copy first and needs mtools/debugfs/sfdisk where it runs
- [ ] a listing for pmos builds: their output lives on the build host
      (`pmos_out`, image/pmos.sh) where no local scan reaches; `wk sysimage
      build` and `wk bridge provision` report the path, nothing lists it
- [ ] two *unmarked* disks of the same transport is the residual ambiguity in
      `disk_resolve_own`: it refuses and lists rather than picking, which
      resolves itself the moment either disk is written (then it has a marker)

## Card write path — written, not yet run against hardware

- [ ] the disk-verb completeness check (`wk selftest` grepping command-position
      calls against definitions) has never been run against a real card write
- [ ] a machine whose card helper is missing or out-ranked names the remedy —
      checked at a terminal, not against a machine actually missing the helper
- [ ] the tailnet seed's `joins <device>` read-only check, and the
      fleet+tailnet edits being read back before the card is unmounted, are
      both written but not yet exercised against hardware
- [ ] a bmap write onto *used* media (not blank) — no board here lacks
      bmaptool, so every write to one takes the dd path today and this is untested
- [ ] "the role is a property of the card, not the build" (`disk_seed_role`,
      the shared self-return/self-disarm units) — confirmed on hardware: the
      marker lands, `systemctl show wk-self-return` reports the condition
      unmet, and the board does not reboot after `IMG_WATCHDOG` seconds

## Board lifecycle and help

- [ ] nothing in this fleet can power a Pi on (moose has a BMC; the boards do
      not) — hardware, not code, and the reason "never touch the boards" is
      not literally met yet

## Removing the fallback-address plumbing

Once both boards run tailnet-carrying images (`docs/HANDOFF-yocto.md` item 5,
matching the first bullet under Remaining above), delete in this order —
build, write, boot, confirm the board answers to its own tailnet name, *then*
remove the fallback. Deleting the fallback first turns a reachable board into
an unreachable one.

## rpi4 recovery USB does not join the tailnet

- [ ] confirm the cause: `wk find rpi4` (finds the MAC on the cable while
      `wk status` shows it off the tailnet = no key was seeded); a card
      written without `--machine rpi4`, or forced past the barrier, carries
      no auth key and the join script exits quietly
- [ ] after the seeding refusal lands (tailnet and WiFi seeds block a write
      instead of no-op'ing), re-write the rpi4 card with `--machine rpi4` and
      confirm it joins

