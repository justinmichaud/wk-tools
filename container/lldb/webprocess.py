# `wk-follow-page`, aliased by `wk gui --lldb web` with this port's web-process
# name: PSON swaps a page into a fresh web process silently, so ask them all.

import os

import lldb

# m_pageMap's count read as memory: WTF::HashTable keeps it before the table
# (keyCountOffset = -3, HashTable.h:614), and size() in the inferior SIGSEGVs.
_PAGES = ("(unsigned)(WebKit::WebProcess::singleton().m_pageMap.m_impl.m_table"
          " ? ((unsigned*)WebKit::WebProcess::singleton()"
          ".m_pageMap.m_impl.m_table)[-3] : 0)")
_SUSPENDED = "(int)WebKit::WebProcess::singleton().m_hasSuspendedPageProxy"


def _pids_named(name):
    want = name[:15]      # /proc/<pid>/comm truncates at 15, as WebKitWebProcess does
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


def _ensure_stopped(process):   # an expression evaluates only in a stopped one
    if process.GetState() == lldb.eStateStopped:
        return True
    return process.Stop().Success() and process.GetState() == lldb.eStateStopped


def _read_int(process, expr):   # None when it cannot be answered
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
    opened = []           # attached by us, and detached below unless kept

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

        # After a swap both report a page; the stale one is m_hasSuspendedPageProxy.
        live = [c for c in candidates if c[2] and not c[3]]

        if not live:
            unreadable = [c for c in candidates if c[2] is None]
            if unreadable:
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
