#!/usr/bin/env bash
#
# Patch the webkit-container-sdk fork so wkdev-create can produce a sandboxed
# workspace. Idempotent: safe to re-run, and it verifies rather than assumes.
#
# Written as an applier rather than a .patch file because these edits need to
# survive rebasing the fork onto upstream, and patch fuzz on a moving target is
# worse than a script that checks what it is editing.
#
# Fourteen numbered sections, each independently upstreamable. The load-bearing
# ones for the sandbox: 2 (--network selectable, so --network none is possible),
# 3 (SYS_ADMIN/NET_RAW/seccomp opt-in), 6 (no host podman socket), 11
# (--isolated: no session D-Bus, keyring, host home or runtime dir). The rest
# are plumbing (--additional-flags, user creation, offline apt, resolv.conf,
# runtime-dir ownership, log noise).
#
# Some section comments mention the retired rootful-podman/nftables model they
# were written against; the sections still apply under the current rootless
# --network none design, and their guards keep the vestigial ones inert when
# upstream no longer has the line they patch.
#
# Every security-relevant section has a token in the verify list at the bottom.
# That is not optional: a section whose anchor text drifts upstream silently
# no-ops, and an unverified hardening patch that no-ops is a sandbox hole that
# looks exactly like a clean run.

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
# The replacement re-indents the line it replaces, so the original text is still
# a substring of the result: without a marker check this section re-applies to
# its own output on every run. `git reset --hard` before patching hid that.
already = "wk: host networking only" in src
new = (
    '    # wk: host networking only -- see sdk-patches/apply.sh section 8.\n'
    '    if [ "${program_options["network"]:-host}" = "host" ]; then\n'
    '        arguments+=("--mount" "type=bind,source=/etc/resolv.conf,destination=/etc/resolv.conf,ro")\n'
    '    fi'
)
if old in src and not already:
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

# --- 11: --isolated, for a workspace that is not a desktop session ----------
# wkdev's premise is tight integration: the container is handed the session and
# system D-Bus sockets, the keyring, dconf, the X11 socket, PulseAudio, the
# journal, and -- via ${XDG_RUNTIME_DIR} mounted at /host/run -- everything else
# in the user's runtime directory. That is exactly right for the tool's intended
# use, where the container is a nicer place to run your own desktop apps from.
#
# It is exactly wrong for an unattended agent. The session bus alone is a full
# host escape: org.freedesktop.systemd1's StartTransientUnit runs any command
# outside the container, as the user, whatever the container's network policy
# says. Mounting ${HOME} at /host/home hands over the home directory the design
# claims a workspace cannot see. Neither is a network problem, so neither is
# addressed by any firewall.
#
# --isolated turns the whole group off. Display integration is deliberately NOT
# in it -- benchmarks need the GPU and a compositor socket, and wk passes those
# itself, one socket at a time, rather than by sharing a directory.
py "$CREATE" <<'PYEOF'
import sys
path = sys.argv[1]
src = open(path).read()

if "isolated" not in src:
    anchor = 'argsparse_use_option unsafe-caps'
    line = src[src.index(anchor):].split("\n")[0]
    src = src.replace(line, line + "\n" +
        'argsparse_use_option isolated "Do not share the host session with the container '
        '(no D-Bus, keyring, dconf, journal, X11, PulseAudio, host home or runtime dir)"',
        1)
    print("added --isolated", file=sys.stderr)

# Host-session integration, one call per line in a single block.
for task in ("try_process_coredump_directory", "try_process_journal",
             "try_process_keyring", "try_process_system_bus",
             "try_process_session_bus", "try_process_dconf",
             "try_process_accessibility", "try_process_x11",
             "try_process_pulseaudio", "try_process_dri",
             "try_process_nvidia_gpu"):
    old = "    %s ${1}" % task
    new = '    argsparse_is_option_set "isolated" || %s ${1}' % task
    if old in src and new not in src:
        src = src.replace(old, new, 1)

