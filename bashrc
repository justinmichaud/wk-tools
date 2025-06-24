#export VISUAL="codium --wait"
#export VISUAL="zed --wait"
#export EDITOR="codium --wait"
#export EDITOR="zed --wait"
source ~/Development/webkit-container-sdk/register-sdk-on-host.sh

export LC_ALL=C

alias clear="clear && echo -e '\e[3J' && clear"

export TERM=vt100 

export TMPDIR=${HOME}/tmp
export PATH="${HOME}/codium/bin:$PATH:${HOME}/Development/wk-tools/"

export CC=clang-19
export CXX=clang++-19
export LLDB=lldb-19
#alias lldb=lldb-19

# Export shared directories for yocto caches
export DL_DIR="${HOME}/Development/.cache/yocto/downloads"
export SSTATE_DIR="${HOME}/Development/.cache/yocto/sstate"
export PARALLEL_MAKE="-j20"
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS} DL_DIR SSTATE_DIR PARALLEL_MAKE"

export GOCACHE="$(mktemp -d)"
export GOPATH="${HOME}/Development/go/packaged"
export GOROOT="${HOME}/Development/go/"   
export PATH=$GOPATH/bin:$PATH


