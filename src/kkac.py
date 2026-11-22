#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KK 状态型空调编码器（type=2）。

算法逐条移植自 halifox/android_xiaomi_ir_lab：
  * app/src/main/java/.../KkAcLuaEncoder.java  —— 模板 / 位规则 / 波形组装
  * app/src/main/java/.../AcConfiguration.java —— 能力解析 / 状态归一化
  * docs/03-air-conditioner.md                —— 字段含义与验证边界

小米码库里空调不是「一个键一段固定波形」，而是协议规则：
  key['1002'] 基础状态字节模板（可能带长度头）
  key['1017'] 扩展状态到位范围的写入规则
  key['1522'] / ['1518'] Lua 校验与状态脚本
  key['309'] 多符号波形，或 ['300'..'307'] 引导 / 逻辑 0 / 逻辑 1
  key['888888'] 扩展功能与该功能的字段覆盖
每次按键按「当前完整状态」拼出整帧，再交给红外发射器。

⚠️ 边界（同原仓库）：这里没有 libkksdk.so 的 enc/enc2，1001~1009 只作为
   原始规则保留、不参与解释；原仓库只在格力空调上实测过，不能保证与小米原版
   逐字节一致，也不保证每套码表都控制真实设备。
"""

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile

# Lua 5.3 位运算符，系统的 lua 5.2 解析不了；与 Java 实现一样跳过这类脚本
LUA53_BITWISE = re.compile(r"(?<![&])&(?![&])|(?<![|])\|(?![|])|(?<![~=])~(?![=])|>>|<<")

MODE_NAMES = ["制冷", "制热", "自动", "送风", "除湿"]
FUNCTION_LABELS = {
    1: "电源", 2: "模式", 3: "温度+", 4: "温度-", 5: "风速",
    6: "上下扫风", 7: "上下定向", 8: "强力", 9: "开机定时", 10: "关机定时",
    22: "睡眠",
}


def _ints(text, default=()):
    if text is None:
        return list(default)
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if part:
            try:
                out.append(int(part))
            except ValueError:
                pass
    return out


def _int(text, fallback=0):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return fallback


def _hex(text):
    raw = str(text).strip()
    if len(raw) % 2:
        raise ValueError("无效十六进制数据 %r" % raw[:24])
    return [int(raw[i:i + 2], 16) for i in range(0, len(raw), 2)]


def _parse_container(value):
    """888888 在 API 里是 Python-repr 风格的字符串，不是标准 JSON。"""
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        try:
            return ast.literal_eval(value)
        except (TypeError, ValueError, SyntaxError):
            return []


def _as_fields(key):
    """把码库 key 里数值键名的原始规则取出来。"""
    out = {}
    for name, value in (key or {}).items():
        try:
            number = int(name)
        except (TypeError, ValueError):
            continue
        if isinstance(value, (str, int, float)):
            out[number] = str(value)
    return out


def _range(lo, hi, step=1):
    return list(range(lo, hi + 1, step))


def _marker(rule, tag):
    for part in str(rule or "").split("|"):
        if part.startswith(tag):
            return part
    return None


def _marker_values(rule, tag):
    """1501~1505 的 T / S 能力表达式 -> 可选的档位列表。"""
    mark = _marker(rule, tag)
    if mark is None:
        return _range(16, 30) if tag == "T" else _range(0, 3)
    if "&" not in mark:
        return []
    excluded = set(_ints(mark.split("&", 1)[1]))
    base = _range(16, 30) if tag == "T" else _range(0, 3)
    return [v for v in base if v not in excluded]


def _range_spec(text):
    """1515 的范围表达式 '10-1440,10' -> (可选值, 默认值)。"""
    text = str(text or "")
    head = text.split("$")[0]
    if "," in head:
        bounds, default = head.split(",", 1)
    else:
        bounds, default = head, None
    if "-" in bounds:
        lo, hi = bounds.split("-", 1)
        lo, hi = _int(lo), _int(hi)
        if hi < lo:
            lo, hi = hi, lo
        values = _range(lo, min(hi, lo + 64))
    else:
        values = [_int(bounds)]
    return values, _int(default, values[0] if values else 0)


def _lua_binary():
    """找能跑码库脚本的 Lua：必须是 LuaJIT 或 5.3+（脚本用 loadfile(env) 和位运算）。

    系统的 `lua` 往往是 5.1，没有 bit32、也不支持 loadfile 第三参，不能当兜底。
    """
    for name in ("luajit", "lua5.4", "lua5.3"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("未安装 luajit（空调码表的 Lua 校验和需要）")


def _run_lua(script, values, state, function_id):
    """在受限 Lua 环境里执行 1522/1518，返回修改后的状态字节。

    本机 lua 是 5.1、没有 bit32；LuaJIT 有 bit.*，这里做一层兼容映射，
    脚本本身保持码库原样不改。
    """
    lua = _lua_binary()
    # 码库里偶有 `bytes1[13]=bytes[4]+0x10;;` 这种连续分号，Lua 5.1/LuaJIT 会直接报
    # 「unexpected symbol near ';'」，这里只做语法层归一化，不改写脚本逻辑。
    script = re.sub(r";;+", ";", script)
    byte_literal = "{" + ",".join(str(v & 0xFF) for v in values) + "}"
    extra_literal = "{" + ",".join(
        "[%d]=%d" % (int(k), int(v)) for k, v in (state.get("exts") or {}).items()
    ) + "}"
    wrapper = """local _bit32
