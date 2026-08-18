# GPU passthrough for workspaces.
#
# Benchmarks that matter -- Speedometer, MotionMark, anything touching WebGL --
# are meaningless under llvmpipe, so a workspace has to reach the real device.
# For a Mesa GPU that is one flag: the container's own Mesa drives /dev/dri and
# nothing else is needed.
#
# NVIDIA is the awkward case, because the userspace libraries must match the
# host's kernel driver exactly (580.173.02 here) and cannot be installed from
# inside an offline container. Two ways to get them in:
#
#   CDI       nvidia-ctk generates /etc/cdi/nvidia.yaml listing every device
#             node and library to inject. This is what upstream wkdev uses, and
#             it works rootless. It needs nvidia-container-toolkit, which is
#             NOT in Ubuntu 26.04: verified against Launchpad, the package is
#             published in universe for 26.10 (stonking) only. On 26.04 it comes
#             from NVIDIA's own apt repository, a second third-party repo.
#
#   derived   Work out the same list from ldconfig and the glvnd/EGL vendor
#             configs, and bind-mount each file at its own path. No extra
#             package, no extra repository, and the mounts are visible in
#             `podman inspect` rather than hidden behind a CDI spec.
#
# The derived path is the default so a stock 26.04 install benchmarks out of the
# box; CDI is used automatically when it happens to be present, because it also
# handles the odd corners (nvidia-persistenced sockets, MIG, ldcache updates).

# gpu_flags -- print podman flags for GPU access, one per line-continued string.
gpu_flags() {
    local flags=""

    # The render node is what actually does the rendering; the card node is
    # only needed for modesetting, which a workspace never does -- the
    # compositor owns that.
    if [ -e /dev/dri/renderD128 ]; then
        flags="$flags --device /dev/dri"
    else
        warn "no /dev/dri render node; workspaces will fall back to software rendering"
        return 0
    fi

    # Counted into a variable rather than `lsmod | grep -q`: grep -q exits at
    # the first match, lsmod takes SIGPIPE, and under `set -o pipefail` the
    # whole pipeline reports failure -- so the nvidia branch is silently never
    # taken and the workspace gets llvmpipe. This cost an hour once.
    local nvidia_modules
    nvidia_modules=$(lsmod 2>/dev/null | grep -c '^nvidia' || true)
    if [ "${nvidia_modules:-0}" -eq 0 ]; then
        # Mesa: nothing further to inject.
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

# Every file the NVIDIA userspace stack needs inside the container, mounted
# read-only at its own path so the dynamic loader and glvnd find them where
# they expect. Missing entries are skipped rather than fatal: the set differs
# between driver branches, and a hard list would rot at the next release.
_nvidia_derived_flags() {
    local out="" f

    # Device nodes. /dev/nvidia-caps is a directory and only matters for MIG.
    for f in /dev/nvidia0 /dev/nvidiactl /dev/nvidia-modeset \
             /dev/nvidia-uvm /dev/nvidia-uvm-tools; do
        [ -e "$f" ] && out="$out --device $f"
    done

    # The libraries, taken from the loader's own cache so the list follows the
    # installed driver rather than a guess. Both the versioned file and its
    # SONAME symlink are needed: glvnd dlopens by SONAME.
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

    # The GBM backend, so EGL can render to a dmabuf the compositor can import.
    for f in /usr/lib/*/gbm/nvidia-drm_gbm.so; do
        [ -e "$f" ] && out="$out --volume $f:$f:ro"
    done

    # Vendor configuration. Without these glvnd loads Mesa and reports
    # llvmpipe, which is the exact failure this whole file exists to prevent.
    for f in /usr/share/glvnd/egl_vendor.d/10_nvidia.json \
             /usr/share/egl/egl_external_platform.d/10_nvidia_wayland.json \
             /usr/share/egl/egl_external_platform.d/15_nvidia_gbm.json \
             /usr/share/vulkan/icd.d/nvidia_icd.json \
             /usr/share/vulkan/implicit_layer.d/nvidia_layers.json; do
        [ -e "$f" ] && out="$out --volume $f:$f:ro"
    done

    printf '%s' "$out"
}
