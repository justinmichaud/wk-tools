#!/usr/bin/env bash
# wk bench mac-ab -- an interleaved A/B on this Mac's benchmark install, with
# nobody in the room.
#
#   wk bench mac-ab [<workspace>] [--plan P] [--rounds N] [--count N]
#                   [--a <staged-id>] [--b <staged-id>] [--stage]
#   wk bench mac-ab <workspace> --patch <ref|diff> [--base <ref>] [--rounds N]
#   wk bench mac-ab --preflight | --status | --collect | --dry-run
#
# The benchmark install has no network (tolken is Wi-Fi only and that install joins nothing), so the job is planted rather than driven: everything it needs is written onto the volume while merely mounted, a per-user LaunchAgent starts it at autologin, and this driver waits and reads. No sudo, because /var/wk and ~bench are both uid 501.
# Nothing here can set which volume the firmware boots -- `nvram boot-volume`, `bless --setBoot` and `systemsetup -getstartupdisk` all fail silently -- so this driver reboots and reports which mode came back: at most one human action per A/B, never one per run, since the planted job holds every round of every arm.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/boot/machines.sh"

HOST="${WK_MAC_SSH:-}"
MACHINE="${WK_MAC_MACHINE:-mbp}"  # static
VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
PLAN="${WK_MAC_PLAN:-speedometer3.0}"
CONFIG="${WK_MAC_CONFIG:-mac-release}"
ROUNDS=2
COUNT=""
TIMEOUT=1800
SETTLE=90
A_ID=""; B_ID=""
PATCH=""; BASE_REF=""; ARMS_PENDING=""
A_ARGS=""; B_ARGS=""
WS=""
DO_STAGE=""
ALLOW_FETCH=""
FORCE=""
AGENT_HOME=""
DRY=""
ACTION=run
BOOT_WAIT="${WK_MAC_BOOT_WAIT:-3600}"

usage() { usage_block "$0" >&2; exit 2; }

mac() {
    mac_ssh "$HOST" "$@"
}
mac_sh() { mac bash -lc "$(sh_quote "$*")"; }

mac_hw_uuid() {   # what macOS names ByHost preferences by; either install answers, since they are one machine
    mac_sh 'ioreg -rd1 -c IOPlatformExpertDevice' 2>/dev/null \
        | awk -F'"' '/IOPlatformUUID/{print $4; exit}' | tr -d '\r'
}

TOOLS="${WK_MAC_TOOLS:-}"
host_tools() {
    [ -n "$TOOLS" ] && { printf '%s' "$TOOLS"; return 0; }
    TOOLS=$(mac_sh 'for d in ~/Development/wk-tools ~/wk-tools; do [ -x "$d/wk" ] && { echo "$d"; exit 0; }; done' 2>/dev/null | tr -d '\r' | head -1)
    [ -n "$TOOLS" ] || die "cannot find wk-tools on $HOST"
    printf '%s' "$TOOLS"
}
rwk() { mac_sh "cd $(sh_quote "$(host_tools)") && ./wk $*"; }

bwk() { mac_sh "cd $(sh_quote "$(bench_root)/wk-tools") && ./wk $*"; }   # the planted copy, the same age as the job

# openrsync, which this Mac ships, sends `/Volumes/WK Bench - Data/...` with its escaping intact and fails with `open: No such file or directory`; over ssh the remote path appears once, inside a command this side quotes.
put_file() {  # $1 = local file, $2 = remote path
    mac "cat > $(sh_quote "$2")" < "$1" || return 1
    local want got   # a `cat >` that wrote nothing exits 0, so verify by byte count
    want=$(wc -c < "$1" | tr -d ' ')
    got=$(mac "wc -c < $(sh_quote "$2")" 2>/dev/null | tr -d ' \r')
    [ "$want" = "$got" ] || {
        warn "put_file: $2 is $got bytes, expected $want"
        return 1
    }
}

# $1 = local dir, $2 = remote dir, replaced wholesale. Verified by a sentinel only this tree carries plus a byte count, because a stale or truncated tree is otherwise discovered after the reboot, where nothing can report it.
put_tree() {
    local src="$1" dst="$2"
    tar -cf - --exclude '.git' -C "$src" . \
        | mac "rm -rf $(sh_quote "$dst") && mkdir -p $(sh_quote "$dst") && tar -xf - -C $(sh_quote "$dst")" \
        || return 1

    local probe="bench/mac-ab.sh" want got
    want=$(wc -c < "$src/$probe" | tr -d ' ')
    got=$(mac "wc -c < $(sh_quote "$dst/$probe") 2>/dev/null" 2>/dev/null | tr -d ' \r')
    if [ -z "$got" ]; then
        warn "put_tree: $dst/$probe is not there -- the tree did not land"
        return 1
    fi
    [ "$want" = "$got" ] || {
        warn "put_tree: $dst/$probe is $got bytes, expected $want"
        return 1
    }
    log "  verified: $dst carries this lane's own tree ($probe, $got bytes)"
}

