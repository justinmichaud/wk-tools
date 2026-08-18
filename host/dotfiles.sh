# Deploy host dotfiles. Shared by macOS and Linux.
#
# The set is deliberately tiny. Editor and terminal configuration used to live
# here for helix, kitty and sway; helix is now installed inside workspaces and
# the others are gone, so Zed is the only editor config the host still needs.
#
# Files are symlinked rather than copied so edits in the repo take effect
# immediately and `git status` shows drift. Anything already present and not a
# symlink is moved to <name>.wk-backup exactly once, never clobbered.

link_config "$WK_ROOT/dotfiles/zed"     "$HOME/.config/zed"
link_config "$WK_ROOT/dotfiles/lldbinit" "$HOME/.lldbinit"

# ssh: an Include, so machine-local hosts and anything private can sit in
# ~/.ssh/config.d without ever being committed. `wk new` writes its generated
# host entries to ~/.ssh/config.d/wk.
ensure_dir "$HOME/.ssh" 0700
ensure_dir "$HOME/.ssh/config.d" 0700
link_config "$WK_ROOT/dotfiles/ssh/config" "$HOME/.ssh/config.d/wk-tools"

if [ -f "$HOME/.ssh/config" ] && grep -q '^Include config.d/\*' "$HOME/.ssh/config" 2>/dev/null; then
    unchanged "ssh Include"
else
    # Include must come first: ssh takes the first value it sees for each
    # keyword, so a trailing Include would be shadowed by anything above it.
    _tmp="$(mktemp)"
    printf 'Include config.d/*\n\n' > "$_tmp"
    [ -f "$HOME/.ssh/config" ] && cat "$HOME/.ssh/config" >> "$_tmp"
    write_file "$HOME/.ssh/config" 0600 < "$_tmp"
    rm -f "$_tmp"
fi

# git: same pattern, so credentials and per-machine settings stay out of git.
if [ -f "$HOME/.gitconfig" ] && grep -q 'wk-tools/dotfiles/gitconfig' "$HOME/.gitconfig" 2>/dev/null; then
    unchanged "gitconfig include"
else
    git config --global --replace-all include.path "$WK_ROOT/dotfiles/gitconfig"
    changed "gitconfig includes $WK_ROOT/dotfiles/gitconfig"
fi

# The global gitignore referenced by dotfiles/gitconfig.
write_file "$HOME/.gitignore" 0644 <<'EOF'
.DS_Store
.idea
.cache
compile_commands.json
EOF

# shell: source the shared rc from whichever startup files exist. Guarded so
# re-running never appends a second copy.
_rc_line=". \"$WK_ROOT/shell/bashrc\""
for _rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -f "$_rc" ] || touch "$_rc"

    # Drop references to paths this restructure moved. Left in place they
    # error on every interactive shell -- the old wk-tools/bashrc and the
    # webkit-container-sdk registration are both gone, the latter because the
    # SDK now runs inside the VM rather than on the host.
    if grep -qE 'wk-tools/bashrc|register-sdk-on-host\.sh' "$_rc"; then
        _tmp="$(mktemp)"
        # `|| true`: grep exits 1 when it prints nothing, which is exactly the
        # case here if the file contains *only* stale lines -- and under
        # `set -e` that aborts the whole stage rather than emptying the file.
        grep -vE '(^|[[:space:]])(source|\.)[[:space:]]+.*(wk-tools/bashrc|register-sdk-on-host\.sh)' "$_rc" > "$_tmp" || true
        mv "$_tmp" "$_rc"
        changed "removed stale source lines from $_rc"
    fi

    if grep -qF "wk-tools/shell/bashrc" "$_rc"; then
        unchanged "shell rc $_rc"
    else
        printf '\n# wk-tools shared shell configuration\n%s\n' "$_rc_line" >> "$_rc"
        changed "source wk-tools/shell/bashrc from $_rc"
    fi
done

unset _rc _rc_line _tmp
