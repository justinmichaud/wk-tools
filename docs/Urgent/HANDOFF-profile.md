# HANDOFF — wk profile: the provisioning half

- [ ] verify `wk profile` sampling mode runs end to end [needs a workspace]
- [ ] verify `wk profile` bytecode mode runs end to end [needs a workspace]
- [ ] verify `wk profile` samply mode runs in a container [needs a workspace]
- [ ] verify `wk profile --mode instruments` runs [needs the macOS guest]
- [ ] verify `wk profile --fetch` works out of a guest [needs the macOS guest]
- [ ] verify `--browser` launches and attaches samply to `ui`/`web`/`network`/`gpu` on a CMake port (docs/defects 13) [needs a workspace with a display]
- [ ] wire `--browser` for heaptrack/massif on the CMake ports (their launch line is a shell fragment, not a plain command)
