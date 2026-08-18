# HANDOFF — `wk debug` and `wk run --until-crash`

The attach recipes and crash loops live in the wiki
(`Debugging-WPE-Linux-(desktop)`, `MacOS-debugging-WebKit`) and get re-typed
per session. Two small verbs encode them. Also the natural home for the
"attach a debugger, including to a layout test" requirement in
`docs/HANDOFF-linux-minibrower.md` — implement these together rather than
twice.

## `wk debug <ws> [--webprocess|--network|--gpu] [--config C] [test-or-url]`

- Start MiniBrowser (or a single layout test) with the target process held:
  `WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH=1` or the `gdbserver`/
  `lldb --wait-for` path, whichever fits the port.
- Print — or directly exec — the matching attach command, instead of leaving
  the ps/awk pid hunt to the user.
- Make sure prewarmed processes, PSON, and site isolation don't attach you to
  the wrong process — the flags to pin one process are part of the recipe.
- Provisioning: core_pattern/systemd-coredump setup and the ddebs debug-symbol
  repository belong in the workspace image, not in per-session instructions.
- The macOS-guest flavour (`lldb -n com.apple.WebKit.WebContent.Development`,
  the `__XPC_` env doubling) is the lane B extension once the Linux shape
  works.

## `wk run <ws> --until-crash [--under-gdb] -- <jsc args>`

Replaces the wiki's `while ... jsc ...; do echo -; done` and
`for i in {1..10}; do gdb ... jsc ...; done` loops: repeat until non-zero
exit, cap the iterations, keep the failing seed/output, and under `--under-gdb`
drop into the debugger at the crash instead of after it.
