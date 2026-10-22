# What a shared build machine has, as key=value lines. Runs on the machine, sent over ssh appended to remote/deps.sh, which defines the functions it calls: `cat remote/deps.sh remote/probe.sh | ssh <host> bash -s`. Sources nothing, having to answer about an unprovisioned machine, and never prompts.

printf 'host=%s\n' "$(hostname 2>/dev/null || echo '?')"

_id=""; _like=""; _pretty=""
if [ -r /etc/os-release ]; then   # in a subshell: /etc/os-release sets ID and friends, names this script keeps
    eval "$(. /etc/os-release 2>/dev/null; printf '_id=%s\n_like=%s\n_pretty=%s\n' \
        "$(printf '%q' "${ID:-}")" "$(printf '%q' "${ID_LIKE:-}")" \
        "$(printf '%q' "${PRETTY_NAME:-}")")"
fi
printf 'os=%s\n' "${_pretty:-unknown}"
printf 'family=%s\n' "$(wk_remote_family "$_id" "$_like")"
printf 'arch=%s\n' "$(uname -m)"
printf 'cores=%s\n' "$(nproc 2>/dev/null || echo '?')"
