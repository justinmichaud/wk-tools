# HANDOFF — MiniBrowser on Linux: profiling it

Running MiniBrowser graphically is done (`wk gui <ws> [url]`, needs `wk session
on`; `--software` falls back to llvmpipe). Debugging it is done too, as
`wk gui --lldb [ui|web]` and `wk test --layout --lldb [web|ui]`
(`docs/Urgent/HANDOFF-debug.md` has what was verified and at what cost).

What is left is the profiler.

## Remaining

- **`wk profile <ws> --browser` on the CMake ports.** It refuses today
  (`cmd/profile:162`), for a reason that is false: "the browser is
  started by Tools/Scripts/run-minibrowser and the process that matters is a
  child of it, so a profiler pointed at the launcher records the launcher".
  run-minibrowser splits `WEBKIT_MINI_BROWSER_PREFIX` and prepends it to the
  MiniBrowser command it builds (`webkitpy/port/{gtk,wpe}.py`), which is exactly
  how `wk gui --lldb` now gets a debugger in front of the right binary while
  keeping the port's own environment. samply goes in the same place.
- **Which process, then** — decision pending (`docs/defects` 13).
