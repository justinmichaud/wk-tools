# HANDOFF — `wk profile`: the provisioning half

The command is written and its resolution is verified in `wk selftest --quick`.
`cmd/profile`'s own header is the interface authority: `wk profile [<ws>]
[--mode <m>] [file.js | --browser [url] | --attach <pid|name>]`, modes
`sampling` | `bytecode` | `samply` | `instruments` | `heaptrack` | `massif` |
`native`, plus `--jit-dump`, `--markers`, `--fetch`, `--env`, `--dry-run`.

What is missing is everything the command needs in order to actually run.

## Remaining

- **No mode has ever been executed.** All five `wk profile` run lines in
  `docs/TESTING.md` §4 are unticked (sampling, bytecode, samply in a container,
  instruments in a guest, `--fetch` out of a guest).
- **The tools are not in the workspace image.** Nothing in `container/` or
  `image/` installs samply, heaptrack, valgrind or sysprof-cli, so the command
  refuses by name with an install line. Seed them like bench payloads or build
  them into the image; no more `~/Development/samply` host paths.
- **`perf_event_paranoid` has no workspace answer.** `cmd/profile:373` reads it
  and dies telling the user to `sudo tee` it on the host. Decide the mechanism
  once (a quiesce-helper verb on the host is the pattern that exists). On a
  bench system it is already ours: `wk sysimage build` writes
  `kernel.perf_event_paranoid = -1` (`cmd/sysimage:847`).
- **Viewing is blocked.** `profiler.firefox.com` is not in `ALLOWED_HOSTS`
  (`container/proxy/wk-proxy.py`) and nothing serves a profile out of a
  workspace. Serve it on loopback plus a forwarded port, or copy it out — and
  document which.
- **`--mode strongrefs`** — the option name (`JSC_enableStrongRefTracker`) has
  never been checked against `OptionsList.h`, and a typo'd `JSC_` variable is
  ignored in silence, which makes a profile look like the code changed. `--env
  NAME=VALUE` covers it meanwhile.
- **`--browser` on the CMake ports** — refused, because `run-minibrowser` is a
  launcher and the process that matters is its child. `--attach` until that is
  wired.
- **The skills already invoke the command** (`jsc-profile`, `jsc-marker-trace`)
  and refuse by name when a tool is missing — so the only thing standing between
  them and a working run is the provisioning above.

## Done means

The jsc-profile and jsc-marker-trace skills contain no raw env-var walls and no
host paths — each mode is one `wk profile` line — and a fresh workspace can run
every mode with no manual install step.
