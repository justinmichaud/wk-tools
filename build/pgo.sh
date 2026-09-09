# What both PGO lanes agree on -- macOS (build/mac-pgo.sh) and the boards (image/pgo.sh); neither lane's own machinery is here. Dependency-free on purpose: build/mac-pgo.sh sources it inside the guest that builds, where lib/common.sh is not set up.

PGO_BENCHMARKS="speedometer3 jetstream3 motionmark"   # the benchmarks a profile is taken from. The ratio they are mixed at is WebKit's own (Tools/Scripts/pgo-profile) and is spelled nowhere here; `lib/wkpgo.py mix` refuses a benchmark upstream does not weigh, so this list cannot drift from it in silence

PGO_COLLECT_TIMEOUT="${WK_PGO_COLLECT_TIMEOUT:-7200}"   # a plan's own timeout is sized for a measured run of an ordinary build, and an instrumented one is several times slower. A collection is not timed, so it may take as long as it needs

PGO_GLIB_LIB=WPEWebKit   # a GLib port links one shared library, so one profile carries the whole engine, where the Apple ports carry three -- one per framework (Tools/Scripts/pgo-profile's PROFILED_DYLIBS)

PGO_BOARD_DIR=/var/wk/pgo   # where an instrumented slot writes on the board; baked in as PGO_PROFILE_DIR so a browser started by hand still writes somewhere writable
PGO_BOARD_FILE="$PGO_BOARD_DIR/$PGO_GLIB_LIB"'_%p.profraw'   # LLVM_PROFILE_FILE at launch: one file per process, so the browser's and each web process's counters arrive separately and merge as peers