BROOT=""
bench_root() {
    [ -n "$BROOT" ] && { printf '%s' "$BROOT"; return 0; }
    BROOT=$(mac_sh "cd $(sh_quote "$(host_tools)") && . lib/common.sh && . boot/machines.sh && machine_load $(sh_quote "$MACHINE") && load_driver \"\$NODE_DRIVER\" && b_bench_root" 2>/dev/null | tr -d '\r' | tail -1)
    [ -n "$BROOT" ] || die "'$VOLUME' is not visible from $HOST right now.
    Either it is not attached, or $HOST is *in* bench mode -- that install's own
    root is the volume, so it is not mounted under /Volumes and every verb here
    is a host-mode verb. 'wk boot $MACHINE --status' over there says which.
    In bench mode the run drives itself; read it back once the machine returns."
    printf '%s' "$BROOT"
}

bench_home() {
    local d; d=$(dirname "$(bench_root)")          # …/private/var
    printf '%s' "$(dirname "$(dirname "$d")")/Users/bench"
}

# Every check here is something that, if wrong, is discovered after the reboot on a machine that cannot be reached.
PF_FAIL=0
ck() {  # ck yes|no <label> <detail>
    if [ "$1" = yes ]; then printf '  \033[32mok\033[0m   %-24s %s\n' "$2" "$3" >&2
    else PF_FAIL=$((PF_FAIL + 1)); printf '  \033[31mFAIL\033[0m %-24s %s\n' "$2" "$3" >&2; fi
}

preflight() {
    info "preflight for an unattended A/B on $HOST"
    PF_FAIL=0

    local mode
    if mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"' 2>/dev/null); then
        mode=$(printf '%s' "$mode" | tr -d '\r')
        if [ -n "$mode" ]; then
            ck no "host mode" "$HOST is in BENCH mode ($mode) -- plant from host mode"
        else
            ck yes "host mode" "$HOST answers and carries no bench marker"
        fi
    else
        ck no "reachable" "$HOST does not answer ssh with a key"
        log "  everything below needs the machine, so nothing else was checked." >&2
        return 1
    fi

    local root; root=$(bench_root 2>/dev/null) || root=""
    if [ -n "$root" ]; then ck yes "bench volume" "$VOLUME at $root"
    else ck no "bench volume" "'$VOLUME' is not attached"; return 1; fi

    if mac "test -w $(sh_quote "$root")" 2>/dev/null; then
        ck yes "staging root" "writable without sudo"
    else
        ck no "staging root" "$root is not writable as this account -- staging would need sudo"
    fi

    local bh; bh=$(bench_home)
    if mac "test -d $(sh_quote "$bh") && test -w $(sh_quote "$bh/Library")" 2>/dev/null; then
        ck yes "bench home" "$bh (LaunchAgents installable without sudo)"
    else
        ck no "bench home" "$bh/Library is not writable -- the agent cannot be planted"
    fi

    # Without a console session the browser has nowhere to draw and the run looks like a hang.
    local alu
    alu=$(mac "defaults read $(sh_quote "$bh/../../Library/Preferences/com.apple.loginwindow") autoLoginUser 2>/dev/null" 2>/dev/null | tr -d '\r')
    if [ "$alu" = bench ]; then ck yes "autologin" "the bench account logs in at the console"
    else ck no "autologin" "autoLoginUser is '${alu:-unset}' -- the run would have no session"; fi

    local sshd
    sshd=$(mac "/usr/libexec/PlistBuddy -c 'Print :com.openssh.sshd' $(sh_quote "$bh/../../private/var/db/com.apple.xpc.launchd/disabled.plist") 2>/dev/null" 2>/dev/null | tr -d '\r')
    log "  note remote login on the bench install: $([ "$sshd" = false ] && echo enabled || echo "disabled/unknown ($sshd)")" >&2

    if mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/objc")" 2>/dev/null; then
        ck yes "pyobjc over there" "in the bench account's site-packages"
    else
        ck no "pyobjc over there" "run-benchmark's prepare_env does a bare 'import objc'"
    fi

    if mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/scipy")" 2>/dev/null; then
        ck yes "scipy over there" "the A/B can be compared on the volume"
    else
        log "  note scipy is not on the bench install; --plant installs it (needs this machine's network)" >&2
    fi

    if mac "test -x $(sh_quote "$root/wk-tools/wk")" 2>/dev/null; then
        ck yes "planted wk-tools" "$root/wk-tools"
    else
        log "  note nothing planted yet; --plant puts this lane's own tree at $root/wk-tools" >&2
    fi

    if mac "test -f $(sh_quote "$bh/../../Library/LaunchDaemons/com.wk.bench-firstboot.plist")" 2>/dev/null; then
        log "  note the first-boot daemon is still installed on this volume. It reverts" >&2
        log "       tooling and reboots the machine ~1 min into a boot; the autorun" >&2
        log "       cancels the reboot and removes it, so this cycle spends its first" >&2
        log "       boot defusing it. The cycle after this one is clean." >&2
    fi

    local staged
    staged=$(mac "ls -1 $(sh_quote "$root/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort)
    if [ -n "$staged" ]; then
        ck yes "staged builds" "$(printf '%s' "$staged" | tr '\n' ' ')"
    elif [ -n "$DO_STAGE" ] || [ -n "$WS" ]; then
        log "  note nothing staged yet; --stage will put a build there" >&2
    else
        ck no "staged builds" "nothing on the volume, and no workspace given to stage from"
    fi

    # Reported, never asserted: the startup manager can override the firmware's own variable without updating it.
    local bv grp
    bv=$(mac "python3 $(sh_quote "$(host_tools)/lib/wkmac.py") boot-volume" 2>/dev/null | tr -d '\r')
    grp="${bv##*:}"
    local bench_grp host_grp
    bench_grp=$(mac "python3 $(sh_quote "$(host_tools)/lib/wkmac.py") volume-group $(sh_quote "/Volumes/$VOLUME")" 2>/dev/null | tr -d '\r')
    host_grp=$(mac "python3 $(sh_quote "$(host_tools)/lib/wkmac.py") volume-group /" 2>/dev/null | tr -d '\r')
    log "" >&2
    log "  the firmware's boot-volume names:" >&2
    if [ -n "$grp" ] && [ "$grp" = "$bench_grp" ]; then
        log "    $grp  = '$VOLUME'" >&2
        log "    so a plain reboot is expected to land in BENCH mode, and this A/B" >&2
        log "    needs no human at all. If it lands in host mode instead, the" >&2
        log "    startup manager is the one step -- the job stays planted for it." >&2
    elif [ -n "$grp" ] && [ "$grp" = "$host_grp" ]; then
        log "    $grp  = the host install" >&2
        log "    so a plain reboot returns here, and entering bench mode is the one" >&2
        log "    human step: hold the power button, pick '$VOLUME'. Everything" >&2
        log "    after that, including coming back, is unattended." >&2
    else
        log "    ${grp:-<unreadable>}  (matches neither install)" >&2
    fi

    log "" >&2
    [ "$PF_FAIL" -eq 0 ] || { warn "$PF_FAIL preflight check(s) failed"; return 1; }
    info "preflight clean"
    return 0
}


