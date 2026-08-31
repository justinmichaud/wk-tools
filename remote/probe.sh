# What a shared build machine has, as key=value lines. Runs *on* the machine.
#
# Sent over ssh appended to remote/deps.sh, which defines the three functions
# below -- so the list of tools has exactly one home and this script cannot
# drift from it:
#
#     cat remote/deps.sh remote/probe.sh | ssh <host> bash -s
#
# Self-contained on purpose: it sources nothing and needs no wk-tools on the
# far side, so it answers about a machine that has never been provisioned and
# about one whose pushed tooling is stale.
#
# Read-only, and nothing here may prompt: `wk doctor` reads it.

printf 'host=%s\n' "$(hostname 2>/dev/null || echo '?')"

_id=""; _like=""; _pretty=""
if [ -r /etc/os-release ]; then
    # In a subshell: /etc/os-release sets ID, VERSION and friends, and this
    # script has its own names to keep.
    eval "$(. /etc/os-release 2>/dev/null; printf '_id=%s\n_like=%s\n_pretty=%s\n' \
        "$(printf '%q' "${ID:-}")" "$(printf '%q' "${ID_LIKE:-}")" \
        "$(printf '%q' "${PRETTY_NAME:-}")")"
fi
printf 'os=%s\n' "${_pretty:-unknown}"
printf 'family=%s\n' "$(wk_remote_family "$_id" "$_like")"
printf 'arch=%s\n' "$(uname -m)"
printf 'cores=%s\n' "$(nproc 2>/dev/null || echo '?')"

# One line per tool whatever its state, so the reader never has to know the list
# to notice one missing: an empty value is "not installed here".
while read -r _tool _need _why; do
    [ -n "$_tool" ] || continue
    printf 'tool.%s=%s\n' "$_tool" "$(command -v "$_tool" 2>/dev/null || true)"
done <<EOF
$(wk_remote_deps)
EOF

# A *login* shell, because that is what a build runs under (t_wk, and
# build/build-in-target.sh through it): a variable exported from
# /etc/profile.d or the account's own profile is set for every build here and
# is invisible from an ordinary `ssh host env`.
for _v in $(wk_remote_build_env_vars); do
    _val=$(bash -lc "printf '%s' \"\${$_v:-}\"" 2>/dev/null || true)
    [ -n "$_val" ] || continue
    printf 'env.%s=%s\n' "$_v" "$_val"
done

# The identity a commit made here would carry, and whether git is fast enough
# in a WebKit checkout for an editor to keep asking (dotfiles/gitconfig, through
# the include remote/provision.sh writes).
if command -v git >/dev/null 2>&1; then
    printf 'git.name=%s\n' "$(git config --get user.name 2>/dev/null || true)"
    printf 'git.email=%s\n' "$(git config --get user.email 2>/dev/null || true)"
    printf 'git.fsmonitor=%s\n' "$(git config --get core.fsmonitor 2>/dev/null || true)"
    printf 'git.manyfiles=%s\n' "$(git config --get feature.manyFiles 2>/dev/null || true)"
fi

# Whether this machine has been provisioned at all, and where it put things.
if [ -f "$HOME/.wk-remote" ]; then
    printf 'marker=yes\n'
    printf 'root=%s\n' "$(sed -n 's/^root=//p' "$HOME/.wk-remote" | tail -1)"
    printf 'target=%s\n' "$(sed -n 's/^target=//p' "$HOME/.wk-remote" | tail -1)"
else
    printf 'marker=no\n'
fi
