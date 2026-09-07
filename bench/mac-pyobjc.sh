# run-benchmark sizes the screen and warps the cursor through AppKit and Quartz, and so does the raiser; Xcode's /usr/bin/python3 carries neither. Pinned and installed together: webkitpy pins its frameworks to whatever pyobjc-core reports.

WK_PYOBJC_VERSION="${WK_PYOBJC_VERSION:-11.1}"
WK_PYOBJC_PYTHON="${WK_PYOBJC_PYTHON:-/usr/bin/python3}"

wk_pyobjc_version() {   # what is importable now, or empty
    "$WK_PYOBJC_PYTHON" -c 'import objc, AppKit, Quartz; print(objc.__version__)' 2>/dev/null
}

wk_pyobjc_have() { [ "$(wk_pyobjc_version)" = "$WK_PYOBJC_VERSION" ]; }

wk_pyobjc_install() {
    wk_pyobjc_have && return 0
    # shellcheck disable=SC1091 -- a guest reaches PyPI only through this machine's proxy, and this shell reads no profile. `${HOME:-}` because a LaunchDaemon inherits no environment at all, and under `set -u` a bare $HOME ends the script that sourced this one.
    if [ -r "${HOME:-}/.wk-egress" ]; then
        . "${HOME:-}/.wk-egress"
    fi
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
