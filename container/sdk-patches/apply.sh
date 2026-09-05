#!/usr/bin/env bash
# Idempotent applier (not a .patch file, so the edits survive rebasing the fork onto upstream); sections 2, 3, 6 and 11 are load-bearing, and each section has a verify token below because one that no-ops reads as a clean run.

set -euo pipefail

SDK="${1:-${WKDEV_SDK:-/opt/webkit-container-sdk}}"
CREATE="$SDK/scripts/host-only/wkdev-create"
ENTER="$SDK/scripts/host-only/wkdev-enter"
INIT="$SDK/scripts/container-only/.wkdev-init"
SYNC="$SDK/scripts/container-only/.wkdev-sync-runtime-state"

[ -f "$CREATE" ] || { echo "not an SDK checkout: $SDK" >&2; exit 1; }

py() { python3 - "$@"; }

py "$CREATE" <<'PYEOF'   # 1 + 2: new options
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

py "$CREATE" <<'PYEOF'   # 2: honour --network
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

py "$CREATE" <<'PYEOF'   # 3: gate the dangerous capabilities
import sys
path = sys.argv[1]
src = open(path).read()

old = (
    "    # Add 'NET_RAW' support, to be able to use ping\n"
    '    arguments+=("--cap-add=NET_RAW")\n'
    '\n'
    "    # Add 'SYS_ADMIN' support, to be able to use CPU profiiling.\n"
    '    arguments+=("--cap-add=SYS_ADMIN")'
)
new = (
    '    # NET_RAW (ping) and SYS_ADMIN (CPU profiling) are opt-in. SYS_ADMIN in\n'
    "    # particular lets a container modify the host's nftables ruleset, which\n"
    '    # would make the workspace egress policy unenforceable.\n'
    '    if argsparse_is_option_set "unsafe-caps"; then\n'
    '        arguments+=("--cap-add=NET_RAW")\n'
    '        arguments+=("--cap-add=SYS_ADMIN")\n'
    '    fi'
)
if old in src:
    src = src.replace(old, new, 1)
    print("gated NET_RAW/SYS_ADMIN behind --unsafe-caps", file=sys.stderr)

old2 = (
    '    # Allow for unprivileged user namespaces (bwrap) to work.\n'
    '    arguments+=("--security-opt" "unmask=ALL")\n'
    '\n'
    '    # Required for rr to work.\n'
    '    arguments+=("--security-opt" "seccomp=unconfined")'
)
new2 = (
    '    # unmask=ALL re-exposes /proc and /sys paths the runtime masks by default;\n'
    '    # seccomp=unconfined removes the syscall filter. Both are needed for bwrap\n'
    '    # and rr, and both substantially widen the sandbox, so they follow the same\n'
    '    # opt-in as the capabilities above.\n'
    '    if argsparse_is_option_set "unsafe-caps"; then\n'
    '        arguments+=("--security-opt" "unmask=ALL")\n'
    '        arguments+=("--security-opt" "seccomp=unconfined")\n'
    '    fi'
)
if old2 in src:
    src = src.replace(old2, new2, 1)
    print("gated unmask=ALL/seccomp behind --unsafe-caps", file=sys.stderr)

open(path, "w").write(src)
PYEOF

py "$CREATE" <<'PYEOF'   # 1: splice the extra flags in before the image
import sys
path = sys.argv[1]
src = open(path).read()
anchor = '    arguments+=("${sdk_repo_qualified}:${container_tag}")'
inject = (
    '    # Caller-supplied podman flags. Must come before the image name, since\n'
    "    # everything after it is the container's own argv.\n"
    '    additional_flags="${program_options["additional-flags"]:-}"\n'
    '    if [ -n "${additional_flags}" ]; then\n'
    '        arguments+=(${additional_flags})\n'
    '    fi\n'
    '\n'
)
if "additional_flags" not in src and anchor in src:
    src = src.replace(anchor, inject + anchor, 1)
    print("spliced --additional-flags into podman create", file=sys.stderr)
    open(path, "w").write(src)
PYEOF

