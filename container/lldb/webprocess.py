# `wk-follow-page` -- find the web process a page is actually in, and stay there.
#
# Imported into lldb by `wk gui --lldb web` (cmd/gui), which also aliases it to
# `follow-page` with this port's web-process name filled in. The name is passed
# rather than written down here, because build/configs.sh already owns it:
# WPEWebProcess on WPE, WebKitWebProcess on GTK.
#
# The problem it solves: WebKit moves a page between processes. A cross-site
# navigation swaps in a fresh web process (PSON, which cannot be turned off on
# these ports), and the debugger is left attached to a
# process the page has left. Nothing announces this. There is no error and no
# exit; the breakpoints simply stop being reached, which reads as "my breakpoint
# is wrong" rather than "I am in the wrong process".
#
# So rather than guess, attach to every web process there is and ask each one
# what it is holding. Two reads answer it, and both were measured across a real
# cross-site navigation:
#
#   pages   entries in WebProcess::m_pageMap. 0 means a prewarmed process that
#           has never held a page.
#   susp    WebProcess::m_hasSuspendedPageProxy, which the UI process sets on
#           the process a page has been swapped *out* of and which is being kept
#           for the back/forward list (WebProcessProxy.cpp:2559).
#
#   pid 2488063  pages=1  susp=1   <- the old process, page already gone
#   pid 2488106  pages=1  susp=0   <- the live page
#   pid 2488132  pages=0  susp=0   <- prewarmed
#
# pages alone is not enough: two processes report a page after a swap, and the
# stale one is the one that was attached. Both reads together are exact.
#
# m_pageMap is read rather than size() being called. WTF::HashTable keeps its
# key count in the four words *before* the table (keyCountOffset = -3,
# HashTable.h:614), so this is a memory read; calling size() in the inferior was
# measured taking SIGSEGV.

import os

import lldb

_PAGES = ("(unsigned)(WebKit::WebProcess::singleton().m_pageMap.m_impl.m_table"
          " ? ((unsigned*)WebKit::WebProcess::singleton()"
          ".m_pageMap.m_impl.m_table)[-3] : 0)")
_SUSPENDED = "(int)WebKit::WebProcess::singleton().m_hasSuspendedPageProxy"


def _pids_named(name):
    """Every running pid whose comm is `name`.

    /proc/<pid>/comm is the kernel's copy of the name, truncated to 15
    characters -- which WebKitWebProcess exceeds and WPEWebProcess does not, so
    the comparison is against the truncation rather than the name.
    """
    want = name[:15]
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "comm")) as handle:
                if handle.read().strip() == want:
                    found.append(int(entry))
        except (OSError, IOError):
            continue          # exited between listdir and open; not a candidate
    return sorted(found)


def _ensure_stopped(process):
    """Expressions only evaluate in a stopped process, and the one already
    attached is normally running -- `continue` is how you got here. Stopping is
    therefore part of asking, not something the caller has to remember; the
    processes that turn out to be the wrong ones are resumed by the detach
    below, and the right one is left stopped because that is where breakpoints
    go next."""
    if process.GetState() == lldb.eStateStopped:
        return True
    return process.Stop().Success() and process.GetState() == lldb.eStateStopped


def _read_int(process, expr):
    """Evaluate `expr` in a stopped process, or None if it cannot be answered."""
    if not _ensure_stopped(process):
        return None
    thread = process.GetSelectedThread()
    if not thread.IsValid():
        return None
    frame = thread.GetFrameAtIndex(0)
    if not frame.IsValid():
        return None
    value = frame.EvaluateExpression(expr)
    if not value.IsValid() or value.GetError().Fail():
        return None
    return value.GetValueAsUnsigned()


def _targets_by_pid(debugger):
    out = {}
    for i in range(debugger.GetNumTargets()):
        target = debugger.GetTargetAtIndex(i)
        process = target.GetProcess()
        if process.IsValid() and process.GetProcessID() != lldb.LLDB_INVALID_PROCESS_ID:
            out[int(process.GetProcessID())] = target
    return out


def follow_page(debugger, command, result, internal_dict):
    name = command.strip() or "WPEWebProcess"

    pids = _pids_named(name)
    if not pids:
        result.SetError("no %s is running" % name)
        return

    was_async = debugger.GetAsync()
    debugger.SetAsync(False)
    existing = _targets_by_pid(debugger)
    # Attached here rather than found already attached -- detached again below
    # unless it turns out to be the one worth keeping.
    opened = []

    try:
        candidates = []
        for pid in pids:
            target = existing.get(pid)
            if target is None:
                target = debugger.CreateTarget("")
                error = lldb.SBError()
                process = target.AttachToProcessWithID(
                    debugger.GetListener(), pid, error)
                if error.Fail() or not process.IsValid():
                    debugger.DeleteTarget(target)
                    print("  pid %-7d could not attach: %s"
                          % (pid, error.GetCString() or "unknown error"))
                    continue
                opened.append(target)
            else:
                process = target.GetProcess()

            pages = _read_int(process, _PAGES)
            susp = _read_int(process, _SUSPENDED)
            candidates.append((pid, target, pages, susp))
            print("  pid %-7d pages=%s suspended=%s%s"
                  % (pid,
                     "?" if pages is None else pages,
                     "?" if susp is None else susp,
                     "   <- attached" if existing.get(pid) is not None else ""))

        live = [c for c in candidates if c[2] and not c[3]]

        if not live:
            unreadable = [c for c in candidates if c[2] is None]
            if unreadable:
                # Almost always this one: WEBKIT_PAUSE_WEB_PROCESS_ON_LAUNCH
                # holds every new web process for 30 s, which is what makes the
                # attach reliable -- and until it ends the process has no
                # WebProcess to ask, and the navigation that started it has not
                # finished either.
                result.SetError(
                    "no %s is holding a live page yet, and %d could not be "
                    "asked -- they are still inside the 30 s launch pause. "
                    "Give it a moment and run this again. Nothing was detached."
                    % (name, len(unreadable)))
            else:
                result.SetError(
                    "no %s is holding a live page (every one is prewarmed or "
                    "suspended). Nothing was detached." % name)
            return
        if len(live) > 1:
            # Not seen, and not guessed at either: picking one would be the same
            # silent wrong answer this command exists to remove.
            result.SetError(
                "%d processes each hold a live page (%s) -- more than one page "
                "is open, so which one to debug is a choice this cannot make. "
                "Attach by pid. Nothing was detached."
                % (len(live), ", ".join(str(c[0]) for c in live)))
            return

        pid, keep, _, _ = live[0]
        for other_pid, target, _, _ in candidates:
            if target is keep:
                continue
            process = target.GetProcess()
            if process.IsValid():
                process.Detach()
            debugger.DeleteTarget(target)
        debugger.SetSelectedTarget(keep)
        opened = []           # kept or already detached; nothing left to undo

        print("\nstaying with pid %d -- the page is in it, and it is stopped."
              % pid)
        print("Breakpoints do not carry across processes: set them again here.")
    finally:
        for target in opened:
            process = target.GetProcess()
            if process.IsValid():
                process.Detach()
            debugger.DeleteTarget(target)
        debugger.SetAsync(was_async)


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand(
        "command script add -f webprocess.follow_page wk-follow-page")