# The home directory mount is still wanted -- that is the workspace's own home.
# What must go is the second mount, of the *host* home, and the systemd user
# unit directory that comes with it.
old_home = '    podman_argument+=("--mount" "type=bind,source=${HOME},destination=/host/home/${container_user_name},rslave")'
new_home = ('    # wk: the host home directory is not the workspace\'s business.\n'
            '    argsparse_is_option_set "isolated" || \\\n'
            + old_home)
if old_home in src and "not the workspace's business" not in src:
    src = src.replace(old_home, new_home, 1)

# This one needs an `if`, not the `||` prefix the other two use, and the
# difference is not cosmetic. The original line is itself a compound:
#
#     [ -d ... ] && podman_argument+=(...)
#
# and `||` and `&&` have equal precedence in shell, left to right. So
# `guard || [ -d ... ] && mount` parses as `(guard || [ -d ... ]) && mount`:
# when --isolated IS set the guard succeeds, the whole left side is true, and
# the mount is added -- the exact opposite of what the gate is for.
#
# The symptom is indirect enough to be worth writing down. The mount makes
# podman create /home/<user>/.config inside the container as container-root,
# which under keep-id is an unmapped subordinate uid on the host. The
# workspace user then cannot chmod its own ~/.config, the helix install in
# firstrun.sh fails on it, `set -e` aborts the rest of firstrun, and the
# workspace comes up with no lldb config and no shell rc -- while wkdev-init
# carries on and reports success.
old_sysd = '    [ -d "${HOME}/.config/systemd/user" ] && podman_argument+=("--mount" "type=bind,source=${HOME}/.config/systemd/user,destination=/home/${container_user_name}/.config/systemd/user,rslave")'
if old_sysd in src and "isolated-systemd-user" not in src:
    src = src.replace(old_sysd,
        '    # wk: isolated-systemd-user -- writing host unit files is a host escape.\n'
        '    if ! argsparse_is_option_set "isolated"; then\n'
        '    ' + old_sysd + '\n'
        '    fi', 1)

# ${XDG_RUNTIME_DIR} at /host/run: the session bus, the keyring socket, the
# pipewire sockets and whatever else the session happens to keep there.
old_run = '    arguments+=("--mount" "type=bind,source=${XDG_RUNTIME_DIR},destination=/host/run,bind-propagation=rslave")'
new_run = ('    # wk: sharing the whole runtime directory shares the session bus with it.\n'
           '    argsparse_is_option_set "isolated" || \\\n' + old_run)
if old_run in src and "shares the session bus" not in src:
    src = src.replace(old_run, new_run, 1)

open(path, "w").write(src)
print("gated host-session integration behind --isolated", file=sys.stderr)
PYEOF

# --- 12: pin the image explicitly -------------------------------------------
# wkdev-create resolves the image from the SDK version and, when --arch is
# given, falls back to a `_${arch}` tag *only if the unsuffixed tag is missing
# locally*. On a machine that already has the current aarch64 image -- which is
# every machine that has ever created a workspace -- the fallback never fires,
# so `--arch arm` hands podman an arm64 image and asks for a 32-bit container
# from it. podman then either refuses or, worse, matches a multiarch manifest
# and produces something that is not the image anyone meant.
#
# --image says which image, and nothing infers it. That is also what keeps the
# native-armhf and the cross-sysroot images apart: they differ by a tag suffix
# (24.04_arm32 vs 24.04_arm32_arm64) and by nothing else visible from here.
py "$CREATE" <<'PYSECTION12'
import sys
path = sys.argv[1]
src = open(path).read()

if "argsparse_use_option =image:" not in src:
    anchor = 'argsparse_use_option =arch:'
    line = src[src.index(anchor):].split("\n")[0]
    src = src.replace(line, line + "\n" +
        'argsparse_use_option =image: "Exact image to use (repository:tag), '
        'instead of resolving one from the SDK version"',
        1)
    print("added --image", file=sys.stderr)

