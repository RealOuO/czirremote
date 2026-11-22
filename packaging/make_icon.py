#!/usr/bin/env python3
"""生成 APPLaunch 用的方图标（64x64）：机身 + 红外发射弧。"""
import sys
from PIL import Image, ImageDraw

S = 64
BG, BODY, EDGE, ACCENT = "#0b1016", "#16222c", "#2b3d4b", "#ff7a45"
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((2, 2, S - 3, S - 3), radius=13, fill=BG, outline=EDGE, width=2)
# 遥控器机身
d.rounded_rectangle((12, 10, 34, 54), radius=7, fill=BODY, outline=EDGE, width=2)
# 顶部发射窗
d.rounded_rectangle((17, 15, 29, 20), radius=2, fill=ACCENT)
# 按键
for cy in (28, 37, 46):
    d.ellipse((19, cy - 3, 27, cy + 3), fill="#243543", outline=EDGE)
# 发射弧线
for r, w in ((7, 2), (12, 2), (17, 2)):
    d.arc((34 - r, 32 - r, 34 + r, 32 + r), start=-52, end=52, fill=ACCENT, width=w)
out = sys.argv[1] if len(sys.argv) > 1 else "cp0-ir-remote.png"
img.save(out)
print("saved", out, img.size)
