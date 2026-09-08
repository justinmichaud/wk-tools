#!/usr/bin/env bash
# wk bench mac-ab -- an interleaved A/B on this Mac's benchmark install, with nobody in the room.
#
#   wk bench mac-ab [<workspace>] [--plan P]... [--rounds N] [--count N]
#                   [--a <staged-id>] [--b <staged-id>] [--stage]
#   wk bench mac-ab <workspace> --patch <ref|diff> [--base <ref>] [--rounds N]
#   wk bench mac-ab ... --shutdown        shut down instead of rebooting, so the
#                                         startup manager is picked from a cold machine
#   wk bench mac-ab --progress            every step, what does it, what proves it
#   wk bench mac-ab --preflight | --status | --collect | --dry-run
#   --plan P repeats; the default is jetstream3, speedometer3, motionmark. --detect
#   PCT alternates until the A/B resolves a difference that small (0.3 by default)
#   and stops there, between --rounds and --max-rounds; --detect 0 runs --rounds
#   exactly. Round 0 is a discarded warmup leg per arm, carrying a samply profile.
#   --count N iterations of the benchmark per leg, 2 by default. One iteration
#   per leg leaves that leg with no within-leg variance, so no p-value can be
#   computed for it; each further iteration costs its own run time and buys
#   fewer rounds to reach --detect.
# The benchmark install has no network (tolken is Wi-Fi only and that install joins nothing), so the job is planted rather than driven: everything it needs is written onto the volume while merely mounted, a per-user LaunchAgent starts it at autologin, and this driver waits and reads. No sudo, because /var/wk and ~bench are both uid 501.
# Nothing here can set which volume the firmware boots -- `nvram boot-volume`, `bless --setBoot` and `systemsetup -getstartupdisk` all fail silently -- so this driver reboots and reports which mode came back: at most one human action per A/B, never one per run, since the planted job holds every round of every arm.

set -euo pipefail
WK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/profiler.sh"
. "$WK_ROOT/lib/bench.sh"
. "$WK_ROOT/boot/machines.sh"

HOST="${WK_MAC_SSH:-}"
MACHINE="${WK_MAC_MACHINE:-mbp}"  # static
VOLUME="${WK_BENCH_VOLUME:-WK Bench}"
PLANS="${WK_MAC_PLANS:-jetstream3 speedometer3 motionmark}"
CONFIG="${WK_MAC_CONFIG:-mac-release-pgo}"
ROUNDS=5
MAX_ROUNDS=40
DETECT=0.3
COUNT=2
TIMEOUT=1800
SETTLE=90
A_ID=""; B_ID=""
PATCH=""; BASE_REF=""; ARMS_PENDING=""
A_ARGS=""; B_ARGS=""
WS=""
PLANS_GIVEN=""
DO_STAGE=""
ALLOW_FETCH=""
FORCE="${WK_FORCE:-}"
AGENT_HOME=""
DRY=""
ACTION=run
GO=restart   # --shutdown: the startup manager wants a cold machine, and then nothing has to be timed
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
# The one path boot/machines.sh declares, never a search: tolken carries two clones, and
# whichever a search reached first would be the tree driving the lane.
host_tools() {
    [ -n "$TOOLS" ] && { printf '%s' "$TOOLS"; return 0; }
    TOOLS=$(machine_tools_dir)
    machine_tools_present "$HOST" || die "no wk-tools at $HOST:$TOOLS, so nothing over there
    can run a leg. One command puts it there, with the privileged helpers:
      wk boot $MACHINE --prepare"
    printf '%s' "$TOOLS"
}
rwk() { mac_sh "cd $(sh_quote "$(host_tools)") && ./wk $*"; }

bwk() { mac_sh "cd $(sh_quote "$(bench_root)/wk-tools") && ./wk $*"; }   # the planted copy, the same age as the job

# wkmac.py travels on stdin rather than being read out of the Mac's own checkout: one implementation of every disk, firmware and display fact, and no tree over there to keep in step with this one.
mac_wkmac() {  # <subcommand> [args...]
    local a q=""
    for a in "$@"; do q="$q $(sh_quote "$a")"; done
    mac "python3 -$q" < "$WK_ROOT/lib/wkmac.py" 2>/dev/null | tr -d '\r'
}

# The firmware's own default has to BE the bench volume: that is what lets this lane restart the machine and have benchmarking begin with nobody at the keyboard.
FW_DETAIL=""
firmware_default_is_bench() {
    local bv grp bench_grp host_grp
    bv=$(mac_wkmac boot-volume)
    grp="${bv##*:}"
    bench_grp=$(mac_wkmac volume-group "/Volumes/$VOLUME")
    host_grp=$(mac_wkmac volume-group /)
    if [ -z "$grp" ]; then
        FW_DETAIL="the firmware publishes no boot-volume, so what a restart enters cannot be read"
        return 1
    fi
    if [ -n "$bench_grp" ] && [ "$grp" = "$bench_grp" ]; then
        FW_DETAIL="$grp = '$VOLUME', so the restart below needs no human"
        return 0
    fi
    if [ -n "$host_grp" ] && [ "$grp" = "$host_grp" ]; then
        FW_DETAIL="$grp = the host install, so a restart comes back here and the A/B never runs"
    else
        FW_DETAIL="$grp matches neither install on this disk"
    fi
    return 1
}

