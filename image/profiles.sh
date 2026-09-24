# Named system profiles are image/configs/*.conf, read by lib/wk/images.py; these are its bash callers' names for it.

_wk_images() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" WK_STORE="${WK_STORE:-}" python3 -m wk.images "$@"; }

image_config_names()    { _wk_images names; }
image_config_file()     { _wk_images conf-path "$1"; }
image_profile_list()    { _wk_images list; }
image_origin_branches() { _wk_images origin-branches; }
image_pgo_wanted()      { _wk_images pgo-wanted "${IMG_BUILDER:-}" "${CFG_RELEASE:-}"; }   # of the loaded profile
image_profile_load()    { local _o _rc=0; _o=$(_wk_images load "${1:-}") || _rc=$?; case "$_rc" in 0) eval "$_o" ;; 3) return 1 ;; *) exit "$_rc" ;; esac; }
