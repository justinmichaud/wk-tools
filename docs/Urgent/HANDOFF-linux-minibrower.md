# HANDOFF — MiniBrowser on Linux: profiling it

Running MiniBrowser graphically is done (`wk gui <ws> [url]`, needs `wk session
on`; `--software` falls back to llvmpipe). Debugging it is done too, as
`wk gui --lldb [ui|web]` and `wk test --layout --lldb [web|ui]` — see
"Debugging (Linux, the CMake ports)" in `docs/TESTING.md` for what was verified
and at what cost.

What is left is the profiler.

## Remaining

- **`wk profile <ws> --browser` on the CMake ports.** It refuses today
  (`cmd/profile:162`), and for a reason that is no longer true: "the browser is
  started by Tools/Scripts/run-minibrowser and the process that matters is a
  child of it, so a profiler pointed at the launcher records the launcher".
  run-minibrowser splits `WEBKIT_MINI_BROWSER_PREFIX` and prepends it to the
  MiniBrowser command it builds (`webkitpy/port/{gtk,wpe}.py`), which is exactly
  how `wk gui --lldb` now gets a debugger in front of the right binary while
  keeping the port's own environment. samply goes in the same place.
- **Which process, then.** `--browser` would profile the UI process, which
  spends its life in a run loop; the web process is nearly always the subject,
  and `--attach $(config_web_process_name)` already reaches it. Whether
  `--browser` should mean "the UI process" or "the browser, web process
  included" is the open question, and it is the same one `--lldb ui` versus
  `--lldb web` answered for the debugger.
