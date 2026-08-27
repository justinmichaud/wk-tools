# HANDOFF — `wk pi deploy` and `wk pi bench`

## Remaining

- [ ] file an on-board `wk pi bench` result beside `wk bench`'s own runs so `wk bench ls`/`compare` can see it and two on-board runs can be compared [needs a Pi board]
- [ ] give the yocto workspace scipy for `wk bench compare` to run there (PEP 668 refuses a bare `pip3 install --user`; use `--break-system-packages --user`) [needs a Pi board]
- [ ] `wk pi bench --ab` should compare only rounds where both arms finished, dropping a crashed arm from the comparison but keeping it in the store [needs a Pi board]
- [ ] replace `wk pi setup`'s `--depth 1` clone (427,711 files, ~4.2 GB) with a sparse checkout of `Tools/` (~5,000 files) [needs a Pi board]
- [ ] run a real-display bench on the rpi4 [needs a Pi board with a display attached]
- [ ] `zip` is missing from the yocto workspace image, so `wk pi deploy` falls back to a plain tar instead of `built-product-archive` [needs the yocto Containerfile rebuilt]
- [ ] test a new boot cmdline arg on the SD (unarmed fall-through) before risking it on the bench stick [needs a Pi board]
- [ ] reproduce the rpi4's bench stick with `wk sysimage write` instead of by hand [needs a Pi board and a confirmed erase]
- [ ] resolve the booted perf system by its tailnet name once the perf image carries tailscale, not by mDNS [needs the yocto tailscale layer]
