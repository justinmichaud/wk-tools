"""The shell preludes cmd/run, cmd/gui, cmd/test and cmd/profile put in front of a target command: the loader path, prepended so the wkdev image's own jhbuild/libwpe prefix survives, and the lldb that starts (run, not found: the image's /opt/swift lldb links a libxml2 it lacks), pinned to the parent after ~/.lldbinit."""


def prelude(var, dir_):
    return 'export %s="%s${%s:+:${%s}}"' % (var, dir_, var, var)


LLDB_PRELUDE = """LLDB=""
for _c in lldb $(ls /usr/bin/lldb-[0-9]* 2>/dev/null | sort -Vr); do
    command -v "$_c" >/dev/null 2>&1 || continue
    "$_c" --version >/dev/null 2>&1 && { LLDB="$_c"; break; }
done
[ -n "$LLDB" ] || { printf 'error: no lldb here that will start -- `lldb` resolves to %s\\n' \\
    "$(command -v lldb || echo 'nothing')" >&2; exit 127; }"""

LLDB_PIN_OPTS = "-O 'settings set target.process.follow-fork-mode parent'"