# Under sudo (rootful podman) `id --user --real` is 0: not the exec user, and /run/user/0 does not exist.
if [ -f "$ENTER" ]; then
py "$ENTER" <<'PYEOF'   # 5: run as the container user, not the invoking user
import sys
path = sys.argv[1]
src = open(path).read()

old = '''        podman_exec_arguments+=("--user" "$(id --user --real):$(id --group --real)")'''
new = (
    "        # wk: use the container's own user. WKDEV_CONTAINER_USER is set by the\n"
    "        # caller; the fallback is the uid the image's user is created with.\n"
    '        podman_exec_arguments+=("--user" "${WKDEV_CONTAINER_UID:-1000}:${WKDEV_CONTAINER_GID:-1000}")'
)
if old in src:
    src = src.replace(old, new, 1)
    print("wkdev-enter: exec as the container user", file=sys.stderr)

old2 = '''        podman_exec_arguments+=("/usr/bin/env" "USER=$(id --user --name)" "${SHELL}" "--login")'''
new2 = (
    '        # wk: ${SHELL} is the *host* shell path and need not exist in the\n'
    '        # container; and USER must be the container account, not the caller.\n'
    '        podman_exec_arguments+=("/usr/bin/env" "USER=${WKDEV_CONTAINER_USER:-$(id --user --name)}" "${WKDEV_CONTAINER_SHELL:-/bin/bash}" "--login")'
)
if old2 in src:
    src = src.replace(old2, new2, 1)
    print("wkdev-enter: use the container shell and user", file=sys.stderr)

open(path, "w").write(src)
PYEOF
fi

if [ -f "$INIT" ]; then
py "$INIT" <<'PYEOF'   # 4a: let .wkdev-init accept a user it is about to create
import re, sys
path = sys.argv[1]
src = open(path).read()

# On structure: the user and group lines are aligned differently upstream.
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

if [ -f "$INIT" ]; then
py "$INIT" <<'PYEOF'   # 4: create the container user when absent
import sys
path = sys.argv[1]
src = open(path).read()
if "wk: create the user" not in src:
    anchor = "try_switch_shell_for_user() {"
    add = (
        'try_ensure_user_exists() {\n'
        '\n'
        "    # wk: create the user if podman did not (the image userdel's uid 1000\n"
        '    # for rootless keep-id; under rootful podman nothing recreates it).\n'
        '    if getent passwd "${container_user_name}" >/dev/null; then\n'
        '        return 0\n'
        '    fi\n'
        '\n'
        '    # Ids from whoever owns the bind-mounted home: rootful podman maps uids\n'
        '    # 1:1, so these must match the host side or nothing here is writable.\n'
        '    local _uid _gid\n'
        '    _uid=$(stat -c %u "/home/${container_user_name}" 2>/dev/null || echo 1000)\n'
        '    _gid=$(stat -c %g "/home/${container_user_name}" 2>/dev/null || echo 1000)\n'
        '\n'
        '    task_step "Creating user ${container_user_name} (${_uid}:${_gid})"\n'
        '    groupadd --force --gid "${_gid}" "${container_group_name}" 2>/dev/null || true\n'
        '    useradd --uid "${_uid}" --gid "${_gid}" --no-create-home \\\n'
        '            --home-dir "/home/${container_user_name}" \\\n'
        '            --shell /bin/bash "${container_user_name}" 2>/dev/null || true\n'
        '}\n'
        '\n'
    )
    src = src.replace(anchor, add + anchor, 1)
    src = src.replace('    "try_switch_shell_for_user"',
                      '    "try_ensure_user_exists"\n    "try_switch_shell_for_user"', 1)
    print("added try_ensure_user_exists", file=sys.stderr)
    open(path, "w").write(src)
PYEOF
fi

py "$CREATE" <<'PYEOF'   # 6: do not hand over the host podman socket
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

py "$INIT" <<'PYEOF'   # 7: let init skip apt when egress is restricted
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

# The host's resolv.conf names a nameserver reachable only through the dropped forward chain; unmounted, podman points at aardvark-dns, which is input.
py "$CREATE" <<'PYEOF'   # 8: let podman manage resolv.conf on a bridge
import sys
path = sys.argv[1]
src = open(path).read()
old = '    arguments+=("--mount" "type=bind,source=/etc/resolv.conf,destination=/etc/resolv.conf,ro")'
# The replacement re-indents its own input, so this needs a marker check.
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

