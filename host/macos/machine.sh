# TODO: measure a machine created without --rootful before dropping the flag.
. "$WK_ROOT/lib/store.sh"

WK_MACHINE="${WK_MACHINE:-wk}"

# applehv permits exactly one running VM, so a leftover machine blocks `wk`.
_others=$(podman machine list --format json 2>/dev/null \
    | python3 -c "
import json,sys
try: ms = json.load(sys.stdin)
except Exception: sys.exit(0)
for m in ms:
    n = (m.get('Name') or '').rstrip('*')
    if n and n != '$WK_MACHINE':
        print(n)
" 2>/dev/null || true)

for _m in $_others; do
    _img="$HOME/.local/share/containers/podman/machine/applehv/${_m}-arm64.raw"
    _sz=$(du -sh "$_img" 2>/dev/null | cut -f1 || echo "?")
    warn "obsolete podman machine '$_m' ($_sz) blocks the wk machine from starting"
    if confirm "  remove machine '$_m' and its images? this cannot be undone"; then
        podman machine stop "$_m" >/dev/null 2>&1 || true
        podman machine rm -f "$_m" >/dev/null
        changed "removed podman machine '$_m' (reclaimed $_sz)"
    else
        warn "keeping '$_m' -- 'wk' will fail to start while it is running"
    fi
done
unset _others _m _img _sz

_cores=$(envelope_cores)
_mem=$(envelope_mem_mb)
# Set only at creation: podman machine cannot resize a disk afterwards.
_disk="${WK_DISK_GB:-200}"

# TODO: upstream -- podman 5.4 does not canonicalise a --volume target against
# the machine OS (ostree, hence /var), and a non-canonical one yields a .mount
# unit that fails at boot: the machine then runs without the mount, silently.
ensure_dir "$(wk_secrets_dir)" 0700
ensure_dir "$(wk_agent_rw_dir)" 0700
_secrets_mount="$(wk_secrets_dir):$WK_STORE/secrets:ro"
_tools_mount="$WK_ROOT:/var/opt/wk-tools:ro"
_agent_rw_mount="$(wk_agent_rw_dir):$WK_STORE/agent-rw:rw"

# `podman machine inspect` does not expose Mounts (podman 5.4); read the config.
_cfg="$HOME/.config/containers/podman/machine/applehv/$WK_MACHINE.json"

_mount_state() {
    python3 - "$_cfg" "$_secrets_mount" "$_tools_mount" "$_agent_rw_mount" <<'PY'
import json, os, sys

# Both sides through realpath: one directory has two spellings (a symlinked
# WK_ROOT, macOS's /var -> /private/var), and `differs` destroys a store.
def key(source, target):
    return "%s:%s" % (os.path.realpath(source), target.rstrip("/") or "/")

cfg = sys.argv[1]
want = {}
for spec in sys.argv[2:]:
    source, target, mode = spec.rsplit(":", 2)
    want[key(source, target)] = mode == "ro"
try:
    mounts = json.load(open(cfg))["Mounts"]
    have = {key(m["Source"], m["Target"]): bool(m.get("ReadOnly"))
            for m in mounts}
except Exception:
    print("unknown")
    raise SystemExit(0)

if set(have) != set(want):
    print("differs")
elif have != want:
    print("notro")
else:
    print("ok")

for m in sorted(mounts, key=lambda m: (m["Source"], m["Target"])):
    raw = "%s:%s" % (m["Source"], m["Target"])
    canon = key(m["Source"], m["Target"])
    print(raw if canon == raw else "%s (%s)" % (raw, canon),
          "ro" if m.get("ReadOnly") else "rw")
PY
}

_report_losses() {
    if [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" != running ]; then
        if [ -n "${WK_DRY_RUN:-}" ]; then
            log "    (not read: '$WK_MACHINE' is stopped and a dry run does not start it)"
            return 0
        fi
        info "starting '$WK_MACHINE' to read what a recreate would lose"
        podman machine start "$WK_MACHINE" >/dev/null
    fi
    podman machine ssh "$WK_MACHINE" -- '
        printf "workspaces  %s\n" "$(podman ps -a --filter name=^wk- --format "{{.Names}}" 2>/dev/null | sed "s/^wk-//" | tr "\n" " ")"
        for d in git base ws cache skills bench; do
            printf "%-11s %s\n" "$d" "$(du -sh /var/lib/wk/$d 2>/dev/null | cut -f1)"
        done
        printf "bench runs  %s\n" "$(ls -1 /var/lib/wk/bench 2>/dev/null | wc -l | tr -d " ")"
    ' </dev/null 2>/dev/null | sed 's/^/    /' >&2
}

_absent_targets() {
    [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}' 2>/dev/null)" = running ] \
        || return 0
    local spec target
    for spec in "$_secrets_mount" "$_tools_mount" "$_agent_rw_mount"; do
        target=${spec%:*}; target=${target##*:}
        podman machine ssh "$WK_MACHINE" -- \
            "findmnt -no TARGET $(sh_quote "$target")" </dev/null >/dev/null 2>&1 \
            || printf '%s\n' "$target"
    done
}

# No `| head`: under pipefail a python killed by SIGPIPE is the caller's failure.
_mounts=""
_verdict=""
_absent=""
_read_mounts() {
    _mounts=$(_mount_state)
    _verdict=${_mounts%%$'\n'*}
    _absent=""
    if [ "$_verdict" = ok ]; then
        _absent=$(_absent_targets)
        [ -z "$_absent" ] || _verdict=absent
    fi
}
_mount_rows() { printf '%s\n' "$_mounts" | tail -n +2 | sed '/^[[:space:]]*$/d'; }

_check_mounts() { # [after-init]
    _read_mounts
    if [ "$_verdict" = absent ] && [ -n "${1:-}" ]; then
        die "internal error: podman machine '$WK_MACHINE' was just created asking for
$(printf '%s\n' "$_absent" | sed 's/^/    /')
    and came up without it. podman writes one systemd .mount unit per --volume;
        podman machine ssh $WK_MACHINE -- systemctl --failed
    names the unit and the reason. A target that is not a canonical path in the
    machine OS is the usual one. Do NOT re-run ./setup: it would destroy and
    recreate the machine to reach exactly this state again. Fix the mount
    triples in host/macos/machine.sh."
    fi
    if [ "$_verdict" = differs ] && [ -n "${1:-}" ]; then
        die "internal error: podman machine '$WK_MACHINE' was just created with
$(printf '    asks %s\n    asks %s\n    asks %s' \
        "$_secrets_mount" "$_tools_mount" "$_agent_rw_mount")
    and reads back as
$(_mount_rows | sed 's/^/    has  /')
    The two spellings above are the same directories written differently, and
    this file compares them by their real paths. Do NOT re-run ./setup: it
    would destroy and recreate the machine to reach exactly this state again.
    Fix the comparison in _mount_state (host/macos/machine.sh)."
    fi
    case "$_verdict" in
        ok) unchanged "machine mounts exactly this checkout and the secrets directory read-only, and the agent credential directory read-write (verified)" ;;
        notro)
            die "podman machine '$WK_MACHINE' has this design's mounts, mounted the wrong way:
$(_mount_rows | sed 's/^/    has /')
    wants $_secrets_mount
    wants $_tools_mount
    wants $_agent_rw_mount
    A workspace could then rewrite this checkout or its own deploy keys.
    This podman took the mode option and dropped it, and nothing here can fix
    that from the outside: the --volume lines in this file have to say
    read-only in whatever spelling it honours." ;;
        differs)
            die "podman machine '$WK_MACHINE' has mounts this design does not:
$(_mount_rows | sed 's/^/    has /')
    Workspaces must not be able to reach the rest of the host filesystem.
    Recreate it with:  ./setup" ;;
        *)  die "could not read mounts from $_cfg -- refusing to proceed.
    Workspace isolation depends on this machine mounting exactly this
    checkout, the secrets directory and the agent-writable directory, and
    that cannot be confirmed." ;;
    esac
}

