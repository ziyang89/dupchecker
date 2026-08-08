#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图标生成器（重复文件检查器）
==========================
设计：双文档 + 放大镜（"扫描 / 查重"主题），现代扁平风。
关键：只画一张 1024x1024 的高分辨率主图，再用 LANCZOS 等比例缩小
到各档尺寸，合成单一多尺寸 ICO。源为正方形、缩放等比，绝不拉伸变形。

依赖: Pillow
生成: appicon_master.png  (预览用)
      appicon.ico        (程序使用，含多尺寸)
"""

import io
import math
import os
import struct

from PIL import Image, ImageDraw

SIZE = 1024

# ---- 配色 --------------------------------------------------------------
C_TOP = (37, 99, 235)      # #2563eb 顶部蓝
C_BOT = (14, 165, 233)     # #0ea5e9 底部青
C_DOC = (255, 255, 255, 255)
C_DOC_BORDER = (226, 232, 240, 255)
C_LINE = (203, 213, 225, 255)
C_GLASS = (255, 255, 255, 255)


def vgradient(top, bot, w, h):
    """竖向渐变：先画 1xN 渐变条再放大，平滑且快。"""
    strip = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        strip.putpixel((0, y), c)
    return strip.resize((w, h), Image.BICUBIC)


def rounded_mask(w, h, radius):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    return m


def make_doc(rect, radius, angle, lines):
    """在透明图层上画一张带文本行、可旋转的文档卡片。"""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    bw = int(SIZE * 0.012)
    d.rounded_rectangle(rect, radius=radius, fill=C_DOC, outline=C_DOC_BORDER, width=bw)
    for (lx, ly, lw, lh) in lines:
        d.rounded_rectangle([lx, ly, lx + lw, ly + lh], radius=lh // 2, fill=C_LINE)
    if angle:
        cx = (rect[0] + rect[2]) / 2
        cy = (rect[1] + rect[3]) / 2
        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=False, center=(cx, cy))
    return layer


def make_magnifier(center, radius, stroke):
    """透明图层上画放大镜（圆环 + 斜手柄 + 轻微玻璃填充）。"""
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # 玻璃淡填充
    d.ellipse([center[0] - radius, center[1] - radius,
               center[0] + radius, center[1] + radius],
              fill=(255, 255, 255, 38))
    # 圆环
    d.ellipse([center[0] - radius, center[1] - radius,
               center[0] + radius, center[1] + radius],
              outline=C_GLASS, width=stroke)
    # 手柄（沿 45° 向外）
    a = math.radians(45)
    x1 = center[0] + radius * 0.72 * math.cos(a)
    y1 = center[1] + radius * 0.72 * math.sin(a)
    x2 = center[0] + (radius + stroke * 2.4) * math.cos(a)
    y2 = center[1] + (radius + stroke * 2.4) * math.sin(a)
    d.line([(x1, y1), (x2, y2)], fill=C_GLASS, width=stroke)
    return layer


def build_master():
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    # 背景：渐变圆角方块（四角透明，现代 App 图标观感）
    bg = vgradient(C_TOP, C_BOT, SIZE, SIZE)
    bg_rgba = bg.convert("RGBA")
    mask = rounded_mask(SIZE, SIZE, int(SIZE * 0.22))
    base.paste(bg_rgba, (0, 0), mask)

    # 后层文档（偏左上，轻微旋转）
    back = make_doc(
        rect=[225, 165, 695, 760], radius=58, angle=7,
        lines=[(330, 320, 270, 34), (330, 420, 250, 34),
               (330, 520, 270, 34), (330, 620, 210, 34)])
    # 前层文档（偏右下，与后层交叠）
    front = make_doc(
        rect=[330, 300, 800, 895], radius=58, angle=-5,
        lines=[(440, 450, 270, 34), (440, 550, 250, 34),
               (440, 650, 270, 34), (440, 750, 200, 34)])

    base.alpha_composite(back)
    base.alpha_composite(front)

    # 放大镜（压在文档上，强调"扫描"）
    mag = make_magnifier(center=(660, 700), radius=180, stroke=42)
    base.alpha_composite(mag)

    return base


def write_ico(path, images):
    """手工写入标准 Windows ICO（PNG payload，每帧一个尺寸，32bpp）。"""
    # images: [(width, height, PIL.Image), ...]
    images = [(w, h, im if im.mode == "RGBA" else im.convert("RGBA")) for (w, h, im) in images]
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    data = bytearray()
    offset = 6 + 16 * count
    for w, h, im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        payload = buf.getvalue()
        # ICO 目录里 0 表示 256
        bw = w if w < 256 else 0
        bh = h if h < 256 else 0
        entries += struct.pack("<BBBBHHII",
                               bw, bh, 0, 0,   # width, height, colors, reserved
                               1, 32,           # planes, bitcount
                               len(payload), offset)
        data += payload
        offset += len(payload)
    with open(path, "wb") as f:
        f.write(header)
        f.write(entries)
        f.write(data)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    master = build_master()
    master_path = os.path.join(here, "appicon_master.png")
    master.save(master_path, "PNG")

    # 等比例缩小到各档尺寸（正方形源 -> 正方形目标，宽高比恒定）
    sizes = [16, 24, 32, 40, 48, 56, 64, 72, 96, 128, 256]
    frames = []
    for s in sizes:
        f = master.resize((s, s), Image.LANCZOS)
        frames.append(f)

    ico_path = os.path.join(here, "appicon.ico")
    write_ico(ico_path, list(zip(sizes, sizes, frames)))
    print("OK master ->", master_path)
    print("OK ico    ->", ico_path, "sizes:", sizes)


if __name__ == "__main__":
    main()
