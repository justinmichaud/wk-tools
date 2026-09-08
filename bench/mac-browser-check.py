#!/usr/bin/env python3
"""Can this macOS install present an accelerated, unthrottled browser? Asked of
the build about to be measured, through OSXMiniDriver's own launch path.
README.md, "The Mac lane", says what each reading proves."""
import argparse
import http.server
import json
import os
import plistlib
import socketserver
import subprocess
import sys
import threading
import time

PAGE = b"""<!doctype html><meta charset=utf-8><title>wk browser check</title>
<canvas id=c width=256 height=256></canvas><script>
var out = {}, c = document.getElementById('c');
var gl = c.getContext('webgl2') || c.getContext('webgl');
if (!gl) { out.webgl = null; } else {
  out.webgl = gl.getParameter(gl.VERSION);
  var d = null;
  try { d = gl.getExtension('WEBGL_debug_renderer_info'); } catch (e) {}
  out.renderer = d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
  // Draw, so the context is not merely created: a lazily-backed one proves nothing.
  gl.clearColor(0, 1, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.finish();
}
out.screen = [screen.width, screen.height];
out.dpr = devicePixelRatio;
out.pid_hint = navigator.userAgent;
var n = 0, t0 = performance.now();
function tick() {
  n++;
  if (performance.now() - t0 < 2000) requestAnimationFrame(tick);
  else {
    out.raf_hz = n / ((performance.now() - t0) / 1000);
    out.hidden = document.hidden;
    out.focused = document.hasFocus();
    fetch('/report', {method: 'POST', body: JSON.stringify(out)});
  }
}
requestAnimationFrame(tick);
</script>"""


# What separates the two populations, not what a healthy machine reaches: a window that lost the focus is rAF-throttled to about 1 Hz and stalls, while a foreground one on a busy guest measured 44.4-57.7 Hz over five collections (2026-09-06). A floor near the healthy range refuses good runs.
MIN_RAF = 30.0

BUNDLE = "org.webkit.MiniBrowser"
WKMAC = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      os.pardir, "lib", "wkmac.py"))


def display_list():
    cp = subprocess.run([sys.executable, WKMAC, "displays"],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        return None
    return json.loads(cp.stdout)["displays"]


def builtin_display(displays):
    return next((d for d in (displays or []) if d.get("builtin")), None)


def frontmost_bundle():
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return "?"
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    ident = app.bundleIdentifier() if app else None
    return str(ident) if ident else None


def parse_expect_display(spec):
    words = (spec or "").split()
    points = words[1].split("x") if len(words) == 2 else []
    if len(words) != 2 or words[0] != "builtin" or len(points) != 2 \
            or not all(p.isdigit() for p in points):
        raise ValueError(f"--expect-display {spec!r} is not 'builtin <w>x<h>', "
                         "as in 'builtin 1470x956'")
    return [int(p) for p in points]


def display_faults(displays, expect):
    # No expectation: the display is recorded and nothing is compared against it.
    if expect is None:
        return []
    if displays is None:
        return ["the display list could not be read, so what run-benchmark sized its "
                "window from is unknown and this score compares with nothing"]
    found = []
    online = [d for d in displays if d.get("online")]
    builtin = builtin_display(online)
    if len(online) != 1:
        found.append(f"{len(online)} displays are online, not one: run-benchmark sizes "
                     "its window from the screen, so a second panel -- or none at all "
                     "-- moves the number for a reason that is not the patch")
    elif not builtin:
        found.append(f"the one online display (id {online[0].get('id')}) is not the "
                     "built-in panel: only the built-in one is declared and measured, "
                     "so this reading compares with no other run")
    if any(d.get("mirrored") for d in displays):
        found.append("a display is in a mirror set: the window is composited for two "
                     "panels at once, and the frames that costs are charged to the patch")
    if builtin and list(builtin.get("points") or []) != expect:
        found.append(f"the built-in display reads {builtin.get('points')} points, not "
                     f"{expect}: MotionMark's score is a function of the area it draws, "
                     "so this run is not comparable with one at the declared mode")
    return found


def accelerator_clients():
    # ioreg -a is a plist; the creator string truncates the name at 16 characters, so the pid is resolved against ps.
    cp = subprocess.run(["ioreg", "-a", "-l", "-w0", "-r", "-c", "IOAccelerator"],
                        capture_output=True)
    if cp.returncode != 0 or not cp.stdout:
        return None, {}
    roots = plistlib.loads(cp.stdout)
    device = roots[0].get("IOClass") if roots else None
    pids = {}

    def walk(node):
        creator = node.get("IOUserClientCreator", "")
        if creator.startswith("pid "):
            pid = creator[4:].split(",", 1)[0].strip()
            if pid.isdigit():
                pids[int(pid)] = None
        for child in node.get("IORegistryEntryChildren", []):
            walk(child)

    for root in roots:
        walk(root)
    for pid in list(pids):
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                            capture_output=True, text=True)
        pids[pid] = ps.stdout.strip()
    return device, pids


