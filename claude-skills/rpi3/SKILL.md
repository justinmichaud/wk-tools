---
name: rpi3
description: Use for running, monitoring, and debugging WebKit browser benchmarks (Speedometer 2.1/3.1, JetStream 2.2/3.0) on the Raspberry Pi 3 test device — a 32-bit WPE/cog/weston buildroot-yocto board. Covers ssh in, weston/seat setup, launching benchmarks, detecting completion/crashes, and root-causing failures (OOM→swap, SIGSEGV→gdb-attach) including every workaround for this specific board. Prompts for the Pi's IP address.
user-invocable: true
allowed-tools:
  - Bash(ssh:*)
  - Bash(scp:*)
---

# Running & debugging WebKit benchmarks on the rpi3

The rpi3 is a 32-bit ARM (`armv7l`) Raspberry Pi 3 test board running a buildroot/yocto
image with **WPE WebKit** driven by **cog** on a **weston** wayland compositor. It has
**931 MB RAM** and (by default) **no swap** — memory is the dominant constraint. A WebKit
build is deployed under `/WebKit/WebKit` (an ext4 disk on `/dev/sda1` mounted at `/WebKit`).

## 0. Get the IP address (always do this first)

**Ask the user for the Pi's IP address before doing anything.** It has historically been
`root@192.168.1.159`, but confirm — offer that as the default. Log in as **root** (no
password / key-based). Everything below assumes `SSH="ssh root@<ip>"`.

Quick connectivity + state check:
```
ssh root@<ip> 'uname -a; mount | grep sda; systemctl is-active weston; pidof weston-desktop-shell'
```

> **Gotcha — ssh drops (exit 255) when you `pkill` cog.** Killing `cog`/the web process
> frequently tears down the ssh session (exit code 255). This is harmless: just reconnect
> and re-check state. Prefer running `pkill -f "cog -P wl"` in its **own** ssh call (don't
> chain important commands after it in the same session).

## 1. Bring up the graphical session

