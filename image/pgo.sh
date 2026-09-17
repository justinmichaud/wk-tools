# The board PGO lane: on a 2.52+ yocto profile a slot is three phases, and the middle one runs on the board. The macOS lane is build/mac-pgo.sh; what the two agree on is build/pgo.sh, and every profdata operation either performs is WebKit's own (lib/wkpgo.py).

command -v config_cross_load >/dev/null 2>&1 || . "$WK_ROOT/build/configs.sh"
command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
command -v image_slot_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/image.sh"
command -v image_profile_load >/dev/null 2>&1 || . "$WK_ROOT/image/profiles.sh"
command -v wkslot >/dev/null 2>&1 || . "$WK_ROOT/lib/bench.sh"
command -v detach_run >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
command -v task_begin >/dev/null 2>&1 || . "$WK_ROOT/lib/task.sh"
command -v job_stop >/dev/null 2>&1 || . "$WK_ROOT/lib/watchdog.sh"
command -v sched_step >/dev/null 2>&1 || . "$WK_ROOT/lib/sched.sh"

# Most of the phases are not builds -- they run on the board -- so no workspace pid is alive through them and a cycle would read as idle. The driver's own pid is what is live for the whole cycle, which is why the record is `here`.
PGO_TASK=""
_pgo_task_end() { [ -z "$PGO_TASK" ] || task_end "$PGO_TASK" "${WK_EXIT_STATUS:-0}"; PGO_TASK=""; return 0; }

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

image_pgo_slot_is() {   # <lane workspace> <slot> <sha> <cross config> -- is that slot already what this phase would build? The config is half the question: a slot built before this profile was profile-guided holds the right commit and the wrong code
    local sj; sj="$(image_slot_dir "$1" "$2")/slot.json"
    [ -f "$sj" ] || return 1
    [ "$(wkslot get "$sj" commit)" = "$3" ] && [ "$(wkslot get "$sj" build_config)" = "$4" ]
}