# One ONLINE display and it the built-in panel. An external monitor changes the compositing, the refresh rate and which GPU the window lands on, and MotionMark's score is a function of the area it draws.
DISPLAY_READ=""
mac_display_check() {
    local out st
    DISPLAY_READ=""
    out=$(mac_wkmac displays)
    if [ -z "$out" ]; then
        DISPLAY_READ="'wkmac.py displays' answered nothing on $HOST -- CoreGraphics could not be asked"
        return 1
    fi
    st=$(printf '%s' "$out" | python3 -c '
import json, sys
def one(d):
    p = d.get("points") or []
    w = p[0] if len(p) > 0 else "?"
    h = p[1] if len(p) > 1 else "?"
    return "%s %sx%s" % ("builtin" if d.get("builtin") else "external", w, h)
try:
    doc = json.load(sys.stdin)
except ValueError:
    print("ok=no"); print("detail=wkmac.py displays did not print JSON"); raise SystemExit(0)
on = [d for d in (doc.get("displays") or []) if d.get("online")]
shown = ", ".join(one(d) for d in on) or "none"
if len(on) != 1:
    print("ok=no"); print("detail=%d online display(s): %s" % (len(on), shown))
elif not on[0].get("builtin"):
    print("ok=no"); print("detail=the one online display is not the built-in panel (%s)" % shown)
else:
    print("ok=yes"); print("detail=%s alone, as the install that answers here reads it" % shown)
')
    DISPLAY_READ=$(kv_get detail <<<"$st")
    [ "$(kv_get ok <<<"$st")" = yes ]
}

# `wk notify` publishes off this machine; a notification that did not go out must never cost a measurement, so every failure here is a warning and nothing else.
notify() {  # <headline> <detail>
    "$WK_ROOT/wk" notify "$1" --detail "$2" --tag mac-ab >/dev/null \
        || warn "  could not send the notification '$1' (above)"
}

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

# The volume's first boot is what quiets the desktop, and it logs the completion line last -- after which it deletes itself, so the log is the only record that it ever finished.
firstboot_log() {
    printf '%s/log/wk-bench-firstboot.log' "$(dirname "$(bench_root)")"
}

volume_provisioned() {
    local n
    n=$(mac "grep -c 'provisioning complete' $(sh_quote "$(firstboot_log)")" 2>/dev/null | tr -d ' \r') || n=0
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    [ "$n" -gt 0 ]
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

    # Everything below is something the volume's first boot creates, and the desktop quieting it applies is what `wk bench staged` requires of every leg -- so an unprovisioned volume fails them all after the reboot, where nothing can report it.
    if volume_provisioned; then
        ck yes "provisioned" "'$VOLUME' has finished a first boot"
    else
        ck no "provisioned" "no 'provisioning complete' in $(firstboot_log)"
        log "       so the desktop was never quieted, and every leg is refused after" >&2
        log "       the reboot as 'not a measured Mac's'. On the Mac:" >&2
        log "         wk bench mac-volume --repair    then boot '$VOLUME' once" >&2
    fi

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
        log "  note the first-boot daemon is still installed on this volume. Once" >&2
        log "       provisioning has completed the autorun removes it and cancels the" >&2
        log "       reboot it schedules; until then the autorun stands aside and lets" >&2
        log "       it finish, so that boot provisions rather than measures." >&2
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

    if mac_display_check; then
        ck yes "one display" "$DISPLAY_READ"
    else
        ck no "one display" "$DISPLAY_READ"
        log "       Disconnect it. An external monitor changes the compositing, the" >&2
        log "       refresh rate and which GPU the window lands on, and MotionMark's" >&2
        log "       score is the area it draws. Refused rather than warned, and no" >&2
        log "       --force crosses it: there is no number to save." >&2
    fi

    if firmware_default_is_bench; then
        ck yes "firmware default" "$FW_DETAIL"
    else
        ck no "firmware default" "$FW_DETAIL"
        log "       This lane restarts $HOST and expects benchmarking to begin with" >&2
        log "       nobody at the keyboard, which only the firmware default gives." >&2
        log "         wk boot $MACHINE            arms it (on $HOST)" >&2
        log "       Where the boot helper is not installed, the startup manager is the" >&2
        log "       way: shut down, hold the power button until 'Loading startup" >&2
        log "       options', pick '$VOLUME'. 'wk bench mac-ab --shutdown' leaves the" >&2
        log "       machine off with the job planted for exactly that." >&2
    fi

    if ! mv_reboot_ready && [ -t 0 ] && [ -z "$DRY" ]; then
        info "  $HOST cannot be restarted unattended yet -- preparing it now"
        machine_prepare "$HOST" || true
    fi
    if mv_reboot_ready; then
        ck yes "restartable" "the boot helper answers sudo -n, so this lane restarts $HOST itself"
    else
        ck no "restartable" "no boot helper on $HOST, and plain sudo there wants a password"
        log "       A graceful restart is refusable -- any application that will not quit" >&2
        log "       declines it -- so an unattended lane cannot use one. One command puts" >&2
        log "       this tree and the helpers on that Mac, and asks for a password once:" >&2
        log "         wk boot $MACHINE --prepare" >&2
        log "       It runs by itself from a terminal; this session had none, or it would" >&2
        log "       have been done above. Crossing this leaves the job planted and correct:" >&2
        log "       reboot $HOST by any means and the A/B runs by itself." >&2
    fi

    log "" >&2
    [ "$PF_FAIL" -eq 0 ] || { warn "$PF_FAIL preflight check(s) failed"; return 1; }
    info "preflight clean"
    return 0
}


# In a variable and not on stdout: a die in a command substitution kills only the subshell, and this one refuses the whole stage.
stage_plan_args() {   # -> STAGE_PLAN_ARGS, the --plan/--payload arguments for `wk bench stage`. A benchmark install has no network, so an unpinned plan is a leg that fails after the reboot where nothing can report it.
    local p payload out=""
    STAGE_PLAN_ARGS=""
    for p in $PLANS; do
        payload=$(rwk bench seed "$WS" "$p" | tr -d '\r' | tail -1) || payload=""
        case "$payload" in /*) ;; *) payload="" ;; esac
        if [ -n "$payload" ] && mac "test -d $(sh_quote "$payload")" 2>/dev/null; then
            log "  payload pinned: $p -> $payload"
            out="$out --plan $p --payload $payload"
        elif [ -n "$ALLOW_FETCH" ]; then
            warn "  $p is not pinned, and --allow-network-fetch was given. That leg will
  clone the benchmark itself, so the benchmark install needs a working network *and*
  the two arms could in principle get different revisions of it."
            out="$out --plan $p"
        else
            die "the $p payload could not be pinned, so nothing may be staged.

    That leg would clone the benchmark itself, over a network the benchmark
    install may not have -- and if it does not, every $p arm fails after the
    reboot, where nothing can say so.

    Pin it here, where the network is:
        wk bench seed $WS $p
    then re-run. If that fails, its error is the thing to fix -- it reads the
    plan file out of '$WS', so the workspace has to be one that has the tree.

    To go ahead anyway (a benchmark install you know has a route out, and a
    revision of the benchmark you are content to have chosen for you):
        --allow-network-fetch"
        fi
    done
    STAGE_PLAN_ARGS="$out"
}

phase_stage() {
    [ -n "$WS" ] || die "--stage needs a workspace to stage from"
    info "stage: $CONFIG from '$WS' onto $VOLUME ($PLANS)"
    [ -n "$DRY" ] && { log "  would: wk vm start $WS; wk bench seed $WS <each plan>; wk bench stage $WS --to $MACHINE"; return 0; }
    rwk vm start "$WS" >/dev/null 2>&1 || true
    stage_plan_args
    # shellcheck disable=SC2086 -- a deliberate word list.
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" $STAGE_PLAN_ARGS

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
    stage_plan_args
    info "  staging $label"
    # shellcheck disable=SC2086 -- a deliberate word list.
    rwk bench stage "$WS" --to "$MACHINE" --config "$CONFIG" $STAGE_PLAN_ARGS >&2 \
        || die "staging $label failed"
    after=$(staged_ids)
    id=$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | tail -1)
    [ -n "$id" ] || die "staging $label produced no new directory on $VOLUME"
    log "  $label staged as $id" >&2
    reclaim_products "$label"
    printf '%s' "$id"
}

# Measured 2026-09-06: a profile-guided arm leaves 101 GB of products in the guest and the next arm wants the same room. Once an arm is staged both are spent -- the instrumented tree existed to be profiled, the measured one to be staged.
reclaim_products() {   # <label>
    local src dirs measured instr out
    src=$(guest_src)
    dirs=$( . "$WK_ROOT/lib/arch.sh" >/dev/null 2>&1
            . "$WK_ROOT/build/configs.sh" >/dev/null 2>&1
            . "$WK_ROOT/build/mac-pgo.sh" >/dev/null 2>&1
            WK_TARGET_KIND=vm   # this lane builds its arms in a macOS guest and nowhere else
            config_load "$CONFIG" macos vm >/dev/null 2>&1
            d=$(config_build_dir "$src")
            printf '%s\n%s\n' "$d" "$d$PGO_INSTR_SUFFIX" )
    measured=$(printf '%s' "$dirs" | sed -n 1p)
    instr=$(printf '%s' "$dirs" | sed -n 2p)
    [ -n "$measured" ] && [ -n "$instr" ] || { warn "  could not name $CONFIG's products; nothing reclaimed"; return 0; }
    out=$(guest_sh "du -sk $(sh_quote "$measured") $(sh_quote "$instr") 2>/dev/null | awk '{s+=\$1} END {print int(s/1048576)}'
rm -rf $(sh_quote "$measured") $(sh_quote "$instr")
df -g / | awk 'NR==2 {print \$4}'" 2>/dev/null | tr -d '\r')
    log "  reclaimed $label's products ($(printf '%s' "$out" | sed -n 1p) GB); $(printf '%s' "$out" | sed -n 2p) GB free in the guest now" >&2
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
    # An unpinned display means two runs at different resolutions compare as if they matched, so it is declared per machine and refused here rather than discovered in the numbers.
    case "${NODE_DISPLAY:-}" in
        "") die "boot/machines/$MACHINE.conf declares no NODE_DISPLAY, so nothing here knows
    what display the measured install must read.

    Add the bench install's own mode, in points:
        NODE_DISPLAY=\"builtin 1470x956\"
    ('python3 lib/wkmac.py displays' on that install prints it.)" ;;
    esac

    local root bh stamp task task_dir cmd p
    root=$(bench_root); bh=$(bench_home)
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    task="$stamp-$MACHINE-mac-ab"
    task_dir=$(bench_task_dir "$task")

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

    # `--detect 0` makes --rounds the whole plan, not the floor under one; the autorun reads the same 0 the same way.
    if awk -v d="${DETECT:-0}" 'BEGIN { exit !(d + 0 == 0) }'; then
        info "plant: $PLANS, exactly $ROUNDS round(s), interleaved; no precision target, so what these resolve is what 'wk bench precision' says of them"
    else
        info "plant: $PLANS, $ROUNDS-$MAX_ROUNDS round(s), interleaved, until it resolves ${DETECT}%"
    fi
    log  "  arm A: $A_ID${A_ARGS:+  args: $A_ARGS}"
    log  "  arm B: $B_ID${B_ARGS:+  args: $B_ARGS}"
    [ "$A_ID" = "$B_ID" ] && [ "$A_ARGS" = "$B_ARGS" ] && \
        warn "  both arms are the same build with the same arguments: this is an A/A
  control. It measures this lane's noise floor, which is the thing you need
  before any real A/B means anything -- but it is not a comparison of two builds."

    if [ -n "$DRY" ]; then
        log "  would sync wk-tools to $root/wk-tools"
        log "  would install $root/bin/mac-bench-autorun.sh"
        log "  would plant samply $SAMPLY_VER for the warmup round's profile"
        log "  would record the task $task in $BENCH_DIR and write its job.json"
        log "  would copy that job to $root/job.json and reset $root/autorun.state"
        log "  would install $bh/Library/LaunchAgents/com.wk.bench-ab.plist"
        return 0
    fi

    # Recorded before the Mac is touched, and in the store `wk status` already lists, so a run killed halfway is still a task that names what was asked for.
    cmd="wk bench mac-ab --a $A_ID --b $B_ID --rounds $ROUNDS --max-rounds $MAX_ROUNDS --detect $DETECT --count $COUNT"
    for p in $PLANS; do cmd="$cmd --plan $p"; done
    bench_task_new "$task" devices="$MACHINE=$CONFIG" \
        plans="$(printf '%s' "$PLANS" | tr ' ' ',')" rounds="$ROUNDS" \
        slots="$A_ID,$B_ID" --command "$cmd"

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

    # No network over there, so the warmup round's profiler goes in now, into that install's store (bench/mac-bench-autorun.sh) where samply_fetch will look.
    local samply triple
    triple=$(samply_triple "$(mac_sh 'uname -m' | tr -d '\r')" Darwin)
    if samply=$(samply_fetch "$(mac_sh 'uname -m' | tr -d '\r')" Darwin 2>/dev/null) && [ -n "$samply" ]; then
        mac "mkdir -p $(sh_quote "$root/cache/samply/$SAMPLY_VER-$triple")"
        if put_file "$samply" "$root/cache/samply/$SAMPLY_VER-$triple/samply"; then
            mac "chmod 0755 $(sh_quote "$root/cache/samply/$SAMPLY_VER-$triple/samply")"
            log "  samply $SAMPLY_VER planted for the warmup round"
        else
            warn "  could not plant samply -- the warmup round will carry no profile"
        fi
    else
        warn "  no samply for $triple here -- the warmup round will carry no profile"
    fi

    info "  installing the autorun"
    mac "mkdir -p $(sh_quote "$root/bin") $(sh_quote "$bh/Library/LaunchAgents")"
    put_file "$WK_ROOT/bench/mac-bench-autorun.sh" "$root/bin/mac-bench-autorun.sh" \
        || die "could not install the autorun script"
    mac "chmod 0755 $(sh_quote "$root/bin/mac-bench-autorun.sh")"

    info "  writing the job"
    # Written through python so quoting is not a shell problem: staged ids and browser arguments reach this from a command line and can contain spaces.
    WK_JOB_PLANS="$PLANS" WK_JOB_ROUNDS="$ROUNDS" WK_JOB_TIMEOUT="$TIMEOUT" \
    WK_JOB_MAX_ROUNDS="$MAX_ROUNDS" WK_JOB_DETECT="$DETECT" \
    WK_JOB_COUNT="$COUNT" WK_JOB_SETTLE="$SETTLE" \
    WK_JOB_A="$A_ID" WK_JOB_B="$B_ID" WK_JOB_AA="$A_ARGS" WK_JOB_BA="$B_ARGS" \
    WK_JOB_TOOLS="/var/wk/wk-tools" WK_JOB_BY="$(hostname)" \
    WK_JOB_STAMP="$stamp" \
    WK_JOB_ASLR="${WK_BENCH_ASLR:-}" WK_JOB_ENVPAD="${WK_BENCH_ENV_PAD:-}" \
    WK_JOB_PATHPAD="${WK_BENCH_PATH_PAD:-}" WK_JOB_SHARED="${WK_BENCH_SHARED_CACHE:-}" \
    WK_JOB_DISPLAY="$NODE_DISPLAY" \
    python3 - <<'PYEOF' > "$task_dir/job.json"
import json, os
g = os.environ.get
arms = [{"label": "A", "id": g("WK_JOB_A"), "browser_args": g("WK_JOB_AA") or ""}]
if g("WK_JOB_B"):
    arms.append({"label": "B", "id": g("WK_JOB_B"), "browser_args": g("WK_JOB_BA") or ""})
print(json.dumps({
    "plans": (g("WK_JOB_PLANS") or "").split(),
    "rounds": int(g("WK_JOB_ROUNDS")),
    "max_rounds": int(g("WK_JOB_MAX_ROUNDS")),
    "detect_pct": float(g("WK_JOB_DETECT")),
    "timeout": int(g("WK_JOB_TIMEOUT")),
    "count": g("WK_JOB_COUNT") or "",
    "display": g("WK_JOB_DISPLAY"),
    "settle": int(g("WK_JOB_SETTLE")),
    "n_arms": len(arms),
    "arms": arms,
    "wk_tools": g("WK_JOB_TOOLS"),
    "created_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
    "created_by": g("WK_JOB_BY"),
    "stamp": g("WK_JOB_STAMP"),
    "aslr": g("WK_JOB_ASLR") or "",
    "env_pad": g("WK_JOB_ENVPAD") or "",
    "path_pad": g("WK_JOB_PATHPAD") or "",
    "shared_cache": g("WK_JOB_SHARED") or "",
}, indent=2))
PYEOF
    put_file "$task_dir/job.json" "$root/job.json" \
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
    log  "  task   $task_dir   ('wk status' lists it; 'wk bench report $task' reads it)"
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
    local verb=reboot
    [ "$GO" = shutdown ] && verb="shut down"
    [ -n "$DRY" ] && { log "  would re-check that the built-in panel is the only display, then $verb $HOST (loginwindow event, no sudo)"; return 0; }

    # Asked again seconds before the transition, not only at preflight: a monitor plugged in between the two costs a whole cycle of numbers nobody can trust. No --force crosses it -- there is no number to save.
    mac_display_check || die "the display on $HOST does not read as one built-in panel: $DISPLAY_READ

    Nothing has been rebooted, and the job stays planted. Disconnect the monitor
    and re-run, or reboot $HOST by hand once it reads right -- the planted job
    runs by itself either way."

    BOOT_BEFORE=$(mac_boottime)
    info "go: $verb $HOST now (boot before: ${BOOT_BEFORE:-unknown})"
    [ "$GO" = shutdown ] || log "  '$VOLUME' is the firmware default (preflight asserted it), so this restart
  enters bench mode by itself and nobody has to be at the keyboard."

    # One implementation of "restart this Mac", the boot driver's: it goes through the
    # privileged helper, whose reboot no application can decline. A shutdown has no
    # helper verb, and loginwindow answers no shutdown event at all (-1708, measured
    # 2026-09-07), so that one is asked of System Events, which host mode has a session
    # for. Backgrounded and its status ignored: the transition kills the ssh carrying it,
    # and `kern.boottime` below is what verifies it.
    if [ "$GO" = shutdown ]; then
        mac_sh '(osascript -e "tell application \"System Events\" to shut down" >/dev/null 2>&1 &)
                exit 0' >/dev/null 2>&1 || true
    else
        b_reboot || true
    fi

    local waited=0
    while [ "$waited" -lt 150 ]; do
        mac_sh true >/dev/null 2>&1 || { info "  $HOST is going down (after ${waited}s)"; return 0; }
        sleep 5
        waited=$((waited + 5))
    done

    die "could not $verb $HOST -- it is still answering, and \`kern.boottime\` is
    unchanged. Both mechanisms exit 0 without acting, so this is checked rather
    than trusted; see the comment in phase_go.

    Nothing has been lost: the job is planted, so rebooting the machine by hand
    -- or booting '$VOLUME' from the startup manager -- runs the A/B and needs
    nothing further from here."
}

phase_wait() {
    local limit="$1" start now mode last=""
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


# The volume's result directories carry the env.json `wk bench staged` wrote beside each one, so they are copied rather than recomposed; runs.tsv adds the only thing they do not know, which round and arm each belongs to. Contaminated legs are left where they are: the lane refuses to compare them, so the store does not carry them either.
collect_runs_into_task() {  # <task dir> <bench root> <runs.tsv on the volume>
    local task_dir="$1" root="$2" runs="$3" tsv dirs n=0 d
    tsv=$(mac "cat $(sh_quote "$runs")" 2>/dev/null | tr -d '\r') || return 1
    dirs=""
    for d in $(printf '%s\n' "$tsv" | awk -F'\t' '$1 != 0 && $5 == "clean" { print $4 }'); do
        dirs="$dirs $(sh_quote "$d")"
    done
    [ -n "$dirs" ] || { warn "  no clean leg after the warmup round -- nothing to record on the task"; return 0; }
    ensure_dir "$task_dir/runs" >/dev/null
    mac "cd $(sh_quote "$root/results") && tar -cf -$dirs" | tar -xf - -C "$task_dir/runs" \
        || { warn "  could not copy the results onto the task"; return 1; }
    local round label sid rid clean plan
    while IFS="$(printf '\t')" read -r round label sid rid clean plan; do
        [ -n "$rid" ] && [ "$round" != 0 ] && [ "$clean" = clean ] || continue
        wkdata env-record "$task_dir/runs/$rid/env.json" --update \
            machine="$MACHINE" plan="$plan" ab.round="$round" ab.staged="$sid" \
            ab.arm="$(printf '%s' "$label" | tr '[:upper:]' '[:lower:]')" \
            || { warn "  could not pair $rid with round $round arm $label"; continue; }
        n=$((n + 1))
    done <<EOF
$tsv
EOF
    log "  recorded $n clean leg(s) onto $task_dir"
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
    local task_dir=""
    [ -z "$stamp" ] || task_dir=$(bench_task_dir "$stamp-$MACHINE-mac-ab")
    if [ -n "$stamp" ] && mac "test -f $(sh_quote "$runs")" 2>/dev/null; then
        log ""
        log "  runs:"
        mac "cat $(sh_quote "$runs")" 2>/dev/null | sed 's/^/    /' >&2
        log ""
        if [ -d "$task_dir" ]; then
            printf '%s\n' "$st" > "$task_dir/autorun.state"
            collect_runs_into_task "$task_dir" "$root" "$runs" || true
        else
            warn "  job $stamp has no task in $BENCH_DIR, so 'wk status' cannot list it"
        fi
        bwk bench ab-summary --root "$(sh_quote "$root")" --runs "$(sh_quote "$runs")" || \
            warn "  the summary could not be produced; the results are still on the volume"
    else
        warn "  no run map at $runs -- no arm completed"
        log  "  the autorun's own log is the place to look:"
        log  "    ssh $HOST tail -60 $(sh_quote "$root/autorun.log")"
    fi
}

# Every step of a macOS A/B, what it is, the command that does it, and the
# command that proves it. Reads only; it takes no lock and changes nothing.
STEP_N=0
step() {   # <yes|no|part> <title> <detail> <do> <verify>
    STEP_N=$((STEP_N + 1))
    local mark
    case "$1" in
        yes)  mark="[x]" ;;
        part) mark="[~]" ;;
        *)    mark="[ ]" ;;
    esac
    printf '  %s %d. %s\n' "$mark" "$STEP_N" "$2" >&2
    [ -z "$3" ] || printf '         %s\n' "$3" >&2
    [ "$1" = yes ] || { [ -z "$4" ] || printf '         do:     %s\n' "$4" >&2; }
    [ -z "$5" ] || printf '         verify: %s\n' "$5" >&2
}

staged_arms() {   # <id> <webkit sha> <gated yes|no> per line
    local root; root=$(bench_root 2>/dev/null) || return 0
    mac "for d in $(sh_quote "$root/staged")/*/; do
             [ -f \"\$d/stage.json\" ] || continue
             s=\$(sed -n 's/.*\"webkit_sha\": \"\\([^\"]*\\)\".*/\\1/p' \"\$d/stage.json\")
             g=no
             ls \"\$d\"/WebKitBuild/*/wk-profile-check.json >/dev/null 2>&1 && g=yes
             printf '%s %s %s\n' \"\$(basename \$d)\" \"\$s\" \"\$g\"
         done" 2>/dev/null | tr -d '\r'
}

phase_progress() {
    info "the macOS A/B on $HOST, step by step"
    local root mode arms narms job rounds outcome results volver

    # Three states, not two: reachable in host mode, reachable in bench mode (the
    # volume is `/` and is not under /Volumes at all), and not answering -- which
    # is what a run in bench mode looks like from here, that install having no
    # tailnet identity of its own.
    if ! mac true >/dev/null 2>&1; then
        step part "the run is under way" \
            "$HOST does not answer. In bench mode it is a different install with no
         tailnet identity, so this is what a running A/B looks like from here; it
         answers again when it hands the machine back." \
            "" "wk bench mac-ab --collect   (once it is back in host mode)"
        log "" >&2
        log "  the volume's own log survives the way back:" >&2
        log "    /Volumes/$VOLUME - Data/private/var/wk/autorun.log" >&2
        return 0
    fi

    local here; here=$(mac 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null | tr -d '\r') || here=""
    if [ -n "$here" ]; then
        step yes "the Mac is in bench mode" "$here -- the A/B is running on it now" "" \
            "wk bench mac-ab --status"
        return 0
    fi

    volver=$(mac "/usr/libexec/PlistBuddy -c 'Print :ProductUserVisibleVersion' \
                  '/Volumes/$VOLUME/System/Library/CoreServices/SystemVersion.plist' 2>/dev/null" 2>/dev/null | tr -d '\r') || volver=""
    if [ -n "$volver" ]; then
        step yes "the benchmark volume exists" "'$VOLUME', macOS $volver" \
            "wk bench mac-volume --all   (on the Mac)" \
            "wk bench mac-volume"
    else
        step no "the benchmark volume exists" "'$VOLUME' is not mounted here (a shutdown unmounts it; --all makes one that is not there at all)" \
            "wk bench mac-volume --all   (on the Mac)" \
            "wk bench mac-volume"
        return 0
    fi

    local marker; marker=$(mac "sed -n 's/^id=//p' '/Volumes/$VOLUME/etc/wk-image' 2>/dev/null" 2>/dev/null | tr -d '\r') || marker=""
    local pyobjc=no
    if mac "test -d $(sh_quote "$(bench_home)/Library/Python/3.9/lib/python/site-packages/objc")" 2>/dev/null; then
        pyobjc=yes
    fi
    # The first boot's completion line, not what it leaves behind: it quiets the desktop after installing pyobjc, so a volume with both markers can still have been cut off before the settings every leg is measured against.
    if volume_provisioned; then
        step yes "it is provisioned" "first boot completed; marker $marker, pyobjc $pyobjc" \
            "wk bench mac-volume --repair   (on the Mac), then boot it once" \
            "wk bench mac-ab --preflight"
    else
        step no "it is provisioned" "$(firstboot_log) has no completion line (marker ${marker:-none}, pyobjc $pyobjc)" \
            "wk bench mac-volume --repair   (on the Mac), then boot it once" \
            "wk bench mac-ab --preflight"
    fi

    arms=$(staged_arms) || arms=""
    narms=$(printf '%s' "$arms" | grep -c . || true)
    local gated; gated=$(printf '%s' "$arms" | awk '$3 == "yes"' | grep -c . || true)
    local shown; shown=$(printf '%s' "$arms" | awk '{printf "%s (%s) gated=%s; ", $1, substr($2,1,12), $3}')
    case "$narms" in
        0) step no   "two arms are built and staged" "nothing staged" \
               "wk bench mac-ab <ws> --patch <ref> --base <ref>" \
               "wk bench staged --ls   (on the Mac)" ;;
        1) step part "two arms are built and staged" "${shown:-one arm}" \
               "wk bench mac-ab <ws> --patch <ref> --base <ref>" \
               "wk bench staged --ls   (on the Mac)" ;;
        *) step yes  "two arms are built and staged" "$shown" "" \
               "wk bench staged --ls   (on the Mac)" ;;
    esac

    if [ "$narms" -gt 0 ] && [ "$gated" = "$narms" ]; then
        step yes "each arm can prove how it was collected" "$gated of $narms carry their readings" "" \
            "cat .../staged/<id>/WebKitBuild/*/wk-profile-check.json   (on the Mac)"
    else
        step part "each arm can prove how it was collected" \
            "$gated of ${narms:-0} carry their readings -- the gates ran either way, an older arm just did not keep them beside the build" \
            "rebuild it: wk build <ws> mac-release-pgo" \
            "cat .../staged/<id>/WebKitBuild/*/wk-profile-check.json   (on the Mac)"
    fi

    mode=$(mac 'sed -n "s/^id=//p" /etc/wk-image 2>/dev/null' 2>/dev/null | tr -d '\r') || mode=""
    if [ -n "$mode" ]; then
        step yes "the Mac is in bench mode" "$mode" "" "wk boot $MACHINE --status"
    else
        step no "the Mac is in bench mode" "it is in host mode" \
            "wk boot $MACHINE   (arms the firmware and reboots)" \
            "wk boot $MACHINE --status"
    fi

    root=$(bench_root 2>/dev/null) || root=""
    # Whose arms: a job left from an earlier experiment reports rounds and
    # results that have nothing to do with what is staged now, and a checklist
    # that counts those is a checklist that lies.
    local job_arms fresh=no
    job_arms=$(mac "sed -n 's/.*\"id\": \"\([^\"]*\)\".*/\1/p' $(sh_quote "$root/job.json") 2>/dev/null" 2>/dev/null | tr -d '\r') || job_arms=""
    job=$(mac "test -f $(sh_quote "$root/job.json") && echo yes" 2>/dev/null | tr -d '\r') || job=""
    if [ -n "$job_arms" ]; then
        fresh=yes
        local _a
        for _a in $job_arms; do
            printf '%s' "$arms" | awk '{print $1}' | grep -qxF "$_a" || fresh=no
        done
    fi
    if [ "$job" = yes ] && [ "$fresh" = yes ]; then
        step yes "a job is planted for the staged arms" "$(printf '%s' "$job_arms" | tr '\n' ' ')" \
            "" "wk bench mac-ab --status"
    elif [ "$job" = yes ]; then
        step no "a job is planted for the staged arms" \
            "the planted job names arms that are not staged now ($(printf '%s' "$job_arms" | tr '\n' ' ')) -- it is an older experiment's, and its rounds and results below are not this one's" \
            "wk bench mac-ab --a <id> --b <id> --detect 0.3" \
            "wk bench mac-ab --status"
    else
        step no "a job is planted for the staged arms" "none" \
            "wk bench mac-ab --a <id> --b <id> --detect 0.3" \
            "wk bench mac-ab --status"
    fi

    rounds=$(mac "sed -n 's/^rounds_done=//p' $(sh_quote "$root/autorun.state") 2>/dev/null | tail -1" 2>/dev/null | tr -d '\r') || rounds=""
    outcome=$(mac "sed -n 's/^outcome=//p' $(sh_quote "$root/autorun.state") 2>/dev/null | tail -1" 2>/dev/null | tr -d '\r') || outcome=""
    results=$(mac "ls -1 $(sh_quote "$root/results") 2>/dev/null | grep -c . || true" 2>/dev/null | tr -d '\r') || results=0
    if [ "$fresh" != yes ]; then
        step no "the rounds are done" "nothing has run for these arms" \
            "boot the volume; the planted job runs them" \
            "wk bench mac-ab --status"
    elif [ -n "$outcome" ]; then
        step yes "the rounds are done" "${rounds:-0} round(s), outcome $outcome, ${results:-0} result(s)" \
            "" "wk bench mac-ab --collect"
    elif [ -n "$rounds" ]; then
        step part "the rounds are done" "${rounds} so far, ${results:-0} result(s)" "" \
            "wk bench mac-ab --status"
    else
        step no "the rounds are done" "not started" \
            "boot the volume; the planted job runs them" \
            "wk bench mac-ab --status"
    fi

    step no "the result is read back" "" \
        "wk bench mac-ab --collect" \
        "wk bench precision <run-a> <run-b>"
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
        --plan)     [ -n "$PLANS_GIVEN" ] || PLANS=""   # the first replaces the default set, the rest add
                    PLANS_GIVEN=1; PLANS="$PLANS${PLANS:+ }${2:-}"; shift 2 ;;
        --config)   CONFIG="${2:-}"; shift 2 ;;
        --rounds)   ROUNDS="${2:-}"; shift 2 ;;
        --max-rounds) MAX_ROUNDS="${2:-}"; shift 2 ;;
        --shutdown) GO=shutdown; shift ;;
        --progress) ACTION=progress; shift ;;
        --detect)   DETECT="${2:-}"; shift 2 ;;
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
        --force)    FORCE=1; WK_FORCE=1; export WK_FORCE; shift ;;
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