phase_stage() {
    [ -n "$WS" ] || die "--stage needs a workspace to stage from"
    info "stage: $CONFIG from '$WS' onto $VOLUME"
    [ -n "$DRY" ] && { log "  would: wk vm start $WS; wk bench seed $WS $PLAN; wk bench stage $WS --to $MACHINE"; return 0; }
    rwk vm start "$WS" >/dev/null 2>&1 || true
    # The payload is pinned here because a run-benchmark that clones it over there dies where nobody can see it. stdout only: `2>&1 | tail -1` would race seed's stderr progress against its stdout path.
    local payload
    payload=$(rwk bench seed "$WS" "$PLAN" | tr -d '\r' | tail -1) || payload=""
    case "$payload" in /*) ;; *) payload="" ;; esac
    if [ -n "$payload" ] && mac "test -d $(sh_quote "$payload")" 2>/dev/null; then
        log "  payload pinned: $payload"
    elif [ -n "$ALLOW_FETCH" ]; then
        payload=""
        warn "  no pinned payload, and --allow-network-fetch was given. Each run will
  clone $PLAN itself, so the benchmark install needs a working network *and*
  the two arms could in principle get different revisions of the benchmark."
    else
        die "the $PLAN payload could not be pinned, so nothing may be staged.

    Each run would clone the benchmark itself, over a network the benchmark
    install may not have -- and if it does not, every arm fails after the
    reboot, where nothing can say so.

    Pin it here, where the network is:
        wk bench seed $WS $PLAN
    then re-run. If that fails, its error is the thing to fix -- it reads the
    plan file out of '$WS', so the workspace has to be one that has the tree.

    To go ahead anyway (a benchmark install you know has a route out, and a
    revision of the benchmark you are content to have chosen for you):
        --allow-network-fetch"
    fi
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" --plan "$PLAN" \
        ${payload:+--payload "$payload"}

    info "  stopping the build guest"   # a running macOS VM competes for CPU with whatever runs next
    rwk vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'"
}

# One command in the build guest, two shells away, written to a file and then run: a script on stdin is consumed by the first thing inside it that reads stdin, silently truncating the rest.
guest_sh() {
    local b; b=$(printf '%s' "$1" | base64 | tr -d '\n')
    mac_sh "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new wk-$(sh_quote "$WS") 'printf %s $b | base64 -d > /tmp/wk-guest.sh && bash /tmp/wk-guest.sh'"
}

guest_src() {
    local d
    d=$(guest_sh 'for p in ~/WebKit ~/webkit; do [ -d "$p/.git" ] && { echo "$p"; exit 0; }; done' 2>/dev/null | tr -d '\r' | head -1)
    [ -n "$d" ] || die "no WebKit checkout found in the build guest '$WS'"
    printf '%s' "$d"
}

staged_ids() {
    mac "ls -1 $(sh_quote "$(bench_root)/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort
}

build_and_stage() {   # the staged id is the directory new since before the stage
    local label="$1" before after id
    before=$(staged_ids)
    info "  building $label"
    rwk build "$WS" "$CONFIG" >&2 || die "the $label build failed"
    local payload
    payload=$(rwk bench seed "$WS" "$PLAN" | tr -d '\r' | tail -1) || payload=""
    case "$payload" in /*) ;; *) die "could not pin the $PLAN payload for $label" ;; esac
    info "  staging $label"
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" --plan "$PLAN" \
        --payload "$payload" >&2 || die "staging $label failed"
    after=$(staged_ids)
    id=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | tail -1)
    [ -n "$id" ] || die "staging $label produced no new directory on $VOLUME"
    log "  $label staged as $id" >&2
    printf '%s' "$id"
}

phase_build_ab() {
    [ -n "$WS" ] || die "--patch needs a workspace to build in (wk bench mac-ab <ws> --patch ...)"
    [ -n "$DRY" ] && {
        log "  would build the baseline (${BASE_REF:-current HEAD}) in '$WS' and stage it"
        log "  would then apply '$PATCH' and stage that as the second arm"
        ARMS_PENDING=1
        A_ID="<baseline ${BASE_REF:-HEAD}, would be built>"
        B_ID="<patched $PATCH, would be built>"
        return 0
    }
    rwk vm start "$WS" >/dev/null 2>&1 || true
    local src; src=$(guest_src)
    log "  checkout: $src"

    local orig   # a detached HEAD prints nothing for --abbrev-ref, so the sha is next
    orig=$(guest_sh "git -C $src symbolic-ref --quiet --short HEAD 2>/dev/null || git -C $src rev-parse HEAD" | tr -d '\r' | head -1)
    [ -n "$orig" ] || die "could not read the guest checkout's current ref"
    log "  will restore '$orig' when done"

    local base="${BASE_REF:-$orig}"
    guest_sh "set -e; git -C $src checkout -q $base" >/dev/null \
        || die "could not check out the baseline '$base' in the guest"
    A_ID=$(build_and_stage "baseline ($base)")

    if [ -f "$PATCH" ]; then
        log "  applying diff $PATCH"
        mac "cat > /tmp/wk-ab.patch" < "$PATCH" || die "could not copy the patch to $HOST"
        guest_sh "set -e; cd $src && git apply --index /tmp/wk-ab.patch" >/dev/null 2>&1 \
            || { guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1
                 die "the patch did not apply cleanly to '$base'; the tree has been put back"; }
    else
        log "  checking out patched ref $PATCH"
        guest_sh "set -e; git -C $src checkout -q $PATCH" >/dev/null \
            || { guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1
                 die "no such ref '$PATCH' in the guest checkout; the tree has been put back"; }
    fi
    B_ID=$(build_and_stage "patched ($PATCH)")

    guest_sh "git -C $src checkout -q $orig" >/dev/null 2>&1 \
        || warn "  could not restore '$orig' in the guest -- the tree is left on the patched ref"

    info "  stopping the build guest"
    rwk vm stop "$WS" >/dev/null 2>&1 || warn "  could not stop '$WS'"

    info "arms: A=$A_ID  B=$B_ID"
}

phase_plant() {
    local root bh stamp
    root=$(bench_root); bh=$(bench_home)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)

    # Defaulting B to A is the A/A control; skipped for a --patch dry run's placeholder arms.
    if [ -z "$ARMS_PENDING" ]; then
        local staged
        staged=$(mac "ls -1 $(sh_quote "$root/staged") 2>/dev/null" 2>/dev/null | tr -d '\r' | sort)
        [ -n "$staged" ] || die "nothing staged on $VOLUME -- pass a workspace with --stage, or --patch to build both arms"
        [ -n "$A_ID" ] || A_ID=$(printf '%s' "$staged" | tail -1)
        [ -n "$B_ID" ] || B_ID="$A_ID"
        printf '%s\n' "$staged" | grep -qx "$A_ID" || die "no staged build '$A_ID' on $VOLUME. There is:
$(printf '%s' "$staged" | sed 's/^/    /')"
        printf '%s\n' "$staged" | grep -qx "$B_ID" || die "no staged build '$B_ID' on $VOLUME"
    fi

    info "plant: $PLAN, $ROUNDS round(s), interleaved"
    log  "  arm A: $A_ID${A_ARGS:+  args: $A_ARGS}"
    log  "  arm B: $B_ID${B_ARGS:+  args: $B_ARGS}"
    [ "$A_ID" = "$B_ID" ] && [ "$A_ARGS" = "$B_ARGS" ] && \
        warn "  both arms are the same build with the same arguments: this is an A/A
  control. It measures this lane's noise floor, which is the thing you need
  before any real A/B means anything -- but it is not a comparison of two builds."

    if [ -n "$DRY" ]; then
        log "  would sync wk-tools to $root/wk-tools"
        log "  would install $root/bin/mac-bench-autorun.sh"
        log "  would write $root/job.json and reset $root/autorun.state"
        log "  would install $bh/Library/LaunchAgents/com.wk.bench-ab.plist"
        return 0
    fi

    # /var/wk, not ~bench/Development/wk-tools: the first-boot daemon `rsync --delete`s over that directory on every boot it runs, replacing a planted tree with an older one.
    info "  syncing wk-tools onto the volume"
    put_tree "$WK_ROOT" "$root/wk-tools" \
        || die "could not sync wk-tools onto the bench volume"

    if ! mac "test -d $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages/scipy")" 2>/dev/null; then
        info "  installing scipy into the bench account's site-packages"
        mac "/usr/bin/python3 -m pip install --quiet --target $(sh_quote "$bh/Library/Python/3.9/lib/python/site-packages") scipy" \
            >/dev/null 2>&1 || warn "  scipy did not install; the A/B will be compared from host mode instead"
    fi

    # A lock mid-run is the same "nowhere to draw" failure as a stolen focus, and invisible to `screen_blocker`, which asks only for the frontmost application. Per-user preferences on a uid-501 home, so they are written here by absolute path with no cfprefsd owning them, then read back.
    local sspath="$bh/Library/Preferences/ByHost/com.apple.screensaver.$(mac_hw_uuid)"
    mac "mkdir -p $(sh_quote "$bh/Library/Preferences/ByHost")"
    mac "defaults write $(sh_quote "$sspath") idleTime -int 0" >/dev/null 2>&1 || true
    mac "defaults write $(sh_quote "$bh/Library/Preferences/com.apple.screensaver") askForPassword -int 0" >/dev/null 2>&1 || true
    local ssv
    ssv=$(mac "defaults read $(sh_quote "$sspath") idleTime 2>/dev/null" 2>/dev/null | tr -d '\r ')
    if [ "$ssv" = 0 ]; then
        log "  screen lock: screensaver disabled on the volume (idleTime=0, verified)"
    elif [ -n "$FORCE" ]; then
        warn "  screen lock: could not disable the screensaver (idleTime reads '${ssv:-unreadable}').
  --force given, so planting anyway: the screen may lock during a run."
    else
        die "could not disable the screensaver on $VOLUME -- idleTime reads '${ssv:-unreadable}'.

    Refused here rather than discovered later. A benchmark makes no keyboard or
    mouse input, so the idle timer runs at full load exactly as it does on an
    abandoned machine; the lock behind it ends a run with the silent timeout that
    nothing over there can report.

    Nothing has been done to the machine yet. To plant anyway:  --force"
    fi

    info "  installing the autorun"
    mac "mkdir -p $(sh_quote "$root/bin") $(sh_quote "$bh/Library/LaunchAgents")"
    put_file "$WK_ROOT/bench/mac-bench-autorun.sh" "$root/bin/mac-bench-autorun.sh" \
        || die "could not install the autorun script"
    mac "chmod 0755 $(sh_quote "$root/bin/mac-bench-autorun.sh")"

    info "  writing the job"
    # Written through python so quoting is not a shell problem: staged ids and browser arguments reach this from a command line and can contain spaces.
    WK_JOB_PLAN="$PLAN" WK_JOB_ROUNDS="$ROUNDS" WK_JOB_TIMEOUT="$TIMEOUT" \
    WK_JOB_COUNT="$COUNT" WK_JOB_SETTLE="$SETTLE" \
    WK_JOB_A="$A_ID" WK_JOB_B="$B_ID" WK_JOB_AA="$A_ARGS" WK_JOB_BA="$B_ARGS" \
    WK_JOB_TOOLS="/var/wk/wk-tools" WK_JOB_BY="$(hostname)" \
    WK_JOB_STAMP="$stamp" WK_JOB_FORCE="$FORCE" \
    WK_JOB_ASLR="${WK_BENCH_ASLR:-}" WK_JOB_ENVPAD="${WK_BENCH_ENV_PAD:-}" \
    WK_JOB_PATHPAD="${WK_BENCH_PATH_PAD:-}" WK_JOB_SHARED="${WK_BENCH_SHARED_CACHE:-}" \
    python3 - <<'PYEOF' > "$(wk_state_dir)/mac-ab-job.json"
import json, os
g = os.environ.get
arms = [{"label": "A", "id": g("WK_JOB_A"), "browser_args": g("WK_JOB_AA") or ""}]
if g("WK_JOB_B"):
    arms.append({"label": "B", "id": g("WK_JOB_B"), "browser_args": g("WK_JOB_BA") or ""})
print(json.dumps({
    "plan": g("WK_JOB_PLAN"),
    "rounds": int(g("WK_JOB_ROUNDS")),
    "timeout": int(g("WK_JOB_TIMEOUT")),
    "count": g("WK_JOB_COUNT") or "",
    "settle": int(g("WK_JOB_SETTLE")),
    "n_arms": len(arms),
    "arms": arms,
    "wk_tools": g("WK_JOB_TOOLS"),
    "created_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "created_by": g("WK_JOB_BY"),
    "stamp": g("WK_JOB_STAMP"),
    "force": bool(g("WK_JOB_FORCE")),
    "aslr": g("WK_JOB_ASLR") or "",
    "env_pad": g("WK_JOB_ENVPAD") or "",
    "path_pad": g("WK_JOB_PATHPAD") or "",
    "shared_cache": g("WK_JOB_SHARED") or "",
}, indent=2))
PYEOF
    put_file "$(wk_state_dir)/mac-ab-job.json" "$root/job.json" \
        || die "could not write the job onto the volume"

    # Reset here and nowhere else: the autorun only advances this state, so a fresh job needs a fresh one or the run is skipped as already done.
    mac "printf 'phase=planted\njob_stamp=%s\nattempts=0\nplanted_at=%s\n' \
        $(sh_quote "$stamp") $(sh_quote "$(date -u +%Y-%m-%dT%H:%M:%SZ)") > $(sh_quote "$root/autorun.state")"
    mac "mkdir -p $(sh_quote "$root/ab/$stamp")"

    info "  installing the launch agent"
    # RunAtLoad and nothing else: KeepAlive would restart a finished benchmark. The agent removes itself once its job is done.
    mac "cat > $(sh_quote "$bh/Library/LaunchAgents/com.wk.bench-ab.plist")" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.wk.bench-ab</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/var/wk/bin/mac-bench-autorun.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>/var/wk/autorun.agent.log</string>
  <key>StandardErrorPath</key><string>/var/wk/autorun.agent.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>WK_AB_ROOT</key><string>/var/wk</string></dict>
</dict>
</plist>
PLIST
    mac "chmod 0644 $(sh_quote "$bh/Library/LaunchAgents/com.wk.bench-ab.plist")"

    info "planted: $stamp"
    log  "  job    $root/job.json"
    log  "  agent  $bh/Library/LaunchAgents/com.wk.bench-ab.plist"
    log  "  log    $root/autorun.log   (readable from host mode afterwards)"
    printf '%s' "$stamp"
}

BOOT_BEFORE=""

mac_boottime() {
    mac_sh 'sysctl -n kern.boottime 2>/dev/null' 2>/dev/null \
        | tr -d '\r' | sed -n 's/.*sec *= *\([0-9]*\).*/\1/p' | head -1
}

