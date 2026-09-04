# Deploy host dotfiles, shared by macOS and Linux. Symlinked, not copied, so
# anything already present and not a symlink moves to <name>.wk-backup once.

link_config "$WK_ROOT/dotfiles/zed"     "$HOME/.config/zed"
link_config "$WK_ROOT/dotfiles/lldbinit" "$HOME/.lldbinit"

# ssh: config.d/wk-tools is this repo, config.d/wk is `wk new`, config.d/local
# is the machine; ~/.ssh/config is an Include and owns nothing.
ensure_dir "$HOME/.ssh" 0700
ensure_dir "$HOME/.ssh/config.d" 0700
link_config "$WK_ROOT/dotfiles/ssh/config" "$HOME/.ssh/config.d/wk-tools"

# A hand-written stanza reusing a fleet name (boot/machines.sh) would shadow the
# fleet's own and silently redirect wk verbs that take an ssh destination, `wk
# sysimage write` included, so those are dropped; the rest moves to config.d/local.
_ssh_conf="$HOME/.ssh/config"
_ssh_local="$HOME/.ssh/config.d/local"
_ssh_include='Include config.d/*'

# grep -vxF, not sed: a person's own Include is content to migrate, not syntax.
_ssh_body() { grep -vxF "$_ssh_include" "$_ssh_conf" 2>/dev/null | grep -vE '^[[:space:]]*(#|$)' || true; }

if [ -f "$_ssh_conf" ] && grep -qxF "$_ssh_include" "$_ssh_conf" && [ -z "$(_ssh_body)" ]; then
    unchanged "ssh config"
else
    if [ -f "$_ssh_conf" ] && [ -n "$(_ssh_body)" ]; then
        # shellcheck disable=SC1091
        _ssh_fleet="$( . "$WK_ROOT/boot/machines.sh"; machine_list | awk '{print $1}' )"

        [ -e "$_ssh_conf.wk-backup" ] || cp -p "$_ssh_conf" "$_ssh_conf.wk-backup"

        # ssh takes a keyword's first value, so an older copy of a migrated
        # stanza is dropped rather than kept below it.
        _ssh_new=$(mktemp)
        grep -vxF "$_ssh_include" "$_ssh_conf" | awk -v fleet="$_ssh_fleet" '
                BEGIN {
                    n = split(fleet, f, "\n")
                    for (i = 1; i <= n; i++) if (f[i] != "") is_fleet[f[i]] = 1
                }
                # A stanza runs from its Host line to the next one.
                tolower($1) == "host" {
                    drop = 0
                    for (i = 2; i <= NF; i++) if ($i in is_fleet) drop = 1
                    if (drop) printf "  dropped hand-written fleet name: %s\n", $0 > "/dev/stderr"
                }
                !drop
            ' > "$_ssh_new"

        {
            printf '# Hosts belonging to this machine rather than to wk-tools, moved out of\n'
            printf '# ~/.ssh/config by ./setup. Edit freely: nothing here is committed --\n'
            printf '# and edit it *here*, not in ~/.ssh/config, which is only an Include and\n'
            printf '# is read after this file.\n\n'
            cat "$_ssh_new"
            if [ -f "$_ssh_local" ]; then
                awk -v newfile="$_ssh_new" '
                    BEGIN {
                        while ((getline line < newfile) > 0) {
                            n = split(line, w, /[ \t]+/)
                            if (tolower(w[1]) == "host")
                                for (i = 2; i <= n; i++) if (w[i] != "") defined[w[i]] = 1
                        }
                    }
                    tolower($1) == "host" {
                        drop = 0
                        for (i = 2; i <= NF; i++) if ($i in defined) drop = 1
                        if (drop) printf "  replaced by a newer stanza: %s\n", $0 > "/dev/stderr"
                    }
                    /^# Hosts belonging to this machine rather than to wk-tools/ { next }
                    /^# ~\/\.ssh\/config by \.\/setup/ { next }
                    /^# and edit it \*here\*, not in ~\/\.ssh\/config/ { next }
                    /^# is read after this file\./ { next }
                    !drop
                ' "$_ssh_local"
            fi
        } | write_file "$_ssh_local" 0600
        rm -f "$_ssh_new"; unset _ssh_new

        # Reported, not removed: config.d/local reads first, and some of those
        # are deliberate (`moose` resolving to localhost *on* moose).
        _ssh_owned=$(awk 'tolower($1) == "host" { for (i = 2; i <= NF; i++) print $i }' \
                        "$WK_ROOT/dotfiles/ssh/config" 2>/dev/null)
        awk -v owned="$_ssh_owned" '
            BEGIN { n = split(owned, o, "\n"); for (i = 1; i <= n; i++) if (o[i] != "") mine[o[i]] = 1 }
            tolower($1) == "host" {
                for (i = 2; i <= NF; i++) if ($i in mine)
                    printf "  %s in config.d/local shadows this repo'"'"'s own stanza (local is read first)\n", $i
            }' "$_ssh_local" 2>/dev/null | sort -u | while read -r line; do warn "$line"; done
        unset _ssh_owned

        warn "hand-written hosts moved from $_ssh_conf to $_ssh_local"
        warn "the original is kept at $_ssh_conf.wk-backup"
        log  "  edit $_ssh_local, not $_ssh_conf: the Include is read first, so a"
        log  "  stanza written in ~/.ssh/config is shadowed by the files behind it"
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

# An include is not the last word: git takes a key's *last* value, so a [user]
# section written after it wins, invisibly, until a commit is already pushed --
# hence removed, not reported. Another identity means editing dotfiles/gitconfig.
for _id in name email; do
    _want=$(git config --file "$WK_ROOT/dotfiles/gitconfig" --get "user.$_id" || true)
    [ -n "$_want" ] || continue
    _have=$(git config --get "user.$_id" || true)
    if [ "$_have" = "$_want" ]; then
        unchanged "git user.$_id ($_want)"
    elif [ -z "$_have" ]; then
        # The include is in place and yet nothing resolves: a broken include path.
        warn "git user.$_id resolves to nothing although the include is in place"
        log  "  check:  git config --show-origin --get user.$_id"
    else
        git config --global --unset-all "user.$_id" 2>/dev/null || true
        _now=$(git config --get "user.$_id" || true)
        if [ "$_now" = "$_want" ]; then
            changed "git user.$_id was '$_have' in ~/.gitconfig, shadowing the repo's '$_want' -- removed"
        else
            warn "git user.$_id is '$_now', not the repo's '$_want'"
            log  "  something outside ~/.gitconfig sets it:  git config --show-origin --get user.$_id"
        fi
    fi
done
unset _id _want _have _now

write_file "$HOME/.gitignore" 0644 <<'EOF'
.DS_Store
.idea
.cache
compile_commands.json
EOF

# .bash_profile too: it is what a login shell (what ssh starts) reads.
_rc_line=". \"$WK_ROOT/shell/bashrc\""
for _rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    [ -f "$_rc" ] || touch "$_rc"

    # register-sdk-on-host.sh is stale: the SDK runs in the VM, not the host.
    if grep -qE 'wk-tools/bashrc|register-sdk-on-host\.sh' "$_rc"; then
        _tmp="$(mktemp)"
        # || true: an all-stale file makes grep exit 1, aborting under set -e.
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
