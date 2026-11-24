"""Frame renderer for the explainer video (needs wxPython, which ships with KiCad).

    python -m pcbtestkit.video_frames storyboard.json

Reads a storyboard written by :mod:`pcbtestkit.video` and writes PNG frames
next to it. Kept separate so the rest of the toolkit never imports wx.
"""
from __future__ import annotations

import json
import os
import sys

import wx

BG = (17, 24, 39)
ACCENT = (47, 125, 79)
WHITE = (245, 247, 250)
MUTED = (170, 178, 190)
SEV = {"critical": (209, 52, 56), "major": (232, 113, 10), "minor": (201, 164, 0), "info": (91, 127, 166)}


def col(c, a=255):
    return wx.Colour(c[0], c[1], c[2], a)


def font(gc, px, bold=False, mono=False, colour=WHITE):
    fam = wx.FONTFAMILY_TELETYPE if mono else wx.FONTFAMILY_SWISS
    f = wx.Font(wx.Size(0, int(px)), fam, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL)
    return gc.CreateFont(f, col(colour))


def wrap(gc, text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if gc.GetTextExtent(t)[0] <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


class Frame:
    def __init__(self, W, H, transparent=False):
        self.W, self.H = W, H
        if transparent:                       # an overlay drawn once and stamped onto every frame of a shot
            self.bmp = wx.Bitmap.FromRGBA(W, H, 0, 0, 0, 0)
            self.dc = wx.MemoryDC(self.bmp)
        else:
            self.bmp = wx.Bitmap(W, H)
            self.dc = wx.MemoryDC(self.bmp)
            self.dc.SetBackground(wx.Brush(col(BG)))
            self.dc.Clear()
        self.gc = wx.GraphicsContext.Create(self.dc)
        self.gc.SetAntialiasMode(wx.ANTIALIAS_DEFAULT)

    def image(self, path, cover=True, dim=0):
        if not path or not os.path.exists(path):
            return
        img = wx.Image(path, wx.BITMAP_TYPE_ANY)
        iw, ih = img.GetWidth(), img.GetHeight()
        s = max(self.W / iw, self.H / ih) if cover else min(self.W / iw, (self.H - 150) / ih)
        nw, nh = int(iw * s), int(ih * s)
        img = img.Scale(nw, nh, wx.IMAGE_QUALITY_HIGH)
        x, y = (self.W - nw) // 2, (self.H - nh) // 2 if cover else 70
        self.gc.DrawBitmap(wx.Bitmap(img), x, y, nw, nh)
        if dim:
            self.rect(0, 0, self.W, self.H, BG, dim)

    def rect(self, x, y, w, h, c, a=255, r=0):
        self.gc.SetPen(wx.TRANSPARENT_PEN)
        self.gc.SetBrush(wx.Brush(col(c, a)))
        if r:
            self.gc.DrawRoundedRectangle(x, y, w, h, r)
        else:
            self.gc.DrawRectangle(x, y, w, h)

    def text(self, s, x, y, px, bold=False, c=WHITE, mono=False, width=None, lh=1.3):
        self.gc.SetFont(font(self.gc, px, bold, mono, c))
        lines = wrap(self.gc, s, width) if width else [s]
        for i, ln in enumerate(lines):
            self.gc.DrawText(ln, x, y + i * px * lh)
        return y + len(lines) * px * lh

    def title_bar(self, step, title):
        self.rect(0, 0, self.W, 64, BG, 215)
        x = 28
        if step:
            self.rect(24, 14, 36, 36, ACCENT, 255, 18)
            self.gc.SetFont(font(self.gc, 20, True))
            tw = self.gc.GetTextExtent(str(step))[0]
            self.gc.DrawText(str(step), 42 - tw / 2, 19)
            x = 74
        self.text(title, x, 16, 28, True)

    def caption(self, text, sub=""):
        h = 96 if sub else 74
        self.rect(0, self.H - h, self.W, h, BG, 225)
        y = self.text(text, 30, self.H - h + 16, 26, True, width=self.W - 60)
        if sub:
            self.text(sub, 30, y + 2, 19, False, MUTED, width=self.W - 60)

    def bullets(self, items, x, y, width, px=24):
        for it in items:
            c = SEV.get(it.get("sev"), ACCENT) if isinstance(it, dict) else ACCENT
            t = it["text"] if isinstance(it, dict) else it
            self.rect(x, y + px * 0.35, 10, 10, c, 255, 5)
            y = self.text(t, x + 22, y, px, False, WHITE, width=width - 22) + px * 0.35
        return y

    def card(self, title, subtitle=""):
        """Title-card text over moving footage: a soft dark band on the left keeps the board visible."""
        br = self.gc.CreateLinearGradientBrush(0, 0, self.W * 0.72, 0, col(BG, 225), col(BG, 0))
        self.gc.SetBrush(br)
        self.gc.SetPen(wx.TRANSPARENT_PEN)
        self.gc.DrawRectangle(0, 0, self.W * 0.72, self.H)
        y = self.text(title, 64, self.H * 0.34, 50, True, width=self.W * 0.52)
        self.rect(64, y + 12, 90, 5, ACCENT, 255, 2)
        if subtitle:
            self.text(subtitle, 64, y + 34, 26, False, MUTED, width=self.W * 0.5)

    def stamp(self, bmp):
        self.gc.DrawBitmap(bmp, 0, 0, self.W, self.H)

    def finish_overlay(self):
        self.gc = None
        self.dc.SelectObject(wx.NullBitmap)
        return self.bmp

    def save(self, path):
        self.gc = None
        self.dc.SelectObject(wx.NullBitmap)
        img = self.bmp.ConvertToImage()
        img.SetOption(wx.IMAGE_OPTION_PNG_COMPRESSION_LEVEL, 1)      # frames are temporary: favour speed
        img.SaveFile(path, wx.BITMAP_TYPE_PNG)


def draw_map(fr, m):
    """Board outline, pads and probes, fitted into the frame."""
    x0, y0, x1, y1 = m["bbox"]
    pad = 40
    s = min((fr.W - 2 * pad) / (x1 - x0), (fr.H - 64 - 110 - 2 * pad) / (y1 - y0))
    ox = (fr.W - (x1 - x0) * s) / 2 - x0 * s
    oy = 64 + pad - y0 * s
    gc = fr.gc
    fr.rect(ox + x0 * s - 8, oy + y0 * s - 8, (x1 - x0) * s + 16, (y1 - y0) * s + 16, (28, 52, 38), 255, 10)
    gc.SetPen(wx.Pen(col((215, 220, 210)), 2))
    for a in m["edges"]:
        gc.StrokeLine(ox + a[0] * s, oy + a[1] * s, ox + a[2] * s, oy + a[3] * s)
    gc.SetPen(wx.TRANSPARENT_PEN)
    gc.SetBrush(wx.Brush(col((196, 160, 70), 200)))
    for p in m["pads"]:
        path = gc.CreatePath()
        path.MoveToPoint(ox + p[0][0] * s, oy + p[0][1] * s)
        for q in p[1:]:
            path.AddLineToPoint(ox + q[0] * s, oy + q[1] * s)
        path.CloseSubpath()
        gc.FillPath(path)
    for pr in m["probes"]:
        c = {"supply": (209, 52, 56), "ground": (230, 230, 230)}.get(pr["role"], (60, 200, 120))
        gc.SetPen(wx.Pen(col(c), 4))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        r = max(7, 1.0 * s)
        gc.DrawEllipse(ox + pr["x"] * s - r, oy + pr["y"] * s - r, 2 * r, 2 * r)
        fr.text(pr["label"], ox + pr["x"] * s + r + 3, oy + pr["y"] * s - r - 6, 15, True, c)


def render(job):
    W, H = job["size"]
    out = job["frames_dir"]
    os.makedirs(out, exist_ok=True)
    for si, sc in enumerate(job["scenes"]):
        kind = sc["kind"]
        files = []

        def new_frame(bg=None, dim=0, cover=True):
            fr = Frame(W, H)
            if bg:
                fr.image(bg, cover=cover, dim=dim)
            return fr

        def finish(fr):
            p = os.path.join(out, "s%02d_%03d.png" % (si, len(files)))
            fr.save(p)
            files.append(p)

        if kind == "card":
            fr = new_frame(sc.get("bg"), dim=150)
            y = H * 0.30
            y = fr.text(sc["title"], 70, y, 52, True, width=W - 140)
            if sc.get("subtitle"):
                y = fr.text(sc["subtitle"], 70, y + 10, 28, False, MUTED, width=W - 140)
            if sc.get("bullets"):
                fr.bullets(sc["bullets"], 70, y + 30, W - 140, 26)
            finish(fr)
        elif kind == "image":
            fr = new_frame(sc.get("bg"), cover=sc.get("cover", True))
            fr.title_bar(sc.get("step"), sc.get("title", ""))
            if sc.get("bullets"):
                bw = 520
                fr.rect(W - bw - 30, 90, bw, 30 + 44 * len(sc["bullets"]), BG, 210, 12)
                fr.bullets(sc["bullets"], W - bw - 10, 108, bw - 30, 22)
            fr.caption(sc.get("caption", ""), sc.get("sub", ""))
            finish(fr)
        elif kind == "anim":
            ov = Frame(W, H, transparent=True)
            if sc.get("overlay") == "card":
                ov.card(sc.get("title", ""), sc.get("subtitle", ""))
            else:
                ov.title_bar(sc.get("step"), sc.get("title", ""))
                if sc.get("bullets"):
                    bw = 470
                    ov.rect(W - bw - 30, 90, bw, 30 + 40 * len(sc["bullets"]), BG, 200, 12)
                    ov.bullets(sc["bullets"], W - bw - 10, 106, bw - 30, 21)
                ov.caption(sc.get("caption", ""), sc.get("sub", ""))
            overlay = ov.finish_overlay()
            for img in sc["images"]:
                fr = new_frame(img)
                fr.stamp(overlay)
                finish(fr)
        elif kind == "map":
            fr = new_frame()
            fr.title_bar(sc.get("step"), sc.get("title", ""))
            draw_map(fr, sc["map"])
            fr.caption(sc.get("caption", ""), sc.get("sub", ""))
            finish(fr)
        elif kind == "terminal":
            lines = sc["lines"]
            per = max(1, len(lines) // 14)
            shown = 0
            while shown < len(lines):
                shown = min(len(lines), shown + per)
                fr = new_frame()
                fr.title_bar(sc.get("step"), sc.get("title", ""))
                fr.rect(40, 84, W - 80, H - 84 - 120, (10, 12, 16), 255, 12)
                vis = lines[max(0, shown - 17):shown]
                for i, ln in enumerate(vis):
                    c = WHITE
                    if " PASS " in ln or ln.strip().startswith("PASS") or "==== PASS" in ln:
                        c = (90, 210, 120)
                    elif "FAIL" in ln:
                        c = (235, 90, 90)
                    elif "SKIP" in ln:
                        c = MUTED
                    fr.text(ln[:96], 64, 100 + i * 27, 20, False, c, mono=True)
                fr.caption(sc.get("caption", ""), sc.get("sub", ""))
                finish(fr)
        sc["files"] = files
    with open(os.path.join(out, "frames.json"), "w") as fh:
        json.dump([sc.get("files", []) for sc in job["scenes"]], fh)


def main():
    with open(sys.argv[1]) as fh:
        job = json.load(fh)
    app = wx.App(False)  # noqa: F841 - needed for fonts/bitmaps
    render(job)
    return 0


if __name__ == "__main__":
    sys.exit(main())