phase_go() {
    [ -n "$DRY" ] && { log "  would reboot $HOST (loginwindow restart event, no sudo)"; return 0; }
    BOOT_BEFORE=$(mac_boottime)
    info "go: rebooting $HOST (boot before: ${BOOT_BEFORE:-unknown})"

    # `tell application "System Events" to restart` returns 0 without rebooting: at the login screen there is no user session to run it in. The loginwindow event is what the login screen's own Restart button uses, and it needs none.
    # The event code is guillemets, so the octal escapes go in printf's *format*, not a %s argument. Backgrounded and its status ignored, because a successful reboot kills the ssh carrying it -- `kern.boottime` below is what verifies it.
    mac_sh 'printf "tell application \"loginwindow\" to \302\253event aevtrrst\302\273\n" > /tmp/wk-restart.scpt
            (osascript /tmp/wk-restart.scpt >/dev/null 2>&1 &)
            exit 0' >/dev/null 2>&1 || true

    local waited=0
    while [ "$waited" -lt 150 ]; do
        mac_sh true >/dev/null 2>&1 || { info "  $HOST is going down (after ${waited}s)"; return 0; }
        sleep 5
        waited=$((waited + 5))
    done

    warn "  the loginwindow restart event did not take; trying sudo -n"
    mac_sh 'sudo -n shutdown -r now >/dev/null 2>&1' >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 60 ]; do
        mac_sh true >/dev/null 2>&1 || { info "  $HOST is going down (after sudo)"; return 0; }
        sleep 5
        waited=$((waited + 5))
    done

    die "could not reboot $HOST -- it is still answering, and \`kern.boottime\` is
    unchanged. Both mechanisms exit 0 without acting, so this is checked rather
    than trusted; see the comment in phase_go.

    Nothing has been lost: the job is planted, so rebooting the machine by hand
    -- or booting '$VOLUME' from the startup manager -- runs the A/B and needs
    nothing further from here."
}