_read_mounts
if podman machine inspect "$WK_MACHINE" >/dev/null 2>&1; then
    if [ "$_verdict" != differs ] && [ "$_verdict" != absent ]; then
        _check_mounts
    else
        if [ "$_verdict" = absent ]; then
            warn "podman machine '$WK_MACHINE' asks for this design's mounts and has not got them:"
            printf '%s\n' "$_absent" | sed 's/^/    absent /' >&2
            log  "  podman writes one systemd .mount unit per --volume, and a unit that"
            log  "  fails at boot leaves the config file naming a mount the machine"
            log  "  never got:  podman machine ssh $WK_MACHINE -- systemctl --failed"
        else
            warn "podman machine '$WK_MACHINE' does not have this design's mounts:"
            _mount_rows | sed 's/^/    has /' >&2
            log  "    wants $_secrets_mount"
            log  "    wants $_tools_mount"
            log  "    wants $_agent_rw_mount"
        fi
        log  "  a mount is set only at creation (podman machine has no way to add"
        log  "  one), so the machine is destroyed and made again. That loses:"
        _report_losses
        log  "  all of it is regenerable -- 'wk sync' refetches the mirror and"
        log  "  publishes a snapshot, 'wk new' remakes a workspace -- except"
        log  "  /var/lib/wk/bench, which is measurements. Copy those out first:"
        log  "    podman machine ssh $WK_MACHINE -- tar -C /var/lib/wk -cf - bench > bench.tar"
        log  "  The deploy keys and agent tokens are not in this list: they are"
        log  "  already on this host ('wk key register' and 'wk key set claude'"
        log  "  put them back if this machine still holds the only copies)."
        if [ -n "${WK_DRY_RUN:-}" ]; then
            warn "dry run: '$WK_MACHINE' would be destroyed and recreated; nothing was touched"
            return 0 2>/dev/null || exit 0
        fi
        confirm "  destroy podman machine '$WK_MACHINE' and everything above, and recreate it?" \
            || die "keeping '$WK_MACHINE'. Nothing that needs /var/opt/wk-tools or the
    keys works with these mounts, so ./setup stops here."
        podman machine stop "$WK_MACHINE" >/dev/null 2>&1 || true
        podman machine rm -f "$WK_MACHINE" >/dev/null
        changed "removed podman machine '$WK_MACHINE'"
    fi
