#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CP0 IR Remote 自测：键码解析 + 四屏流程状态机。

运行：python3 tests/selftest.py
  * 第一部分构造 input_event 字节流验证解析，并实测真实键盘能否独占
  * 第二部分把红外发射打桩，注入按键序列跑完整流程，断言状态迁移
"""

import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import miir  # noqa: E402
import cp0_ir_remote as app_mod  # noqa: E402
import kkac  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)


# ------------------------------------------------------------------ 键码

def ev(etype, code, value):
    return struct.pack("<qqHHi", 0, 0, etype, code, value)


def test_keyboard():
    print("\n== 1. 键码解析 + 真实键盘可访问性 ==")
    stream = b"".join([
        ev(1, 33, 1), ev(0, 0, 0),      # F 按下
        ev(1, 33, 0), ev(0, 0, 0),      # F 抬起
        ev(1, 108, 1), ev(0, 0, 0),     # DOWN 按下
        ev(1, 108, 2), ev(0, 0, 0),     # DOWN 长按重复
        ev(1, 1, 1), ev(0, 0, 0),       # ESC 按下
        ev(2, 0, 5),                    # 非按键事件，应忽略
    ])
    got = app_mod.parse_events(stream)
    names = [app_mod.KEYS.get(c, c) for c, _v in got]
    check("解析出 4 个按键（value=1/2 都保留）", len(got) == 4, str(names))
    check("顺序为 F/DOWN/DOWN/ESC", names == ["F", "DOWN", "DOWN", "ESC"], str(names))
    check("长按重复标记为 value=2", got[2][1] == 2, str(got[2]))

    node = app_mod.detect_keyboard()
    check("定位到 tca8418 键盘节点", bool(node) and "event" in str(node), str(node))

    virt = "/dev/input/by-path/platform-3f804000.i2c-event"
    # 复现 APPLaunch 场景：启动器把 by-path 虚拟节点塞进环境变量/参数，
    # 主输入源仍必须是真实物理键盘，否则界面正常但所有按键无响应。
    try:
        kb = app_mod.Keyboard(virt)
        check("APPLaunch 场景下主源仍是真实物理键盘",
              bool(node) and os.path.realpath(kb.path) == os.path.realpath(node),
              kb.path)
        check("物理键盘独占成功", kb.grabbed, str(kb.grabbed))
        links = []
        for fd in kb.fds:
            try:
                links.append(os.readlink("/proc/self/fd/%d" % fd))
            except OSError:
                pass
        if os.path.exists(virt):
            check("虚拟节点作为补充输入源被监听",
                  any(os.path.realpath(virt) == os.path.realpath(p) for p in links),
                  str(links))
        # 真实事件流：往补充输入源写一段 input_event，poll 必须解析出来。
        # 之前的 selftest 只直接调 on_key()，绕过了 Keyboard.poll 的真实读取。
        r, w = os.pipe()
        kb.fds.append(r)
        try:
            os.write(w, ev(1, 45, 1) + ev(0, 0, 0))
            got = kb.poll(0.4)
            check("poll 能合并多个输入源并解析真实事件流", (45, 1) in got, str(got))
        finally:
            kb.fds.remove(r)
            os.close(r)
            os.close(w)
        # 双源同时来同一个按键（物理 + cardputer-remote 转发）时只能上报一次
        r1, w1 = os.pipe()
        r2, w2 = os.pipe()
        kb.fds.extend([r1, r2])
        try:
            os.write(w1, ev(1, 33, 1))
            os.write(w2, ev(1, 33, 1))
            got = kb.poll(0.4)
            check("同一按键双源只上报一次", got.count((33, 1)) == 1, str(got))
        finally:
            kb.fds.remove(r1)
            kb.fds.remove(r2)
            for fd in (r1, r2, w1, w2):
                os.close(fd)
        kb.close()
    except Exception as exc:  # noqa: BLE001
        check("APPLaunch 场景键盘可独占", False, "%s: %s" % (type(exc).__name__, exc))

    try:
        kb = app_mod.Keyboard(node)
        check("直接指定物理节点也能独占", kb.grabbed, kb.path)
        kb.close()
    except Exception as exc:  # noqa: BLE001
        check("直接指定物理节点也能独占", False, "%s: %s" % (type(exc).__name__, exc))

    # 物理节点被别人独占时（grab 失败）物理 fd 读不到任何东西，
    # 必须继续监听 cardputer-remote 转发出来的虚拟节点，否则按键全哑。
    holder = app_mod.Keyboard(node)
    os.environ["APPLAUNCH_LINUX_KEYBOARD_DEVICE"] = virt
    try:
        second = app_mod.Keyboard()
        links = [os.readlink("/proc/self/fd/%d" % fd) for fd in second.fds]
        check("物理节点已被占用时抢不到独占", second.grabbed is False, str(second.grabbed))
        check("抢不到独占也照样监听虚拟节点",
              any(os.path.realpath(virt) == os.path.realpath(p) for p in links), str(links))
        second.close()
    except Exception as exc:  # noqa: BLE001
        check("抢占失败时仍保留虚拟输入源", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        os.environ.pop("APPLAUNCH_LINUX_KEYBOARD_DEVICE", None)
        holder.close()


# ------------------------------------------------------------------ 流程

def settle(app, timeout=60):
    _, _, err = app.job.wait(timeout)
    if err:
        raise RuntimeError(err)
    app.poll_job()


def test_flow():
    print("\n== 2. 四屏流程状态机 ==")
    # 用临时配置目录，避免污染真实「最近使用」记录，也保证起始状态干净
    import tempfile
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="cp0ir-test-")
    sent = []
    real_transmit = miir.transmit
    real_decrypt = miir.decrypt_pattern
    miir.transmit = lambda freq, timings, device=None, repeat=1: sent.append((freq, len(timings)))
    miir.decrypt_pattern = lambda enc, key=None: [9000, 4500, 560, 1690]

    try:
        app = app_mod.App(fb=None, kb=None)
        settle(app)
        check("设备类型已加载", len(app.devices) > 0, "%d 类" % len(app.devices))

        # 有「最近使用」时列表会整体下移，按 kind 定位而不是按设备索引
        idx = next(i for i, it in enumerate(app.items)
                   if it.get("kind") == "device" and "电视" in it["label"])
        app.sel = idx
        app._activate()
        settle(app)
        check("进入品牌列表", app.screen == "brands" and len(app.brands) > 0,
              "%d 个品牌" % len(app.brands))

        app.sel = 0
        app.on_key(103, 1)                                   # UP
        check("首个元素按 UP 不越界", app.sel == 0)
        app.sel = len(app.brands) - 1
        app.on_key(108, 1)                                   # DOWN
        check("末元素按 DOWN 不越界", app.sel == len(app.brands) - 1)
        app.on_key(44, 1)                                    # Z 向前翻页
        check("Z 向前翻 6 行", app.sel == len(app.brands) - 7, str(app.sel))

        # 屏幕下方 4~8 五颗键（keycode 5..9）必须驱动列表底栏
        app.sel = 3
        app.on_key(5, 1)                                     # 键 4 → 上
        check("底栏 4 上移一行", app.sel == 2, str(app.sel))
        app.on_key(6, 1)                                     # 键 5 → 下
        check("底栏 5 下移一行", app.sel == 3, str(app.sel))
        calls = []
        real_activate, real_back = app._activate, app._back
        app._activate = lambda: calls.append("ok")
        app._back = lambda: calls.append("back")
        app.on_key(7, 2)                                     # 键 6 长按重复
        check("重复事件不连点选定/返回", not calls, str(calls))
        app.on_key(7, 1)                                     # 键 6 → 选定
        app.on_key(8, 1)                                     # 键 7 → 返回
        check("底栏 6 选定 / 7 返回各触发一次", calls == ["ok", "back"], str(calls))
        app._activate, app._back = real_activate, real_back

        # 列表屏与遥控页共用同一套字母键：W/S 上下，A/D 翻页
        app.sel = 10
        app.hold_key, app.hold_n = None, 0
        app.on_key(17, 1)                                    # W
        check("列表屏 W 上移一行", app.sel == 9, str(app.sel))
        app.hold_key, app.hold_n = None, 0
        app.on_key(31, 1)                                    # S
        check("列表屏 S 下移一行", app.sel == 10, str(app.sel))
        app.on_key(30, 1)                                    # A
        check("列表屏 A 向前翻页", app.sel == 4, str(app.sel))
        app.on_key(32, 1)                                    # D
        check("列表屏 D 向后翻页", app.sel == 10, str(app.sel))

        # ---- 品牌搜索 + 热门置顶
        hot = [it["label"] for it in app.items if it.get("hot")]
        labels = [it["label"] for it in app.items]
        check("热门品牌置顶在第一屏", bool(hot) and labels[:len(hot)] == hot, str(hot[:4]))
        check("置顶只在前面出现一次（不重复也不丢）",
              len(app.items) == len(app.brands)
              and len({id(it["data"]) for it in app.items}) == len(app.items),
              "%d/%d" % (len(app.items), len(app.brands)))

        app.on_key(15, 1)                                    # TAB → 进入搜索
        check("TAB 进入搜索态", app.searching and app.query == "", str(app.query))
        app.on_key(45, 1)                                    # X
        app.on_key(50, 1)                                    # M
        check("字母键写进搜索词", app.query == "xm", app.query)
        found = [it["label"] for it in app.items]
        check("拼音首字母 xm 命中小米", "小米" in found, str(found[:5]))
        check("搜索结果被收窄", 0 < len(app.items) < len(app.brands),
              "%d 条" % len(app.items))
        app.on_key(33, 1)                                    # F：搜索态下是字母不是方向键
        check("搜索态下 F 当字母输入", app.query == "xmf", app.query)
        app.on_key(14, 1)                                    # 退格
        app.on_key(14, 1)
        check("退格删掉搜索词", app.query == "x", app.query)
        app.on_key(44, 1)                                    # Z 也是字母
        app.on_key(44, 1)
        check("搜不到时列表为空且不崩", app.items == [] and app.query == "xzz", app.query)
        app._activate()                                      # 空列表选定不能炸
        check("空结果下选定不崩", app.screen == "brands")
        app.on_key(9, 1)                                     # 键 8 → 清空
        check("键 8 清空关键词", app.query == "" and len(app.items) == len(app.brands),
              str(len(app.items)))
        app.on_key(8, 1)                                     # 键 7 → 退出搜索（留在品牌页）
        check("键 7 退出搜索并恢复全量",
              not app.searching and app.screen == "brands"
              and len(app.items) == len(app.brands), app.screen)

        app.sel = next(i for i, it in enumerate(app.items)
                       if it["label"] in ("创维", "海信", "TCL"))
        app._activate()
        settle(app)
        check("进入方案列表", app.screen == "groups" and len(app.groups) > 0,
              "%d 组" % len(app.groups))
        check("每组都带候选码表", all(g["keysetids"] for g in app.groups))

        app.sel = 0
        app._activate()
        settle(app)
        check("进入遥控器屏", app.screen == "remote", app.screen)
        check("已试发电源码一次", len(sent) == 1, str(sent))
        check("按键表已载入", len(app.ir_keys) > 10, "%d 键" % len(app.ir_keys))
        # 回归：任务结果只能消费一次。此前 poll_job() 每帧把「已完成」重放一遍，
        # 遥控页 grid_sel 被 50ms 重置回 0 —— 表现就是「进入操作页光标按了不动」。
        app.grid_sel = 7
        for _ in range(3):
            app.poll_job()
        check("任务结果只消费一次（不再把光标重置回 0）",
              app.grid_sel == 7, str(app.grid_sel))
        check("常用键排在最前",
              app.ir_keys[0] == "power" and {"vol+", "vol-"} <= set(app.ir_keys[:6]),
              str(app.ir_keys[:6]))
        check("型号名带品牌与方案", "方案" in (app.model["name"] or ""), app.model["name"])

        # 底栏不能被码表里的数字键名抢走（4~8 必须发快捷功能码）
        sent_names = []
        real_send = app.send_ir
        app.send_ir = lambda n: sent_names.append(n)
        for code in (5, 6, 7, 8, 9):                         # 键 4/5/6/7/8
            app.on_key(code, 1)
        check("遥控屏底栏 5 颗键都有动作", len(sent_names) == 5, str(sent_names))
        check("底栏 4 发电源而不是数字 4",
              sent_names[0] in app_mod.FOOTER_ALIASES[0] and sent_names[0] != "4",
              str(sent_names[0]))
        check("底栏 5/6 发音量而非数字 5/6",
              sent_names[1] in app_mod.FOOTER_ALIASES[1]
              and sent_names[2] in app_mod.FOOTER_ALIASES[2], str(sent_names[:3]))
        app.on_key(5, 2)                                     # 长按重复
        check("遥控屏重复事件不连发", len(sent_names) == 5, str(sent_names))
        app.grid_sel = 3
        app.on_key(6, 1)                                     # 底栏键 5 = 快捷发射
        check("底栏快捷发射不移动网格光标", app.grid_sel == 3, str(app.grid_sel))
        app.send_ir = real_send

        app.grid_sel = 0
        app.on_key(106, 1)                                   # RIGHT
        check("→ 移动一格", app.grid_sel == 1, str(app.grid_sel))
        for _ in range(3):
            app.on_key(106, 1)
        check("越界自动翻到下一页", app.grid_sel == 4, str(app.grid_sel))
        app.on_key(103, 1)                                   # UP
        check("↑ 上移一行", app.grid_sel == 0, str(app.grid_sel))

        # 这块键盘没有方向键键码（设备树 keymap 里没有 103/105/106/108），
        # 网格导航必须靠真实存在的 W/A/S/D；用固定 12 键网格避免依赖具体码表。
        saved_keys = app.ir_keys
        app.ir_keys = ["power", "vol-", "vol+", "mute", "menu", "ok",
                       "up", "down", "left", "right", "input", "home"]
        app.grid_sel = 0
        for code, label, want in ((32, "D 右", 1), (31, "S 下", 5),
                                  (30, "A 左", 4), (17, "W 上", 0)):
            app.on_key(code, 1)
            check("WASD 别名导航：%s" % label, app.grid_sel == want,
                  str(app.grid_sel))
        app.grid_sel = 5
        for code, label, want in ((33, "F 上", 1), (46, "C 右", 2),
                                  (44, "Z 左", 1), (45, "X 下", 5)):
            app.on_key(code, 1)
            check("FXZC 方向操作：%s" % label, app.grid_sel == want,
                  str(app.grid_sel))
        # 角标字母直发：按 Y/J/M 直接发射本页第 1/6/12 格，光标同步到该格
        real_send2 = app.send_ir
        direct = []
        app.send_ir = lambda n: direct.append(n)
        app.grid_sel = 0
        for code, key, want in ((21, "Y", 0), (36, "J", 5), (50, "M", 11)):
            app.on_key(code, 1)
            check("角标 %s 直发第 %d 格" % (key, want + 1),
                  direct[-1:] == [app.ir_keys[want]] and app.grid_sel == want,
                  "%s / sel=%d" % (direct[-1:], app.grid_sel))
        n = len(direct)
        app.on_key(21, 2)                                    # 长按重复事件
        check("角标长按不连发", len(direct) == n, str(direct[n:]))
        app.ir_keys = app.ir_keys[:6]                        # 只有 6 键的短码表
        app.grid_sel = 0
        n = len(direct)
        app.on_key(47, 1)                                    # V = 第三行第一格，本页没有
        check("空白格角标不误发", len(direct) == n and app.grid_sel == 0,
              str(direct[n:]))
        app.send_ir = real_send2

        app.grid_sel = 0
        app.ir_keys = saved_keys

        before = len(sent)
        app.send_ir("power")
        time.sleep(0.4)
        check("send_ir 走发射路径", len(sent) == before + 1, str(len(sent)))

        if len(app.group["keysetids"]) > 1:
            app.on_key(15, 1)                                # TAB
            settle(app)
            check("TAB 换到组内下一个码表", app.keyset_pos == 1, str(app.keyset_pos))
        else:
            check("该组仅 1 个码表，TAB 跳过", True)

        app.on_key(1, 1)                                     # ESC
        check("ESC 回方案列表", app.screen == "groups", app.screen)
        app.on_key(1, 1)
        check("ESC 回品牌列表", app.screen == "brands", app.screen)
        app.on_key(1, 1)
        check("ESC 回设备类型", app.screen == "types", app.screen)

        check("最近使用已落盘", bool(app.state.get("recent")),
              str(len(app.state.get("recent"))))
        app.sel = 0
        app._activate()
        check("最近使用可打开", app.screen == "recent" and len(app.items) >= 1)

        # 空调/风扇类：API 的 tree 为空，码表在 data.others，方案页不能是空的
        app._build_items()
        app.go("types")
        app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "空调")
        app._activate()
        settle(app)
        check("空调品牌列表非空", app.screen == "brands" and len(app.brands) > 0,
              "%s %d" % (app.screen, len(app.brands)))
        app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "小米")
        app._activate()
        settle(app)
        check("小米空调有方案（不再是没内容）",
              app.screen == "groups" and len(app.groups) > 0,
              "%s %d 组" % (app.screen, len(app.groups)))
        check("方案标记为 state 且带 vendor",
              all(g.get("kind") == "state" and g.get("vendor") for g in app.groups),
              str(app.groups[:1])[:120])
        check("状态型方案列出为「方案 N」",
              bool(app.items) and app.items[0]["label"] == "方案 1"
              and app.items[0]["sub"] == "状态型",
              str(app.items[0] if app.items else ""))
        app._activate()
        settle(app)
        check("状态型方案进入遥控器", app.screen == "remote", app.screen)
        check("空调配置已载入", app.ac is not None)
        check("遥控页是功能按钮而非数字键",
              app.ir_keys[:2] == ["ac_on", "ac_off"], str(app.ir_keys[:3]))
        wave = app.ac.encode(app.ac_state, 1)
        check("空调波形结构合法",
              len(wave) >= 20 and len(wave) % 2 == 0 and all(x > 0 for x in wave),
              "%d 段 %.1f ms" % (len(wave), sum(wave) / 1000.0))

        # 回归：状态必须真的写进帧里。此前 1001/1003/1004/1005 这些紧凑规则
        # 完全没解析，温度+/模式/风速发出去的都是同一帧（真机表现为「按了没反应」）。
        def state_bytes(**over):
            cfg = app.ac
            state = dict(cfg.initial_state())
            state.update(over)
            raw = kkac._hex(cfg.fields_for(1)[1002])
            if raw and raw[0] == len(raw) - 1:
                raw = raw[1:]
            cfg._apply_state(raw, state)
            return raw

        def frame(**over):
            cfg = app.ac
            state = dict(cfg.initial_state())
            state.update(over)
            return cfg.encode(state, 1)

        base = state_bytes(power=0)
        check("小米码表温度写在状态字节里（26℃ -> 0x34）",
              base[7] == 0x34, " ".join("%02X" % b for b in base))
        check("电源关闭会改写状态字节",
              state_bytes(power=1) != base,
              " ".join("%02X" % b for b in state_bytes(power=1)))
        check("温度+/温度- 不再是同一帧",
              frame(power=0, temperature=25) != frame(power=0, temperature=27),
              "%d/%d" % (len(frame(power=0, temperature=25)),
                         len(frame(power=0, temperature=27))))
        check("换模式会改变波形",
              len({tuple(frame(power=0, mode=m))
                   for m in app.ac.usable_indexes("mode")}) > 1,
              str(app.ac.usable_indexes("mode")))
        check("换风速会改变波形",
              len({tuple(frame(power=0, fanSpeed=s))
                   for s in app.ac.usable_indexes("fan")}) > 1,
              str(app.ac.usable_indexes("fan")))
        # 结构判定：码表里没有可调字段时，必须报「不支持」而不是照样发一帧
        check("码表能力判定与实际规则一致",
              app.ac.supports("ac_temp_up") == (len(app.ac.temp_map) > 1)
              and app.ac.supports("ac_mode") == (len(app.ac.mode_map) > 1),
              "temp=%s mode=%s" % (len(app.ac.temp_map), len(app.ac.mode_map)))
        before = len(sent)
        app._ac_press("ac_on")
        time.sleep(0.6)
        check("开机按状态拼帧并发射", len(sent) == before + 1,
              str(sent[before:]))
        check("开机后状态为开", app.ac_state.get("power") == 0, str(app.ac_state))
        # 回归：主循环每帧都会跑 poll_job()，任务结果若被重放就会把空调状态重置，
        # 表现是「按温度+ 没反应」。按一次温度+ 后必须能扛住空转轮询。
        before_temp = app.ac_state.get("temperature")
        app.on_key(23, 1)                                    # I = 温度+
        time.sleep(0.6)
        for _ in range(5):
            app.poll_job()
        check("温度+ 之后空转轮询不再重置空调状态",
              app.ac_state.get("temperature") == before_temp + 1,
              "%s -> %s" % (before_temp, app.ac_state.get("temperature")))
        before = len(sent)
        app.on_key(22, 1)                                    # U = 第 2 格「关机」
        time.sleep(0.6)
        check("角标字母也能操作空调状态",
              len(sent) == before + 1 and app.ac_state.get("power") == 1,
              str(sent[before:]))
        app.on_key(9, 1)                                     # 底栏 8 → 模式
        time.sleep(0.6)
        check("底栏也能操作状态型遥控", len(sent) >= before + 2, str(len(sent)))
        ac_kid, ac_vendor = app.model["id"], app.model.get("vendor")

        # 关键回归：others 只是「API 不返分组树」的响应形态，不等于状态型。
        # 只有空调的 type=2 码表要拼帧；风扇/盒子/功放/单反/灯泡等仍是
        # 普通脉冲码表（每键一段 AES 密文），解密后直接发。
        mixed = miir.brand_tree(3, 72)                       # 格力：type=1/2 混排
        kinds = sorted(set(g["kind"] for g in mixed["groups"]))
        check("格力方案 state/pulse 混排（不按品牌一刀切）",
              kinds == ["pulse", "state"], str(kinds))

        app._build_items()
        app.go("types")
        app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "风扇")
        app._activate()
        settle(app)
        app.sel = next(i for i, x in enumerate(app.items) if x["label"] == "美的")
        app._activate()
        settle(app)
        check("风扇方案非空且全部 pulse",
              bool(app.groups) and all(g.get("kind") == "pulse" for g in app.groups),
              str([g["kind"] for g in app.groups[:4]]))
        check("风扇方案不显示「状态型」",
              bool(app.items) and app.items[0]["sub"] != "状态型",
              str(app.items[0] if app.items else ""))
        app._activate()
        settle(app)
        check("风扇进普通遥控页（无 ac 配置、有 power 键）",
              app.screen == "remote" and app.ac is None and "power" in app.ir_keys,
              "%s %s" % (app.screen, app.ir_keys[:4]))
        before = len(sent)
        app.send_ir("power")
        time.sleep(0.6)
        check("风扇电源键按脉冲码发射", len(sent) == before + 1, str(sent[before:]))
        fan_kid, fan_vendor = app.model["id"], app.model.get("vendor")

        # 运行时兜底：预标记与实际码表不符时，按码表真实内容自动回落
        app.open_pair({"kind": "state", "keysetids": [fan_kid],
                       "vendor": fan_vendor, "frequency": 38000,
                       "power": None}, test=False)
        settle(app)
        check("state 误标时回落普通遥控",
              app.ac is None and "power" in app.ir_keys, str(app.ir_keys[:4]))
        app.open_pair({"kind": "pulse", "keysetids": [ac_kid],
                       "vendor": ac_vendor, "frequency": 38000,
                       "power": None}, test=False)
        settle(app)
        check("pulse 误标时仍走空调拼帧", app.ac is not None, str(app.ac))

        for p, pg in ((1, 0), (8, 0)):
            hint = app._remote_hint(p, pg)
            check("遥控页提示写明 FXZC（%d 页）" % p, "FXZC" in hint, hint)
        check("提示不再写错误的「Z/C 翻页」",
              "Z/C" not in app._remote_hint(8, 0), app._remote_hint(8, 0))

        for screen in ("types", "brands", "groups", "recent", "remote"):
            app.screen = screen
            app.draw()
            check("绘制 %s 无异常" % screen, app.last_frame is not None)
        app.running = False
    finally:
        miir.transmit = real_transmit
        miir.decrypt_pattern = real_decrypt


def test_table_kind():
    print("\n== 3. 码表类型判定（纯函数，不联网） ==")
    check("type=2 判为状态型",
          app_mod.is_state_table({"type": "2", "keys": {1002: "00", 1522: "x"}}))
    check("普通按键码表判为脉冲型",
          not app_mod.is_state_table({"type": None,
                                      "keys": {"power": "aa", "vol+": "bb", "1": "cc"}}))
    check("无 type 但含 1002 + Lua 也判状态型",
          app_mod.is_state_table({"keys": {"1002": "x", "1518": "y"}}))
    check("只有 1002 没有校验脚本不误判",
          not app_mod.is_state_table({"keys": {"1002": "x"}}))
    check("空码表不崩", not app_mod.is_state_table({}))


def test_grid_letters():
    print("\n== 5. 角标字母映射（纯函数，不联网） ==")
    L = app_mod.GRID_LETTERS
    check("12 个角标对应 12 个格子", len(L) == app_mod.GRID_PER_PAGE, str(len(L)))
    check("三行字母按网格行排布",
          L[:4] == ["Y", "U", "I", "O"] and L[4:8] == ["H", "J", "K", "L"]
          and L[8:] == ["V", "B", "N", "M"], str(L))
    check("角标字母互不重复", len(set(L)) == len(L), str(L))
    reserved = set("WASDFZXC") | set(app_mod.BOTTOM_KEYS) | {
        "ENTER", "ESC", "TAB", "SPACE", "BACKSPACE"}
    check("角标不占用导航/底栏/功能键", not (set(L) & reserved),
          str(set(L) & reserved))
    check("角标字母都在设备 keymap 里",
          set(L) <= set(app_mod.KEYS.values()),
          str(set(L) - set(app_mod.KEYS.values())))


def test_job_once():
    print("\n== 6. 后台任务结果一次性消费 ==")
    job = app_mod.Job()
    check("初始不处于运行态", not job.running and not job.done)
    job.start("demo", "计算", lambda: 42)
    _, value, error = job.wait(3)
    check("结果可取", value == 42 and error is None, "%s %s" % (value, error))
    check("第一次 take 取到结果", job.take() == ("demo", 42, None))
    check("take 之后任务复位", job.take() is None and not job.done and not job.running)
    job.start("boom", "出错", lambda: 1 / 0)
    job.wait(3)
    kind, value, error = job.take() or (None, None, None)
    check("异常也按一次结果返回", kind == "boom" and value is None and bool(error), str(error))


def test_brand_index():
    print("\n== 4. 品牌搜索索引（拼音首字母 + 命中强度） ==")
    py = app_mod.cp0_pinyin
    check("拼音表覆盖常用汉字", len(py._TABLE) > 3000, "%d 字" % len(py._TABLE))
    check("小米 → xm", py.variants("小米")[:1] == ["xm"], str(py.variants("小米")))
    check("格力 首个是常用读音 gl", py.variants("格力")[0] == "gl", str(py.variants("格力")))
    check("长虹 多音字 ch/zh 都能命中",
          {"ch", "zh"} <= set(py.variants("长虹")), str(py.variants("长虹")))
    check("纯英文品牌直接小写", py.variants("TCL") == ["tcl"], str(py.variants("TCL")))
    check("带后缀品牌也能出首字母串",
          py.variants("创维 C6000")[0].startswith("cwc6000"), str(py.variants("创维 C6000")))
    check("未收字不抛异常", py.variants("") == [] and py.letters_of("\u3000") == (),
          str(py.variants("")))

    score = app_mod.App._brand_score
    xm = {"name": "小米", "_ascii": "小米", "_py": ["xm"]}
    tcl = {"name": "TCL", "_ascii": "tcl", "_py": ["tcl"]}
    ch = {"name": "长虹", "_ascii": "长虹", "_py": ["zh", "ch"]}
    check("名字前缀命中强度最高", score(tcl, "tc") == 0, str(score(tcl, "tc")))
    check("常用读音命中等次一级", score(xm, "xm") == 2, str(score(xm, "xm")))
    check("生僻读音命中排在常用读音之后", score(ch, "ch") == 3, str(score(ch, "ch")))
    check("不匹配返回 -1", score(xm, "zz") == -1, str(score(xm, "zz")))
    check("空查询命中全部", score(xm, "") == 0)

    stub = type("Stub", (), {})()
    stub.device = {"name": "电视机顶盒"}
    check("设备类型名精确匹配热门表",
          app_mod.App._hot_names(stub)[:1] == ["创维"], str(app_mod.App._hot_names(stub)[:3]))
    stub.device = {"name": "空调"}
    check("空调热门按优先级给出", app_mod.App._hot_names(stub)[:3] == ["小米", "格力", "美的"],
          str(app_mod.App._hot_names(stub)[:3]))
    stub.device = {"name": "卫星电视"}
    check("没有热门表时返回空（不被「电视」误匹配）",
          app_mod.App._hot_names(stub) == [], str(app_mod.App._hot_names(stub)))


def main():
    test_keyboard()
    test_flow()
    test_table_kind()
    test_brand_index()
    test_grid_letters()
    test_job_once()
    print("\n结果:", "全部通过" if not FAIL else "失败 %d 项: %s" % (len(FAIL), FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
