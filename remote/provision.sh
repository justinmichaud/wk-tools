#!/usr/bin/env bash
#
# Runs on a shared build machine, over ssh from `wk remote setup`.
#
# Nothing here needs root: these are other people's machines. Everything lives
# under $HOME with prerequisites checked, never installed. Non-interactive:
# cleanup is a decision left to `wk remote setup`, which has a terminal.

set -euo pipefail

TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WK_ROOT="$TOOLS"
. "$TOOLS/lib/common.sh"

TARGET="${WK_REMOTE_TARGET:-}"
ROOT="${WK_REMOTE_ROOT:-$HOME/wk}"

[ -n "$TARGET" ] || die "WK_REMOTE_TARGET is not set (run this through 'wk remote setup')"

info "provisioning $(hostname) for target '$TARGET'"

# --- prerequisites -----------------------------------------------------------
# git is load-bearing (checkout, serialisation between builds); anything else fails with its own error.
_missing=""
for _t in git; do
    have "$_t" || _missing="$_missing $_t"
done
[ -z "$_missing" ] || die "missing on this machine:$_missing
    Installing needs root, and this never asks for it. Ask the machine's
    administrators, or use a target that has them."

# ccache's absence is silent otherwise: builds would just stay cold forever.
have ccache || warn "no ccache on this machine -- every build starts cold"

# --- the markers -------------------------------------------------------------
ensure_dir "$ROOT"
ensure_dir "$ROOT/ws"
ensure_dir "$ROOT/cache/ccache"

write_file "$HOME/.wk-remote" 0644 <<EOF
# wk: this machine hosts wk remote workspaces. Written by remote/provision.sh.
#
# It is not a workspace (there are several here) and not a workstation (it owns
# no store, no VM and no hardware of yours), so \`wk\` reads this to know which
# target it is the far end of -- and refuses the commands that only make sense
# on a workstation.
target=$TARGET
root=$ROOT
EOF

# No second conf: targets/remote.sh recomputes WK_TARGET_KIND, WK_REMOTE_LOCAL
# and WK_REMOTE_ROOT from this marker.
# --- the push keys, and how ssh finds them -----------------------------------
# ~/.ssh/config is often shared over NFS with several machines, so ssh is
# pointed at the key per checkout (`core.sshCommand`) instead. `wk key ensure`
# generates the key; `wk push` moves it between secrets/ and push-keys/.
ensure_dir "$ROOT/secrets" 0700
write_file "$ROOT/ssh/config" 0600 <<EOF
# Written by remote/provision.sh. One ssh alias per fork, because GitHub takes
# one deploy key per repository and both forks live on github.com -- so the key
# is selected by alias, never by hostname.
#
# A checkout points at this file with core.sshCommand; nothing outside the wk
# root is touched. A missing key file is simply "no identity", which is what
# 'wk push off' leaves behind.
$(WK_ROOT="$TOOLS" bash -c '. "$1/lib/common.sh"; . "$1/lib/store.sh"; wk_ssh_alias_blocks "$2"' _ "$TOOLS" "$ROOT/secrets")
EOF

# --- git: identity, and how fast git is here ---------------------------------
# An include, so the identity is declared once for every machine (dotfiles/
# gitconfig) rather than copied per box -- and so the settings that decide
# whether `git status` in a WebKit checkout answers in milliseconds
# (fsmonitor, untrackedCache, manyFiles) reach a build box too. An editor
# driving this machine over ssh asks git that question on every keystroke.
#
# --replace-all, so a re-run after the checkout moves does not leave the old
# path behind: git reads every include.path it finds.
git config --global --replace-all include.path "$TOOLS/dotfiles/gitconfig"
changed "gitconfig includes $TOOLS/dotfiles/gitconfig"
[ -f "$HOME/.gitignore" ] || printf '.DS_Store\n.cache\ncompile_commands.json\n' > "$HOME/.gitignore"

# The include is not the last word: git takes a key's last value, so a [user]
# section below it in ~/.gitconfig wins and every commit from this box carries
# it -- silently, until the commit is pushed. One of these machines had
# `user.name = no`, an answer to a prompt long ago.
#
# So the shadowing value in *this account's* ~/.gitconfig is removed and the
# include allowed to answer. Only that file: a value from /etc/gitconfig belongs
# to the machine and is reported instead.
for _id in name email; do
    _want=$(git config --file "$TOOLS/dotfiles/gitconfig" --get "user.$_id" || true)
    [ -n "$_want" ] || continue
    _have=$(git config --get "user.$_id" || true)
    if [ "$_have" = "$_want" ]; then
        unchanged "git user.$_id ($_want)"
        continue
    fi
    git config --global --unset-all "user.$_id" 2>/dev/null || true
    _now=$(git config --get "user.$_id" || true)
    if [ "$_now" = "$_want" ]; then
        changed "git user.$_id was '${_have:-unset}' here, shadowing the repo's '$_want' -- removed"
    else
        warn "git user.$_id here is '${_now:-unset}', not the repo's '$_want'
  it comes from outside this account's ~/.gitconfig:
      git config --show-origin --get user.$_id"
    fi
done
unset _id _want _have _now

# --- shell -------------------------------------------------------------------
# `chsh` wants a password and is often refused under LDAP; on these boxes
# $HOME (and its login shell) is shared between machines. shell/bashrc execs
# zsh from bash instead: per-session, reversible with NO_ZSH=1.
if have zsh; then
    info "zsh: $(command -v zsh) -- interactive bash sessions will move to it"
else
    warn "no zsh on this machine, and installing one needs root.
  Staying in bash: the same rc configures both, so only the line editor differs."
fi

_rc_line=". \"$TOOLS/shell/bashrc\""
for _rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    [ -f "$_rc" ] || : > "$_rc"

    # A moved checkout leaves a stale source line that errors every shell.
    if grep -qE 'wk-tools/bashrc' "$_rc" 2>/dev/null; then
        _tmp="$(mktemp)"
        # || true: under set -e, a match-nothing grep (an all-stale file) would abort here instead of emptying it.
        grep -vE '(^|[[:space:]])(source|\.)[[:space:]]+.*wk-tools/bashrc' "$_rc" > "$_tmp" || true
        mv "$_tmp" "$_rc"
        changed "removed stale wk-tools/bashrc line from $_rc"
    fi

    if grep -qF "$TOOLS/shell/bashrc" "$_rc" 2>/dev/null; then
        unchanged "shell rc $_rc"
    else
        printf '\n# wk-tools shared shell configuration\n%s\n' "$_rc_line" >> "$_rc"
        changed "source wk-tools/shell/bashrc from $_rc"
    fi
done
unset _rc _rc_line _tmp _t _missing

info "provisioned. On this machine:"
log  "  wk ls / wk status            what is here"
log  "  wk build <ws> <config>       polite: sized from this machine's load"
log  "  wk run <ws> -- <args>        run jsc from that workspace's build"
log  "  wk test <ws> / wk logs <ws>  the rest of the in-workspace interface"
log  ""
log  "  Workspaces are created and destroyed from the workstation -- it keeps"
log  "  the record of which machine each one lives on."
