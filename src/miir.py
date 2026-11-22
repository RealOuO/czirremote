#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""miir.py — 小米万能遥控码库内核（CardputerZero 红外收发）

码库来源：ysard/mi_remote_database 逆向的小米 IRDB（米家万能遥控同源）
解密链  ：Base64 -> AES-ECB(32 字符 ASCII 作 key) -> rstrip -> GZIP -> JSON(微秒时长)
发射    ：/dev/lirc0  (gpio-ir-tx,  GPIO12)
接收    ：/dev/lirc1  (gpio_ir_recv, GPIO13)

用法：
  ./miir.py devices
  ./miir.py brands 1 小米
  ./miir.py models 1 153
  ./miir.py keys   153_5122
  ./miir.py code   153_5122 power
  ./miir.py send   153_5122 power
  ./miir.py raw    /tmp/x.ir 38000
  ./miir.py listen 5
"""
import base64
import gzip
import hashlib
import hmac
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- 常量
SERVER = "https://sg-urc.io.mi.com"
URL_TOKEN = "0f9dfa001cba164d7bda671649c50abf"
URL_SKEY = "581582928c881b42eedce96331bff5d3"
PATTERN_KEY = "fd7e915003168929c1a9b0ec32a60788"  # 32 字符 => AES-256
UA = "okhttp/3.8.0"
DEV_TX = "/dev/lirc0"
DEV_RX = "/dev/lirc1"


def _cache_dir():
    """缓存目录：遵循 XDG，不写死任何用户名/绝对路径。"""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "cp0-ir-remote")


CACHE_DIR = _cache_dir()
CACHE_TTL = 30 * 86400

VENDOR_BY_PREFIX = {"xm_": "mi", "kk_": "kk", "mx_": "mx", "yk_": "yk"}


# ---------------------------------------------------------------- HTTP
def _build_url(prefix, params, country="CN"):
    """按小米 App 的 anti-replay 方式拼 url：固定参数 + 业务参数 + opaque 签名"""
    fixed = [
        ("country", country),
        ("version", 6034),
        ("ts", int(time.time() * 1000)),
        ("nonce", random.randint(-500000000, 500000000)),
    ]
    query = urllib.parse.urlencode(fixed + list(params))
    path_url = prefix + "?" + query
    sig = hmac.new(
        URL_SKEY.encode(), (path_url + "&token=" + URL_TOKEN).encode(), hashlib.sha1
    ).hexdigest()
    return SERVER + path_url + "&opaque=" + sig


def _cache_path(key):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, safe + ".json")


def _http_json(url, cache_key=None, ttl=CACHE_TTL):
    """带磁盘缓存的 GET；缓存能显著减少重复请求"""
    if cache_key:
        path = _cache_path(cache_key)
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

    req = urllib.request.Request(
        url, headers={"accept-encoding": "gzip", "user-agent": UA}
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        body = resp.read()
        if "gzip" in resp.headers.get("content-encoding", ""):
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
    data = json.loads(body)

    if cache_key:
        # 缓存只是加速，写入失败（只读 HOME / 无权限）不能连带把请求也判失败
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = _cache_path(cache_key) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _cache_path(cache_key))
        except OSError:
            pass
    return data


# ---------------------------------------------------------------- 解密
def _aes_ecb_decrypt(raw, key):
    """AES-ECB 解密。用 openssl 子进程，避免依赖 pycryptodome"""
    n = len(key)
    if n not in (16, 24, 32):
        raise ValueError("AES key 长度必须是 16/24/32 字节，当前 %d" % n)
    algo = {16: "aes-128-ecb", 24: "aes-192-ecb", 32: "aes-256-ecb"}[n]
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("找不到 openssl，无法解密码库")
    proc = subprocess.run(
        [openssl, "enc", "-d", "-" + algo, "-K", key.encode().hex(), "-nopad"],
        input=raw,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("AES 解密失败: %s" % proc.stderr.decode("utf-8", "replace")[:200])
    return proc.stdout


def decrypt_pattern(encoded, secret_key=None):
    """Base64 -> AES -> 去尾部空白 -> GZIP -> 微秒时长数组

    :param encoded: 型号 JSON 里 key 字段的 Base64 密文
    :param secret_key: 型号自带 seceret_key（可选，覆盖全局 key）
    :return: list[int] 交替的 mark/space 微秒时长，首项是 pulse
    """
    if not encoded or not isinstance(encoded, str):
        raise ValueError("空密文")
    raw = base64.b64decode(encoded)
    key = secret_key or PATTERN_KEY
    plain = _aes_ecb_decrypt(raw, key).rstrip()
    if plain[:2] != b"\x1f\x8b":
        # 型号自带 key 可能是 hex 形式，退一步再试
        if secret_key:
            try:
                plain = _aes_ecb_decrypt(raw, bytes.fromhex(secret_key).decode("latin-1")).rstrip()
            except Exception:
                pass
    if plain[:2] != b"\x1f\x8b":
        raise ValueError("解密结果不是 GZIP（key 不匹配？头部 %s）" % plain[:2].hex())
    return json.loads(gzip.decompress(plain).decode("utf-8"))


# ---------------------------------------------------------------- 查询
def devices():
    """返回 [{deviceid, name}]"""
    j = _http_json(_build_url("/controller/device/1", []), "devices")
    out = []
    for d in _as_list(j.get("data")):
        lang = d.get("language") or []
        name = ""
        for item in lang:
            if isinstance(item, dict) and item.get("lang") in ("zh", "cn", "zh_CN"):
                name = item.get("name") or ""
                break
        if not name and lang and isinstance(lang[0], dict):
            name = lang[0].get("name") or ""
        out.append({"deviceid": d.get("deviceid"), "name": name or ("设备%s" % d.get("deviceid"))})
    return out


def brands(deviceid):
    """返回 [{brandid, name}]，按 brandid 去重保留首次出现"""
    j = _http_json(
        _build_url("/controller/brand/list/1", [("devid", deviceid)]),
        "brands_%s" % deviceid,
    )
    seen, out = set(), []
    for b in _as_list(j.get("data")):
        bid = b.get("brandid")
        if bid in seen:
            continue
        seen.add(bid)
        out.append({"brandid": bid, "name": b.get("name") or str(bid)})
    return out


def models(deviceid, brandid):
    """返回该品牌的型号列表 [{id, name, vendor}]"""
    j = _http_json(
        _build_url(
            "/controller/match/tree/1",
            [("devid", deviceid), ("miyk", 1), ("brandid", brandid), ("power", 1)],
        ),
        "tree_%s_%s" % (deviceid, brandid),
    )
    data = j.get("data") or {}
    out = []

    for node in (data.get("tree") or {}).get("nodes") or []:
        for mid in node.get("keysetids") or []:
            out.append({"id": mid, "name": node.get("name") or "", "vendor": _guess_vendor(mid)})

    for other in data.get("others") or []:
        mid = other.get("_id")
        if not mid:
            continue
        src = other.get("source")
        out.append({
            "id": mid,
            "name": other.get("name") or "",
            "vendor": src if src in ("mi", "kk", "mx", "yk") else _guess_vendor(mid),
        })
    return out


def brand_tree(deviceid, brandid):
    """品牌匹配树：按「试发电源码」分组的方案列表

    小米码库里没有型号名称：品牌下只有一组组候选码表（keysetids），
    每组共用一条电源码（ir_zip_key），靠试发确认哪组可用，
    这与米家 App 的配遥控器流程一致。
    """
    j = _http_json(
        _build_url(
            "/controller/match/tree/1",
            [("devid", deviceid), ("miyk", 1), ("brandid", brandid), ("power", 1)],
        ),
        "tree_%s_%s" % (deviceid, brandid),
    )
    data = j.get("data") or {}
    tree = data.get("tree") or {}
    groups = []
    for node in tree.get("nodes") or []:
        if node.get("level") != 1:
            continue
        groups.append({
            "index": node.get("index"),
            "frequency": node.get("frequency") or 38000,
            "power": node.get("ir_zip_key"),
            "keysetids": node.get("keysetids") or [],
            "children": node.get("children_index") or [],
            "kind": "pulse",           # 普通脉冲码表，可直接解密发射
        })

    if not groups:
        # API 不返回分组树时，码表直接列在 data.others。
        # ⚠️ others 只是「响应形态」，不代表码表类型：
        #   type=2 是空调状态帧（1002 模板 + Lua 校验 + 位时序，需拼帧）；
        #   type=1 / 无 type 是普通脉冲码表（每键一段 AES 密文，解密后直接发）。
        # 风扇/盒子/功放/投影仪/单反/灯泡/热水器等都属于后者；
        # 同一个空调品牌里也会混着 type=1 的普通码表（格力、海尔等），
        # 所以必须逐条读 others[].type，不能按设备或品牌一刀切。
        others = [o for o in (data.get("others") or []) if o.get("_id")]

        def order_of(o):
            try:
                return int(o.get("order") or 0)
            except (TypeError, ValueError):
                return 0

        for i, o in enumerate(sorted(others, key=order_of)):
            mid = o["_id"]
            try:
                freq = int(o.get("frequency") or 38000)
            except (TypeError, ValueError):
                freq = 38000
            groups.append({
                "index": i,
                "frequency": freq,
                "power": None,        # 没有现成的电源码密文，不能试发
                "keysetids": [mid],
                "children": [],
                "vendor": o.get("source") or _guess_vendor(mid),
                "kind": "state" if str(o.get("type") or "") == "2" else "pulse",
            })
    return {"frequency": tree.get("frequency") or 38000, "groups": groups}


def _vendor_candidates(matchid):
    """型号 ID 前缀不一定可靠（如 153_5122 其实属于 kk 库），按优先级逐个试"""
    first = _guess_vendor(matchid)
    return [first] + [v for v in ("mi", "kk", "mx", "yk") if v != first]


def _fetch_remote_data(matchid, vendor):
    j = _http_json(
        _build_url("/controller/code/1", [("matchid", matchid), ("vendor", vendor)]),
        "code_%s_%s" % (matchid, vendor),
    )
    data = j.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    return data or {}


def remote(matchid, vendor=None):
    """取一个型号的全部按键密文：{id, frequency, type, keys{name: cipher}}

    未指定 vendor 时依次尝试 mi/kk/mx/yk，取第一个有按键的；空结果不留在缓存里。
    """
    candidates = [vendor] if vendor else _vendor_candidates(matchid)
    empty = []
    for v in candidates:
        data = _fetch_remote_data(matchid, v)
        keys = data.get("key") or {}
        if keys:
            return {
                "id": data.get("_id") or matchid,
                "vendor": v,
                "frequency": data.get("frequency") or 38000,
                "type": data.get("type"),
                "secret_key": data.get("seceret_key"),
                "keys": keys,
            }
        empty.append(v)
        # 空结果会把缓存"钉死"，删掉
        p = _cache_path("code_%s_%s" % (matchid, v))
        if os.path.exists(p):
            os.remove(p)
    raise LookupError("型号 %s 在 vendor=%s 下都没有按键" % (matchid, ",".join(empty)))


def key_names(matchid, vendor=None):
    return list(remote(matchid, vendor)["keys"].keys())


def timing(matchid, key, vendor=None):
    """返回 (frequency, [微秒...])"""
    r = remote(matchid, vendor)
    cipher = r["keys"].get(key)
    if cipher is None:
        raise KeyError("该型号没有按键 %r，可选：%s" % (key, ", ".join(r["keys"]) or "(空)"))
    return r["frequency"], decrypt_pattern(cipher, r.get("secret_key"))


def _as_list(data):
    if isinstance(data, dict):
        return data.get("data") or []
    return data or []


def _guess_vendor(matchid):
    for prefix, vendor in VENDOR_BY_PREFIX.items():
        if str(matchid).startswith(prefix):
            return vendor
    return "mi"


# ---------------------------------------------------------------- 发射 / 接收
def to_lirc_text(timings):
    """微秒交替数组 -> ir-ctl 可读的 pulse/space 文本"""
    lines = []
    for i, us in enumerate(timings):
        if us <= 0:
            continue
        lines.append("%s %d" % ("pulse" if i % 2 == 0 else "space", us))
    # 结尾 space 无意义，去掉避免 ir-ctl warning
    if lines and lines[-1].startswith("space"):
        lines.pop()
    return "\n".join(lines) + "\n"


def transmit(frequency, timings, device=DEV_TX, repeat=1):
    """在 GPIO12 上按 raw 波形发射（bit-bang，任意协议都能发）"""
    if not os.path.exists(device):
        raise FileNotFoundError("%s 不存在：gpio-ir-tx overlay 未启用？" % device)
    text = to_lirc_text(timings)
    last = None
    for _ in range(max(1, repeat)):
        with tempfile.NamedTemporaryFile("w", suffix=".ir", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            proc = subprocess.run(
                ["ir-ctl", "-d", device, "--carrier=%d" % int(frequency), "-s", path],
                capture_output=True,
            )
            last = proc
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        time.sleep(0.05)
    if last and last.returncode != 0:
        raise RuntimeError("ir-ctl 失败: %s" % last.stderr.decode("utf-8", "replace")[:300])
    return True


def send(matchid, key, vendor=None, device=DEV_TX, repeat=1):
    freq, timings = timing(matchid, key, vendor)
    transmit(freq, timings, device, repeat)
    return freq, len(timings), sum(timings) / 1000.0


def listen(seconds=5, device=DEV_RX):
    """录一段红外原始波形，返回 ir-ctl 的 mode2 输出行"""
    proc = subprocess.run(
        ["ir-ctl", "-d", device, "--mode2", "-r"],
        capture_output=True,
        timeout=seconds,
    )
    return proc.stdout.decode("utf-8", "replace").splitlines()


# ---------------------------------------------------------------- CLI
def _print_table(rows, headers):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]

    if cmd == "devices":
        rows = [(d["deviceid"], d["name"]) for d in devices()]
        _print_table(rows, ["id", "设备"])
        return 0

    if cmd == "brands":
        devid = int(argv[2])
        kw = argv[3] if len(argv) > 3 else None
        items = brands(devid)
        if kw:
            items = [b for b in items if kw in b["name"]]
        _print_table([(b["brandid"], b["name"]) for b in items], ["brandid", "品牌"])
        print("  共 %d 条" % len(items))
        return 0

    if cmd == "models":
        devid, brandid = int(argv[2]), int(argv[3])
        items = models(devid, brandid)
        _print_table([(m["id"], m["vendor"], m["name"]) for m in items], ["matchid", "vendor", "型号"])
        return 0

    if cmd == "keys":
        mid = argv[2]
        vendor = argv[3] if len(argv) > 3 else None
        r = remote(mid, vendor)
        print("型号 %s  vendor=%s  载波 %d Hz  type=%s" % (
            r["id"], r["vendor"], r["frequency"], r["type"]))
        print("按键(%d)：%s" % (len(r["keys"]), ", ".join(r["keys"])))
        return 0

    if cmd == "code":
        mid, key = argv[2], argv[3]
        vendor = argv[4] if len(argv) > 4 else None
        freq, arr = timing(mid, key, vendor)
        print("载波 %d Hz, %d 段, 总时长 %.1f ms" % (freq, len(arr), sum(arr) / 1000))
        print("前 16:", arr[:16])
        print("末 6 :", arr[-6:])
        return 0

    if cmd == "send":
        mid, key = argv[2], argv[3]
        vendor = argv[4] if len(argv) > 4 else None
        freq, n, ms = send(mid, key, vendor)
        print("✅ 已发射 %s/%s：%d Hz, %d 段, %.1f ms" % (mid, key, freq, n, ms))
        return 0

    if cmd == "raw":
        path = argv[2]
        freq = int(argv[3]) if len(argv) > 3 else 38000
        with open(path, encoding="utf-8") as f:
            arr = [int(x) for x in f.read().split()]
        transmit(freq, arr)
        print("✅ 已发射 raw：%d 段" % len(arr))
        return 0

    if cmd == "listen":
        secs = int(argv[2]) if len(argv) > 2 else 5
        lines = listen(secs)
        print("收到 %d 行" % len(lines))
        for ln in lines[:40]:
            print("  ", ln)
        return 0

    print("未知命令: %s" % cmd)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
