# Install what depends on wk-tools inside the VM: the SDK and the egress proxy.
# Provisioning goes first: it is what holds the mounts read-only.
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/host/units.sh"

WK_MACHINE="${WK_MACHINE:-wk}"

podman machine inspect "$WK_MACHINE" >/dev/null 2>&1 || {
    debug "no machine yet; skipping VM tooling"
    return 0 2>/dev/null || true
}

if [ "$(podman machine inspect "$WK_MACHINE" --format '{{.State}}')" != running ]; then
    info "starting machine '$WK_MACHINE'"
    podman machine start "$WK_MACHINE" >/dev/null
fi

_ssh_port=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.Port}}')
_ssh_key=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.IdentityPath}}')
_ssh_user=$(podman machine inspect "$WK_MACHINE" --format '{{.SSHConfig.RemoteUsername}}')

# A podman machine is recreated by ./setup, not upgraded, so its host key is
# disposable (_unpinned_host_key_opts, lib/reach.sh).
command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_rsh() {
    # shellcheck disable=SC2046
    ssh -o BatchMode=yes -o ConnectTimeout="$(wk_ssh_timeout)" $(_unpinned_host_key_opts) \
        -p "$_ssh_port" -i "$_ssh_key" \
        "$_ssh_user@localhost" "$@"
}

# A playbook that dies at its third task also reports changed=0.
debug "re-applying machine provisioning"
scp -q -P "$_ssh_port" -i "$_ssh_key" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$WK_ROOT/host/macos/playbook.yaml" "$_ssh_user@localhost:/home/core/playbook.yaml"

# `<verdict> <changed-count>` first, then the failing tasks. ansible-core 2.16
# ships no machine-readable callback, so the recap line is what there is to read.
_playbook_verdict() {
    python3 -c '
import re, sys

text = sys.stdin.read()
recap = re.search(r"^\S+\s+:\s+(ok=\d+.*)$", text, re.M)
if not recap:
    print("norecap 0")
    raise SystemExit(0)
counts = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", recap.group(1))}
bad = counts.get("failed", 0) + counts.get("unreachable", 0)
print("failed" if bad else "ok", counts.get("changed", 0))

for line in text.splitlines():
    if line.startswith(("fatal:", "failed:")):
        print(line[:600])
'
}

# `|| true` so the recap decides, not ssh's exit status under `set -e`.
_pb=$({ _rsh 'ansible-playbook /home/core/playbook.yaml 2>&1' || true; } | _playbook_verdict)
read -r _pb_state _pb_changed <<<"${_pb%%$'\n'*}"
case "$_pb_state" in
    ok) if [ "$_pb_changed" -eq 0 ]; then
            unchanged "machine provisioning"
        else
            changed "machine provisioning (changed=$_pb_changed)"
        fi ;;
    failed)
        die "provisioning the machine failed, so everything below the failing task
    was skipped and this machine is not provisioned:
$(printf '%s\n' "$_pb" | tail -n +2 | sed 's/^/    /')" ;;
    *)  die "the provisioning playbook printed no recap, so nothing here knows what
    it did. Re-run it and read the output:
        podman machine ssh $WK_MACHINE -- ansible-playbook /home/core/playbook.yaml" ;;
esac

# From inside: what is asked for at creation and what the machine has differ.
_verify_mounts() {
    if _rsh 'test -x /opt/wk-tools/wk'; then
        unchanged "this checkout is mounted at /opt/wk-tools"
    else
        die "/opt/wk-tools/wk is not executable inside '$WK_MACHINE', so nothing in
    there can run this tooling. The machine mounts this checkout there when it
    is created:  ./setup --stage machine"
    fi

    if _rsh "findmnt -no TARGET $(sh_quote "$WK_STORE/secrets")" >/dev/null 2>&1; then
        unchanged "the secrets directory is mounted at $WK_STORE/secrets"
    else
        die "$WK_STORE/secrets is not a mount inside '$WK_MACHINE', so the keys this
    host holds ($(wk_secrets_dir)) reach no workspace. The machine mounts them
    there when it is created:  ./setup --stage machine"
    fi

    # The Claude CLI rewrites the credential in place when it spends the token.
    if ! _rsh "findmnt -no TARGET $(sh_quote "$WK_STORE/agent-rw")" >/dev/null 2>&1; then
        die "$WK_STORE/agent-rw is not a mount inside '$WK_MACHINE', so the claude.ai
    login this host holds ($(wk_agent_rw_dir)) reaches no workspace. The machine
    mounts it there when it is created:  ./setup --stage machine"
    elif _rsh "test -w $(sh_quote "$WK_STORE/agent-rw")"; then
        unchanged "the agent-writable directory is mounted read-write at $WK_STORE/agent-rw"
    else
        die "$WK_STORE/agent-rw is mounted read-only inside '$WK_MACHINE'. The Claude
    CLI rewrites the login credential in place, so a workspace would be logged
    out the first time it refreshes. The machine mounts it read-write when it
    is created:  ./setup --stage machine"
    fi

    # podman takes `--volume src:target:ro` and mounts it read-write anyway.
    local target
    for target in /opt/wk-tools "$WK_STORE/secrets"; do
        if _rsh "findmnt -no TARGET -O ro $(sh_quote "$target")" >/dev/null 2>&1; then
            unchanged "$target is mounted read-only"
        else
            die "$target is writable inside '$WK_MACHINE', so a workspace can rewrite
    this checkout and the deploy keys it pushes with. podman drops the
    read-only mode it is given and the machine's provisioning puts it back
    (host/macos/playbook.yaml); this says that provisioning did not run."
        fi
    done
}
_verify_mounts