weston is a systemd service but is normally inactive. netdata competes for resources, so
stop it first (per the board's runbook):
```
ssh root@<ip> 'systemctl stop netdata; systemctl start weston; sleep 4; pidof weston-desktop-shell'
```
If `/WebKit` isn't mounted: `sudo mkdir -p /WebKit && sudo mount /dev/sda1 /WebKit`.

### Deploying a build (only if you uploaded a fresh archive)
The checkout lives at `/WebKit/WebKit`. To extract an uploaded product archive:
```
cd /WebKit/WebKit && Tools/CISupport/built-product-archive --platform=wpe --release extract
```
Build lives at `/WebKit/WebKit/WebKitBuild/WPE/Release/{bin,lib}`. The `jsc` shell is at
`.../bin/jsc`; `cog` at `.../Tools/cog-prefix/src/cog-build/launcher/cog`.

> **Which build am I on?** The `libWPEWebKit-X.Y.so.N` SONAME only reflects the **WPE API
> version** compiled against — it is NOT a code-version indicator. To tell builds apart,
> use **file timestamps** (`ls -l .../bin/jsc`), not the SONAME.

## 2. Fake input seat (REQUIRED before launching cog)

cog aborts at startup without an input seat:
```
Cog-Wayland:ERROR ... cog_wl_platform_create_im_context: assertion failed: (display->seat_default)
```
Create a virtual uinput device and leave it running for the whole session. Use
`scripts/fakeseat.py` (scp it over), or the one-liner from the board runbook:
```
scp scripts/fakeseat.py root@<ip>:/tmp/
ssh root@<ip> 'setsid python3 /tmp/fakeseat.py < /dev/null > /tmp/fakeseat.log 2>&1 & sleep 2; pgrep -f fakeseat.py && echo seat-alive'
```
Verify with `pgrep -f fakeseat.py`. Clean up with `pkill -f fakeseat.py`. The seat survives
across benchmark runs — start it once per session.

## 3. Get weston's wayland env

Benchmark launches need `XDG_RUNTIME_DIR` and `WAYLAND_DISPLAY` matching the running weston.
They are typically `/run/user/1000` and `wayland-1`. To read them live:
```
ssh root@<ip> 'strings /proc/$(pidof weston-desktop-shell)/environ | grep -P "(XDG_RUNTIME_DIR|WAYLAND_DISPLAY)"'
```
The helper scripts hard-code `/run/user/1000` + `wayland-1`; update them if weston differs.

## 4. Launching benchmarks

Benchmark URLs (browserbench.org):
| Bench | URL |
|-------|-----|
| Speedometer 3.1 | `https://browserbench.org/Speedometer3.1/?startAutomatically=true` |
| Speedometer 2.1 (SP2) | `https://browserbench.org/Speedometer2.1/InteractiveRunner.html?startAutomatically` |
| JetStream 3.0 | `https://browserbench.org/JetStream3.0/?startAutomatically=1` |
| JetStream 2.2 (JS2) | `https://browserbench.org/JetStream2.2/?report=true` |

**Two ways to launch:**

- **Simple (matches the board runbook):**
  ```
  cd /WebKit/WebKit
  source <(strings /proc/$(pidof weston-desktop-shell)/environ | grep -P '(XDG_RUNTIME_DIR|WAYLAND_DISPLAY)') && export XDG_RUNTIME_DIR && export WAYLAND_DISPLAY
  Tools/Scripts/run-minibrowser --wpe "<URL>" -P wl
  ```
  (`run-minibrowser --wpe` actually launches cog under the hood. It does NOT enable console
  output, so you can't see JS console/crash detail this way.)

- **Recommended — cog directly with console output** (`scripts/run-cog.sh <log> <url> [--debug]`):
  launches cog with `--enable-write-console-messages-to-stdout=1` so console messages, load
  status, and `Crash!` warnings land in `<log>`. Run detached and capture the log:
  ```
  scp scripts/run-cog.sh scripts/monitor.sh root@<ip>:/tmp/
  ssh root@<ip> 'chmod +x /tmp/run-cog.sh /tmp/monitor.sh; rm -f /tmp/js2.log; \
     setsid /tmp/run-cog.sh /tmp/js2.log "<URL>" < /dev/null > /dev/null 2>&1 & echo launched'
  ```
  Add `--debug` (3rd arg) to disable the sandbox — needed for gdb (see §6).

## 5. Detecting completion / crashes

There is **no easy score capture**: `weston-screenshooter` is gated on this board ("Output
capture error: unauthorized"), and the benchmarks don't print their score to the console
(SP2's InteractiveRunner and JS2's `report=true` keep results in the DOM only). So confirm
**completion**, not the numeric score. For real numeric scores use WebKit's official harness
`Tools/Scripts/run-benchmark` (a `linux_minibrowserwpe_driver` and speedometer2.1/jetstream2.2
plans exist) — it also needs the fake seat from §2.

Use `scripts/monitor.sh <log> <maxsecs>` (run it as a background/detached process — it prints
a single verdict at the end). It watches the `WPEWebProcess` and emits:
- **`CRASH_OR_ASSERT`** — crash/assert/OOM string appeared in the console log.
- **`WEBPROC_GONE`** — the web process vanished after having run.
- **`IDLE_DONE`** — ran hot, then went **truly idle** (low CPU *and* low load-average for
  120 s) → finished / sitting on results screen.
- **`TIMEOUT`** — hit the cap.

> **Why the load-average check matters:** under swap (§6) the web process can be busy but
> show low `%CPU` (blocked in D-state on swap I/O). CPU-only idle detection would then falsely
> report "done". Requiring low loadavg *and* low CPU avoids that.

Rough wall-clock on this board: **SP2 ≈ 8 min**; **JS2 ≈ 34 min** (longer with swap or under gdb).

## 6. Debugging failures

### Failure A — OOM kill (the #1 issue for JetStream on this board)
Symptom: web process dies mid-run; console shows repeated `Memory pressure relief:` then
`Crash!: The renderer process crashed`. Confirm it's the kernel OOM-killer:
```
ssh root@<ip> 'dmesg -T | grep -iE "oom|Killed process"; grep oom_kill /proc/vmstat'
```
`... invoked oom-killer ... Out of memory: Killed process NNNN (WPEWebProcess) anon-rss:~700MB`
with 931 MB RAM and **no swap** is the classic case.

**Fix — enable swap.** There is a pre-formatted (but not enabled) `/WebKit/swapfile` (1 GB),
and `/WebKit` has several GB free. Enable it and add more headroom (~3 GB total is comfortable):
```
ssh root@<ip> 'chmod 600 /WebKit/swapfile; swapon /WebKit/swapfile; \
  [ -f /WebKit/swapfile2 ] || { fallocate -l 2G /WebKit/swapfile2 && chmod 600 /WebKit/swapfile2 && mkswap /WebKit/swapfile2; }; \
  swapon /WebKit/swapfile2; swapon --show; free -m'
```
With swap enabled JS2 gets much further (and runs slower). If it then crashes with only tens
of MB of swap used, memory was NOT the limiter → it's a real crash (Failure B).

### Failure B — SIGSEGV in the web process (a real JSC crash)
Confirm it's a signal, not OOM:
```
ssh root@<ip> 'dmesg -T | grep -iE "sig=11|WPEWebProcess.*sig"; grep oom_kill /proc/vmstat'
```
`audit: type=1701 ... comm="WPEWebProcess" ... sig=11` = SIGSEGV. If `oom_kill` didn't
increment, it's not the OOM-killer.

**Core dumps DO NOT work here:** WebKit makes the web process non-dumpable
(`RLIMIT_CORE=0` / `PR_SET_DUMPABLE 0`), so the kernel writes no core even with
`kernel.core_pattern` set to an absolute path. **Use gdb-attach instead.**

Use `scripts/attach-gdb.sh <url> [bt_log] [console_log]`. It launches cog with the **sandbox
disabled** (so `WPEWebProcess` is a plain child you can attach to), attaches gdb, and captures
the fatal backtrace. Critically it does `handle SIGUSR1/SIGUSR2 nostop noprint pass` — **WTF
uses SIGUSR1 for GC stop-the-world thread suspension**, and a plain `continue` would trap that
benign signal instead of the crash. Run it detached (it takes ~35 min for JS2), then poll the
bt log for `FATAL SIGNAL CAUGHT` / `GDB DONE`:
```
scp scripts/attach-gdb.sh root@<ip>:/tmp/
ssh root@<ip> 'chmod +x /tmp/attach-gdb.sh; setsid /tmp/attach-gdb.sh "<URL>" /tmp/bt.log /tmp/console.log < /dev/null >/dev/null 2>&1 & echo launched'
# ...later...
ssh root@<ip> 'grep -vE "New LWP|New Thread|Thread .* exited|Thread debugging|host libthread" /tmp/bt.log'
```
Read the fault address: a small/near-null `si_addr` suggests an allocation-failure null-deref
(still memory-related); a plausible-but-unmapped heap pointer (e.g. reading `[butterfly-4]`)
suggests a **dangling/freed pointer (use-after-free / GC corruption)**.

### Fast standalone repro attempts in the jsc shell
Some crashes only reproduce under the browser's real memory pressure and will **not** repro in
`jsc` (which owns the whole machine). Still worth trying — it's seconds vs. a 34-min browser
run. Use `scripts/jsc-gdb.sh <jsfile> [env JSC_*=...]`:
```
scp scripts/jsc-gdb.sh /path/to/repro.js root@<ip>:/tmp/
ssh root@<ip> 'chmod +x /tmp/jsc-gdb.sh; JSC_collectContinuously=1 /tmp/jsc-gdb.sh /tmp/repro.js'
```
GC-stress options worth trying: `JSC_collectContinuously=1`, `JSC_useConcurrentGC=0` (nocgc),
`JSC_useGenerationalGC=0` (nogen), `JSC_useJIT=0` (nojit — forces LLInt, giving cleaner
symbolized frames), `JSC_scribbleFreeCells=1`. If a crash reproduces with JIT off AND
concurrent/generational GC off, it's tier/GC-mode independent (points at the object model /
allocator / a backport, not the JIT).

### Direct cog debugging launch (single URL, console to stdout)
```
JSC_validateOptions=1 WEBKIT_EXEC_PATH=/WebKit/WebKit/WebKitBuild/WPE/Release/bin \
WEBKIT_INJECTED_BUNDLE_PATH=/WebKit/WebKit/WebKitBuild/WPE/Release/lib \
LD_LIBRARY_PATH=/WebKit/WebKit/WebKitBuild/WPE/Release/lib \
COG_MODULEDIR=/WebKit/WebKit/WebKitBuild/WPE/Release/Tools/cog-prefix/src/cog-build/platform \
/WebKit/WebKit/WebKitBuild/WPE/Release/Tools/cog-prefix/src/cog-build/launcher/cog \
--enable-write-console-messages-to-stdout=1 <URL>
```

## Workarounds & gotchas (quick reference)

| Symptom | Cause / Fix |
|---|---|
| `seat_default` assertion at cog startup | No input seat → run `fakeseat.py` (§2), leave running. |
| ssh exits 255 right after `pkill cog` | Session teardown; harmless — reconnect. Isolate `pkill` in its own ssh call. |
| `weston-screenshooter` → "unauthorized" | Screenshooter is gated; can't screenshot. Use monitor heuristics / `run-benchmark` for scores. |
| No score in console | Benchmarks keep results in DOM. Use `run-benchmark` for numbers; else confirm completion only. |
| JS2 dies ~13 min, `Killed process ... WPEWebProcess` | Kernel OOM (931 MB RAM, no swap) → enable swap (§6A). |
| Web process idle but not done (swap thrash) | Low %CPU but high loadavg → monitor requires BOTH low (§5). |
| No core file despite `core_pattern` set | Web process is non-dumpable → gdb-attach instead (§6B). |
| gdb stops on `SIGUSR1` (thread in futex/park) | WTF GC suspension signal → `handle SIGUSR1/USR2 nostop noprint pass` (built into `attach-gdb.sh`). |
| `Error loading the injected bundle ... Permission denied` | Harmless warning with sandbox disabled; the benchmark JS still runs in the web process. |
| Which build? | Go by file timestamps, NOT the `libWPEWebKit-X.Y` SONAME (that's the WPE API version). |

## Helper scripts (`scripts/`)
- `fakeseat.py` — create the virtual uinput seat cog needs (run once per session).
- `run-cog.sh <log> <url> [--debug]` — launch a benchmark in cog with console capture.
- `monitor.sh <log> <maxsecs>` — watch a run; emit CRASH_OR_ASSERT / WEBPROC_GONE / IDLE_DONE / TIMEOUT.
- `attach-gdb.sh <url> [bt_log] [console_log]` — sandbox-off launch + gdb attach → fatal backtrace.
- `jsc-gdb.sh <jsfile> [env JSC_*=…]` — run a JS file in `jsc` under gdb for fast repro attempts.

Copy scripts over with `scp scripts/* root@<ip>:/tmp/` and `chmod +x`.
