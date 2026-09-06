#!/usr/bin/env python3
"""Can this macOS install present an accelerated, unthrottled browser?

Asked of the build that is about to be measured or profiled, through the launch
path OSXMiniDriver uses, and answered by the page itself rather than by
inference:

  webgl      WebKit has no software WebGL on macOS -- getContext returns null
             when there is no Metal device -- so a context at all is the
             acceleration, and the renderer string names what it got.
  accelerator the WebKit GPU process holding a user client on the machine's
             IOAccelerator, which is what says the work went to that device and
             not somewhere else (the same evidence the board lane reads off DRM).
  raf        requestAnimationFrame's measured rate. An unfocused or napped
             window is throttled to a crawl, and a benchmark run behind one
             measures the throttle -- silently, with a plausible-looking score.
  screen     the dimensions run-benchmark sizes the window from.

Exit 0 only when every one of those passes; a PGO collection or a measured run
behind a failure is a number nobody can attribute.

  bench/mac-browser-check.py --build-directory <products> [--json <path>]
"""
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


def accelerator_clients():
    """Every process holding a user client on this machine's IOAccelerator, as
    {pid: process name}. ioreg -a is a plist; the creator string truncates the
    name at 16 characters, so the pid is resolved against ps."""
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
    """The environment OSXMiniDriver builds for a custom build directory
    (webkitpy/benchmark_runner/browser_driver/osx_minibrowser_driver.py), so
    what is checked is what will be run."""
    env = dict(os.environ)
    for key in ("DYLD_FRAMEWORK_PATH", "DYLD_LIBRARY_PATH",
                "__XPC_DYLD_FRAMEWORK_PATH", "__XPC_DYLD_LIBRARY_PATH"):
        env[key] = build
    binary = os.path.join(build, "MiniBrowser.app", "Contents", "MacOS", "MiniBrowser")
    if not os.path.exists(binary):
        sys.exit(f"mac-browser-check: no MiniBrowser at {binary}")
    return subprocess.Popen([binary, "--url", url], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def faults(reading, clients, device, min_raf):
    """The verdict, as a list of reasons -- separated from the run so it can be
    exercised against a reading rather than against a Mac."""
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
    width, height = ((reading.get("screen") or [0, 0]) + [0, 0])[:2]
    if width < 640 or height < 480:
        found.append(f"the screen reports {width}x{height}, which run-benchmark cannot "
                     "size a window from")
    return found


def main():
    parser = argparse.ArgumentParser(prog="mac-browser-check", allow_abbrev=False)
    parser.add_argument("--build-directory", required=True,
                        help="the products directory holding MiniBrowser.app")
    parser.add_argument("--json", help="write the whole reading here")
    parser.add_argument("--min-raf", type=float, default=45.0,
                        help="the rate below which the window is throttled (default 45)")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    reading = {}
    server = serve(reading)
    port = server.server_address[1]
    # A WebKit GPU process left behind by an earlier browser holds a client of its
    # own; without this the check would pass on somebody else's evidence.
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

    browser.terminate()
    try:
        browser.wait(timeout=15)
    except subprocess.TimeoutExpired:
        browser.kill()

    reading["accelerator"] = device
    reading["webkit_gpu_clients"] = {str(k): v for k, v in clients_seen.items()}
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(reading, handle, indent=2, sort_keys=True)

    found = faults(reading, clients_seen, device, args.min_raf)

    for key in ("accelerator", "renderer", "webgl", "raf_hz", "screen", "dpr", "focused"):
        print(f"{key}={reading.get(key)}")
    print("webkit_gpu_clients=" + ",".join(
        f"{pid}:{name}" for pid, name in sorted(clients_seen.items())))

    if found:
        print("\nthis machine cannot present an accelerated, unthrottled browser:", file=sys.stderr)
        for fault in found:
            print(f"  {fault}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
