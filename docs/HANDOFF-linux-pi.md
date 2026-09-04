# HANDOFF — put the Pi test devices on the tailnet

Scope: rpi4 and rpi3; the rpi5 workstation goes through its own `./setup`.

- [ ] run `wk pi setup rpi4` against the running device, and rpi3 when it is up [needs the rpi4 and rpi3]
- [ ] confirm the address lands in `$WK_STORE/pi-hosts` and a workspace can reach the board through `container/proxy/ssh-proxy.py` [needs the rpi4]
- [ ] confirm a tailnet address not in `pi-hosts` is refused [needs the rpi4]
- [ ] decide the Tailscale ACL grant `wk pi setup` prints as its remaining manual step (tag:wk reaching tag:wk, in the admin console) [decision]
- [ ] give `host/linux/rpi5/` an owner: its stability half into that board's `./setup`, its perf half into `webkit-2.52-yocto-rpi5-64` [decision]
- [ ] confirm `wk pi setup` killed mid-push converges on re-run with no duplicate or stale `pi-hosts` address [needs the rpi4]
- [ ] build `wk provision <machine>` / `wk unprovision <machine>`, composing `wk pi setup` and `wk pi boot-order` from a blank card to an answering tailnet name, and back [decision]
