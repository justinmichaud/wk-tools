#!/usr/bin/env bash
#
# Patch the webkit-container-sdk fork so wkdev-create can produce a sandboxed
# workspace. Idempotent: safe to re-run, and it verifies rather than assumes.
#
# Written as an applier rather than a .patch file because these edits need to
# survive rebasing the fork onto upstream, and patch fuzz on a moving target is
# worse than a script that checks what it is editing.
#
# Four changes, each independently upstreamable:
#
#   1. --additional-flags for wkdev-create. wkdev-enter has had this since
#      forever (wkdev-enter:22); create has no way to pass extra podman flags
#      at all, so the overlay mount, cache volumes and resource caps are
#      impossible without it.
#
#   2. --network selectable. wkdev-create hardcodes `--network host`, which
#      puts the container in the host's network namespace. That defeats any
#      egress firewall: there is nothing to filter, because the traffic never
#      crosses a bridge.
#
#   3. SYS_ADMIN, NET_RAW, unmask=ALL and seccomp=unconfined become opt-in.
#      With SYS_ADMIN plus host networking a container can rewrite the very
#      nftables rules meant to confine it. SYS_PTRACE stays on: gdb and lldb
#      need it and it is not a network-escape risk.
#
#   4. .wkdev-init creates the container user if it is missing. The image
#      deliberately userdel's uid 1000, and rootful podman cannot use
#      --userns keep-id to recreate the passwd entry, so init aborts in
#      usermod without this.

set -euo pipefail

SDK="${1:-${WKDEV_SDK:-/opt/webkit-container-sdk}}"
CREATE="$SDK/scripts/host-only/wkdev-create"
ENTER="$SDK/scripts/host-only/wkdev-enter"
INIT="$SDK/scripts/container-only/.wkdev-init"
SYNC="$SDK/scripts/container-only/.wkdev-sync-runtime-state"

[ -f "$CREATE" ] || { echo "not an SDK checkout: $SDK" >&2; exit 1; }

py() { python3 - "$@"; }

# --- 1 + 2: new options ------------------------------------------------------
py "$CREATE" <<'PYEOF'
import sys, re
path = sys.argv[1]
src = open(path).read()

if "additional-flags" not in src:
    anchor = 'argsparse_use_option =arch:'
    line = src[src.index(anchor):].split("\n")[0]
    src = src.replace(line, line + "\n" +
        'argsparse_use_option additional-flags: "Additional flags to pass directly to podman create"\n'
        'argsparse_use_option network: "Container network (default: host)" default:host\n'
        'argsparse_use_option unsafe-caps "Add SYS_ADMIN/NET_RAW, host podman socket, and disable seccomp (unsafe in a sandbox)"',
        1)
    print("added --additional-flags, --network, --unsafe-caps", file=sys.stderr)

open(path, "w").write(src)
PYEOF

# --- 2: honour --network -----------------------------------------------------
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = '    # Share network namepace with host.\n    arguments+=("--network" "host")'
new = ('    # Network namespace. Defaults to host for upstream compatibility, but a\n'
       '    # named network is what allows egress filtering: traffic has to cross a\n'
       '    # bridge before nftables can see it.\n'
       '    arguments+=("--network" "${program_options["network"]:-host}")')
