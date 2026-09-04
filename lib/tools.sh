# Putting this tree's wk-tools on a machine across ssh, as a real git checkout.
#
#   tools_push <dest> <run...>   converge <dest> over there to this tree's HEAD
#   tools_head                   this tree's HEAD sha
#   tools_committed              is there a commit to send? (the refusal)
#
# A machine gets a commit and nothing else, so `wk status` compares the two by
# sha; an uncommitted tree is refused, since there is no sha for a copy of it.
# <run...> is the driver's own ssh wrapper (targets/remote.sh, targets/vm.sh),
# called with one command string appended and the bundle on stdin. The bundle
# carries the whole history rather than negotiating: 3.8 MB in 0.9s here.

command -v warn >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

tools_head() {
    git -C "$WK_ROOT" rev-parse HEAD 2>/dev/null || return 1
}

# Tracked changes only: an untracked file -- a .DS_Store, a per-machine conf, a
# file not yet added -- is not a change to this repository.
tools_committed() {
    local st
    if ! git -C "$WK_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        warn "$WK_ROOT is not a git checkout, so there is no commit to put on a machine.
    A machine is given a commit and nothing else -- that is what lets
    'wk status' compare the copy over there with this one by sha.
    Run wk from a clone:  git clone https://github.com/justinmichaud/wk-tools"
        return 1
    fi
    st=$(git -C "$WK_ROOT" status --porcelain --untracked-files=no 2>/dev/null) || st=""
    [ -n "$st" ] || return 0
    warn "wk-tools here has uncommitted changes, so there is no commit to put on a machine.
    A machine is given a commit and nothing else -- that is what lets
    'wk status' compare the copy over there with this one by sha.
    Commit them and re-run:
        git -C $WK_ROOT status --short
        git -C $WK_ROOT commit -a"
    return 1
}

# The floor under the far side's `rm -rf "$d"`. It must be absolute: the far side
# runs the script from whatever directory its login shell lands in.
tools_dest_ok() { # <dest>
    local d="$1" rest
    case "$d" in
        ""|/) return 1 ;;
        */)   return 1 ;;
        /*)   ;;
        *)    return 1 ;;
    esac
    if [ -n "${HOME:-}" ] && [ "$d" = "$HOME" ]; then return 1; fi
    rest=${d#/}
    case "$rest" in
        */?*) return 0 ;;
    esac
    return 1
}

# The far side's half, as one shell script: POSIX, since it runs in whatever login
# shell the account over there has (bash on a build box, zsh in a macOS guest). It
# reads the bundle from stdin and prints the sha it ended at. `git clean` runs
# without -x, so ignored files (a build directory, a machine-local conf) survive.
tools_converge_script() { # <dest> <sha>
    printf 'set -e\nd=%s\ns=%s\n' "$(sh_quote "$1")" "$(sh_quote "$2")"
    cat <<'EOF'
command -v git >/dev/null 2>&1 || {
    echo "no git on this machine, so wk-tools cannot be a checkout here" >&2
    exit 1
}
if [ -L "$d" ]; then
    echo "$d is a symlink, and wk-tools here is a directory; not replacing it" >&2
    exit 1
fi
if ! { [ -d "$d/.git" ] && git -C "$d" rev-parse --git-dir >/dev/null 2>&1; }; then
    if [ -e "$d" ]; then
        echo "replacing $d: it is not a git checkout" >&2
    fi
    rm -rf "$d"
    mkdir -p "$d"
    git -c init.defaultBranch=main init -q "$d"
fi
b="$d/.git/wk-tools-push.bundle"
cat > "$b"
git -C "$d" fetch -q "$b" HEAD
rm -f "$b"
git -C "$d" reset -q --hard "$s"
git -C "$d" clean -qfd
git -C "$d" rev-parse HEAD
EOF
}

tools_push() { # <dest> <run...>
    local dest="$1"; shift
    local sha bundle got
    tools_dest_ok "$dest" || {
        warn "refusing to push wk-tools to '$dest'. The far side replaces that directory
    outright when it is not already a checkout, so the destination must be an
    absolute path of at least two components, with no trailing slash, and
    neither / nor the account's home."
        return 1
    }
    tools_committed || return 1
    sha=$(tools_head) || { warn "cannot read HEAD in $WK_ROOT"; return 1; }

    bundle=$(mktemp "${TMPDIR:-/tmp}/wk-tools-push.XXXXXX") || return 1
    if ! git -C "$WK_ROOT" bundle create "$bundle" HEAD >/dev/null 2>&1; then
        rm -f "$bundle"
        warn "could not bundle $WK_ROOT at $sha for the push"
        return 1
    fi

    debug "pushing wk-tools $sha -> $dest"
    got=$("$@" "$(tools_converge_script "$dest" "$sha")" < "$bundle" | tr -d '\r' | tail -1) || got=""
    rm -f "$bundle"

    [ "$got" = "$sha" ] && return 0
    warn "wk-tools at $dest did not end at $sha (it answers '${got:-nothing}')"
    return 1
}