def webkit_gpu_holders(clients):
    return {pid: name for pid, name in clients.items() if "WebKit.GPU" in (name or "")}


def serve(result):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            result.update(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(204)
            self.end_headers()

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def launch(build, url):
    # osx_minibrowser_driver.py's own environment, so what is checked is what will be run.
    env = dict(os.environ)
    for key in ("DYLD_FRAMEWORK_PATH", "DYLD_LIBRARY_PATH",
                "__XPC_DYLD_FRAMEWORK_PATH", "__XPC_DYLD_LIBRARY_PATH"):
        env[key] = build
    binary = os.path.join(build, "MiniBrowser.app", "Contents", "MacOS", "MiniBrowser")
    if not os.path.exists(binary):
        sys.exit(f"mac-browser-check: no MiniBrowser at {binary}")
    return subprocess.Popen([binary, "--url", url], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def faults(reading, clients, device, min_raf, expect):
    found = []
    if not reading:
        found.append("the page never reported: MiniBrowser did not load it, or it "
                     "exited first (this build may not run here at all)")
    if not reading.get("webgl"):
        found.append("no WebGL context: WebKit has no software fallback on macOS, so "
                     "this machine presented no Metal device at all")
    if not clients:
        found.append(f"no WebKit GPU process held a client on {device or 'any IOAccelerator'} "
                     "while the page rendered, so the drawing did not reach that device")
    raf = reading.get("raf_hz")
    if raf is None or raf < min_raf:
        found.append(f"requestAnimationFrame ran at {raf if raf is None else round(raf, 1)} Hz, "
                     f"below {min_raf}: this window is throttled, and a benchmark behind "
                     "one measures the throttle")
    if not reading.get("focused"):
        found.append("the page did not have the focus: the measured window is not the key "
                     "one, so what draws in it is rAF-throttled and the benchmark measures "
                     "the throttle")
    found += display_faults(reading.get("displays"), expect)
    # Judged whatever the expectation: brightness ambient light can raise again is not a held setting, and power is thermal headroom. Absent is unknown and reported, never refused -- a guest panel has no sensor to ask.
    if (builtin_display(reading.get("displays")) or {}).get("auto_brightness"):
        found.append("the built-in display is under ambient-light control, so the "
                     "brightness this run pinned can rise again mid-measurement and the "
                     "thermal headroom with it")
    frontmost = reading.get("frontmost")
    if frontmost == "?":
        found.append("nothing here could say which application was frontmost -- AppKit did "
                     "not import, and the bench account needs pyobjc: an unfocused window "
                     "is rAF-throttled and a benchmark behind one measures the throttle")
    elif frontmost != BUNDLE:
        found.append(f"the frontmost application was {frontmost}, not {BUNDLE}: the window "
                     "about to be measured is behind something, and a window that is not "
                     "frontmost is rAF-throttled")
    return found


def display_summary(displays):
    if displays is None:
        return "?"
    builtin = builtin_display(displays)
    return (f"count={len(displays)} builtin={builtin.get('id') if builtin else None} "
            f"points={builtin.get('points') if builtin else None} "
            f"mirrored={any(d.get('mirrored') for d in displays)} "
            f"asleep={any(d.get('asleep') for d in displays)} "
            f"auto_brightness={builtin.get('auto_brightness') if builtin else None}")


def report(reading, clients):
    for key in ("accelerator", "renderer", "webgl", "raf_hz", "screen", "dpr",
                "focused", "frontmost", "brightness"):
        print(f"{key}={reading.get(key)}")
    print("displays=" + display_summary(reading.get("displays")))
    print("webkit_gpu_clients=" + ",".join(
        f"{pid}:{name}" for pid, name in sorted(clients.items())))


def take_reading(args):
    reading = {}
    server = serve(reading)
    port = server.server_address[1]
    # A WebKit GPU process left behind by an earlier browser holds a client of its own, and the check would pass on somebody else's evidence.
    device, before = accelerator_clients()
    stale = set(webkit_gpu_holders(before))
    browser = launch(args.build_directory, f"http://127.0.0.1:{port}/")

    deadline = time.time() + args.timeout
    clients_seen = {}
    while time.time() < deadline and not reading:
        device_now, clients = accelerator_clients()
        device = device_now or device
        clients_seen.update({pid: name for pid, name in webkit_gpu_holders(clients).items()
                             if pid not in stale})
        if browser.poll() is not None:
            break
        time.sleep(0.5)

    reading["frontmost"] = frontmost_bundle()
    reading["displays"] = display_list()
    builtin = builtin_display(reading["displays"])
    reading["brightness"] = builtin.get("brightness") if builtin else None

    browser.terminate()
    try:
        browser.wait(timeout=15)
    except subprocess.TimeoutExpired:
        browser.kill()

    reading["accelerator"] = device
    reading["webkit_gpu_clients"] = {str(k): v for k, v in clients_seen.items()}
    return reading


def main():
    parser = argparse.ArgumentParser(prog="mac-browser-check", allow_abbrev=False)
    parser.add_argument("--build-directory",
                        help="the products directory holding MiniBrowser.app")
    parser.add_argument("--read", metavar="JSON",
                        help="report a reading already taken (what --json wrote) "
                             "instead of taking one; needs no Mac and no browser")
    parser.add_argument("--expect-display", metavar="SPEC",
                        help="the display this reading must be taken on, as "
                             "'builtin <w>x<h>' (boot/machines/<node>.conf's "
                             "NODE_DISPLAY). Display identity is what makes two runs "
                             "comparable; without it the display is recorded and "
                             "judged against nothing, which is what a run compared "
                             "with nothing -- a PGO collection -- wants")
    parser.add_argument("--json", help="write the whole reading here")
    parser.add_argument("--min-raf", type=float, default=MIN_RAF,
                        help=f"the rate below which the window is throttled (default {MIN_RAF})")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    # Parsed before the browser is launched: two minutes into a run is the wrong place to find the argument malformed.
    if args.expect_display:
        try:
            parse_expect_display(args.expect_display)
        except ValueError as exc:
            parser.error(str(exc))

    if args.read:
        with open(args.read) as handle:
            reading = json.load(handle)
    elif args.build_directory:
        reading = take_reading(args)
    else:
        parser.error("--build-directory to take a reading, or --read to report one")

    # A reading carries what it was judged against, so re-deriving its verdict reaches the same one with no argument.
    spec = args.expect_display or reading.get("expect_display")
    expect = parse_expect_display(spec) if spec else None
    if spec:
        reading["expect_display"] = spec

    # Derived on every report, never stored in the reading: one place holds the floors.
    clients = {str(k): v for k, v in (reading.get("webkit_gpu_clients") or {}).items()}
    found = faults(reading, clients, reading.get("accelerator"), args.min_raf, expect)

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)

    report(reading, clients)

    if found:
        sys.stdout.flush()  # the readings above belong before the faults, down a pipe too
        print("\nthis machine cannot present an accelerated, unthrottled browser:", file=sys.stderr)
        for fault in found:
            print(f"  {fault}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
