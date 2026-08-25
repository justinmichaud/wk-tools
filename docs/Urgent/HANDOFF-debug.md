# HANDOFF — crash loops, and the provisioning debugging needs

The attach recipes this file used to own are built. There is no `wk debug`
verb and there will not be one: a debugger is not a different way of running
something, so it is a flag on the verb that already runs it —
`wk run --lldb` (jsc), `wk gui --lldb [ui|web]` (MiniBrowser),
`wk test --layout --lldb [web|ui]` (one layout test), on the Apple ports and on
the CMake ports alike. `cmd/gui:29` states that decision where it is acted on;
`docs/TESTING.md` has what was verified on each platform, and the three
image-level obstacles that had to be cleared on Linux first.

What is left is one verb, some provisioning, and two processes nobody has
needed yet.

## Remaining

- **`wk run <ws> --until-crash [--under-gdb]`** — repeat until non-zero exit,
  cap the iterations, keep the failing seed and output; under `--under-gdb`,
  drop into the debugger *at* the crash rather than after it. Nothing of this
  exists.
- **Provisioning that has to exist first.** core_pattern / systemd-coredump and
  the ddebs debug-symbol repository belong in the workspace image, not in
  per-session instructions. Without them `--until-crash` has nothing to hand
  back but an exit code.
- **The other two child processes.** `--lldb web` reaches the web process
  because `config_web_process_name` names it per port. The network and GPU
  processes have names in the same files and no caller —
  `WPENetworkProcess`/`WPEGPUProcess`, `WebKitNetworkProcess`/`WebKitGPUProcess`
  (`Source/WebKit/Platform{WPE,GTK}.cmake:17-18`). Add them when something
  needs them, not before.
- **`gtk-debug` has never been driven under a debugger**, and neither has an
  actual `Debug` build of either GLib port. Release now carries source-level
  debug info, so the gap is narrower than it was and no `wpe-debug` config is
  proposed until something needs one.

## Constraints

Prewarmed processes, PSON and site isolation will attach you to the wrong
process. Prewarming is handled — it cannot happen before the page has loaded,
so the first web process is the page's. Site isolation is off by default and
untried.

PSON is not handled and cannot be: `processSwapsOnNavigation()` is
`m_processSwapsOnNavigationFromClient.value_or(...the preference...)`, and
`WebKitWebContext.cpp:444` engages the client value unconditionally on WPE and
GTK4, so the preference MiniBrowser exposes through `--features` can never win.
Verified inert by measurement as well as by reading.

Since the swap cannot be prevented, `wk gui --lldb web` follows it instead:
`follow-page` attaches to every web process and asks each one whether it holds a
live page (`m_pageMap` non-empty and `m_hasSuspendedPageProxy` clear), keeps the
one that does and detaches the rest. The whole finding, with what was measured,
is under "Debugging (Linux, the CMake ports)" in `docs/TESTING.md`.