phase_wait() {
    local limit="$1" start now mode last=""
    [ -n "$DRY" ] && { log "  would wait up to ${limit}s for $HOST to answer again"; printf 'dry'; return 0; }
    info "wait: up to $((limit / 60)) minutes for $HOST to answer"
    start=$(date +%s)
    sleep 45   # for the first seconds the machine is still up, and an immediate poll would report host mode too soon
    while :; do
        if mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"; echo READY' 2>/dev/null); then
            mode=$(printf '%s' "$mode" | tr -d '\r' | head -1)
            local bt; bt=$(mac_boottime)   # the same boot as before means it never rebooted
            if [ -n "$BOOT_BEFORE" ] && [ "$bt" = "$BOOT_BEFORE" ]; then
                warn "  $HOST is answering on the SAME boot ($bt) -- it never rebooted"
                printf 'noreboot'; return 1
            fi
            case "$mode" in
                READY) info "  $HOST is back in HOST mode"; printf 'host'; return 0 ;;
                *)     info "  $HOST answers in BENCH mode ($mode)"; printf 'bench'; return 0 ;;
            esac
        fi
        now=$(date +%s)
        if [ $((now - start)) -ge "$limit" ]; then
            warn "  $HOST has not answered in ${limit}s"
            printf 'silent'; return 1
        fi
        [ "$last" != waiting ] && { log "  no answer yet (this is the reboot, or bench mode, which has no network)"; last=waiting; }
        sleep 20
    done
}


