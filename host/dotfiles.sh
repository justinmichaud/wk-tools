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

# ssh: an Include, and nothing but an Include. Three files sit behind it and
# each has exactly one owner -- config.d/wk-tools is this repo, config.d/wk is
# `wk new`, config.d/local is the machine. ~/.ssh/config itself owns nothing.
ensure_dir "$HOME/.ssh" 0700
ensure_dir "$HOME/.ssh/config.d" 0700
link_config "$WK_ROOT/dotfiles/ssh/config" "$HOME/.ssh/config.d/wk-tools"

# This used to prepend the Include and leave whatever was already below it,
# which reads as harmless: the Include is first, ssh takes the first value it
# sees for each keyword, so the repo's copy already won any name the two
# shared. The entries sharing *no* name were the problem. Nothing re-read them
# and nothing reconciled them, so they kept resolving long after they had
# stopped being true.
#
# `Host rpi4` is why this is a migration rather than a comment. It named
# rpi4-compilers-0, a shared build box behind a ProxyJump, while the fleet's
# rpi4 (boot/machines.sh) is a 2 GB board on the LAN. Every wk verb that takes
# an ssh destination would have aimed at the build box, and `wk sysimage write`
# aims at a disk.
#
# So: a fleet name may not be defined by hand. Those stanzas are dropped --
# they are shadows of a machine, not hosts -- and everything else moves to
# config.d/local, where a host that belongs to this machine rather than to this
# repo was always supposed to live. Only fleet names are dropped, deliberately:
# a duplicate of something in dotfiles/ssh/config was already being shadowed
# and carrying it across changes nothing, while guessing which of the two the
# machine actually wanted would.
#
# After the first run ~/.ssh/config is one line and all of this is a no-op.
_ssh_conf="$HOME/.ssh/config"
_ssh_local="$HOME/.ssh/config.d/local"
_ssh_include='Include config.d/*'

# grep -vxF, not sed: this recognises the one line it writes itself and nothing
# else, so an Include a person added for their own reasons is content, not
# syntax, and gets migrated like any other line.
_ssh_body() { grep -vxF "$_ssh_include" "$_ssh_conf" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' || true; }

if [ -f "$_ssh_conf" ] && grep -qxF "$_ssh_include" "$_ssh_conf" && [ -z "$(_ssh_body)" ]; then
    unchanged "ssh config"
else
    if [ -f "$_ssh_conf" ] && [ -n "$(_ssh_body)" ]; then
        # shellcheck disable=SC1091
        _ssh_fleet="$( . "$WK_ROOT/boot/machines.sh"; machine_list | awk '{print $1}' )"

        [ -e "$_ssh_conf.wk-backup" ] || cp -p "$_ssh_conf" "$_ssh_conf.wk-backup"

        {
            [ -f "$_ssh_local" ] && cat "$_ssh_local"
            printf '# Hosts belonging to this machine rather than to wk-tools, moved out of\n'
            printf '# ~/.ssh/config by ./setup. Edit freely: nothing here is committed.\n\n'
            grep -vxF "$_ssh_include" "$_ssh_conf" | awk -v fleet="$_ssh_fleet" '
                BEGIN {
                    n = split(fleet, f, "\n")
                    for (i = 1; i <= n; i++) if (f[i] != "") is_fleet[f[i]] = 1
                }
                # A stanza runs from its Host line to the next one, so dropping
                # a name means suppressing every line until the next Host.
                tolower($1) == "host" {
                    drop = 0
                    for (i = 2; i <= NF; i++) if ($i in is_fleet) drop = 1
                    if (drop) printf "  dropped hand-written fleet name: %s\n", $0 > "/dev/stderr"
                }
                !drop
            '
        } | write_file "$_ssh_local" 0600

        warn "hand-written hosts moved from $_ssh_conf to $_ssh_local"
        warn "the original is kept at $_ssh_conf.wk-backup"
    fi

    printf '%s\n' "$_ssh_include" | write_file "$_ssh_conf" 0600
fi
unset -f _ssh_body
unset _ssh_conf _ssh_local _ssh_include _ssh_fleet

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
#
# .bash_profile is in this list for the same reason .bashrc and .zshrc are:
# it's exactly the file a login shell (what ssh starts) reads, so a stale
# `wk-tools/bashrc` reference here after this restructure moved that file
# means every ssh session lands in bash and never even reaches the rest of
# this rc, let alone the bash -> zsh switch it's supposed to make.
_rc_line=". \"$WK_ROOT/shell/bashrc\""
for _rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
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
