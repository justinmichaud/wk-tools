# GPU passthrough for workspaces: a benchmark is meaningless under llvmpipe.
# NVIDIA's userspace libraries must match the host kernel driver exactly and
# cannot be installed inside an offline container, so either CDI (nvidia-ctk,
# not packaged for Ubuntu 26.04, only 26.10) or a list derived from ldconfig.
gpu_flags() {
    local flags=""

    # The card node is only needed for modesetting, which a workspace never does.
    if [ -e /dev/dri/renderD128 ]; then
        flags="$flags --device /dev/dri"
    else
        warn "no /dev/dri render node; workspaces will fall back to software rendering"
        return 0
    fi

    # Counted, not `lsmod | grep -q`: grep -q exits early, lsmod takes SIGPIPE.
    local nvidia_modules
    nvidia_modules=$(lsmod 2>/dev/null | grep -c '^nvidia' || true)
    if [ "${nvidia_modules:-0}" -eq 0 ]; then
        printf '%s' "$flags"
        return 0
    fi

    if _cdi_available; then
        debug "GPU: CDI spec present, using nvidia.com/gpu=all"
        printf '%s' "$flags --device nvidia.com/gpu=all"
        return 0
    fi

    debug "GPU: no CDI spec, deriving the NVIDIA library set from ldconfig"
    printf '%s' "$flags$(_nvidia_derived_flags)"
}

_cdi_available() {
    local f
    for f in /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml /var/run/cdi/*.json; do
        [ -f "$f" ] || continue
        [ "$(grep -c 'nvidia.com/gpu' "$f" 2>/dev/null || true)" != 0 ] && return 0
    done
    return 1
}

# Missing entries are skipped: the set differs between driver branches.
_nvidia_derived_flags() {
    local out="" f

    # /dev/nvidia-caps is a directory and only matters for MIG.
    for f in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-modeset \
             /dev/nvidia-uvm /dev/nvidia-uvm-tools; do
        [ -e "$f" ] && out="$out --device $f"
    done

    # Both the versioned file and its SONAME symlink: glvnd dlopens by SONAME.
    local libs
    libs=$(ldconfig -p 2>/dev/null \
        | awk '{print $NF}' \
        | grep -E '/(libnvidia-.*|libcuda|libcudadebugger|libEGL_nvidia|libGLX_nvidia|libnvcuvid|libnvoptix)\.so' \
        | sort -u)

    local lib real
    for lib in $libs; do
        [ -e "$lib" ] || continue
        out="$out --volume $lib:$lib:ro"
        real=$(readlink -f "$lib" 2>/dev/null || true)
        if [ -n "$real" ] && [ "$real" != "$lib" ] && [ -e "$real" ]; then
            case " $libs " in
                *" $real "*) ;;
                *) out="$out --volume $real:$real:ro" ;;
            esac
        fi
    done

    # The GBM backend, so EGL can render to a dmabuf the compositor imports.
    for f in /usr/lib/*/gbm/nvidia-drm_gbm.so; do
        [ -e "$f" ] && out="$out --volume $f:$f:ro"
    done

    # Without these glvnd loads Mesa and reports llvmpipe.
    for f in /usr/share/glvnd/egl_vendor.d/10_nvidia.json \
             /usr/share/egl/egl_external_platform.d/10_nvidia_wayland.json \
             /usr/share/egl/egl_external_platform.d/15_nvidia_gbm.json \
             /usr/share/vulkan/icd.d/nvidia_icd.json \
             /usr/share/vulkan/implicit_layer.d/nvidia_layers.json; do
        [ -e "$f" ] && out="$out --volume $f:$f:ro"
    done

    printf '%s' "$out"
}