fi

if ! podman machine inspect "$WK_MACHINE" >/dev/null 2>&1; then
    if [ -n "${WK_DRY_RUN:-}" ]; then
        warn "dry run: podman machine '$WK_MACHINE' would be created (${_cores} cpus,
    ${_mem} MiB, ${_disk} GiB) with these mounts and no others:"
        log  "    $_secrets_mount"
        log  "    $_tools_mount"
        log  "    $_agent_rw_mount"
        return 0 2>/dev/null || exit 0
    fi
    info "creating podman machine '$WK_MACHINE' (${_cores} cpus, ${_mem} MiB, ${_disk} GiB)"
    log  "this downloads a Fedora CoreOS image and takes a few minutes"

    # Naming any --volume overrides podman's defaults (/Users, /private, /var/folders).
    # No --playbook: it runs from a first-boot unit whose failures nothing reads.
    podman machine init "$WK_MACHINE" \
        --cpus "$_cores" \
        --memory "$_mem" \
        --disk-size "$_disk" \
        --rootful \
        --volume "$_secrets_mount" \
        --volume "$_tools_mount" \
        --volume "$_agent_rw_mount"

    changed "created podman machine '$WK_MACHINE'"

    podman machine start "$WK_MACHINE" >/dev/null
fi

_check_mounts after-init

_cur_cpus=$(podman machine inspect "$WK_MACHINE" --format '{{.Resources.CPUs}}' 2>/dev/null || echo "")
_cur_mem=$(podman machine inspect "$WK_MACHINE" --format '{{.Resources.Memory}}' 2>/dev/null || echo "")

if [ "$_cur_cpus" = "$_cores" ] && [ "$_cur_mem" = "$_mem" ]; then
    unchanged "machine resources (${_cores} cpus, ${_mem} MiB)"
else
    _was_running=""
    [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" = running ] && _was_running=1
    [ -n "$_was_running" ] && podman machine stop "$WK_MACHINE" >/dev/null
    podman machine set "$WK_MACHINE" --cpus "$_cores" --memory "$_mem"
    changed "machine resources -> ${_cores} cpus, ${_mem} MiB (host keeps ${WK_RESERVE_CORES} cores / ${WK_RESERVE_MB} MiB)"
    [ -n "$_was_running" ] && podman machine start "$WK_MACHINE" >/dev/null
fi

unset _cores _mem _disk _mounts _verdict _absent _cfg _secrets_mount _tools_mount \
      _agent_rw_mount _cur_cpus _cur_mem _was_running
unset -f _mount_state _absent_targets _read_mounts _mount_rows _check_mounts _report_losses
