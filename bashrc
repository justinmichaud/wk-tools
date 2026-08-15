#!/bin/sh
# wk-tools/bashrc — shared rc, sourced by BOTH bash and zsh.
#
# Must stay POSIX-clean at the top level; shell-specific code goes inside the
# $BASH_VERSION / $ZSH_VERSION guards below.
#
# Escape hatches:
#   NO_ZSH=1 bash     -> stay in bash
#   HISTFILE=...      -> override history location (set before sourcing)

# =============================================================================
# 0. Interactive-only guard
# =============================================================================
# ~/.bashrc is sourced non-interactively in some contexts (ssh with
# SSH_SOURCE_BASHRC, some scp/rsync setups). Anything below would corrupt those
# sessions, so bail early. No bare `return` here: it errors under zsh when the
# file isn't sourced, and would skip the rest of a shared rc anyway.
case $- in
  *i*) _WK_INTERACTIVE=1 ;;
  *)   _WK_INTERACTIVE=  ;;
esac

if [ -n "$_WK_INTERACTIVE" ]; then

# =============================================================================
# 1. Switch bash -> zsh
# =============================================================================
# Skipped entirely under zsh (no $BASH_VERSION), which is what keeps `shopt`
# from blowing up when ~/.zshrc sources this file.
#
# Also skipped for TERM=dumb and Emacs subshells, where zsh's line editor and
# prompt escapes break shell-mode / tramp.

if [ -n "$BASH_VERSION" ] \
   && [ -z "$BASH_TO_ZSH_GUARD" ] \
   && [ -z "$NO_ZSH" ] \
   && [ "$TERM" != "dumb" ] \
   && [ -z "$INSIDE_EMACS" ] \
   && [ -t 0 ]
then
  # Find zsh. command -v first (Nix, ~/.local, source builds, asdf), then the
  # usual install paths — Homebrew is often absent from PATH under a stripped
  # SSH environment before ~/.zprofile has run.
  _wk_zsh=""
  for _wk_c in \
    "$(command -v zsh 2>/dev/null)" \
    /opt/homebrew/bin/zsh \
    /usr/local/bin/zsh \
    /usr/bin/zsh \
    /bin/zsh
  do
    if [ -n "$_wk_c" ] && [ -x "$_wk_c" ]; then _wk_zsh="$_wk_c"; break; fi
  done
  unset _wk_c

  if [ -n "$_wk_zsh" ]; then
    export BASH_TO_ZSH_GUARD=1
    export SHELL="$_wk_zsh"

    # Login-shell semantics. On macOS every Terminal.app/iTerm2 tab is a login
    # shell, so `exec zsh -l` would re-run /usr/libexec/path_helper via
    # /etc/zprofile after /etc/profile already ran it for bash. path_helper
    # rebuilds PATH with system paths first, shoving wk-tools and any local
    # JSC build behind /usr/bin. So: login semantics on Linux, plain exec on
    # Darwin, and let ~/.zshrc own PATH there.
    if [ "$(uname -s)" = "Darwin" ]; then
      exec "$_wk_zsh"
    elif shopt -q login_shell; then
      exec "$_wk_zsh" -l
    else
      exec "$_wk_zsh"
    fi

    # Only reached if exec failed — stay in bash rather than leaving a broken
    # guard in the environment.
    unset BASH_TO_ZSH_GUARD
  fi
  unset _wk_zsh
fi

# =============================================================================
# 2. History — shared settings
# =============================================================================
# Keep HISTFILE on local disk. If wk-tools is synced (git, Dropbox, iCloud),
# a history file inside it will be clobbered by concurrent writes from two
# machines — the single most reliable way to lose history.
: "${HISTFILE:=$HOME/.${_WK_SHELL_NAME:-sh}_history}"

if [ -n "$ZSH_VERSION" ]; then
  HISTFILE="$HOME/.zsh_history"
elif [ -n "$BASH_VERSION" ]; then
  HISTFILE="$HOME/.bash_history"
fi

# =============================================================================
# 3. History — zsh
# =============================================================================
if [ -n "$ZSH_VERSION" ]; then

  # HISTSIZE is the in-memory list, SAVEHIST the on-disk one. HISTSIZE must be
  # >= SAVEHIST or the file gets truncated to the smaller value on every write.
  # Keep HISTSIZE larger to leave headroom.
  HISTSIZE=1200000
  SAVEHIST=1000000

  # macOS ships an /etc/zshrc that sets HISTFILE/HISTSIZE/SAVEHIST to small
  # values. It runs before ~/.zshrc, so these assignments win — but only if
  # this file is actually sourced from ~/.zshrc. Verify that.

  setopt EXTENDED_HISTORY        # write timestamp + duration per entry
  setopt APPEND_HISTORY          # append at exit, never truncate the file
  setopt INC_APPEND_HISTORY_TIME # write each command as it finishes, not at
                                 # exit — survives kill -9, crashes, reboots,
                                 # and closing the terminal with the X button
  setopt HIST_FCNTL_LOCK         # fcntl() locking: correct and fast when many
                                 # shells write concurrently
  setopt HIST_VERIFY             # expand !! etc. onto the line, don't run blind
  setopt HIST_IGNORE_SPACE       # leading space = deliberate omission (secrets)
  setopt HIST_FIND_NO_DUPS       # dedupe search results only, not the file

  # Explicitly off. Each of these deletes entries, which contradicts the goal.
  unsetopt HIST_IGNORE_ALL_DUPS
  unsetopt HIST_SAVE_NO_DUPS
  unsetopt HIST_EXPIRE_DUPS_FIRST
  unsetopt HIST_IGNORE_DUPS

  # SHARE_HISTORY is the alternative to INC_APPEND_HISTORY_TIME: it also
  # imports other shells' commands live, so up-arrow in one terminal walks
  # through what you typed in another. Nothing is lost either way. To switch:
  #     unsetopt INC_APPEND_HISTORY_TIME; setopt SHARE_HISTORY
  unsetopt SHARE_HISTORY

