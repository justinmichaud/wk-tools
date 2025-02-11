export VISUAL="codium --wait"
export EDITOR="codium --wait"
source ~/Development/webkit-container-sdk/register-sdk-on-host.sh

export LC_ALL=C

alias clear="clear && echo -e '\e[3J' && clear"

export TERM=vt100 

export TMPDIR=${HOME}/tmp
export PATH="$PATH:${HOME}/Development/wk-tools/"

export CC=clang-19
export CXX=clang++-19
export LLDB=lldb-19
alias lldb=lldb-19

# Export shared directories for yocto caches
export DL_DIR="${HOME}/Development/.cache/yocto/downloads"
export SSTATE_DIR="${HOME}/Development/.cache/yocto/sstate"
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS} DL_DIR SSTATE_DIR"