# Workspaces share this read-write, so re-syncing every run destroys their edits.
if _rsh 'test -d /var/lib/wk/skills && test -n "$(ls -A /var/lib/wk/skills 2>/dev/null)"'; then
    unchanged "shared skills present (not overwritten)"
    _rsh 'diff -rq /opt/wk-tools/claude/skills /var/lib/wk/skills >/dev/null 2>&1' \
        || log "note: shared skills differ from the repo -- 'wk skills status' inside the VM"
else
    info "seeding the shared skills directory"
    _rsh 'mkdir -p /var/lib/wk/skills && cp -a /opt/wk-tools/claude/skills/. /var/lib/wk/skills/'
    changed "seeded /var/lib/wk/skills"
fi

"$WK_ROOT/cmd/key" ensure 2>&1 | sed 's/^/  /' || true
if [ -f "$(wk_secrets_dir)/build_key_fork.pub" ]; then
    unchanged "build key present"
else
    warn "no build key; workspaces will not be able to push"
fi

# Everything below is reapplied every run; these are data, and are left alone:
#   /var/lib/wk/git      the mirror        /var/lib/wk/ws       workspaces
#   /var/lib/wk/base     snapshots         /var/lib/wk/cache    ccache et al
#   /var/lib/wk/skills   mutable skills

# Without --additional-flags the overlay mount cannot be attached, and without a
# selectable --network the container shares the host namespace.
if _rsh 'test -d /opt/webkit-container-sdk/.git'; then
    unchanged "webkit-container-sdk present"
else
    info "cloning webkit-container-sdk into the machine"
    _rsh 'sudo mkdir -p /opt/webkit-container-sdk && sudo chown core:core /opt/webkit-container-sdk &&
          git clone -q https://github.com/Igalia/webkit-container-sdk.git /opt/webkit-container-sdk'
    changed "cloned webkit-container-sdk"
fi

# Without the reset the idempotent patcher leaves a tampered file as found.
debug "resetting and re-patching the SDK"
_sdk_hash() {
    _rsh 'cat /opt/webkit-container-sdk/scripts/host-only/wkdev-create \
              /opt/webkit-container-sdk/scripts/host-only/wkdev-enter \
              /opt/webkit-container-sdk/scripts/container-only/.wkdev-init \
              /opt/webkit-container-sdk/scripts/container-only/.wkdev-sync-runtime-state \
          2>/dev/null | sha256sum | cut -d" " -f1'
}
_sdk_before=$(_sdk_hash)
_rsh 'cd /opt/webkit-container-sdk && git reset --hard --quiet && git clean -qfd'
if [ -n "${WK_DEBUG:-}" ]; then
    _rsh 'bash /opt/wk-tools/container/sdk-patches/apply.sh /opt/webkit-container-sdk'
else
    _rsh 'bash /opt/wk-tools/container/sdk-patches/apply.sh /opt/webkit-container-sdk' >/dev/null 2>&1 \
        || die "SDK patching failed; re-run with WK_DEBUG=1"
fi
_sdk_after=$(_sdk_hash)
if [ "$_sdk_before" = "$_sdk_after" ]; then
    unchanged "SDK patches"
else
    changed "SDK patches re-applied (result differs from before)"
fi

# In place of nftables, which requires rootful podman, the proxy needs no
# privilege and expresses policy in hostnames.
debug "installing the egress proxy in the machine"

# %t in a unit body expands to /run/user/501: the machine's `core` is uid 501.
_unit_root=/opt/wk-tools
_unit_store=/var/lib/wk
_unit_journal="podman machine ssh $WK_MACHINE -- "

unit_start wk-proxy.service "$_unit_root" "$_unit_store" \
    "workspaces will have no egress" "$_unit_journal" _rsh

# Its socket is in %t/wk, which every container bind-mounts at /run/wk, so a
# workspace uses a key it never reads. `wk push on|off` fills and empties it.
unit_start wk-ssh-agent.service "$_unit_root" "$_unit_store" \
    "no workspace here can push" "$_unit_journal" _rsh

# Terminates TLS for api.github.com and puts the real token in the Authorization
# header. Its socket is under the store, not %t/wk: reached through the policy.
unit_start wk-github-inject.service "$_unit_root" "$_unit_store" \
    "'git-webkit pr' in a workspace will fail" "$_unit_journal" _rsh

# The standing read token goes in beside it: reading is open whatever position
# `wk push` is in, so this file is delivered here and by `wk key set github-pat`
# -- and removed again when this device no longer holds a token.
if push_agent_pat_sync push_agent_exec "$(push_agent_machine_read_pat)"; then
    debug "GitHub read token converged on the machine"
else
    warn "could not converge the GitHub read token on '$WK_MACHINE', so a read
  from a workspace answers 401 ('wk key set github-pat' stores one)"
fi

unset _ssh_port _ssh_key _ssh_user _unit_root _unit_store _unit_journal _pb _pb_state _pb_changed
unset -f _verify_mounts _playbook_verdict