if type(bit32) == "table" then _bit32 = bit32
elseif type(bit) == "table" then _bit32 = {
  band = bit.band, bor = bit.bor, bxor = bit.bxor, bnot = bit.bnot,
  lshift = bit.lshift, rshift = bit.rshift, arshift = bit.arshift,
} end
local env = {
  bytes = %s,
  power = %d, mode = %d, temperature = %d, windSpeed = %d,
  udWindMode = %d, functionId = %d, exts = %s,
  bit32 = _bit32, math = math, table = table, string = string,
  tonumber = tonumber, tostring = tostring, ipairs = ipairs, pairs = pairs,
}
local chunk, err = loadfile(%s, "t", env)
if not chunk then error(err) end
if setfenv then setfenv(chunk, env) end
chunk()
local b = env.bytes
local out = {}
for i = 1, #b do out[i] = tostring(math.floor(tonumber(b[i]) or 0) %% 256) end
io.write(table.concat(out, ","))
""" % (byte_literal, _int(state.get("power")), _int(state.get("mode")),
       _int(state.get("temperature")), _int(state.get("fanSpeed")),
       _int(state.get("windDirection")), int(function_id), extra_literal,
       json.dumps("__SCRIPT__"))

    with tempfile.TemporaryDirectory(prefix="kkac-") as folder:
        script_path = os.path.join(folder, "script.lua")
        wrapper_path = os.path.join(folder, "wrap.lua")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)
        with open(wrapper_path, "w", encoding="utf-8") as fh:
            fh.write(wrapper.replace('"__SCRIPT__"', json.dumps(script_path)))
        proc = subprocess.run(
            [lua, wrapper_path], capture_output=True, text=True, timeout=5
        )
    if proc.returncode != 0:
        raise RuntimeError("Lua 执行失败: %s" % (proc.stderr or "").strip()[:120])
    return [_int(v) & 0xFF for v in proc.stdout.strip().split(",") if v.strip()]


def _write_bits(values, start, end_exclusive, value):
    """1017：把一个整数按高位优先写进状态字节的位范围（Java writeBits）。"""
    width = end_exclusive - start
    for offset in range(width):
        position = start + offset
        index = position // 8
        if index < 0 or index >= len(values):
            continue
        mask = 1 << (7 - position % 8)
        bit = (value >> (width - 1 - offset)) & 1
        values[index] = values[index] | mask if bit else values[index] & ~mask & 0xFF


def _apply_function_rules(values, encoded, exts):
    if not encoded:
        return
    for item in str(encoded).split("@"):
        try:
            record = _hex(item)
        except ValueError:
            continue
        if len(record) < 3 or record[0] != len(record) - 1:
            continue
        if exts.get(record[1]) != record[2]:
            continue
        for index in range(3, len(record) - 2, 3):
            _write_bits(values, record[index], record[index + 1], record[index + 2])


def _records(text, width):
    """解码 1001/1003/1004/1005/1011/1012/1013 这类紧凑状态规则。

    结构（与原版 libkksdk 的 initRemote 规则同形，逐字节可从码库验证）：
        每条记录 = [count][count 个字节]，重复到字符串结束；
        count 为 width 的整数倍，按 width 切成若干条写入指令：
          width=2 -> (字节下标, 字节值)   下标 0 基，与 Lua 的 bytes[] 差 1
          width=3 -> (起始位, 结束位, 值) 绝对位下标，语义同 1017
    count=0 是一条「空记录」，表示该状态不改字节（沿用模板）。

    证据链：
      * 美的 1001 = 00 08 (02,DE)(03,21)(04,07)(05,F8)，与该型号 1522 里
        power==1 时 bytes[3..6]=DE,21,07,F8 完全对应（Lua 1 基 = 规则 0 基 + 1）；
      * 小米 7631 1003 = 02 02 (02,0x10..0x1E)，模板第 3 字节正好是 0x1A=26℃；
      * 格力 1001 = 00 02 (00,0x41)，模板首字节 0x49 —— 只差 0x08 电源位。
    """
    data = _hex(text or "")
    out = []
    index = 0
    while index < len(data):
        count = data[index]
        index += 1
        chunk = data[index:index + count]
        index += count
        items = [tuple(chunk[i:i + width]) for i in range(0, len(chunk) - width + 1, width)]
        out.append(items)
    return out


def _apply_record(values, record):
    """把一条记录里的写入指令落到状态字节上。"""
    for item in record:
        if len(item) == 2:
            index_byte, value = item
            if 0 <= index_byte < len(values):
                values[index_byte] = value & 0xFF
        elif len(item) >= 3:
            _write_bits(values, item[0], item[1], item[2])


def _value_map(records):
    """字段取值序号 -> 规则记录。

    部分码表（如美的 1011）首条是空记录，那一位是占位符，真正的
    16℃ 从第 2 条开始 —— 模板字节与第 2 条完全一致可以佐证；这里据此
    整体后移一位，避免「16℃ 和 17℃ 发出同一帧」。
    """
    if len(records) > 1 and not records[0]:
        return {i - 1: rec for i, rec in enumerate(records[1:], 1) if rec}
    return {i: rec for i, rec in enumerate(records) if rec}


def _append_signed(output, pattern):
    """309：合并连续同相位的正负时长（Java appendSignedPattern）。"""
    for duration in pattern:
        if not output and duration <= 0:
            continue
        next_is_mark = len(output) % 2 == 0
        starts_new_phase = duration > 0 if next_is_mark else duration < 0
        magnitude = abs(duration)
        if starts_new_phase or not output:
            output.append(magnitude)
        else:
            output[-1] += magnitude


def _build_pattern_wave(encoded, symbol_limit, repeat, values):
    records = [_ints(part) for part in str(encoded).split("|") if part.strip()]
    records = [r for r in records if r]
    if not records:
        raise ValueError("字段 309 没有符号波形")
    size = len(records)
    bits = 8 if size > 16 else 4 if size > 4 else 2 if size > 2 else 1
    per_byte = 8 // bits
    output = []
    symbols = 0
    for value in values:
        for offset in range(per_byte):
            index = ((value << (offset * bits)) & 0xFF) >> (8 - bits)
            if index >= size:
                raise ValueError("字段 309 缺少符号 %d" % index)
            _append_signed(output, records[index])
            symbols += 1
            if symbol_limit > 0 and symbols >= symbol_limit:
                break
        else:
            continue
        break
    if len(output) % 2:
        output.append(1000)
    return output * max(1, repeat)


def _build_wave(fields, values):
    if fields.get(309):
        return _build_pattern_wave(fields[309], _int(fields.get(310), 0),
                                   max(1, _int(fields.get(1508), 1)), values)
    lead = _ints(fields.get(300))
    zero = _ints(fields.get(301))
    one = _ints(fields.get(302))
    if not zero or not one:
        raise ValueError("配置缺少字段 301/302（逻辑波形）")
    little = str(fields.get(306)) == "1"
    add_trailer = str(fields.get(307)) != "1"
    repeat = max(1, _int(fields.get(1508), 1))
    bit_counts = {}
    for item in str(fields.get(1509) or "").split("|"):
        if "&" in item:
            idx, count = item.split("&", 1)
            bit_counts[_int(idx)] = _int(count, 8)
    delays = {}
    for item in str(fields.get(303) or "").split("|"):
        if "&" in item:
            idx, wave = item.split("&", 1)
            delays[_int(idx)] = _ints(wave)
    output = list(lead)
    last = len(values) - 1
    for index, value in enumerate(values):
        count = bit_counts.get(index, bit_counts.get(-1, 8) if index == last else 8)
        bits = range(count) if little else range(count - 1, -1, -1)
        for bit in bits:
            output.extend(one if (value & (1 << bit)) else zero)
        output.extend(delays.get(index, ()))
    if add_trailer and one:
        output.append(one[0])
    output.extend(delays.get(-1, ()))
    if len(output) % 2:
        output.append(1000)
    return output * repeat


def _validate(pattern):
    if not pattern:
        raise ValueError("空调波形为空")
    for value in pattern:
        if value <= 0:
            raise ValueError("空调波形包含非正时长")


class AcConfig:
    """一个 type=2 型号的配置与状态模型。"""

    def __init__(self, key, model_id=""):
        self.model_id = model_id
        self.raw = _as_fields(key)
        self.extras = _parse_container(key.get("888888") if isinstance(key, dict) else None)
        self.overrides = {}
        if isinstance(self.extras, list):
            for item in self.extras:
                if not isinstance(item, dict) or "fid" not in item:
                    continue
                fields = _as_fields(item.get("exts") or {})
                if fields:
                    self.overrides[_int(item["fid"], -1)] = fields

        self.modes = []
        for mode in range(5):
            rule = self.raw.get(1501 + mode)
            if rule and "NA" in rule.upper():
                continue
            self.modes.append({
                "value": mode,
                "name": MODE_NAMES[mode],
                "temperatures": _marker_values(rule, "T"),
                "fanSpeeds": _marker_values(rule, "S"),
            })
        if not self.modes:
            self.modes.append({"value": 0, "name": "制冷",
                               "temperatures": _range(16, 30), "fanSpeeds": _range(0, 3)})

        self.extra_state = {}
        rules = self.raw.get(1515)
        if rules:
            for item in str(rules).split("@"):
                parts = item.split("|")
                if len(parts) < 4:
                    continue
                fid = _int(parts[0], -1)
                if fid <= 7:
                    continue
                values, default = _range_spec(parts[1])
                self.extra_state[fid] = default
        if isinstance(self.extras, list):
            for item in self.extras:
                if isinstance(item, dict) and "fid" in item:
                    self.extra_state.setdefault(_int(item["fid"], -1), 0)

        # 电源/温度/模式/风速：码库里由 1001~1013 的紧凑规则描述，
        # 每张表用哪一组字段并不统一（小米用 1012/1013 表示模式/风速，
        # 美的用 1011 表示温度），所以两组都试。
        self.power_rules = _records(self.raw.get(1001), 2)
        self.temp_rules = _records(self.raw.get(1003), 2) or _records(self.raw.get(1011), 3)
        self.mode_rules = _records(self.raw.get(1004), 2) or _records(self.raw.get(1012), 3)
        self.fan_rules = _records(self.raw.get(1005), 2) or _records(self.raw.get(1013), 3)
        self.power_map = {i: rec for i, rec in enumerate(self.power_rules) if rec}
        self.temp_map = _value_map(self.temp_rules)
        self.mode_map = _value_map(self.mode_rules)
        self.fan_map = _value_map(self.fan_rules)

    def usable_indexes(self, kind):
        """该字段里真正能拼出不同帧的取值序号（空记录 = 沿用模板，发出去看不出变化）。"""
        return sorted({"temp": self.temp_map, "mode": self.mode_map,
                       "fan": self.fan_map}.get(kind, {}))

    def supports(self, action):
        if action in ("ac_on", "ac_off"):
            return True
        if action.startswith("ac_temp"):
            return len(self.temp_map) > 1
        if action in ("ac_mode", "ac_cool", "ac_heat"):
            return len(self.mode_map) > 1
        if action == "ac_fan":
            return len(self.fan_map) > 1
        return True


    def fields_for(self, function_id):
        fields = dict(self.raw)
        fields.update(self.overrides.get(function_id, {}))
        return fields

    def initial_state(self):
        usable_modes = self.usable_indexes("mode")
        mode = (self.mode(usable_modes[0]) if usable_modes else self.modes[0])
        temps = mode["temperatures"] or _range(16, 30)
        usable_temp = set(self.usable_indexes("temp"))
        allowed = [t for i, t in enumerate(temps) if i in usable_temp] or temps
        preferred = {0: 26, 1: 20, 4: 23}.get(mode["value"], 24)
        usable_fan = set(self.usable_indexes("fan"))
        speeds = [s for s in (mode["fanSpeeds"] or [0]) if s in usable_fan] or (mode["fanSpeeds"] or [0])
        return {
            "power": 1,                       # 0=开，1=关
            "mode": mode["value"],
            "temperature": preferred if preferred in allowed else allowed[0],
            "fanSpeed": speeds[0],
            "windDirection": 0,
            "exts": dict(self.extra_state),
        }

    def normalize(self, state, function_id):
        result = dict(state)
        exts = dict(result.get("exts") or {})
        if function_id == 1:                # 电源操作取消定时与睡眠
            for fid in (9, 10, 22):
                if fid in exts:
                    exts[fid] = 0
        result["exts"] = exts
        return result

    def mode(self, value):
        for item in self.modes:
            if item["value"] == value:
                return item
        return self.modes[0]

    def encode(self, state, function_id):
        """按当前状态生成一帧微秒波形。"""
        fields = self.fields_for(function_id)
        template = fields.get(1002)
        if not template:
            raise ValueError("配置缺少字段 1002（状态模板）")
        values = _hex(template)
        if values and values[0] == len(values) - 1:   # 去掉长度头
            values = values[1:]
        self._apply_state(values, state)
        _apply_function_rules(values, fields.get(1017), state.get("exts") or {})

        script = fields.get(1522)
        if not script:
            legacy = fields.get(1518)
            if legacy and not LUA53_BITWISE.search(legacy):
                script = legacy
        if script and str(script).strip():
            values = _run_lua(script, values, state, function_id)
        pattern = _build_wave(fields, values)
        _validate(pattern)
        return pattern

    def _apply_state(self, values, state):
        """把风速/模式/温度/电源写进状态字节（先于 Lua 校验和）。

        顺序按码库的字段重叠关系定：
          * 风速写整字节，模式在 auto 档会强写这几字节，所以风速先写、模式后写；
          * 温度规则只写温度字段的低半字节，必须排在模式之后，否则会被模式的
            整字节写入覆盖（美的 1004/1011 就是这种叠法）；
          * 电源记录是整帧覆盖（小米 1001 直接写整个字节，该字节同装模式/扫风位），
            放最后，让关机帧盖住前面的状态位。
        """
        mode = self.mode(state.get("mode", 0))
        temps = mode["temperatures"] or _range(16, 30)
        temperature = state.get("temperature", temps[0])
        index = temps.index(temperature) if temperature in temps else temperature - temps[0]
        for lookup, key in ((self.fan_map, state.get("fanSpeed", 0)),
                            (self.mode_map, state.get("mode", 0)),
                            (self.temp_map, index),
                            (self.power_map, 1 if state.get("power") else 0)):
            record = lookup.get(key)
            if record:
                _apply_record(values, record)


    def summary(self, state):
        mode = self.mode(state.get("mode", 0))
        return "%s %d℃ %s %s" % (
            "开" if state.get("power") == 0 else "关",
            state.get("temperature", 26), mode["name"],
            ["自动", "低", "中", "高"][state.get("fanSpeed", 0) % 4],
        )
