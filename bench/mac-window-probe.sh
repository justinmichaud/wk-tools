# What is actually on the screen, from the window server rather than from a
# list of process names: CGWindowListCopyWindowInfo answers for every window
# that is on screen, whether or not its owner is an application. Owner, layer
# and bounds need no Screen Recording permission -- window *titles* do, and
# nothing here asks for one. Sourced, never run.

# The windows wk itself puts on a measured Mac's screen. Anything else holding
# a layer-0 window came up on its own and is over the thing being measured;
# higher layers are the menu bar, the Dock and Notification Centre's own
# click-catcher, which are always there and cover nothing.
wk_window_expected() {
    printf '%s\n' "${WK_SCREEN_EXPECTED:-Terminal|Finder|MiniBrowser|Safari}" | tr '|' '\n'
}

# Every layer-0 window in a `windows=` reading whose owner wk did not put there.
wk_window_unexpected() { # <windows reading>
    local entry owner layer expected
    expected=$(wk_window_expected)
    printf '%s' "$1" | tr ';' '\n' | while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        owner="${entry%%:*}"
        layer="${entry#*:}"; layer="${layer%%:*}"
        [ "$layer" = 0 ] || continue
        printf '%s\n' "$expected" | grep -qxF "$owner" || printf '%s;' "$entry"
    done
}

_wk_window_src() { echo "${TMPDIR:-/tmp}/.wk-window-probe.c"; }
_wk_window_bin() { echo "${TMPDIR:-/tmp}/.wk-window-probe"; }

_wk_window_build() {
    local src bin
    src=$(_wk_window_src); bin=$(_wk_window_bin)
    cat > "$src.new" <<'PROBE'
#include <ApplicationServices/ApplicationServices.h>
#include <stdio.h>

static long num(CFDictionaryRef d, CFStringRef k) {
    CFNumberRef n = CFDictionaryGetValue(d, k);
    long v = 0;
    if (n) CFNumberGetValue(n, kCFNumberLongType, &v);
    return v;
}

int main(void) {
    CFArrayRef ws = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID);
    if (!ws) { fprintf(stderr, "the window server returned no list\n"); return 1; }
    for (CFIndex i = 0; i < CFArrayGetCount(ws); i++) {
        CFDictionaryRef w = CFArrayGetValueAtIndex(ws, i);
        CFStringRef owner = CFDictionaryGetValue(w, kCGWindowOwnerName);
        CFDictionaryRef b = CFDictionaryGetValue(w, kCGWindowBounds);
        CGRect r = CGRectZero;
        if (b) CGRectMakeWithDictionaryRepresentation(b, &r);
        char name[256] = "?";
        if (owner) CFStringGetCString(owner, name, sizeof name, kCFStringEncodingUTF8);
        for (char *p = name; *p; p++) if (*p == '|' || *p == ';' || *p == ':') *p = ' ';
        printf("%s:%ld:%.0fx%.0f@%.0f,%.0f\n", name, num(w, kCGWindowLayer),
               r.size.width, r.size.height, r.origin.x, r.origin.y);
    }
    CFRelease(ws);
    return 0;
}
PROBE
    # Only when it changed, or the rebuild is a second of every `wk vm check`.
    if cmp -s "$src.new" "$src" && [ -x "$bin" ]; then
        rm -f "$src.new"
        return 0
    fi
    mv "$src.new" "$src"
    cc -O1 -o "$bin" "$src" -framework ApplicationServices 2>/dev/null
}

# `windows=<owner>:<layer>:<w>x<h>@<x>,<y>;...`, one entry per on-screen window.
# `?` means it could not be asked, which is not "nothing on screen".
wk_window_probe() {
    if [ "$(uname -s)" != Darwin ] || ! command -v cc >/dev/null 2>&1; then
        printf 'windows=?\n'
        return 0
    fi
    _wk_window_build || { printf 'windows=?\n'; return 0; }
    printf 'windows=%s\n' "$("$(_wk_window_bin)" 2>/dev/null | tr '\n' ';')"
}
