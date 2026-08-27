# HANDOFF — `wk profile`: the provisioning half

The command is written and its resolution is verified in `wk selftest --quick`.
`cmd/profile`'s own header is the interface authority: `wk profile [<ws>]
[--mode <m>] [file.js | --browser [url] | --attach <pid|name>]`, modes
`sampling` | `bytecode` | `samply` | `instruments` | `heaptrack` | `massif` |
`native`, plus `--jit-dump`, `--markers`, `--fetch`, `--env`, `--dry-run`.

What is missing is everything the command needs in order to actually run.

## Remaining

- **No mode has ever been executed.** None of the five `wk profile` run lines
  is confirmed working (sampling, bytecode, samply in a container, instruments
  in a guest, `--fetch` out of a guest).
- **`--mode strongrefs`** — the option name (`JSC_enableStrongRefTracker`) has
  never been checked against `OptionsList.h`, and a typo'd `JSC_` variable is
  ignored in silence, which makes a profile look like the code changed. `--env
  NAME=VALUE` covers it meanwhile.
- **`--browser` on the CMake ports** — decision pending (`docs/defects` 13);
  `--attach` until that is wired.
- **The skills already invoke the command** (`jsc-profile`, `jsc-marker-trace`)
  and refuse by name when a tool is missing — so the only thing standing between
  them and a working run is the provisioning above.

## Done means

The jsc-profile and jsc-marker-trace skills contain no raw env-var walls and no
host paths — each mode is one `wk profile` line — and a fresh workspace can run
every mode with no manual install step.
