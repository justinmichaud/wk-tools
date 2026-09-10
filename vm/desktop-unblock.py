#!/usr/bin/env python3
"""Press Setup Assistant's panes away over the Accessibility API, which answers
an ssh session because the guest runs with SIP disabled. Elements are chosen by
AXIdentifier, never by position."""
import ctypes
import subprocess
import sys
import time
from ctypes import byref, c_int, c_uint32, c_void_p

AS = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
UTF8 = 0x08000100

CF.CFStringCreateWithCString.restype = c_void_p
CF.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
CF.CFStringGetCString.argtypes = [c_void_p, c_void_p, c_int, c_uint32]
CF.CFArrayGetCount.restype = c_int
CF.CFArrayGetCount.argtypes = [c_void_p]
CF.CFArrayGetValueAtIndex.restype = c_void_p
CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_int]
CF.CFGetTypeID.restype = ctypes.c_ulong
CF.CFGetTypeID.argtypes = [c_void_p]
CF.CFStringGetTypeID.restype = ctypes.c_ulong
CF.CFArrayGetTypeID.restype = ctypes.c_ulong
CF.CFBooleanGetTypeID.restype = ctypes.c_ulong
CF.CFBooleanGetValue.restype = ctypes.c_bool
CF.CFBooleanGetValue.argtypes = [c_void_p]
AS.AXIsProcessTrusted.restype = ctypes.c_bool
AS.AXUIElementCreateApplication.restype = c_void_p
AS.AXUIElementCreateApplication.argtypes = [c_int]
AS.AXUIElementCopyAttributeValue.restype = c_int
AS.AXUIElementCopyAttributeValue.argtypes = [c_void_p, c_void_p, c_void_p]
AS.AXUIElementPerformAction.restype = c_int
AS.AXUIElementPerformAction.argtypes = [c_void_p, c_void_p]

PROC = "Setup Assistant.app/Contents/MacOS"
ACTIONABLE = ("AXButton", "AXMenuItem")
# Highest first: a confirmation sheet's Skip settles the pane behind it.
BY_ID = ("action-button-1", "userDeclinediCloud", "Next Button", "Alternate Button")
SKIP_TITLES = ("skip", "later", "not now", "continue", "set up later", "don't")
NEVER = ("Previous Button", "action-button-2")
STALL_PASSES = 15


def _str(x):
    return CF.CFStringCreateWithCString(None, x.encode(), UTF8)


def _text(v):
    if not v or CF.CFGetTypeID(v) != CF.CFStringGetTypeID():
        return None
    buf = ctypes.create_string_buffer(2048)
    return buf.value.decode() if CF.CFStringGetCString(v, buf, 2048, UTF8) else None


def _attr(el, name):
    out = c_void_p()
    return out if AS.AXUIElementCopyAttributeValue(el, _str(name), byref(out)) == 0 else None


def _enabled(el):
    v = _attr(el, "AXEnabled")
    if not v or CF.CFGetTypeID(v) != CF.CFBooleanGetTypeID():
        return True
    return CF.CFBooleanGetValue(v)


def _items(v):
    if not v or CF.CFGetTypeID(v) != CF.CFArrayGetTypeID():
        return []
    return [CF.CFArrayGetValueAtIndex(v, i) for i in range(CF.CFArrayGetCount(v))]


def _collect(el, depth=0, acc=None):
    if acc is None:
        acc = []
    role = _text(_attr(el, "AXRole"))
    if role in ACTIONABLE and _enabled(el):
        acc.append((_text(_attr(el, "AXIdentifier")) or "",
                    (_text(_attr(el, "AXTitle")) or "").strip(), el))
    if depth < 8:
        for child in _items(_attr(el, "AXChildren")):
            _collect(child, depth + 1, acc)
    return acc


def _pid():
    out = subprocess.run(["/usr/bin/pgrep", "-f", PROC], capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split()]
    return pids[0] if pids else None


def _pick(cands):
    live = [c for c in cands if c[0] not in NEVER]
    for want in BY_ID:
        for ident, title, el in live:
            if ident == want:
                return ident or title, el
    for ident, title, el in live:
        if any(t in title.lower() for t in SKIP_TITLES):
            return ident or title, el
    return None, None


def _pane(pid):
    app = AS.AXUIElementCreateApplication(pid)
    for win in _items(_attr(app, "AXWindows")):
        cands = _collect(win)
        if cands:
            return _text(_attr(win, "AXIdentifier")) or "?", cands
    return None, []


def main():
    passes = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    if not AS.AXIsProcessTrusted():
        print("desktop-unblock: the Accessibility API is not answering this process",
              file=sys.stderr)
        return 2

    # A pane disables every control while it settles the last answer, so one empty reading is a wait; only an unbroken run of them is a dead end.
    stalled, last = 0, "?"
    for _ in range(passes):
        pid = _pid()
        if pid is None:
            print("desktop-unblock: Setup Assistant is gone")
            return 0
        name, cands = _pane(pid)
        what, el = _pick(cands) if cands else (None, None)
        if el is None:
            stalled += 1
            if stalled > STALL_PASSES:
                # Measured: the account pane spins for good once declined. Every answer is already written, and the login after this one settles whether the flow finished.
                print(f"desktop-unblock: pane {name or last!r} stopped responding; "
                      f"quitting Setup Assistant", file=sys.stderr)
                subprocess.run(["/usr/bin/killall", "Setup Assistant"],
                               capture_output=True)
                time.sleep(2)
                return 0 if _pid() is None else 1
            time.sleep(2)
            continue
        stalled, last = 0, name
        AS.AXUIElementPerformAction(el, _str("AXPress"))
        print(f"desktop-unblock: {name} -> pressed {what!r}")
        time.sleep(2)

    print(f"desktop-unblock: Setup Assistant still up after {passes} passes", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
