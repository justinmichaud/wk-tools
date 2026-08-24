# HANDOFF — `wk debug` and `wk run --until-crash`

Two verbs that encode the wiki's attach recipes and crash loops
(`Debugging-WPE-Linux-(desktop)`, `MacOS-debugging-WebKit`) so they are not
re-typed per session. Nothing is built: no `debug` in `cmd/`, no
`--until-crash` in `cmd/run`, no `WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH` anywhere.

Also owns the debugger half of `docs/Urgent/HANDOFF-linux-minibrower.md` —
implement them together, not twice.

## Remaining

- **`wk debug <ws> [--webprocess|--network|--gpu] [--config C] [test-or-url]`** —
  start MiniBrowser, or a single layout test, with the target process held
  (`WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1`, or the `gdbserver` /
  `lldb --wait-for` path, whichever fits the port), then print or exec the
  matching attach command instead of leaving a ps/awk pid hunt to the user.
- **`wk run <ws> --until-crash [--under-gdb]`** — repeat until non-zero exit,
  cap the iterations, keep the failing seed and output; under `--under-gdb`,
  drop into the debugger *at* the crash rather than after it.
- **Provisioning that has to exist first**: core_pattern / systemd-coredump and
  the ddebs debug-symbol repository belong in the workspace image, not in
  per-session instructions.
- **The macOS-guest flavour** (`lldb -n com.apple.WebKit.WebContent.Development`,
  the `__XPC_` env doubling) once the Linux shape works.

## Constraints

Prewarmed processes, PSON and site isolation will attach you to the wrong
process — the flags that pin one process are part of the recipe, not a detail.
