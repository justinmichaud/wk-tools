# HANDOFF — MiniBrowser on Linux: profiling it

`wk gui`/`wk test --layout --lldb` are done; the profiler is what's left.

- [ ] drive `wk profile <ws> --browser --process ui|web|network|gpu` against a live browser on a board or workspace (docs/defects 13) [needs a workspace with a display]
- [ ] give heaptrack/massif a prefix-safe launch line so `--browser` can reach them on the CMake ports
- [ ] verify `--process web/network/gpu`'s fixed-sleep attach against a real, slower-loading page [needs a workspace with a display]
