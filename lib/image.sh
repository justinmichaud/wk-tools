command -v image_config_names >/dev/null 2>&1 || . "$WK_ROOT/image/profiles.sh"


# The spec, the image workspace (`<builder>-<profile>[-<arm>]`) and the slot and collection paths under it are lib/wk/images.py, what each builder left in one lib/wk/sysimage/ls.py, the image cache and pinned fetch lib/wk/sysimage/task.py. A shim keeps the name its unported callers use.
image_slot_dir()          { _wk_images slot-dir "$1" "$2"; }
image_toolchain_holds()   { _wk_images toolchain-holds "$1" "$2"; }
image_build_subject()     { _wk_images build-subject "${1:-}" "${2:-}" "${3:-}" "${4:-}" "${5:-}"; }
