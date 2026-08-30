# HANDOFF — writing a system onto a card in a local reader

- [ ] write to a card reader attached to the workstation directly, with no ssh target and interactive sudo (no NOPASSWD) [decision: needs a verb name other than `flash`, tombstoned in cmd/sysimage]
- [ ] wire a bmaptool-based streamed write for used media into the write path — not there today [decision]
- [ ] disambiguate two unmarked disks of the same transport in `disk_resolve_own` [needs a card in a reader]