anchor = '    if argsparse_is_option_set "arch"; then'
inject = """    # wk: an explicitly pinned image, which no version resolution or
    # architecture fallback may then second-guess. See sdk-patches/apply.sh 12.
    if argsparse_is_option_set "image"; then
        sdk_repo_qualified="${program_options["image"]%:*}"
        container_tag="${program_options["image"]##*:}"
    fi

"""
if "an explicitly pinned image" not in src and anchor in src:
    src = src.replace(anchor, inject + anchor, 1)
    print("spliced --image into image resolution", file=sys.stderr)

open(path, "w").write(src)
PYSECTION12

# --- 13: let a non-SDK container be initialised ------------------------------
# `.wkdev-init` refuses to run outside a wkdev-sdk container, and the test is
# `is_running_in_container && [ -f /usr/bin/podman-host ]` -- that file being
# something the SDK image ships. The guard is right: running it on a host would
# be a mess.
#
# But the Yocto builder needs a workspace whose image is *not* the SDK. Yocto
# scarthgap's supported build hosts stop at Ubuntu 24.04 and the SDK image is
# 26.04, and that gap breaks the build in five different ways
# (container/yocto/Containerfile). So `wk sysimage build <a yocto profile>` creates
# its workspace from a plain 24.04 image with Yocto's host tooling in it, and
# `.wkdev-init` then aborts with "intended to run from within the wkdev-sdk
# container only".
#
# The fix is a second discriminator rather than a fake `/usr/bin/podman-host`:
# our image writes `/etc/wk-container`, and that is accepted too. Writing a stub
# podman-host would have been shorter and would have been a lie -- that file is
# a working podman wrapper in the SDK image, and something would eventually call
# it.
#
# Additive and host-safe by construction: the added clause is another `-f` test
# on a path that does not exist outside such a container, so on the host and in
# a real SDK container the answer is exactly what it was.
SETTINGS="$SDK/utilities/settings.sh"
if [ -f "$SETTINGS" ]; then
    py "$SETTINGS" <<'PYSECTION13'
import sys
path = sys.argv[1]
src = open(path).read()

old = 'is_running_in_wkdev_sdk_container() { is_running_in_container && [ -f "/usr/bin/podman-host" ]; }'
new = ('# wk: /etc/wk-container marks a container wk built for a workspace whose\n'
       '# image is deliberately not the SDK -- see sdk-patches/apply.sh 13.\n'
       'is_running_in_wkdev_sdk_container() { is_running_in_container && '
       '{ [ -f "/usr/bin/podman-host" ] || [ -f "/etc/wk-container" ]; }; }')

if "/etc/wk-container" not in src and old in src:
    src = src.replace(old, new, 1)
    print("accepted /etc/wk-container as a wk-built container", file=sys.stderr)

open(path, "w").write(src)
PYSECTION13
fi

# --- 14: ask podman.sh's question, not the general one -----------------------
# `utilities/podman.sh` selects the podman binary with
# `is_running_in_wkdev_sdk_container`, then unconditionally requires `systemctl`
# and a working podman. Its own comment says what it actually depends on:
# "Requires the presence of /usr/bin/podman-host in the container image."
#
# Those are two different questions, and section 13 made the difference visible
# by conflating them: teaching the general guard about `/etc/wk-container` made
# a Yocto build workspace claim podman-host integration it does not have, and
# `.wkdev-init` then died with "Cannot find required 'systemctl' executable".
#
# So this asks the narrower question in the place that means it. A container
# with no podman-host has no podman to reach and nothing here needs one, so the
# requirement is skipped rather than aborted on. Host behaviour is byte-for-byte
# what it was -- `is_running_in_container` is false there, so the verify still
# runs -- and a real SDK container still has podman-host, so it still runs
# there too. Upstreamable on its own: it makes the code agree with its comment.
PODMANSH="$SDK/utilities/podman.sh"
if [ -f "$PODMANSH" ]; then
    py "$PODMANSH" <<'PYSECTION14'
