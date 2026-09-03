# The podman machine that hosts every workspace.
#
# Three properties matter, and each is verified rather than assumed:
#
#   exactly three mounts, and only one writable
#                     podman machine mounts /Users, /private and /var/folders
#                     by default. Workspaces need none of them. What the
#                     machine needs is this checkout at /opt/wk-tools, so a
#                     container runs the tree being edited rather than a copy
#                     of it; this device's secrets directory at
#                     $WK_STORE/secrets, so `wk key set` and `wk push` write
#                     the keys with no VM running and every container reads
#                     the same bytes live; and the agent-writable directory at
#                     $WK_STORE/agent-rw, the only read-write mount in the
#                     design, holding one thing -- the claude.ai login
#                     credential the Claude CLI rotates in place
#                     (wk_agent_rw_dir, lib/store.sh; README.md, "wk key set").
#                     Any other mount -- /Users above all -- is host filesystem
#                     access a workspace must not have, and is the recreate
#                     condition below.
#
#   rootful           the machine's podman service runs as root, which is the
#                     socket `podman -c wk` on this host connects to. A
#                     workspace itself is rootless in the VM (targets/
#                     container.sh's WK_SANDBOX).
#                     TODO: nothing here is known to need root. Measure a
#                     machine created without --rootful -- `wk new`, `wk
#                     build`, `wk status` and `wk enter` from this host --
#                     before dropping it.
#
#   bounded resources the VM must not be able to starve the desktop.
#
# The mounts are why moving or renaming this checkout breaks the machine: the
# fix is `./setup`, which recreates it around the new path.

# For $WK_STORE (the machine's own path for the store) and wk_secrets_dir
# (this host's path for the secrets directory it mounts): the two ends of one
# of the mounts below.
. "$WK_ROOT/lib/store.sh"

WK_MACHINE="${WK_MACHINE:-wk}"

# --- retire other machines ---------------------------------------------------
# applehv permits exactly one running VM, so a leftover machine is not merely
# wasted disk: it blocks `wk` from starting with "only one VM can be active at
# a time". Machine storage is per-machine, so nothing here is shared with the
# wk machine and removing it loses only that machine's own images.
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
# WK_DISK_GB overrides the machine's virtual disk size, set only at creation
# (podman machine cannot resize one afterwards).
_disk="${WK_DISK_GB:-200}"

# The mounts, as `source:target:mode` triples -- one list, read by the init
# below and by the verify that follows it, so the machine cannot be created
# with a set the check then refuses. The source directories exist first:
# podman refuses to init against a mount source that is not there.
ensure_dir "$(wk_secrets_dir)" 0700
ensure_dir "$(wk_agent_rw_dir)" 0700
_secrets_mount="$(wk_secrets_dir):$WK_STORE/secrets:ro"
_tools_mount="$WK_ROOT:/opt/wk-tools:ro"
_agent_rw_mount="$(wk_agent_rw_dir):$WK_STORE/agent-rw:rw"

# Read from the machine's config file, not `podman machine inspect`: inspect
# does not expose Mounts at all (podman 5.4), so a --format query silently
# yields nothing and the check would pass by accident -- which is worse than no
# check, because it reports a guarantee it never verified.
_cfg="$HOME/.config/containers/podman/machine/applehv/$WK_MACHINE.json"

# One of `ok`, `notro`, `differs` or `unknown` on the first line, then one
# `source:target ro|rw` line per mount the machine actually has.
#
# The set of source:target pairs and their modes are two different verdicts on
# purpose: a machine with a *different set* is recreated, while one with the
# right set mounted the wrong way round is refused. Recreating that second one
# would ask podman for exactly the same modes again and loop.
_mount_state() {
    python3 - "$_cfg" "$_secrets_mount" "$_tools_mount" "$_agent_rw_mount" <<'PY'
import json, os, sys

# Both sides through realpath, because the two spellings of one directory are
# not the same string: a symlinked WK_ROOT, macOS's /var -> /private/var, a
# trailing slash. A comparison by string reads `differs` on a machine this run
# has just created with these very paths, and `differs` is the verdict that
# destroys a store.
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

# The rows are the machine's own spelling, not the canonical one: a person
# reading a refusal is looking for what is in the config file. The canonical
# form is what the verdict above was decided on, and it appears beside the raw
# one only where the two differ.
for m in sorted(mounts, key=lambda m: (m["Source"], m["Target"])):
    raw = "%s:%s" % (m["Source"], m["Target"])
    canon = key(m["Source"], m["Target"])
    print(raw if canon == raw else "%s (%s)" % (raw, canon),
          "ro" if m.get("ReadOnly") else "rw")
PY
}