phase_collect() {
    local root; root=$(bench_root)
    info "collect: reading the A/B off $VOLUME"

    local st; st=$(mac "cat $(sh_quote "$root/autorun.state") 2>/dev/null" 2>/dev/null | tr -d '\r')
    if [ -n "$st" ]; then
        log "  autorun state:"
        printf '%s\n' "$st" | sed 's/^/    /' >&2
    else
        warn "  no autorun state on the volume -- the agent never ran"
    fi

    local stamp; stamp=$(kv_get job_stamp <<<"$st")
    local runs="$root/ab/$stamp/runs.tsv"
    if [ -n "$stamp" ] && mac "test -f $(sh_quote "$runs")" 2>/dev/null; then
        log ""
        log "  runs:"
        mac "cat $(sh_quote "$runs")" 2>/dev/null | sed 's/^/    /' >&2
        log ""
        bwk bench ab-summary --root "$(sh_quote "$root")" --runs "$(sh_quote "$runs")" || \
            warn "  the summary could not be produced; the results are still on the volume"
    else
        warn "  no run map at $runs -- no arm completed"
        log  "  the autorun's own log is the place to look:"
        log  "    ssh $HOST tail -60 $(sh_quote "$root/autorun.log")"
    fi
}

phase_status() {
    local root; root=$(bench_root 2>/dev/null) || die "'$VOLUME' is not attached on $HOST"
    local mode; mode=$(mac 'cat /etc/wk-image 2>/dev/null | sed -n "s/^id=//p"' 2>/dev/null | tr -d '\r')
    if [ -n "$mode" ]; then info "$HOST is in bench mode ($mode)"
    else                   info "$HOST is in host mode"; fi
    mac "cat $(sh_quote "$root/job.json") 2>/dev/null" 2>/dev/null | sed 's/^/  /' >&2 \
        || log "  no job planted"
    log ""
    mac "cat $(sh_quote "$root/autorun.state") 2>/dev/null" 2>/dev/null | sed 's/^/  /' >&2 \
        || log "  no autorun state"
    log ""
    log "  last 20 lines of the autorun log:"
    mac "tail -20 $(sh_quote "$root/autorun.log") 2>/dev/null" 2>/dev/null | sed 's/^/    /' >&2 \
        || log "    (none)"
}