if old in src:
    src = src.replace(old, new, 1)
    print("made --network selectable", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- 3: gate the dangerous capabilities --------------------------------------
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()

old = """    # Add 'NET_RAW' support, to be able to use ping
    arguments+=("--cap-add=NET_RAW")

    # Add 'SYS_ADMIN' support, to be able to use CPU profiiling.
    arguments+=("--cap-add=SYS_ADMIN")"""
new = """    # NET_RAW (ping) and SYS_ADMIN (CPU profiling) are opt-in. SYS_ADMIN in
    # particular lets a container modify the host's nftables ruleset, which
    # would make the workspace egress policy unenforceable.
    if argsparse_is_option_set "unsafe-caps"; then
        arguments+=("--cap-add=NET_RAW")
        arguments+=("--cap-add=SYS_ADMIN")
    fi"""
if old in src:
    src = src.replace(old, new, 1)
    print("gated NET_RAW/SYS_ADMIN behind --unsafe-caps", file=sys.stderr)

old2 = """    # Allow for unprivileged user namespaces (bwrap) to work.
    arguments+=("--security-opt" "unmask=ALL")

    # Required for rr to work.
    arguments+=("--security-opt" "seccomp=unconfined")"""
new2 = """    # unmask=ALL re-exposes /proc and /sys paths the runtime masks by default;
    # seccomp=unconfined removes the syscall filter. Both are needed for bwrap
    # and rr, and both substantially widen the sandbox, so they follow the same
    # opt-in as the capabilities above.
    if argsparse_is_option_set "unsafe-caps"; then
        arguments+=("--security-opt" "unmask=ALL")
        arguments+=("--security-opt" "seccomp=unconfined")
    fi"""
if old2 in src:
    src = src.replace(old2, new2, 1)
    print("gated unmask=ALL/seccomp behind --unsafe-caps", file=sys.stderr)

open(path, "w").write(src)
PYEOF

# --- 1: splice the extra flags in before the image ---------------------------
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
anchor = '    arguments+=("${sdk_repo_qualified}:${container_tag}")'
inject = """    # Caller-supplied podman flags. Must come before the image name, since
    # everything after it is the container's own argv.
    additional_flags="${program_options["additional-flags"]:-}"
    if [ -n "${additional_flags}" ]; then
        arguments+=(${additional_flags})
    fi

"""
if "additional_flags" not in src and anchor in src:
    src = src.replace(anchor, inject + anchor, 1)
    print("spliced --additional-flags into podman create", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- 5: run as the container user, not the invoking user ---------------------
# wkdev-enter derives the exec user from `id` on the host. That is right for
# upstream's rootless keep-id setup, where host and container uids match. Here
# the SDK is driven under sudo -- rootful podman is the only way the workspace
# firewall can apply, since rootless pasta traffic never reaches the forward
# chain -- so `id -u` is 0 and every command would run as root inside the
# container, leaving root-owned files all over the overlay.
if [ -f "$ENTER" ]; then
py "$ENTER" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()

old = '''        podman_exec_arguments+=("--user" "$(id --user --real):$(id --group --real)")'''
new = '''        # wk: use the container's own user. WKDEV_CONTAINER_USER is set by the
        # caller; the fallback is the uid the image's user is created with.
        podman_exec_arguments+=("--user" "${WKDEV_CONTAINER_UID:-1000}:${WKDEV_CONTAINER_GID:-1000}")'''
if old in src:
    src = src.replace(old, new, 1)
    print("wkdev-enter: exec as the container user", file=sys.stderr)

old2 = '''        podman_exec_arguments+=("/usr/bin/env" "USER=$(id --user --name)" "${SHELL}" "--login")'''
new2 = '''        # wk: ${SHELL} is the *host* shell path and need not exist in the
        # container; and USER must be the container account, not the caller.
        podman_exec_arguments+=("/usr/bin/env" "USER=${WKDEV_CONTAINER_USER:-$(id --user --name)}" "${WKDEV_CONTAINER_SHELL:-/bin/bash}" "--login")'''
if old2 in src:
    src = src.replace(old2, new2, 1)
    print("wkdev-enter: use the container shell and user", file=sys.stderr)

open(path, "w").write(src)
PYEOF
fi

# --- 4a: let .wkdev-init accept a user it is about to create ------------------
# type:username makes argsparse validate --user against the *container's*
# /etc/passwd at parse time -- before any task runs. The image deliberately
# userdel's uid 1000, so the account does not exist yet and init aborts with
# "Invalid value for option user" before it can create it. Dropping the type
# check is what lets try_ensure_user_exists below ever get a chance to run.
if [ -f "$INIT" ]; then
py "$INIT" <<'PYEOF'
import re, sys
path = sys.argv[1]
src = open(path).read()

# Match on structure, not on exact whitespace: the user and group lines are
# aligned with different numbers of spaces and an exact-string patch silently
# skips one of them.
new_src, n = re.subn(
    r'(argsparse_use_option\s+=(?:user|group):\s+"[^"]*")\s+type:\w+',
    r'\1',
    src,
)
if n:
    print("relaxed .wkdev-init user/group validation (%d)" % n, file=sys.stderr)
    open(path, "w").write(new_src)
PYEOF
fi

# --- 4: create the container user when absent --------------------------------
if [ -f "$INIT" ]; then
py "$INIT" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
if "wk: create the user" not in src:
    anchor = "try_switch_shell_for_user() {"
    add = '''try_ensure_user_exists() {

    # wk: create the user if podman did not. The image userdel's uid 1000 so
    # that rootless --userns keep-id can map the host user in; under rootful
    # podman keep-id is unavailable, nothing recreates the passwd entry, and
    # every later task that looks the user up aborts.
    if getent passwd "${container_user_name}" >/dev/null; then
        return 0
    fi

    # Take the ids from whoever owns the home directory that was bind-mounted
    # in. Rootful podman maps uids 1:1, so these must match the host side or the
    # user cannot write to its own home, the shared caches, or the checkout.
    local _uid _gid
    _uid=$(stat -c %u "/home/${container_user_name}" 2>/dev/null || echo 1000)
    _gid=$(stat -c %g "/home/${container_user_name}" 2>/dev/null || echo 1000)

    task_step "Creating user ${container_user_name} (${_uid}:${_gid})"
    groupadd --force --gid "${_gid}" "${container_group_name}" 2>/dev/null || true
    useradd --uid "${_uid}" --gid "${_gid}" --no-create-home \\
            --home-dir "/home/${container_user_name}" \\
            --shell /bin/bash "${container_user_name}" 2>/dev/null || true
}

'''
    src = src.replace(anchor, add + anchor, 1)
    src = src.replace('    "try_switch_shell_for_user"',
                      '    "try_ensure_user_exists"\n    "try_switch_shell_for_user"', 1)
    print("added try_ensure_user_exists", file=sys.stderr)
    open(path, "w").write(src)
PYEOF
fi

# --- 6: do not hand the container the host podman socket ---------------------
# wkdev-create mounts the host's podman socket so podman-in-podman works. Under
# rootful podman that is a complete sandbox escape: anything holding that socket
# can start a privileged container and own the machine. Gated behind the same
# opt-in as the other sandbox-widening features.
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = "    try_process_podman ${1}"
new = """    # wk: the host podman socket is a sandbox escape under rootful podman.
    argsparse_is_option_set "unsafe-caps" && try_process_podman ${1}"""
if old in src and "sandbox escape under rootful" not in src:
    src = src.replace(old, new, 1)
    print("gated host podman socket behind --unsafe-caps", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- 7: let init skip apt when egress is restricted --------------------------
# .wkdev-init runs `apt-get update` and can install packages. The workspace
# egress policy allows only the Anthropic API, GitHub and the Pi test devices,
# so every Ubuntu mirror is unreachable and those tasks stall until apt times
# out on each repository in turn.
#
# Widening the firewall is not a good trade: Ubuntu's archive is CDN-hosted
# across a large, shifting address space, and allowlisting it would punch a
# broad hole for the sake of packages the SDK image already contains. So the
# apt tasks become opt-in via WKDEV_OFFLINE, which wk sets.
py "$INIT" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()

if "WKDEV_OFFLINE" not in src:
    for task in ("try_update_apt_cache", "try_install_shell_package",
                 "try_install_optional_drivers", "try_install_additional_packages"):
        old = '    "%s"' % task
        new = '    $([ -n "${WKDEV_OFFLINE:-}" ] || echo "%s")' % task
        if old in src:
            src = src.replace(old, new, 1)
    print("apt tasks now skipped when WKDEV_OFFLINE is set", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- 8: let podman manage resolv.conf on a bridge network --------------------
# wkdev-create bind-mounts the host's /etc/resolv.conf into the container. On
# upstream's --network host that is right -- same namespace, same resolver.
#
# On a bridge it is actively wrong: the container inherits the *host's*
# nameserver address, which it can only reach through the forward chain, where
# the egress policy drops it. Hostname lookups then fail while raw IPs still
# work -- a genuinely confusing failure mode.
#
# Without the mount, podman points the container at aardvark-dns on the bridge
# gateway. That is input traffic, not forward, so DNS works without opening
# port 53 to the world.
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = '    arguments+=("--mount" "type=bind,source=/etc/resolv.conf,destination=/etc/resolv.conf,ro")'
new = (
    '    # wk: host networking only -- see sdk-patches/apply.sh section 8.\n'
    '    if [ "${program_options["network"]:-host}" = "host" ]; then\n'
    '        arguments+=("--mount" "type=bind,source=/etc/resolv.conf,destination=/etc/resolv.conf,ro")\n'
    '    fi'
)
if old in src:
    src = src.replace(old, new, 1)
    print("resolv.conf mount limited to host networking", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- 9: quieten flatpak runtime-state syncing --------------------------------
# .wkdev-sync-runtime-state runs on every wkdev-enter and unconditionally
# symlinks flatpak instance directories from /host/run. In this setup /host/run
# is the VM's runtime dir and those directories never exist, so every single
# `wk run`, `wk build` and `wk enter` prints three bare "ln: No such file or
# directory" lines with no context. That is pure noise in front of real output,
# and it trains you to ignore errors from this script -- which is exactly what
# you do not want when a genuinely broken mount shows up here later.
if [ -f "$SYNC" ]; then
py "$SYNC" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = """mount_host_flatpak_instance_data() {"""
new = """mount_host_flatpak_instance_data() {

    # wk: the whole function is meaningless without a host runtime directory to
    # share, which is the case for every headless container here. Left enabled
    # it emits bare "ln: No such file or directory" lines before the output of
    # every wk run/build/enter -- noise that teaches you to ignore errors from
    # this script.
    [ -d /host/run ] || return 0
"""
if old in src and "the whole function is meaningless" not in src:
    src = src.replace(old, new, 1)
    print("quietened flatpak runtime-state syncing", file=sys.stderr)
    open(path, "w").write(src)
PYEOF
fi

# --- 10: XDG_RUNTIME_DIR must match the container user -----------------------
# wkdev-create derives it from `id --user --real` on the invoking side. Upstream
# runs rootless with keep-id, so that uid is also the container's. Here the SDK
# is driven under sudo (rootful podman is what makes the egress firewall
# enforceable), so the invoking uid is 0 and every container gets
# XDG_RUNTIME_DIR=/run/user/0 -- a directory that does not exist, while the real
# one is /run/user/<container uid>.
#
# The visible symptom is bare "ln: No such file or directory" lines before the
# output of every command, but the actual breakage is wider: anything that
# writes runtime state (dbus, wayland, pipewire, and assorted tooling) lands in
# a path that is not there.
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()
old = '    arguments+=("--env" "XDG_RUNTIME_DIR=/run/user/${host_user_id}")'
new = ('    # wk: the container user\'s uid, not the invoking user\'s -- see\n'
       '    # sdk-patches/apply.sh section 10.\n'
       '    arguments+=("--env" "XDG_RUNTIME_DIR=/run/user/${WKDEV_CONTAINER_UID:-${host_user_id}}")')
if old in src:
    src = src.replace(old, new, 1)
    print("XDG_RUNTIME_DIR now follows the container user", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# --- verify ------------------------------------------------------------------
fail=0
for token in "additional-flags" "unsafe-caps" 'program_options\["network"\]'; do
    grep -q "$token" "$CREATE" || { echo "verify failed: $token missing from wkdev-create" >&2; fail=1; }
done
bash -n "$CREATE" || fail=1
[ -f "$ENTER" ] && { bash -n "$ENTER" || fail=1; }
[ -f "$INIT" ] && { bash -n "$INIT" || fail=1; }
[ -f "$SYNC" ] && { bash -n "$SYNC" || fail=1; }

[ "$fail" -eq 0 ] && echo "SDK patched and syntax-checked: $SDK"
exit $fail
