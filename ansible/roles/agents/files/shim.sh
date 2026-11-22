#!/bin/sh
# wk-shim: a build tool's name on PATH. This line is how one shim knows another, so two never exec each other.
self=${0##*/}
if [ -n "${WK_AGENT:-}" ] && [ -z "${WK_BUILD:-}" ]; then
    echo "$self: refused: an agent builds and tests through wk, which sizes the job count to this machine." >&2
    echo "    use: wk build <ws> <preset>  |  wk test <ws> -- <args>  |  wk run <ws> -- <args>" >&2
    exit 1
fi
rest=$PATH:
while [ -n "$rest" ]; do
    dir=${rest%%:*}
    rest=${rest#*:}
    [ -n "$dir" ] || continue
    tool=$dir/$self
    [ -f "$tool" ] && [ -x "$tool" ] || continue
    head -n 2 "$tool" | grep -q '^# wk-shim:' && continue
    exec "$tool" "$@"
done
echo "$self: not found: PATH holds no $self but wk's shim" >&2
exit 127