fi

# =============================================================================
# 4. History — bash
# =============================================================================
# Reached on hosts with no zsh installed, or under NO_ZSH=1.
if [ -n "$BASH_VERSION" ]; then

  shopt -s histappend   # append, don't overwrite — without this the last shell
                        # to exit wins and clobbers every other session
  shopt -s cmdhist      # multi-line command -> one history entry
  shopt -s lithist      # ...preserving real newlines instead of semicolons

  # Unlimited needs bash 4.3+. macOS system bash is 3.2, where -1 is not
  # understood, so fall back to a large finite value there.
  if [ "${BASH_VERSINFO[0]}" -gt 4 ] 2>/dev/null || \
     { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; } 2>/dev/null
  then
    HISTSIZE=-1
    HISTFILESIZE=-1
  else
    HISTSIZE=1000000
    HISTFILESIZE=1000000
  fi

  HISTCONTROL=ignorespace          # not ignoredups: that discards entries
  HISTTIMEFORMAT='%F %T '          # also makes bash write timestamps to file

  # Flush after every command. Append `history -a` to any existing
  # PROMPT_COMMAND rather than replacing it.
  case "$PROMPT_COMMAND" in
    *history\ -a*) : ;;
    "")            PROMPT_COMMAND='history -a' ;;
    *)             PROMPT_COMMAND="${PROMPT_COMMAND%;}; history -a" ;;
  esac
  # For live cross-terminal sharing (bash's rough SHARE_HISTORY equivalent),
  # use 'history -a; history -c; history -r' instead.

fi

# =============================================================================
# 5. History backup
# =============================================================================
# Append-only settings protect against clobbering, not against a truncated or
# corrupted file (full disk, interrupted write, a stray `> ~/.zsh_history`).
# One dated snapshot per day, 30 kept. Runs at shell start, costs nothing after
# the first shell of the day.
_wk_hb_dir="${HISTBACKUP_DIR:-$HOME/.history-backups}"
if [ -s "$HISTFILE" ]; then
  _wk_hb_file="$_wk_hb_dir/$(basename "$HISTFILE").$(date +%Y%m%d)"
  if [ ! -f "$_wk_hb_file" ]; then
    if mkdir -p "$_wk_hb_dir" 2>/dev/null; then
      cp -p "$HISTFILE" "$_wk_hb_file" 2>/dev/null
      # Prune all but the 30 newest. Names are dated, so no spaces to worry
      # about. -t is supported by both BSD and GNU ls.
      ls -1t "$_wk_hb_dir"/ 2>/dev/null | tail -n +31 | while IFS= read -r _wk_f; do
        rm -f "$_wk_hb_dir/$_wk_f"
      done
    fi
  fi
  unset _wk_hb_file
fi
unset _wk_hb_dir _wk_f

fi  # _WK_INTERACTIVE
unset _WK_INTERACTIVE

export TERM=xterm-256color

if command -v hx > /dev/null; then
    export EDITOR="hx"
    export VISUAL="hx"
else
    export EDITOR="vim"
    export VISUAL="vim"
fi
source $HOME/Development/webkit-container-sdk/register-sdk-on-host.sh

#export LD_LIBRARY_PATH=$lD_LIBRARY_PATH:/usr/lib/arm-linux-gnueabihf

alias clear="clear && echo -e '\e[3J' && clear"

#export TMPDIR=${HOME}/tmp
export PATH="`echo ${HOME}/.rustup/toolchains/*/bin/`:${HOME}/codium/bin:$PATH:${HOME}/Development/wk-tools/:${HOME}/:${HOME}/Development/wabt/bin/:${HOME}/Development/samply/target/release/"

# Better bash history
HISTFILE=~/.zsh_history
HISTSIZE=50000
SAVEHIST=50000

# Share history instantly across all sessions
setopt SHARE_HISTORY

# Recommended additions
setopt EXTENDED_HISTORY
setopt HIST_IGNORE_ALL_DUPS

# Export shared directories for yocto caches
export DL_DIR="${HOME}/Development/.cache/yocto/downloads"
export SSTATE_DIR="${HOME}/Development/.cache/yocto/sstate"
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS} DL_DIR SSTATE_DIR PARALLEL_MAKE"

# We run out of memory otherwise;
if [ -f /proc/meminfo ]; then
    export jobs=$(( ($(awk '/MemAvailable/ {print $2}' /proc/meminfo) - 10 * 1000 * 1000) / (4 * 1000 * 1000)))
else
    export jobs=8
fi
export PARALLEL_MAKE="-j${jobs}"
export CMAKE_BUILD_PARALLEL_LEVEL=${jobs}

# These push hooks always take down 8 cores for some reason
unalias git 2>/dev/null
git ()
{
    if [ "$1" = push ]; then
        shift;
        command git push --no-verify "$@";
    else
        command git "$@";
    fi
}