image_slot_holds() {   # <lane workspace> <slot> <sha> -- is that slot the one this commit would produce in that lane? On a profile-guided release the build config is half the question, so the A/B and the cycle ask one predicate
    ( image_profile_load "$(image_lane_profile "$1")" >/dev/null 2>&1 || exit 1
      if image_pgo_wanted; then
          image_pgo_slot_is "$1" "$2" "$3" wpe-cross-pgo-use
      else
          local sj="$(image_slot_dir "$1" "$2")/slot.json"
          [ -f "$sj" ] && [ "$(wkslot get "$sj" commit)" = "$3" ]
      fi )
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

# Every one of these runs on the machine holding the lane, the collection included: the board writes its profiles into that lane's build directory, over the bind mount the builder reads them back through.
image_pgo_steps() {   # <profile>[@<machine>] <lane workspace> <commit> <slot> <board> [the step they wait for] -- the phases as steps of the open graph, each one command
    local spec="$1" lane="$2" commit="$3" slot="$4" board="$5" need="${6:-}"
    local instr on res plan legs="" id w dev="device:$board"
    instr=$(image_pgo_instr_slot "$slot")
    on=$(image_lane_machine "$lane" "$(image_spec_machine "$spec")")
    res=$(image_build_resource "$on")
    w="--workspace $lane"
    sched_step "instr:$lane:$slot" "$on" "$need" "$res" \
        "$(image_holds_predicate "$spec" "$lane" --slot "$instr" --commit "$commit" --config wpe-cross-pgo-collect)" \
        "wk sysimage webkit $spec $w --commit $commit --slot $instr --config wpe-cross-pgo-collect"
    sched_step "deploy:$board:$instr" "$on" "instr:$lane:$slot" "$dev" "" \
        "wk pi deploy $spec $board $w --slot $instr"
    for plan in $PGO_BENCHMARKS; do
        id="collect:$board:$slot:$plan"
        sched_step "$id" "$on" "deploy:$board:$instr" "$dev" "" \
            "wk pi bench $board $plan --slot $instr --pgo $spec $w"
        legs="${legs:+$legs,}$id"
    done
    sched_step "mix:$lane:$slot" "$on" "$legs" "$res" "" \
        "wk sysimage build $spec $w --stage pgo-mix --slot $slot"
    sched_step "slot:$lane:$slot" "$on" "mix:$lane:$slot" "$res" \
        "$(image_holds_predicate "$spec" "$lane" --slot "$slot" --commit "$commit")" \
        "wk sysimage webkit $spec $w --commit $commit --slot $slot --config wpe-cross-pgo-use"
}

_PGO_STEPS=""
_pgo_steps_gone() { [ -z "$_PGO_STEPS" ] || rm -f "$_PGO_STEPS"; _PGO_STEPS=""; return 0; }

image_pgo_graph() {   # <profile>[@<machine>] <lane workspace> <commit> <slot> <board> -- the cycle as a graph of its own
    _PGO_STEPS=$(mktemp "${TMPDIR:-/tmp}/wk-pgo-steps.XXXXXX")
    wk_atexit _pgo_steps_gone
    sched_begin "$_PGO_STEPS" "$WK_ROOT/image/pgo.sh"
    image_pgo_steps "$1" "$2" "$3" "$4" "$5"
}

image_pgo_webkit() {   # <profile>[@<machine>] <the rest of `wk sysimage webkit`'s arguments>
    local spec="$1" profile; profile=$(image_spec_profile "$1"); shift
    local commit="" slot="" dry="" detach="" config="" stop="" lane=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --commit) commit="${2:-}"; shift ;;
            --slot)   slot="${2:-}"; image_check_slot_name "$slot"; shift ;;
            --workspace) lane="${2:-}"; shift ;;
            --dry-run) dry=1 ;;
            --detach)  detach=1 ;;
            --stop)    stop=1 ;;
            # One phase of the cycle, or the unprofiled build. `wpe-cross` is not a default: it exists so a slot without a profile can be measured against one, and it records itself -- the slot's manifest says wpe-cross, and every run off it carries that into its env.json.
            --config) config="${2:-}"
                      case "$config" in
                          wpe-cross|wpe-cross-pgo-collect|wpe-cross-pgo-use) ;;
                          *) die "--config takes one of the configs $profile is built with, not '$config':
    wpe-cross-pgo-collect and wpe-cross-pgo-use are one phase of the cycle each
    ('wk sysimage webkit $profile --commit <sha> --slot <name>' runs all of
    them), and 'wpe-cross' is a slot built without a profile, for measuring
    against one." ;;
                      esac
                      shift ;;
            *) die "usage: wk sysimage webkit $profile --commit <sha> --slot <name> [--workspace <lane>] [--detach|--dry-run|--stop]
    unexpected: $1
    $profile is $CFG_RELEASE, so its slots are profile-guided and built in
    three phases (image/pgo.sh); the flags that belong to one phase of an
    ordinary build do not apply." ;;
        esac
        shift
    done
    [ -n "$lane" ] || lane=$(image_lane_ws "$profile")

    # Before the --commit rule: stopping the cycle running for a slot is about
    # the slot, and the record it stops names exactly this command.
    if [ -n "$stop" ]; then
        [ -n "$slot" ] || die "usage: wk sysimage webkit $profile --slot <name> --stop
    --stop stops the cycle running for one slot, so it needs --slot"
        [ -z "$commit$dry$detach$config" ] || die "'wk sysimage webkit $spec --slot $slot --stop' stops the cycle already
    running for that slot and takes nothing with it -- no --commit, --config,
    --detach or --dry-run."
        local rc=0; job_stop "$lane/$slot" pgo || rc=$?
        [ "$rc" != 1 ] || die "the driver of '$lane/$slot' outlived a TERM and a KILL:
        ps -p $(task_field "$(task_find pgo "$lane/$slot")" pid)"
        return 0
    fi

    [ -n "$commit" ] && [ -n "$slot" ] || die "usage: wk sysimage webkit $profile --commit <sha> --slot <name>
    a slot needs both --commit <sha> and --slot <name>"
    case "$commit" in *[!0-9a-f]*) die "--commit takes a full sha (40 hex digits), got '$commit'" ;; esac
    [ "${#commit}" -eq 40 ] || die "--commit takes a full sha (40 hex digits), got '$commit'"

    if [ -n "$config" ]; then
        image_pgo_phase "$lane" "$commit" "$slot" "$config" "$dry" "$detach"
        return $?
    fi

    local machine
    if [ -n "$dry" ]; then   # a dry run reports the board it would need rather than refusing over it
        machine=$(image_pgo_machine "$profile") || machine=""
        log "would build slot '$slot' of $profile as a profile-guided build"
        log "  lane        $lane"
        log "  board       ${machine:-none declares this image as its NODE_PROFILE, so this refuses}$([ -z "$machine" ] || echo " -- it has to be running this image; wk boot $machine --status")"
        log "  collection  $(image_pgo_dir "$lane" "$slot")"
        log "  benchmarks  $PGO_BENCHMARKS, mixed at WebKit's own weights"
        log ""
        image_pgo_graph "$spec" "$lane" "$commit" "$slot" "${machine:-<board>}"
        sched_plan
        log "dry run -- nothing was built."
        return 0
    fi
    machine=$(_pgo_require_board "$profile")

    if [ -n "$detach" ]; then
        local dlog; dlog="$(wk_ws_dir "$lane")/pgo-$slot.log"
        local pid; pid=$(detach_run "$dlog" -- \
            "$WK_ROOT/wk" sysimage webkit "$spec" --workspace "$lane" --commit "$commit" --slot "$slot")
        info "detached as pid $pid -- this end can go away"
        log  "  follow:  tail -f $dlog"
        return 0
    fi

    image_pgo_slot "$spec" "$lane" "$commit" "$slot" "$machine"
}

