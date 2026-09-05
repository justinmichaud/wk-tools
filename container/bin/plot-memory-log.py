#!/usr/bin/env python3
"""Chart a WebKitWebProcess memory sampler log; stdlib only, since it draws on
a workspace image with nothing but the interpreter. The log format, which
nothing upstream defines: one sample per line, whitespace- or comma-separated,
the first field the time in seconds and the rest named byte columns, named by a
leading '#' comment or by the first line; a wrong-width row is skipped."""

import argparse
import csv
import sys


def _split_row(line):
    line = line.strip()
    if not line:
        return None
    if "," in line:
        return [f.strip() for f in next(csv.reader([line]))]
    return line.split()


def read_log(path):                    # -> (columns without the time column,
    columns = None                     #     [(time, {column: value})])
    rows = []
    skipped = 0
    with open(path, newline="") as f:
        for lineno, raw in enumerate(f, 1):
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if columns is None:
                    fields = _split_row(stripped.lstrip("#"))
                    if fields and len(fields) >= 2:
                        columns = fields[1:]
                continue
            fields = _split_row(stripped)
            if fields is None:
                continue
            if columns is None:
                columns = fields[1:]   # no '#' header: the first line names them
                continue
            if len(fields) != len(columns) + 1:
                print(f"plot-memory-log.py: {path}:{lineno}: expected "
                      f"{len(columns) + 1} fields, got {len(fields)} -- skipped",
                      file=sys.stderr)
                skipped += 1
                continue
            try:
                t = float(fields[0])
                values = {c: float(v) for c, v in zip(columns, fields[1:])}
            except ValueError:
                print(f"plot-memory-log.py: {path}:{lineno}: non-numeric field -- skipped",
                      file=sys.stderr)
                skipped += 1
                continue
            rows.append((t, values))
    if columns is None:
        raise SystemExit(f"plot-memory-log.py: {path}: no header and no data -- empty log?")
    if skipped:
        print(f"plot-memory-log.py: {skipped} line(s) skipped", file=sys.stderr)
    return columns, rows


_PALETTE = [
    "#4c72b0", "#dd8452", "#55a868", "#c44e52",
    "#8172b2", "#937860", "#da8bc3", "#8c8c8c",
]


def render_svg(columns, rows, title):
    width, height = 960, 480
    pad_l, pad_r, pad_t, pad_b = 72, 24, 40, 48

    if not rows:
        body = (f'<text x="{width/2}" y="{height/2}" text-anchor="middle" '
                f'font-family="sans-serif" font-size="16">no samples in range</text>')
        return _svg_document(width, height, title, body)

    times = [t for t, _ in rows]
    tmin, tmax = min(times), max(times)
    if tmax == tmin:
        tmax = tmin + 1.0

    allvals = [v for _, vals in rows for v in vals.values()]
    vmax = max(allvals) if allvals else 1.0
    vmax = vmax * 1.05 if vmax > 0 else 1.0

    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_of(t):
        return pad_l + (t - tmin) / (tmax - tmin) * plot_w

    def y_of(v):
        return pad_t + plot_h - (v / vmax) * plot_h

    parts = []

    for i in range(6):                 # gridlines and labels, 5 divisions
        gy = pad_t + plot_h * i / 5
        val = vmax * (5 - i) / 5
        parts.append(f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
                      f'stroke="currentColor" stroke-opacity="0.12" />')
        parts.append(f'<text x="{pad_l - 8}" y="{gy + 4:.1f}" text-anchor="end" '
                      f'font-family="sans-serif" font-size="11">{_fmt_bytes(val)}</text>')
    for i in range(6):
        gx = pad_l + plot_w * i / 5
        val = tmin + (tmax - tmin) * i / 5
        parts.append(f'<text x="{gx:.1f}" y="{height - pad_b + 18}" text-anchor="middle" '
                      f'font-family="sans-serif" font-size="11">{val:.0f}s</text>')

    parts.append(f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" '
                  f'stroke="currentColor" stroke-opacity="0.4" />')
    parts.append(f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - pad_r}" y2="{pad_t + plot_h}" '
                  f'stroke="currentColor" stroke-opacity="0.4" />')

    for i, col in enumerate(columns):
        color = _PALETTE[i % len(_PALETTE)]
        pts = " ".join(f"{x_of(t):.1f},{y_of(vals[col]):.1f}"
                        for t, vals in rows if col in vals)
        if pts:
            parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" />')
        ly = pad_t + 14 * i
        parts.append(f'<rect x="{width - pad_r - 110}" y="{ly - 9}" width="10" height="10" fill="{color}" />')
        parts.append(f'<text x="{width - pad_r - 96}" y="{ly}" font-family="sans-serif" '
                      f'font-size="11" fill="currentColor">{_esc(col)}</text>')

    return _svg_document(width, height, title, "\n".join(parts))


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _svg_document(width, height, title, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" font-family="sans-serif" color="#222">\n'
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="white" />\n'
            f'<text x="{width/2}" y="24" text-anchor="middle" font-size="16" '
            f'font-weight="bold">{_esc(title)}</text>\n'
            f'{body}\n</svg>\n')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="the memory sampler log to plot")
    ap.add_argument("--include", help="comma-separated column names to plot (default: all)")
    ap.add_argument("--max-seconds", type=float, default=None,
                     help="drop samples past this many seconds")
    ap.add_argument("--out", default="chart.html", help="output path (.svg or any other extension for HTML)")
    ap.add_argument("--title", default=None, help="chart title (default: the log's filename)")
    args = ap.parse_args(argv)

    columns, rows = read_log(args.log)

    if args.include:
        wanted = [c.strip() for c in args.include.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in columns]
        if unknown:
            raise SystemExit(f"plot-memory-log.py: --include names column(s) not in the log's "
                              f"header ({', '.join(columns)}): {', '.join(unknown)}")
        columns = wanted

    if args.max_seconds is not None:
        rows = [(t, v) for t, v in rows if t <= args.max_seconds]

    title = args.title or args.log
    svg = render_svg(columns, rows, title)

    if args.out.endswith(".svg"):
        out_content = svg
    else:
        out_content = (f"<!doctype html>\n<meta charset=\"utf-8\">\n"
                        f"<title>{_esc(title)}</title>\n{svg}")

    with open(args.out, "w") as f:
        f.write(out_content)
    print(f"plot-memory-log.py: {len(rows)} sample(s), {len(columns)} column(s) -> {args.out}")


if __name__ == "__main__":
    main()