if [ -f "$SYNC" ]; then
py "$SYNC" <<'PYEOF'   # 9: quieten flatpak runtime-state syncing
import sys
path = sys.argv[1]
src = open(path).read()
old = """mount_host_flatpak_instance_data() {"""
new = (
    'mount_host_flatpak_instance_data() {\n'
    '\n'
    '    # wk: the whole function is meaningless without a host runtime directory to\n'
    '    # share, which is the case for every headless container here. Left enabled\n'
    '    # it emits bare "ln: No such file or directory" lines before the output of\n'
    '    # every wk run/build/enter -- noise that teaches you to ignore errors from\n'
    '    # this script.\n'
    '    [ -d /host/run ] || return 0\n'
)
if old in src and "the whole function is meaningless" not in src:
    src = src.replace(old, new, 1)
    print("quietened flatpak runtime-state syncing", file=sys.stderr)
    open(path, "w").write(src)
PYEOF
fi

py "$CREATE" <<'PYEOF'   # 10: XDG_RUNTIME_DIR must match the container user
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

# The session bus alone is a host escape: StartTransientUnit runs anything.
py "$CREATE" <<'PYEOF'   # 11: --isolated, for a workspace that is no session
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

old_home = '    podman_argument+=("--mount" "type=bind,source=${HOME},destination=/host/home/${container_user_name},rslave")'
new_home = ('    # wk: the host home directory is not the workspace\'s business.\n'
            '    argsparse_is_option_set "isolated" || \\\n'
            + old_home)
if old_home in src and "not the workspace's business" not in src:
    src = src.replace(old_home, new_home, 1)

# An `if`, not the `||` the other two use: this line is itself a compound, and
# `guard || [ -d x ] && mount` mounts exactly when --isolated is set.
old_sysd = '    [ -d "${HOME}/.config/systemd/user" ] && podman_argument+=("--mount" "type=bind,source=${HOME}/.config/systemd/user,destination=/home/${container_user_name}/.config/systemd/user,rslave")'
if old_sysd in src and "isolated-systemd-user" not in src:
    src = src.replace(old_sysd,
        '    # wk: isolated-systemd-user -- writing host unit files is a host escape.\n'
        '    if ! argsparse_is_option_set "isolated"; then\n'
        '    ' + old_sysd + '\n'
        '    fi', 1)

old_run = '    arguments+=("--mount" "type=bind,source=${XDG_RUNTIME_DIR},destination=/host/run,bind-propagation=rslave")'
new_run = ('    # wk: sharing the whole runtime directory shares the session bus with it.\n'
           '    argsparse_is_option_set "isolated" || \\\n' + old_run)
if old_run in src and "shares the session bus" not in src:
    src = src.replace(old_run, new_run, 1)

open(path, "w").write(src)
print("gated host-session integration behind --isolated", file=sys.stderr)
PYEOF

py "$CREATE" <<'PYSECTION12'   # 12: pin the image explicitly
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
inject = (
    '    # wk: an explicitly pinned image, which no version resolution or\n'
    '    # architecture fallback may then second-guess. See sdk-patches/apply.sh 12.\n'
    '    if argsparse_is_option_set "image"; then\n'
    '        sdk_repo_qualified="${program_options["image"]%:*}"\n'
    '        container_tag="${program_options["image"]##*:}"\n'
    '    fi\n'
    '\n'
)
if "an explicitly pinned image" not in src and anchor in src:
    src = src.replace(anchor, inject + anchor, 1)
    print("spliced --image into image resolution", file=sys.stderr)

open(path, "w").write(src)
PYSECTION12

SETTINGS="$SDK/utilities/settings.sh"
if [ -f "$SETTINGS" ]; then
    py "$SETTINGS" <<'PYSECTION13'   # 13: let a non-SDK container initialise
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

PODMANSH="$SDK/utilities/podman.sh"
if [ -f "$PODMANSH" ]; then
    py "$PODMANSH" <<'PYSECTION14'   # 14: ask podman.sh's own question
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

fail=0                                  # one token per section that matters
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
