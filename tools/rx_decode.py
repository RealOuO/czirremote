# -*- coding: utf-8 -*-
"""抓真实遥控器的红外帧并还原成空调码表的 12 字节状态。

设备：/dev/lirc1（gpio_ir_recv, GPIO13）。用法：

    python3 rx_decode.py 20          # 监听 20 秒，每收到一帧就打印
    python3 rx_decode.py --selftest  # 不用硬件，校验解码器本身

这类表（小米/美的等）的波形是「引导 1000/580 + 48 个数字 + 尾部 600」，
数字由间隔长度区分：380→0、880→1、2210→2、1470→3。
48 个数字 = 12 字节状态（每个字节 4 个 2 bit 数字），
最后一字节是前 11 字节的 XOR 校验。
"""
import os
import subprocess
import sys
import time

DEV_RX = "/dev/lirc1"
SYMBOL_SPACES = [380, 880, 2210, 1470]      # 索引即数字 0/1/2/3
TOLERANCE = 300
SYMBOL_COUNT = 48
LEAD_PULSE = (850, 1600)


def parse_event(line):
    parts = line.split()
    if len(parts) != 2:
        return None
    kind, value = parts[0].lower(), parts[1]
    if kind not in ("pulse", "space") or not value.isdigit():
        return None
    return kind, int(value)


def frame_to_digits(frame):
    """引导 mark + 引导 space + (数据 mark + 数字 space) × 48 + 尾部 mark。"""
    lead = None
    for index, (kind, us) in enumerate(frame):
        if kind == "pulse" and LEAD_PULSE[0] <= us <= LEAD_PULSE[1]:
            lead = index
            break
    if lead is None:
        return []
    digits, skipped = [], False
    for kind, us in frame[lead + 1:]:
        if kind != "space":
            continue
        if not skipped:                      # 引导间隔 580，不是数据
            skipped = True
            continue
        if us > 6000:                        # 帧尾静默
            break
        digit = min(range(4), key=lambda i: abs(us - SYMBOL_SPACES[i]))
        if abs(us - SYMBOL_SPACES[digit]) > TOLERANCE:
            break
        digits.append(digit)
        if len(digits) >= SYMBOL_COUNT:
            break
    return digits


def digits_to_state(digits):
    """48 个 2 bit 数字 -> 12 字节状态（Lua 重打包的逆运算）。"""
    if len(digits) < SYMBOL_COUNT:
        return None
    out = []
    for index in range(0, SYMBOL_COUNT, 4):
        value = 0
        for chunk in digits[index:index + 4]:
            value = (value << 2) | chunk
        out.append(value)
    return out


def describe(frame):
    digits = frame_to_digits(frame)
    state = digits_to_state(digits)
    if not state:
        spaces = [us for kind, us in frame if kind == "space"][:10]
        return "数字不足（%d/%d） 间隔前几项 %s" % (len(digits), SYMBOL_COUNT, spaces)
    acc = 0
    for value in state[:-1]:
        acc ^= value
    return "%-38s 校验%s 共%d段" % (
        " ".join("%02X" % v for v in state),
        "OK" if acc == state[-1] else "错", len(frame))


def watch(seconds):
    proc = subprocess.Popen(["ir-ctl", "-d", DEV_RX, "--mode2", "-r"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    print("监听 %s %.0f 秒，请把遥控器对准设备按一下…" % (DEV_RX, seconds), flush=True)
    deadline = time.time() + seconds
    frame, count = [], 0
    try:
        for line in proc.stdout:
            event = parse_event(line.strip())
            if event is None:
                continue
            kind, us = event
            if kind == "pulse" and LEAD_PULSE[0] <= us <= LEAD_PULSE[1]:
                if len(frame) > 20:
                    count += 1
                    print("帧 %d: %s" % (count, describe(frame)), flush=True)
                frame = [event]
            elif frame:
                if us > 12000:
                    if len(frame) > 20:
                        count += 1
                        print("帧 %d: %s" % (count, describe(frame)), flush=True)
                    frame = []
                else:
                    frame.append(event)
            if time.time() > deadline:
                break
    finally:
        proc.terminate()
    if len(frame) > 20:
        count += 1
        print("帧 %d: %s" % (count, describe(frame)), flush=True)
    print("共识别 %d 帧" % count, flush=True)


def selftest():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import kkac
    import miir

    data = miir.remote("kk_3_153_7988", "kk")
    cfg = kkac.AcConfig(data["keys"], "kk_3_153_7988")
    state = cfg.initial_state()
    state["power"] = 0
    wave = cfg.encode(state, 3)
    frame = [("pulse" if index % 2 == 0 else "space", us) for index, us in enumerate(wave)]
    print("波形 %d 段" % len(wave))
    print("   ", describe(frame))
    got = digits_to_state(frame_to_digits(frame))
    ok = bool(got) and sum(got[:-1]) >= 0 and (lambda a: a == got[-1])(_xor(got))
    print("自测解码:", "PASS" if ok else "FAIL", "b7=0x%02X" % got[7] if got else "")


def _xor(values):
    acc = 0
    for value in values[:-1]:
        acc ^= value
    return acc


def main(argv):
    if "--selftest" in argv:
        selftest()
        return 0
    seconds = int(argv[1]) if len(argv) > 1 else 20
    if not os.path.exists(DEV_RX):
        print("%s 不存在" % DEV_RX)
        return 1
    watch(seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