import sys
path = sys.argv[1]
src = open(path).read()

old_sel = (
    'if is_running_in_wkdev_sdk_container; then\n'
    '    # Requires the presence of /usr/bin/podman-host in the container image.\n'
    '    # It acts as portal to access the host podman instance.\n'
    '    podman_executable="/usr/bin/podman-host"\n'
    'fi'
)
new_sel = (
    '# wk: the file itself, which is what the comment below has always said this\n'
    '# depends on -- not "is this a container the SDK may initialise". See\n'
    '# sdk-patches/apply.sh 14.\n'
    'if [ -f "/usr/bin/podman-host" ]; then\n'
    '    # Requires the presence of /usr/bin/podman-host in the container image.\n'
    '    # It acts as portal to access the host podman instance.\n'
    '    podman_executable="/usr/bin/podman-host"\n'
    'fi'
)

old_v = 'verify_executables_exist systemctl\n\nverify_podman_is_acceptable "${podman_executable}"'
new_v = (
    '# wk: a container with no podman-host has no podman to reach, and nothing in\n'
    '# this file needs one there. On the host, and in a real SDK container, both\n'
    '# checks run exactly as before.\n'
    'if ! is_running_in_container || [ -f "/usr/bin/podman-host" ]; then\n'
    '    verify_executables_exist systemctl\n'
    '\n'
    '    verify_podman_is_acceptable "${podman_executable}"\n'
    'fi'
)

changed = False
if 'not "is this a container the SDK may initialise"' not in src and old_sel in src:
    src = src.replace(old_sel, new_sel, 1); changed = True
if 'has no podman to reach' not in src and old_v in src:
    src = src.replace(old_v, new_v, 1); changed = True
if changed:
    print("scoped podman.sh to podman-host", file=sys.stderr)

open(path, "w").write(src)
PYSECTION14
fi

# --- verify ------------------------------------------------------------------
# One token per section that matters, matching text the section *writes*, so a
# section that silently no-opped (upstream reformatted its anchor) fails here
# rather than shipping an unhardened script that looks patched.
fail=0
for token in \
    "additional-flags" \
    'program_options\["network"\]' \
    'egress policy unenforceable' \
    'argsparse_is_option_set "unsafe-caps"' \
    'sandbox escape under rootful' \
    'wk: host networking only' \
    'WKDEV_CONTAINER_UID' \
    'argsparse_is_option_set "isolated" || try_process_session_bus' \
    "not the workspace's business" \
    'isolated-systemd-user' \
    'shares the session bus' \
    'an explicitly pinned image'
do
    grep -q "$token" "$CREATE" || { echo "verify failed: $token missing from wkdev-create" >&2; fail=1; }
done
grep -q "WKDEV_OFFLINE" "$INIT" 2>/dev/null \
    || { echo "verify failed: WKDEV_OFFLINE missing from .wkdev-init" >&2; fail=1; }
if [ -f "$SDK/utilities/podman.sh" ]; then
    grep -q "has no podman to reach" "$SDK/utilities/podman.sh" \
        || { echo "verify failed: podman.sh not scoped to podman-host (section 14)" >&2; fail=1; }
    bash -n "$SDK/utilities/podman.sh" || fail=1
fi
if [ -f "$SDK/utilities/settings.sh" ]; then
    grep -q "/etc/wk-container" "$SDK/utilities/settings.sh" \
        || { echo "verify failed: /etc/wk-container missing from settings.sh (section 13)" >&2; fail=1; }
    bash -n "$SDK/utilities/settings.sh" || fail=1
fi
bash -n "$CREATE" || fail=1
[ -f "$ENTER" ] && { bash -n "$ENTER" || fail=1; }
[ -f "$INIT" ] && { bash -n "$INIT" || fail=1; }
[ -f "$SYNC" ] && { bash -n "$SYNC" || fail=1; }

[ "$fail" -eq 0 ] && echo "SDK patched and syntax-checked: $SDK"
exit $fail
