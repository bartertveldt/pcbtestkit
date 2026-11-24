#!/usr/bin/env python3
"""Draw the KiCad Plugin Manager icon (pcm/icon.png, 64x64 RGBA) with the standard library only.

Same motif as the 24 px toolbar icon in resources/: a green PCB with gold pads and a pogo pin
touching the middle pad. Shapes are drawn 4x supersampled and averaged down for smooth edges.

    python3 tools/make_icon.py [--size 64] [--out pcm/icon.png]
"""
import argparse
import os
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SS = 4  # supersampling factor


def png(path, w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def draw(size):
    n = size * SS
    canvas = [None] * (n * n)          # RGBA tuples or None (transparent)
    u = n / 64.0                        # design units: the icon is laid out on a 64 x 64 grid

    def rect(x0, y0, x1, y1, col, r=0.0):
        for y in range(int(y0 * u), int(y1 * u)):
            for x in range(int(x0 * u), int(x1 * u)):
                if r:
                    cx = min(max(x / u, x0 + r), x1 - r)
                    cy = min(max(y / u, y0 + r), y1 - r)
                    if (x / u - cx) ** 2 + (y / u - cy) ** 2 > r * r:
                        continue
                canvas[y * n + x] = col

    def poly(pts, col):
        ys = [p[1] for p in pts]
        for y in range(int(min(ys) * u), int(max(ys) * u) + 1):
            fy = (y + 0.5) / u
            xs = []
            for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
                if (ay <= fy < by) or (by <= fy < ay):
                    xs.append(ax + (fy - ay) * (bx - ax) / (by - ay))
            xs.sort()
            for a, b in zip(xs[::2], xs[1::2]):
                for x in range(int(a * u), int(b * u) + 1):
                    if 0 <= x < n and 0 <= y < n:
                        canvas[y * n + x] = col

    green, green_dark = (38, 122, 62, 255), (24, 84, 42, 255)
    gold, gold_dark = (226, 170, 40, 255), (170, 120, 20, 255)
    barrel, barrel_dark = (96, 102, 114, 255), (62, 66, 75, 255)
    steel = (196, 200, 208, 255)
    # PCB with an edge (thickness) and pads
    rect(4, 42, 60, 58, green_dark, r=3)
    rect(4, 40, 60, 54, green, r=3)
    for x0 in (10, 28, 46):
        rect(x0, 36, x0 + 8, 41, gold_dark, r=1)
        rect(x0, 35, x0 + 8, 40, gold, r=1)
    # pogo pin: receptacle collar, barrel, plunger and crown tip touching the middle pad
    rect(24, 6, 40, 10, barrel_dark, r=1.5)
    rect(26, 9, 38, 24, barrel, r=1.5)
    rect(26, 9, 29, 24, barrel_dark)
    rect(30, 23, 34, 31, steel)
    poly([(29, 31), (35, 31), (33.5, 35), (32, 33.5), (30.5, 35)], steel)
    # average SS x SS blocks
    out = []
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for yy in range(SS):
                for xx in range(SS):
                    c = canvas[(y * SS + yy) * n + x * SS + xx]
                    if c:
                        a = c[3]
                        acc[0] += c[0] * a
                        acc[1] += c[1] * a
                        acc[2] += c[2] * a
                        acc[3] += a
            a = acc[3]
            out += [acc[0] // a, acc[1] // a, acc[2] // a, a // (SS * SS)] if a else [0, 0, 0, 0]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=64)
    ap.add_argument("--out", default=os.path.join(ROOT, "pcm", "icon.png"))
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    png(a.out, a.size, a.size, draw(a.size))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