# Deferred until MACHINE is final, so `--machine benchvm` picks up benchvm's own conf. Loaded whether or not --host was given: NODE_DISPLAY is read from the same conf.
machine_load "$MACHINE" >/dev/null 2>&1 || die "no such machine: $MACHINE (wk boot --list)"
[ -n "$HOST" ] || HOST="${NODE_SSH:-}"
[ -n "$HOST" ] || die "$MACHINE (boot/machines/$MACHINE.conf) sets no NODE_SSH"
NODE_SSH="$HOST"   # one address for both halves, so --host moves the reads and the restart together
load_driver "$NODE_DRIVER" || die "$MACHINE names no boot driver this lane can restart it with"

_lc() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
if is_macos && [ "$(_lc "$(hostname -s 2>/dev/null)")" = "$(_lc "$HOST")" ]; then
    die "this lane reboots $HOST, so it cannot be driven from $HOST -- the reboot
  would take the driver with it. Run it from another machine (rpi5, moose)."
fi

case "$ACTION" in
    progress) phase_progress; exit 0 ;;
    preflight) preflight; exit $? ;;
    status)    phase_status; exit 0 ;;
    collect)   phase_collect; exit 0 ;;
esac

if ! preflight; then
    if [ -n "$DRY" ]; then
        warn "preflight failed; showing the plan anyway because this is --dry-run"
    else
        # `barrier` (lib/common.sh) and not a die: the tooling a volume needs in
        # order to provision itself travels in the plant, so the one operator who
        # has to plant onto a volume that fails this is the one fixing it.
        barrier "$PF_FAIL preflight check(s) failed on $HOST, and nothing there has been
    changed yet. Each one is something a run discovers after the reboot, in bench
    mode, where nothing can report it."
    fi
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