# What a recreate destroys, read off the machine at the moment of asking: the
# workspaces by name and the store's own directories. Everything here is
# regenerable except bench/, which holds measurements that cannot be taken
# again -- so it is named separately and the remedy for it is printed.
_report_losses() {
    if [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" != running ]; then
        # A stopped machine cannot be read, and a dry run must not start one:
        # then the honest answer is that the list is unknown.
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

# The verdict and the mount list, from one read: `_mounts` is every line and
# `_verdict` its first. Read into variables rather than piped into `head`,
# which under `pipefail` makes a python killed by SIGPIPE the caller's failure.
_mounts=""
_verdict=""
_read_mounts() {
    _mounts=$(_mount_state)
    # $'\n' and not "$(printf '\n')": command substitution strips the trailing
    # newline, leaving `%%*`, which strips the whole string.
    _verdict=${_mounts%%$'\n'*}
}
# Every mount but the verdict line -- and nothing at all for a machine with
# none, where `tail` alone would print one blank row.
_mount_rows() { printf '%s\n' "$_mounts" | tail -n +2 | sed '/^[[:space:]]*$/d'; }

# The one gate, called before the recreate decision and again after it: `ok` or
# a refusal that names what to do. Only a *different set* of mounts is a
# recreate, and that decision is the caller's below -- a mount that is there but
# read-write, or a config this cannot read, is refused instead: recreating would
# ask podman for exactly the same thing again and destroy a store for nothing.
#
# <after-init> is set on the call that follows this run's own `podman machine
# init`. A `differs` there is not the user's machine being wrong -- the init
# was handed these exact triples a moment ago -- so it is this file and podman
# disagreeing about how a path is spelled, and advising `./setup` would send
# the next run round the same destroy-and-recreate loop for ever.
_check_mounts() { # [after-init]
    _read_mounts
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
    if [ "$_verdict" != differs ]; then
        _check_mounts
    else
        warn "podman machine '$WK_MACHINE' does not have this design's mounts:"
        _mount_rows | sed 's/^/    has /' >&2
        log  "    wants $_secrets_mount"
        log  "    wants $_tools_mount"
        log  "    wants $_agent_rw_mount"
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
        # A dry run reports and stops: everything after this point destroys a
        # store, and `./setup --dry-run` says what would change.
        if [ -n "${WK_DRY_RUN:-}" ]; then
            warn "dry run: '$WK_MACHINE' would be destroyed and recreated; nothing was touched"
            return 0 2>/dev/null || exit 0
        fi
        confirm "  destroy podman machine '$WK_MACHINE' and everything above, and recreate it?" \
            || die "keeping '$WK_MACHINE'. Nothing that needs /opt/wk-tools or the
    keys works with these mounts, so ./setup stops here."
        podman machine stop "$WK_MACHINE" >/dev/null 2>&1 || true
        podman machine rm -f "$WK_MACHINE" >/dev/null
        changed "removed podman machine '$WK_MACHINE'"
    fi
fi

if ! podman machine inspect "$WK_MACHINE" >/dev/null 2>&1; then
    # A dry run reports and stops here too: creating the machine downloads an
    # image and takes a disk, and `./setup --dry-run` says what would change.
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

    # These --volume flags and nothing else: naming any overrides podman's
    # default mount list (/Users, /private, /var/folders). Verified below,
    # because a silent fallback to the defaults would quietly hand every
    # workspace read-write access to the entire home directory. The mode is
    # part of each triple, so what is asked for here is what the verify holds
    # the machine to.
    podman machine init "$WK_MACHINE" \
        --cpus "$_cores" \
        --memory "$_mem" \
        --disk-size "$_disk" \
        --rootful \
        --volume "$_secrets_mount" \
        --volume "$_tools_mount" \
        --volume "$_agent_rw_mount" \
        --playbook "$WK_ROOT/host/macos/playbook.yaml"

    changed "created podman machine '$WK_MACHINE'"
fi

# --- verify the mounts -------------------------------------------------------
# Refuse to continue if the VM can see anything else of this host's, or can
# write to what it does see. This is the single most important invariant in the
# design, so it is checked on every setup run rather than trusted from creation
# time -- including straight after an init, whose own flags it holds to.
_check_mounts after-init

# --- resources ---------------------------------------------------------------
# Re-apply the envelope if the host changed (or the machine predates this
# policy). `podman machine set` requires the machine to be stopped.
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

unset _cores _mem _disk _mounts _verdict _cfg _secrets_mount _tools_mount _agent_rw_mount \
      _cur_cpus _cur_mem _was_running
unset -f _mount_state _read_mounts _mount_rows _check_mounts _report_losses