while [ $# -gt 0 ]; do
    case "$1" in
        --plan)     PLAN="${2:-}"; shift 2 ;;
        --config)   CONFIG="${2:-}"; shift 2 ;;
        --rounds)   ROUNDS="${2:-}"; shift 2 ;;
        # The first Speedometer iteration is never trimmed from a result: it is a real iteration, and dropping it would bias the comparison.
        --count)    COUNT="${2:-}"; shift 2 ;;
        --timeout)  TIMEOUT="${2:-}"; shift 2 ;;
        --settle)   SETTLE="${2:-}"; shift 2 ;;
        --patch)    PATCH="${2:-}"; shift 2 ;;
        --base)     BASE_REF="${2:-}"; shift 2 ;;
        --a)        A_ID="${2:-}"; shift 2 ;;
        --b)        B_ID="${2:-}"; shift 2 ;;
        --a-args)   A_ARGS="${2:-}"; shift 2 ;;
        --b-args)   B_ARGS="${2:-}"; shift 2 ;;
        --host)     HOST="${2:-}"; shift 2 ;;
        --tools)    TOOLS="${2:-}"; shift 2 ;;
        --machine)  MACHINE="${2:-}"; shift 2 ;;
        --stage)    DO_STAGE=1; shift ;;
        --allow-network-fetch) ALLOW_FETCH=1; shift ;;
        --force)    FORCE=1; shift ;;
        --agent-home) AGENT_HOME="${2:-}"; shift 2 ;;
        --preflight) ACTION=preflight; shift ;;
        --status)   ACTION=status; shift ;;
        --collect)  ACTION=collect; shift ;;
        --plant)    ACTION=plant; shift ;;
        --dry-run)  DRY=1; shift ;;
        -h|--help)  usage ;;
        -*)         die "unknown option: $1" ;;
        *)          [ -z "$WS" ] || die "one workspace at a time (got '$WS' and '$1')"
                    WS="$1"; shift ;;
    esac