if [ -n "$DRY" ]; then
    [ "$ACTION" = plant ] || phase_go   # the plan is not complete without how it would leave the machine
    info "dry run -- nothing on $HOST was changed and nothing was rebooted"
    log  "  not checked here: whether a leg would pass on '$VOLUME'. That gate reads"
    log  "  the running system, and this one is not running. In bench mode, ask it:"
    log  "    wk bench staged --plan jetstream3 --dry-run"
    exit 0
fi

if [ "$ACTION" = plant ]; then
    info "planted and not started. The A/B runs the next time '$VOLUME' boots --"
    log  "  by itself if it is the firmware default, or from the startup manager."
    exit 0
fi

phase_go

GOING="$HOST has gone down to measure, and answers again when it hands the machine back."
if [ "$GO" = shutdown ]; then
    GOING="$HOST is off with the job planted: hold the power button and pick '$VOLUME' to start it."
fi
notify "mac-ab planted on $HOST" \
    "$PLANS, $ROUNDS-$MAX_ROUNDS rounds, count $COUNT. Arms $A_ID / $B_ID. $GOING"

if [ "$GO" = shutdown ]; then
    info "$HOST is powering off with the job planted."
    log  "  start it holding the power button until 'Loading startup options',"
    log  "  pick '$VOLUME' and press Return. The run is unattended; unlocking the"
    log  "  host install when it hands the machine back is yours."
    log  "  watch it:   wk bench mac-ab --progress"
    log  "  read it:    wk bench mac-ab --collect"
    exit 0
