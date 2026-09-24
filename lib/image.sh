command -v image_config_names >/dev/null 2>&1 || . "$WK_ROOT/image/profiles.sh"

image_build_locations() {  # every place a build can leave bytes, so `wk gc` reclaims each; a new builder adds a line, and `wk selftest` checks every IMG_BUILDER has one
    printf '%s\n' "$WK_STORE/cache/buildroot"  # builder: buildroot
    printf '%s\n' "$WK_STORE/cache/yocto"      # builder: yocto
    printf '%s\n' "$(image_cache_dir)"         # builder: fetch
    :  # builder: pmos -- on the pmos build host, pruned there by gc_pmos
}

_image_task()      { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" WK_STORE="${WK_STORE:-}" python3 -m wk.sysimage.task "$@"; }
image_cache_dir()  { _image_task cache-dir; }
image_fetch_base() { local _o; _o=$(_image_task fetch "$1" "$2") || exit $?; printf '%s\n' "$_o"; }

# The spec, the image workspace (`<builder>-<profile>[-<arm>]`) and the slot and collection paths under it are lib/wk/images.py, what each builder left in one lib/wk/sysimage/ls.py, the image cache and pinned fetch lib/wk/sysimage/task.py. A shim keeps the name its unported callers use.
image_spec_profile()      { _wk_images spec-profile "$1"; }
image_spec_machine()      { _wk_images spec-machine "$1"; }
image_spec_target()       { _wk_images spec-target "$1" "$(wk_machine_name)" "$(default_target)"; }
image_lane_ws()           { _wk_images ws "$1"; }
image_lane_profile()      { _wk_images ws-profile "$1"; }
image_lane_arg()          { _wk_images ws-arg ${@+"$@"}; }
image_lane_machine()      { local t=""; [ -n "${2:-}" ] || t=$(ws_target "$1") || return $?; _wk_images ws-machine "${2:-}" "$t" "$(wk_machine_name)"; }
image_lane_here()         { _wk_images ws-here "$1" "$(wk_machine_name)" || exit $?; }
image_slot_dir()          { _wk_images slot-dir "$1" "$2"; }
image_toolchain_holds()   { _wk_images toolchain-holds "$1" "$2"; }
image_pgo_dir()           { _wk_images pgo-dir "$1" "$2"; }
image_pgo_dir_in()        { _wk_images pgo-dir-in "$1"; }
image_pgo_instr_slot()    { _wk_images instr-slot "$1"; }
image_pgo_measured_slot() { _wk_images measured-slot "$1"; }
image_build_resource()    { _wk_images build-resource "$1"; }
image_holds_predicate()   { _wk_images holds-predicate "$@"; }
image_build_subject()     { _wk_images build-subject "${1:-}" "${2:-}" "${3:-}" "${4:-}" "${5:-}"; }
image_check_slot_name()   { _wk_images check-slot-name "${1:-}" || exit $?; }
