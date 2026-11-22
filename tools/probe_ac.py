#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时探针：空调功能键到底改了状态字节的哪几位。

用法：
  python3 tools/probe_ac.py                 # 走 UI 路径（小米空调）
  python3 tools/probe_ac.py <mid> <vendor>  # 直接载入指定码表
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import miir            # noqa: E402
import kkac            # noqa: E402

captured = []
_orig = kkac._build_wave


def spy(fields, values):
    captured.append(list(values))
    return _orig(fields, values)


kkac._build_wave = spy


def frame_of(cfg, state, fid):
    captured.clear()
    cfg.encode(state, fid)
    return captured[-1] if captured else []


def diff(a, b):
    return ["%d:%02x->%02x" % (i, x, y) for i, (x, y) in enumerate(zip(a, b)) if x != y]


def load(mid, vendor):
    data = miir.remote(mid, vendor)
    return kkac.AcConfig(data["keys"], mid)


def run(cfg, tag):
    print("\n==== %s  %s ====" % (tag, cfg.model_id))
    print("modes:", [(m["value"], m["name"], (m["temperatures"] or [])[:2],
                      (m["temperatures"] or [])[-1:], m["fanSpeeds"]) for m in cfg.modes])
    print("temp_map:", {k: cfg.temp_map[k] for k in sorted(cfg.temp_map)[:3]},
          "... 共 %d" % len(cfg.temp_map))
    print("mode_map:", {k: cfg.mode_map[k] for k in sorted(cfg.mode_map)})
    print("fan_map :", {k: cfg.fan_map[k] for k in sorted(cfg.fan_map)})
    print("power_map:", {k: cfg.power_map[k] for k in sorted(cfg.power_map)})

    state = cfg.initial_state()
    state["power"] = 0
    state["temperature"] = 26
    base = frame_of(cfg, state, 1)
    print("\n基准 26℃ 制冷:", " ".join("%02x" % v for v in base))

    for label, over in (("温度- (25℃)", {"temperature": 25}),
                        ("温度+ (27℃)", {"temperature": 27}),
                        ("模式->制热", {"mode": 1}),
                        ("制热+25℃", {"mode": 1, "temperature": 25})):
        st = dict(state)
        st.update(over)
        frame = frame_of(cfg, st, 1)
        print("%-12s %s | diff: %s" % (
            label, " ".join("%02x" % v for v in frame), " ".join(diff(base, frame)) or "-"))


def main(argv):
    if len(argv) >= 2:
        run(load(argv[0], argv[1]), "直接载入")
        return 0

    import cp0_ir_remote as app_mod

    miir.transmit = lambda *a, **k: True

    def settle(app, timeout=60):
        for _ in range(int(timeout / 0.2)):
            if not app.job.running:
                break
            time.sleep(0.2)
        app.poll_job()

    app = app_mod.App(fb=None, kb=None)
    settle(app)
    app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "空调")
    app._activate()
    settle(app)
    app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "小米")
    app._activate()
    settle(app)
    app._activate()
    settle(app)
    if not app.ac:
        print("没载入空调")
        return 1
    run(app.ac, "UI 路径")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
