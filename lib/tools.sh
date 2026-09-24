# Putting $WK_ROOT's commit on a machine, for a bash caller: each function one call into lib/wk/tools.py, found beside this file since a caller's WK_ROOT may be another tree.

_tools_py() { PYTHONPATH="${BASH_SOURCE[0]%/*}" WK_ROOT="$WK_ROOT" python3 -m wk.tools "$@"; }   # committed | push <dest> <ssh destination> [ssh option]...

tools_committed() { _tools_py committed; }

tools_push() { _tools_py push "$1" "$3"; }   # <dest> mac_ssh <ssh destination>: boot/machines.sh's one call, whose transport is plain ssh
