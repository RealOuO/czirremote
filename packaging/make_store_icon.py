#!/usr/bin/env python3
"""生成商店用的方图标：满底、无圆角无描边（商店不建议图标自带边框）。

用法：python3 packaging/make_store_icon.py [输出路径] [边长，默认 256]
"""
import sys
from PIL import Image, ImageDraw

S = int(sys.argv[2]) if len(sys.argv) > 2 else 256
k = S / 64.0                      # 所有坐标按 64x64 设计稿等比放大
BG, BODY, EDGE, ACCENT = "#0b1016", "#16222c", "#2b3d4b", "#ff7a45"


def s(v):
    return int(round(v * k))


img = Image.new("RGB", (S, S), BG)   # 满底，不透明 → 商店列表里不会出现白角
d = ImageDraw.Draw(img)

# 遥控器机身
d.rounded_rectangle((s(12), s(10), s(34), s(54)), radius=s(7), fill=BODY,
                    outline=EDGE, width=max(1, s(2)))
# 顶部发射窗
d.rounded_rectangle((s(17), s(15), s(29), s(20)), radius=s(2), fill=ACCENT)
# 按键
for cy in (28, 37, 46):
    d.ellipse((s(19), s(cy - 3), s(27), s(cy + 3)), fill="#243543", outline=EDGE)
# 发射弧线
for r, w in ((7, 2), (12, 2), (17, 2)):
    d.arc((s(34 - r), s(32 - r), s(34 + r), s(32 + r)),
          start=-52, end=52, fill=ACCENT, width=max(1, s(w)))

out = sys.argv[1] if len(sys.argv) > 1 else "store/icon.png"
img.save(out)
print("saved", out, img.size, img.mode)
