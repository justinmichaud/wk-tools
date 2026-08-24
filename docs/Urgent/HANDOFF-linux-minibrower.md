# HANDOFF — MiniBrowser on Linux: the debugging half

Running MiniBrowser graphically and interacting with it is done: `wk gui <ws>
[url]` drives it in the host's graphical session (needs `wk session on`;
`--software` falls back to llvmpipe). What is missing is debugging it.

Reference: https://github.com/justinmichaud/justinmichaud.github.io/wiki/Debugging-WPE-Linux-(desktop)

## Remaining

- **Attach a debugger on Linux.** `wk gui --lldb` refuses today with "--lldb is
  only wired up for the Apple ports" (`cmd/gui:116`).
- **Debug a single layout test.**
- **Do not let prewarm processes, PSON or site isolation attach the debugger to
  the wrong process.**

All three are specced as `wk debug` in `docs/Urgent/HANDOFF-debug.md`, which is
also unbuilt. Build them there, once.
