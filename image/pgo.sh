# The board PGO lane: on a 2.52+ yocto profile a slot is three phases, and the middle one runs on the board. The macOS lane is build/mac-pgo.sh; what the two agree on is build/pgo.sh, and every profdata operation either performs is WebKit's own (lib/wkpgo.py).

command -v config_cross_load >/dev/null 2>&1 || . "$WK_ROOT/build/configs.sh"
command -v detach_run >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"

image_pgo_wanted() {   # the profile is loaded. 2.52 is where upstream's cmake support for a profile-guided build arrives (Source/cmake/WebKitFeatures.cmake's USE_PGO_PROFILE); before it there is nothing to turn on, after it every number a board produces is a profile-guided build's
    [ "${IMG_BUILDER:-}" = yocto ] || return 1
    [ -n "${CFG_RELEASE:-}" ] || return 1
    [ "$(printf '%s\n2.52\n' "$CFG_RELEASE" | sort -V | head -1)" = 2.52 ]
}

image_pgo_machine() {   # <profile> -- the board that carries this image, read off the fleet rather than given as a flag: a collection has to run on the hardware the profile is measured on
    local f n
    for f in "$(machines_dir)"/*.conf; do
        [ -f "$f" ] || continue
        n=$(basename "$f" .conf)
        ( machine_load "$n" >/dev/null 2>&1 && [ "${NODE_PROFILE:-}" = "$1" ] ) || continue
        printf '%s' "$n"; return 0
    done
    return 1
}

image_pgo_mode() {   # <machine> -- what it is running now, recomputed
    ( machine_load "$1" >/dev/null 2>&1
      b_probe >/dev/null 2>&1 || true
      printf '%s' "${MODE:-unreachable}" )
}

image_pgo_slot_is() {   # <profile> <slot> <sha> <cross config> -- is that slot already what this phase would build? The config is half the question: a slot built before this profile was profile-guided holds the right commit and the wrong code
    local sj; sj="$(image_slot_dir "$1" "$2")/slot.json"
    [ -f "$sj" ] || return 1
    [ "$(wkslot get "$sj" commit)" = "$3" ] && [ "$(wkslot get "$sj" build_config)" = "$4" ]
}

_pgo_require_board() {   # <profile> -- prints the machine, or refuses with the way out
    local profile="$1" machine mode
    machine=$(image_pgo_machine "$profile") || die "no fleet board carries $profile, so there is
    nowhere to collect a profile. A board declares the image it is for as
    NODE_PROFILE in boot/machines/<name>.conf; that declaration is what this
    reads. Every number from a $CFG_RELEASE board is a profile-guided build's,
    so there is no plain build of this profile to fall back to."
    mode=$(image_pgo_mode "$machine")
    case "$mode" in
        "bench $profile-"*) ;;
        *) die "$machine is '$mode', not a bench system built from $profile, so a
    collection there would profile the wrong code -- or nothing at all.
    Put it into the image first:
        wk sysimage write --from $profile --disk $machine:<device>
        wk boot $machine
    ('wk sysimage disks $machine' lists what is attached; 'wk boot $machine
    --status' says what it is running now.)" ;;
    esac
    printf '%s' "$machine"
}

image_pgo_plan() {   # <profile> <commit> <slot> <machine> -- one line per phase, in order
    local profile="$1" commit="$2" slot="$3" machine="$4" plan
    printf 'wk sysimage webkit %s --commit %s --slot %s-instr   (instrumented)\n' "$profile" "$commit" "$slot"
    printf 'wk pi deploy %s %s --slot %s-instr\n' "$profile" "$machine" "$slot"
    for plan in $PGO_BENCHMARKS; do
        printf 'wk pi bench %s %s --slot %s-instr --pgo %s\n' \
            "$machine" "$plan" "$slot" "$(image_pgo_dir "$profile" "$slot")"
    done
    printf 'wk sysimage build %s --stage pgo-mix --slot %s\n' "$profile" "$slot"
    printf 'wk sysimage webkit %s --commit %s --slot %s   (against the mixed profile)\n' "$profile" "$commit" "$slot"
}

image_pgo_webkit() {   # <profile> <the rest of `wk sysimage webkit`'s arguments>
    local profile="$1"; shift
    local commit="" slot="" dry="" detach=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --commit) commit="${2:-}"; shift ;;
            --slot)   slot="${2:-}"; image_check_slot_name "$slot"; shift ;;
            --dry-run) dry=1 ;;
            --detach)  detach=1 ;;
            *) die "usage: wk sysimage webkit $profile --commit <sha> --slot <name> [--detach|--dry-run]
    unexpected: $1
    $profile is $CFG_RELEASE, so its slots are profile-guided and built in
    three phases (image/pgo.sh); the flags that belong to one phase of an
    ordinary build do not apply." ;;
        esac
        shift
    done
    [ -n "$commit" ] && [ -n "$slot" ] || die "usage: wk sysimage webkit $profile --commit <sha> --slot <name>
    a slot needs both --commit <sha> and --slot <name>"
    case "$commit" in *[!0-9a-f]*) die "--commit takes a full sha (40 hex digits), got '$commit'" ;; esac
    [ "${#commit}" -eq 40 ] || die "--commit takes a full sha (40 hex digits), got '$commit'"

    local machine
    if [ -n "$dry" ]; then   # a dry run reports the board it would need rather than refusing over it
        machine=$(image_pgo_machine "$profile") || machine=""
        log "would build slot '$slot' of $profile as a profile-guided build"
        log "  board       ${machine:-none declares this image as its NODE_PROFILE, so this refuses}$([ -z "$machine" ] || echo " -- it has to be running this image; wk boot $machine --status")"
        log "  collection  $(image_pgo_dir "$profile" "$slot")"
        log "  benchmarks  $PGO_BENCHMARKS, mixed at WebKit's own weights"
        log "  phases:"
        image_pgo_plan "$profile" "$commit" "$slot" "${machine:-<board>}" | sed 's/^/    /' >&2
        log "dry run -- nothing was built."
        return 0
    fi
    machine=$(_pgo_require_board "$profile")

    if [ -n "$detach" ]; then
        local dlog; dlog="$(wk_ws_dir "$(yocto_ws_default "$profile")")/pgo-$slot.log"
        local pid; pid=$(detach_run "" "$dlog" -- \
            "$WK_ROOT/wk" sysimage webkit "$profile" --commit "$commit" --slot "$slot")
        info "detached as pid $pid -- this end can go away"
        log  "  follow:  tail -f $dlog"
        return 0
    fi

    image_pgo_slot "$profile" "$commit" "$slot" "$machine"
}

image_pgo_slot() {   # <profile> <commit> <slot> <machine>
    local profile="$1" commit="$2" slot="$3" machine="$4"
    local instr="$slot-instr" dir plan
    dir=$(image_pgo_dir "$profile" "$slot")

    info "profile-guided slot '$slot' of $profile: instrument, collect on $machine, rebuild"
    log  "  collection  $dir"
    log  "  benchmarks  $PGO_BENCHMARKS, mixed at WebKit's own weights (Tools/Scripts/pgo-profile)"

    # The one phase worth not repeating on a re-run, and only on evidence: a slot that already holds this commit built with this config is the instrumented build.
    if image_pgo_slot_is "$profile" "$instr" "$commit" wpe-cross-pgo-collect; then
        info "slot '$instr' already holds ${commit:0:12} instrumented; collecting against it"
    else
        yocto_build "$profile" --stage webkit --commit "$commit" --slot "$instr" \
            --config wpe-cross-pgo-collect
    fi

    "$WK_ROOT/wk" pi deploy "$profile" "$machine" --slot "$instr" \
        || die "could not put the instrumented build on $machine; nothing was collected"

    rm -rf "$dir"   # a collection is every leg of one run of one build, never a mixture of two
    for plan in $PGO_BENCHMARKS; do
        "$WK_ROOT/wk" pi bench "$machine" "$plan" --slot "$instr" --pgo "$dir" \
            || die "the $plan leg did not finish on $machine, so this profile would be
    missing a workload it is weighted for. What it did collect is in
    $dir; re-run this command once the board is well again."
    done

    yocto_build "$profile" --stage pgo-mix --slot "$slot"

    yocto_build "$profile" --stage webkit --commit "$commit" --slot "$slot" \
        --config wpe-cross-pgo-use \
        --pgo-profile "$(image_pgo_dir_in "$slot")/output/$PGO_GLIB_LIB.profdata"

    info "slot '$slot' is a profile-guided build of ${commit:0:12}"
    log  "  profile     $dir/output/$PGO_GLIB_LIB.profdata"
    log  "  readings    $dir/profile-check.json  ('wk sysimage ls' has the slot)"
    log  "  next:       wk pi deploy $profile $machine --slot $slot"
}
