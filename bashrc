if command -v hx &> /dev/null; then
    export EDITOR="hx"
    export VISUAL="hx"
else
    export EDITOR="vim"
    export VISUAL="vim"
fi

source ~/Development/webkit-container-sdk/register-sdk-on-host.sh

export LC_ALL=

export LD_LIBRARY_PATH=$lD_LIBRARY_PATH:/usr/lib/arm-linux-gnueabihf

alias clear="clear && echo -e '\e[3J' && clear"

export TMPDIR=${HOME}/tmp
export PATH="${HOME}/codium/bin:$PATH:${HOME}/Development/wk-tools/"

# Export shared directories for yocto caches
export DL_DIR="${HOME}/Development/.cache/yocto/downloads"
export SSTATE_DIR="${HOME}/Development/.cache/yocto/sstate"
export BB_ENV_PASSTHROUGH_ADDITIONS="${BB_ENV_PASSTHROUGH_ADDITIONS} DL_DIR SSTATE_DIR PARALLEL_MAKE"

# We run out of memory otherwise; Let's give each thread 3.5 GB
export jobs=$(($(awk '/MemTotal/ {print $2}' /proc/meminfo) / (3758096) ))
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
