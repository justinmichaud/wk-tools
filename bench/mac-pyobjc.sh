# What makes a macOS install able to drive a browser at a benchmark. run-benchmark
# sizes the screen and warps the cursor through AppKit and Quartz
# (webkitpy/benchmark_runner/browser_driver/osx_browser_driver.py), and wk's raiser
# (bench/mac-raiser.sh) keeps MiniBrowser frontmost through the same modules.
# Xcode's /usr/bin/python3 carries none of them, so every macOS install wk
# provisions installs them here: the guest at `wk vm start`, the benchmark install
# at first boot, this Mac at ./setup.
#
# Pinned, and all three together: webkitpy autoinstalls the two frameworks at
# pyobjc-core's own version, so a mismatch is a second import path nobody chose,
# and two arms collected on different installs must be collected by the same code.
# Sourced, not run; the caller provides info/warn.

WK_PYOBJC_VERSION="${WK_PYOBJC_VERSION:-11.1}"
WK_PYOBJC_PYTHON="${WK_PYOBJC_PYTHON:-/usr/bin/python3}"

wk_pyobjc_version() {   # what is importable now, or empty
    "$WK_PYOBJC_PYTHON" -c 'import objc, AppKit, Quartz; print(objc.__version__)' 2>/dev/null
}

wk_pyobjc_have() { [ "$(wk_pyobjc_version)" = "$WK_PYOBJC_VERSION" ]; }

wk_pyobjc_install() {
    wk_pyobjc_have && return 0
    # A guest reaches PyPI only through this machine's egress proxy, and the
    # provisioning bundle arrives on a shell that reads no profile.
    # shellcheck disable=SC1091
    [ -r "$HOME/.wk-egress" ] && . "$HOME/.wk-egress"
    "$WK_PYOBJC_PYTHON" -m pip install --user --disable-pip-version-check --no-warn-script-location \
        "pyobjc-core==$WK_PYOBJC_VERSION" \
        "pyobjc-framework-Cocoa==$WK_PYOBJC_VERSION" \
        "pyobjc-framework-Quartz==$WK_PYOBJC_VERSION" >&2 || return 1
    wk_pyobjc_have
}

wk_pyobjc_findings() {   # state <tab> what <tab> remedy
    local v; v=$(wk_pyobjc_version)
    if [ "$v" = "$WK_PYOBJC_VERSION" ]; then
        printf 'ok\tpyobjc %s is importable, so a browser can be driven and raised here\t\n' "$v"
    elif [ -n "$v" ]; then
        printf 'wrong\tpyobjc here is %s and this fleet measures with %s -- webkitpy pins its frameworks to whatever pyobjc-core reports\twk vm start <name>, or ./setup on a workstation\n' \
            "$v" "$WK_PYOBJC_VERSION"
    else
        printf 'wrong\tno pyobjc: run-benchmark cannot size the screen and nothing can keep MiniBrowser frontmost, so a run here measures a throttled browser\twk vm start <name>, or ./setup on a workstation\n'
    fi
}