image_pgo_phase() {   # <lane workspace> <commit> <slot> <config> <dry> <detach> -- one phase of the cycle, as one command
    local lane="$1" commit="$2" slot="$3" config="$4" dry="${5:-}" detach="${6:-}"
    local extra=()
    case "$config" in
        wpe-cross)
            warn "slot '$slot' is being built WITHOUT a profile, on a release where every
  measured build has one. Nothing but a comparison against a profile-guided
  slot should be taken from it; 'wk sysimage ls' and each run's env.json
  record it as wpe-cross." ;;
        wpe-cross-pgo-collect)
            # A collection is every leg of one run of one build: what was taken against the last instrumented build says nothing about this one.
            act rm -rf "$(image_pgo_dir "$lane" "$(image_pgo_measured_slot "$slot")")" ;;
        wpe-cross-pgo-use)
            extra=(--pgo-profile "$(image_pgo_dir_in "$slot")/output/$PGO_GLIB_LIB.profdata") ;;
    esac
    yocto_build "$(image_lane_profile "$lane")" --workspace "$lane" \
        --stage webkit --commit "$commit" --slot "$slot" \
        --config "$config" ${extra[@]+"${extra[@]}"} ${dry:+--dry-run} ${detach:+--detach}
}

image_pgo_slot() {   # <profile>[@<machine>] <lane workspace> <commit> <slot> <board> -- the whole cycle, as the graph its phases are
    local spec="$1" lane="$2" commit="$3" slot="$4" machine="$5" rc=0 line dir profile
    profile=$(image_spec_profile "$spec")
    dir=$(image_pgo_dir "$lane" "$slot")
    image_pgo_graph "$spec" "$lane" "$commit" "$slot" "$machine"
    local plan=()
    while IFS= read -r line; do [ -z "$line" ] || plan+=("$line"); done <<EOF
$(sched_steps)
EOF
    PGO_TASK=$(task_begin pgo here "$lane/$slot" \
        "wk sysimage webkit $spec --workspace $lane --slot $slot --stop" \
        "$(yocto_log "$lane" webkit)" "${plan[@]}")
    wk_atexit _pgo_task_end
    info "profile-guided slot '$slot' of $profile in lane $lane: instrument, collect on $machine, rebuild"
    log  "  collection  $dir"
    log  "  benchmarks  $PGO_BENCHMARKS, mixed at WebKit's own weights (Tools/Scripts/pgo-profile)"

    sched_run --on-event "task_step_event $(sh_quote "$PGO_TASK") {step} {event}" || rc=$?
    [ "$rc" -eq 0 ] || die "the cycle for '$slot' stopped: the steps above say which phase is left
    and why. What was collected is in $dir, and re-running this command takes
    up what is left rather than starting again."

    info "slot '$slot' is a profile-guided build of ${commit:0:12}"
    log  "  profile     $dir/output/$PGO_GLIB_LIB.profdata"
    log  "  readings    $dir/profile-check.json  ('wk sysimage ls' has the slot)"
    log  "  next:       wk pi deploy $spec $machine --workspace $lane --slot $slot"
}
