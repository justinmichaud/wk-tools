# Deploy host dotfiles. Shared by macOS and Linux.
#
# The set is deliberately tiny: helix is installed inside workspaces, and Zed is
# the only editor config the host needs.
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

# Prepending the Include and leaving what is already below it is not enough
# on its own: ssh takes the first value it sees for each keyword, so the
# repo's copy already wins any name the two share, but an entry sharing *no*
# name with the repo's stanzas is never reconciled and keeps resolving even
# after it stops being true.
#
# A fleet name (boot/machines.sh) may not be defined by hand: a hand-written
# stanza reusing one is a shadow of the fleet's own definition, not a host of
# its own, and would silently point every wk verb that takes an ssh
# destination -- including `wk sysimage write`, which aims at a disk -- at
# whatever the hand-written stanza names instead. Those stanzas are dropped
# rather than migrated. Everything else moves to config.d/local, where a host
# belonging to this machine rather than to this repo belongs; a duplicate of
# something already in dotfiles/ssh/config is dropped too, since carrying it
# across changes nothing and guessing which of the two the machine actually
# wanted would.
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

        # Migrated stanzas go **first**, and any older copy of a name they
        # define is dropped rather than kept below them: ssh takes the first
        # value it sees for a keyword, so appending a corrected stanza
        # underneath a stale one of the same name would file the fix behind
        # the mistake, permanently, and add another duplicate on every run --
        # an entry that editing never changes, because the edit is shadowed by
        # a line the editor never saw.
        #
        # Dropping the old copy rather than relying on order also keeps the
        # file honest -- one stanza per name, and the surviving one is the one
        # that was written most recently.
        _ssh_new=$(mktemp)
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
            ' > "$_ssh_new"

        {
            printf '# Hosts belonging to this machine rather than to wk-tools, moved out of\n'
            printf '# ~/.ssh/config by ./setup. Edit freely: nothing here is committed --\n'
            printf '# and edit it *here*, not in ~/.ssh/config, which is only an Include and\n'
            printf '# is read after this file.\n\n'
            cat "$_ssh_new"
            # The previous contents, minus this header and minus any stanza the
            # migration above has just redefined.
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

        # Shadowing is reported, not removed. config.d/local is read *before*
        # config.d/wk-tools, so a hand-written stanza for a name this repo also
        # defines wins -- and some of those are deliberate (`moose` resolves to
        # localhost *on* moose, which the repo's own stanza cannot know). A
        # silent win risks a stanza staying stale after the repo's copy is
        # corrected, so the collision is said out loud and the choice is left
        # to a person.
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
# `wk-tools/bashrc` reference here means every ssh session lands in bash and
# never even reaches the rest of this rc, let alone the bash -> zsh switch
# it's supposed to make.
_rc_line=". \"$WK_ROOT/shell/bashrc\""
for _rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    [ -f "$_rc" ] || touch "$_rc"

    # Drop stale source lines for paths that no longer exist -- an old
    # wk-tools/bashrc location and the webkit-container-sdk host registration
    # (the SDK runs inside the VM instead) -- left in place they error on
    # every interactive shell.
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