done

# Deferred until MACHINE is final, so `--machine benchvm` picks up benchvm's own conf.
if [ -z "$HOST" ]; then
    machine_load "$MACHINE" >/dev/null 2>&1 || die "no such machine: $MACHINE (wk boot --list)"
    HOST="${NODE_SSH:-}"
    [ -n "$HOST" ] || die "$MACHINE (boot/machines/$MACHINE.conf) sets no NODE_SSH"
fi

_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
if is_macos && [ "$(_lc "$(hostname -s 2>/dev/null)")" = "$(_lc "$HOST")" ]; then
    die "this lane reboots $HOST, so it cannot be driven from $HOST -- the reboot
  would take the driver with it. Run it from another machine (rpi5, moose)."
fi

case "$ACTION" in
    preflight) preflight; exit $? ;;
    status)    phase_status; exit 0 ;;
    collect)   phase_collect; exit 0 ;;
esac

if ! preflight; then
    [ -n "$DRY" ] || die "preflight failed -- nothing on $HOST has been changed"
    warn "preflight failed; showing the plan anyway because this is --dry-run"
fi

# --patch stages both arms itself, before the plant, which validates A_ID and B_ID against the volume.
if [ -n "$PATCH" ]; then
    [ -z "$DO_STAGE" ] || warn "--stage is redundant with --patch (which stages both arms)"
    [ -z "$A_ID$B_ID" ] || die "--patch chooses both arms; do not also pass --a/--b"
    phase_build_ab
elif [ -n "$DO_STAGE" ]; then
    phase_stage
fi
phase_plant >/dev/null

[ "$ACTION" = plant ] && {
    info "planted and not started. The A/B runs the next time '$VOLUME' boots --"
    log  "  by itself if it is the firmware default, or from the startup manager."
    exit 0
}

phase_go

came_back=$(phase_wait "$BOOT_WAIT") || true
log ""
case "$came_back" in
    dry)
        info "dry run -- nothing on $HOST was changed and nothing was rebooted" ;;
    bench)
        info "$HOST came back in BENCH mode and is reachable -- the A/B is running there."
        log  "  'wk bench mac-ab --status' follows it." ;;
    host)
        phase_collect ;;   # the state file tells "ran it and came back" from "never left" apart
    noreboot)
        warn "$HOST never rebooted, so the A/B has not run."
        log  "  The job is planted and still valid -- nothing needs re-staging."
        log  "  Reboot the machine by any means (the startup manager works too)"
        log  "  and it runs by itself; 'wk bench mac-ab --collect' reads it after." ;;
    *)
        warn "$HOST is not answering."
        log  "  If it went to bench mode, that is expected: that install has no"
        log  "  network. The job carries a watchdog and its own hand-back, so it"
        log  "  returns on its own; 'wk bench mac-ab --collect' reads the result"
        log  "  once it does." ;;
esac