fi

came_back=$(phase_wait "$BOOT_WAIT") || true
log ""
case "$came_back" in
    bench)
        info "$HOST came back in BENCH mode and is reachable -- the A/B is running there."
        log  "  'wk bench mac-ab --status' follows it." ;;
    host)
        notify "mac-ab: $HOST is back in host mode" \
            "the result is collectable now: wk bench mac-ab --collect"
        phase_collect ;;   # the state file tells "ran it and came back" from "never left" apart
    noreboot)
        warn "$HOST never rebooted, so the A/B has not run."
        log  "  The job is planted and still valid -- nothing needs re-staging."
        log  "  Reboot the machine by any means (the startup manager works too)"
        log  "  and it runs by itself; 'wk bench mac-ab --collect' reads it after."
        notify "mac-ab: $HOST never rebooted" \
            "the A/B has not run. The job is planted and still valid: reboot $HOST by any means, including the startup manager, and it runs by itself." ;;
    *)
        warn "$HOST is not answering."
        log  "  If it went to bench mode, that is expected: that install has no"
        log  "  network. The job carries a watchdog and its own hand-back, so it"
        log  "  returns on its own; 'wk bench mac-ab --collect' reads the result"
        log  "  once it does."
        notify "mac-ab: $HOST has gone silent" \
            "expected if it entered bench mode -- that install has no network. The job carries a watchdog and its own hand-back, so it returns on its own; wk bench mac-ab --collect reads the result then." ;;
esac
