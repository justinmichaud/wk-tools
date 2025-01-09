export VISUAL="codium --wait"
export EDITOR="codium --wait"
source ~/Development/webkit-container-sdk/register-sdk-on-host.sh

export TMPDIR=${HOME}/tmp
export PATH="$PATH:${HOME}/Development/wk-tools/"

export CC=clang-18
export CXX=clang++-18
export LLDB=lldb-18
alias lldb=lldb-18

# Export shared directories for yocto caches
export DL_DIR="${HOME}/Development/.cache/yocto/downloads"
export SSTATE_DIR="${HOME}/Development/.cache/yocto/sstate"
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS} DL_DIR SSTATE_DIR"
