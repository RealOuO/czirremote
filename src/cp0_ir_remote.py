#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CP0 IR Remote —— CardputerZero 原生红外遥控器。

直写 /dev/fb0（320x170 RGB565），直读 /dev/input/event*，不经过浏览器/合成器。
码库来自同目录 miir.py（小米米家码库），发射走 /dev/lirc0。

流程与米家配遥控器一致：设备类型 → 品牌 → 方案组（试发电源码）→ 遥控器。
小米码库里没有型号名称，一个品牌下的候选码表被分成若干方案组，
每组共用一条电源码，试发有反应即选中该组。

键位按 cardputer.cc 官方键盘文档：
  * 屏幕正对数字键 4~8，底部五个快捷键中心 X = 49/105/161/217/273，Y = 153
  * 非输入状态下 F/Z/X/C 直接当方向键用
"""

import ctypes as C
import fcntl
import json
import mmap
import os
import select
import signal
import struct
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import miir
import kkac  # noqa: E402
import cp0_pinyin  # noqa: E402

# ---------------------------------------------------------------- 常量

W, H = 320, 170

BG = "#0b1016"
PANEL = "#121b23"
FG = "#e6eef4"
DIM = "#7d8fa0"
ACCENT = "#ff7a45"
DANGER = "#ff5470"
SEL_TXT = "#0b1016"

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
EVIOCGRAB = 0x40044590
EV_KEY = 1

FONT_REG = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
]
FONT_BOLD = [
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
]

# tca8418 上报标准 PC 键码
KEYS = {
    1: "ESC", 14: "BACKSPACE", 15: "TAB", 28: "ENTER", 57: "SPACE",
    103: "UP", 105: "LEFT", 106: "RIGHT", 108: "DOWN",
    16: "Q", 17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I",
    24: "O", 25: "P", 30: "A", 31: "S", 32: "D", 33: "F", 34: "G", 35: "H",
    36: "J", 37: "K", 38: "L", 44: "Z", 45: "X", 46: "C", 47: "V", 48: "B",
    49: "N", 50: "M", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6",
    8: "7", 9: "8", 10: "9", 11: "0",
}

# 红外功能名 -> 中文标签（码库里的键名不统一，认识多少写多少，其余原样显示）
IR_LABELS = {
    "power": "电源", "power_off": "关机", "vol+": "音量+", "vol-": "音量-",
    "volume_up": "音量+", "volume_down": "音量-", "mute": "静音",
    "up": "上", "down": "下", "left": "左", "right": "右", "ok": "确定",
    "back": "返回", "home": "主页", "menu": "菜单", "exit": "退出",
    "input": "信号源", "source": "信号源", "ch+": "频道+", "ch-": "频道-",
    "channel_up": "频道+", "channel_down": "频道-",
    "play": "播放", "pause": "暂停", "stop": "停止", "prev": "上一曲",
    "next": "下一曲", "forward": "快进", "rewind": "快退",
    "info": "信息", "guide": "指南", "settings": "设置", "setup": "设置",
    "subtitle": "字幕", "audio": "声道", "display": "显示", "text": "图文",
    "red": "红", "green": "绿", "yellow": "黄", "blue": "蓝",
    "fan_speed": "风速", "temp_up": "温度+", "temp_down": "温度-",
    "mode": "模式", "swing": "摆风", "timer": "定时", "sleep": "睡眠",
    "light": "灯光", "eco": "节能", "favorite": "收藏", "epg": "节目单",
    "tv": "电视", "av": "AV", "hdmi": "HDMI", "usb": "USB", "zoom": "缩放",
    "freeze": "冻结", "sound mode": "音效", "shortcuts": "快捷",
    "cloud assistant": "语音", "16:9": "16:9", "4:3": "4:3",
    "channel list": "频道表", "return": "返回", "backspace": "退格",
    "smart": "智能", "apps": "应用", "netflix": "Netflix", "youtube": "YouTube",
}

IR_ORDER = [
    "power", "vol+", "vol-", "mute", "ch+", "ch-",
    "up", "down", "left", "right", "ok", "back", "home", "menu", "exit",
    "input", "source", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
]

BOTTOM_X = [49, 105, 161, 217, 273]
BOTTOM_KEYS = ["4", "5", "6", "7", "8"]
# 底栏标签与动作共用同一份定义，避免「画了按钮却没绑键」
FOOTER_LIST = ["上", "下", "选定", "返回", ""]
FOOTER_REMOTE = ["电源", "音量-", "音量+", "静音", "菜单"]

# 品牌页热门置顶：按品类给一份优先级表，码库里没有的自动跳过，
# 名称按前缀命中，能匹配「创维 C6000 贵阳广电」这类带后缀的条目。
HOT_BRANDS = {
    "空调": ["小米", "格力", "美的", "海尔", "奥克斯", "TCL", "海信", "志高"],
    "电视机顶盒": ["创维", "长虹", "华为", "九联", "中兴", "小米"],
    "电视": ["小米", "海信", "创维", "TCL", "长虹", "康佳", "索尼", "三星"],
    "盒子": ["小米", "华为", "天猫魔盒", "创维", "泰捷"],
    "风扇": ["美的", "格力", "艾美特", "先锋", "奥克斯", "戴森"],
    "功放": ["天龙", "雅马哈", "安桥", "马兰士", "索尼", "先锋"],
    "DVD": ["索尼", "飞利浦", "先锋", "松下", "万利达", "步步高"],
    "投影仪": ["明基", "爱普生", "索尼", "松下", "宏碁", "优派"],
    "灯泡": ["欧普", "松下", "夏普", "东芝", "日立"],
    "空气净化器": ["松下", "布鲁雅尔", "戴森", "大金", "亚都", "海尔"],
    "热水器": ["海尔", "美的", "万和", "阿里斯顿", "史密斯", "万家乐", "樱花"],
    "单反": ["佳能", "尼康", "索尼", "松下", "富士"],
}
SEARCH_MAX = 12
# 同一功能在小米码库里有多种键名，底栏按键按顺序尝试
FOOTER_ALIASES = [
    ["power", "power_off", "poweron", "on"],
    ["vol-", "volume_down", "volume-", "voldown"],
    ["vol+", "volume_up", "volume+", "volup"],
    ["mute", "muting"],
    ["menu", "settings", "setup", "home"],
]

# 状态型空调：每次按键按当前完整状态拼一帧（kkac.AcConfig.encode）
AC_GRID = ["ac_on", "ac_off", "ac_temp_up", "ac_temp_down",
           "ac_mode", "ac_fan", "ac_cool", "ac_heat"]
AC_LABELS = {
    "ac_on": "开机", "ac_off": "关机", "ac_temp_up": "温度+", "ac_temp_down": "温度-",
    "ac_mode": "模式", "ac_fan": "风速", "ac_cool": "制冷", "ac_heat": "制热",
}
AC_FID = {"ac_on": 1, "ac_off": 1, "ac_temp_up": 3, "ac_temp_down": 4,
          "ac_mode": 2, "ac_fan": 5, "ac_cool": 2, "ac_heat": 2}
AC_FOOTER = ["开机", "关机", "温度-", "温度+", "模式"]
AC_FOOTER_ACTIONS = ["ac_on", "ac_off", "ac_temp_down", "ac_temp_up", "ac_mode"]


def is_state_table(data):
    """按码表真实内容判定状态型，不看设备类型/品牌/文件名。

    状态型码表的 key 是 1002 状态模板 + 1518/1522 Lua 校验脚本，
    而不是 power / vol+ 这样的按键名；API 的 type=2 可作辅助判据。
    """
    keys = (data or {}).get("keys") or {}
    has_template = bool(keys.get("1002") or keys.get(1002))
    has_lua = bool(keys.get("1522") or keys.get(1522)
                   or keys.get("1518") or keys.get(1518))
    return str((data or {}).get("type") or "") == "2" or (has_template and has_lua)


GRID_COLS, GRID_ROWS = 4, 3
GRID_PER_PAGE = GRID_COLS * GRID_ROWS

# 网格每格一个字母：按角标字母直接发射该格功能，不用移动光标。
# 取键盘右侧 4×3 一块，按行对应网格三行；与 WASD/FZXC 导航、4~8 底栏、Enter/ESC 均不冲突。
GRID_LETTERS = ["Y", "U", "I", "O",
                "H", "J", "K", "L",
                "V", "B", "N", "M"]
LETTER_SLOT = {n: i for i, n in enumerate(GRID_LETTERS)}
assert len(GRID_LETTERS) == GRID_PER_PAGE and len(LETTER_SLOT) == GRID_PER_PAGE


# ---------------------------------------------------------------- 字体

_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    chosen = None
    for path, index in (FONT_BOLD if bold else FONT_REG):
        try:
            face = ImageFont.truetype(path, size, index=index)
            if face.getbbox("小")[2] > 0:
                chosen = face
                break
        except Exception:
            continue
    _font_cache[key] = chosen or ImageFont.load_default()
    return _font_cache[key]


def clip(face, text, maxw):
    text = str(text)
    if face.getlength(text) <= maxw:
        return text
    while text and face.getlength(text + "…") > maxw:
        text = text[:-1]
    return text + "…"


# ---------------------------------------------------------------- 帧缓冲

class Framebuffer:
    """映射 /dev/fb0，按 fb 自带 bitfield 打包像素（不硬编码 RGB565）。"""

    def __init__(self, device=None):
        device = device or os.environ.get("APPLAUNCH_LINUX_FBDEV_DEVICE", "/dev/fb0")

        class Fix(C.Structure):
            _fields_ = [
                ("id", C.c_char * 16), ("smem_start", C.c_ulong),
                ("smem_len", C.c_uint32), ("type", C.c_uint32),
                ("type_aux", C.c_uint32), ("visual", C.c_uint32),
                ("xpanstep", C.c_uint16), ("ypanstep", C.c_uint16),
                ("ywrapstep", C.c_uint16), ("line_length", C.c_uint32),
                ("mmio_start", C.c_ulong), ("mmio_len", C.c_uint32),
                ("accel", C.c_uint32), ("capabilities", C.c_uint16),
                ("reserved", C.c_uint16 * 2),
            ]

        self.fd = os.open(device, os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var)
        w, h, _, _, self.xoff, self.yoff, self.bpp, _ = struct.unpack_from("8I", var)
        self.channels = [struct.unpack_from("3I", var, 32 + i * 12) for i in range(4)]
        fixbuf = bytearray(C.sizeof(Fix))
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fixbuf)
        fix = Fix.from_buffer_copy(fixbuf)
        self.width, self.height, self.stride = w, h, fix.line_length
        self.mem = mmap.mmap(self.fd, fix.smem_len,
                             mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def _pack(self, image):
        rgb = np.asarray(image, dtype=np.uint32)
        out = np.zeros((H, W), dtype=np.uint32)
        for index, (offset, length, _msb) in enumerate(self.channels):
            if length:
                values = rgb[:, :, index] if index < 3 else 255
                out |= ((values * ((1 << length) - 1) + 127) // 255) << offset
        n = self.bpp // 8
        return out.astype("<u2" if n == 2 else "<u4").tobytes(), n

    def show(self, image):
        raw, n = self._pack(image)
        for y in range(min(H, self.height)):
            start = (y + self.yoff) * self.stride + self.xoff * n
            self.mem[start:start + W * n] = raw[y * W * n:(y + 1) * W * n]

    def snapshot(self):
        return bytes(self.mem[:(H + self.yoff) * self.stride])

    def restore(self, blob):
        self.mem[:len(blob)] = blob

    def close(self):
        try:
            self.mem.close()
        finally:
            os.close(self.fd)


# ---------------------------------------------------------------- 键盘

def detect_keyboard():
    """定位真正那颗 tca8418（i2c 上的），忽略 cardputer-remote 注入的虚拟节点。"""
    try:
        with open("/proc/bus/input/devices", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    fallback = None
    for block in text.split("\n\n"):
        if "tca8418" not in block:
            continue
        node = None
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                for tok in line.split("=", 1)[1].split():
                    if tok.startswith("event"):
                        node = "/dev/input/" + tok
        if not node:
            continue
        if "i2c" in block or "/devices/platform" in block:
            return node
        fallback = fallback or node
    return fallback


def parse_events(data):
    """把 input_event 字节流解成 [(keycode, value)]，只保留按键。"""
    events = []
    for off in range(0, len(data) - 23, 24):
        _s, _us, etype, code, value = struct.unpack("<qqHHi", data[off:off + 24])
        if etype == EV_KEY and value in (1, 2):
            events.append((code, value))
    return events


class Keyboard:
    """机身按键 + 远程注入键盘。

    机身按键只从 tca8418 物理节点（event4）进来。APPLaunch 传的
    APPLAUNCH_LINUX_KEYBOARD_DEVICE 是 by-path 软链，指向 cardputer-remote
    创建的 uinput 虚拟节点（event6）；桌面态这个节点由启动器/合成器体系
    占用，机身按键不直接落在上面。以前把它当主输入源，从 APPLaunch 启动
    时就会出现「界面正常、所有按键无响应」，而独立进程测试却能正常收键。

    物理节点是唯一的机身按键来源。grab 只是为了「不让桌面层吃掉按键」：
    grab 成功时 cardputer-remote 的转发线程读不到事件，虚拟节点上只剩网页
    远程注入的按键；grab 失败（被别的进程独占）时物理 fd 静默，靠虚拟节点
    兜底，不会整机无响应。两个源都可能来同一按键时按 (code,value) 去重。
    """

    def __init__(self, device=None):
        env_virt = os.environ.get("APPLAUNCH_LINUX_KEYBOARD_DEVICE")
        real = detect_keyboard()
        primary = real or device or env_virt or "/dev/input/event4"
        aux = []
        for cand in (device, env_virt):
            if not cand:
                continue
            try:
                if os.path.realpath(cand) == os.path.realpath(primary):
                    continue
            except OSError:
                continue
            if cand not in aux:
                aux.append(cand)

        self.path = primary
        self.fds = []
        self.sources = {}
        self._grabbed = []
        self._last = {}
        self.grabbed = False

        self.fd = os.open(primary, os.O_RDONLY | os.O_NONBLOCK)
        self.fds.append(self.fd)
        self.sources[self.fd] = os.path.realpath(primary)
        self.grabbed = self._grab(self.fd)
        # 补充源恒开，不再以「物理节点是否抢到独占」为条件：
        # grab 失败通常意味着别的进程把物理节点独占了，那时物理 fd 一个事件也读不到，
        # 唯一还能收到按键的就是 cardputer-remote 转发出来的虚拟节点。
        for path in aux:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            self.fds.append(fd)
            self.sources[fd] = os.path.realpath(path)

    def _grab(self, fd):
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError:
            return False
        self._grabbed.append(fd)
        return True

    def poll(self, timeout=0.05):
        try:
            ready, _, _ = select.select(self.fds, [], [], timeout)
        except InterruptedError:
            return []
        events = []
        now = time.time()
        for fd in ready:
            try:
                batch = parse_events(os.read(fd, 24 * 64))
            except OSError:
                continue
            for code, value in batch:
                if self._duplicate(fd, code, value, now):
                    continue
                events.append((code, value))
        return events

    def _duplicate(self, fd, code, value, now):
        """同一个按键从两个源各来一次时只留第一份。

        物理节点和虚拟节点同时能收到，只发生在物理节点没被我们独占、
        而 cardputer-remote 仍在转发的情况下；两份间隔在毫秒级。
        """
        key = (code, value)
        last = self._last.get(key)
        self._last[key] = (now, fd)
        return bool(last and last[1] != fd and now - last[0] < 0.04)

    def close(self):
        for fd in self._grabbed:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            except OSError:
                pass
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        self.sources = {}
        self._grabbed = []
        self._last = {}
        self.grabbed = False


# ---------------------------------------------------------------- 后台任务

class Job:
    """把阻塞的网络/发射放到线程里，主循环轮询结果，避免界面卡死。"""

    def __init__(self):
        self.kind = None
        self.label = ""
        self.thread = None
        self.result = None
        self.error = None
        self.done = False
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.thread is not None and not self.done

    def start(self, kind, label, fn):
        self.kind, self.label = kind, label
        self.result = None
        self.error = None
        self.done = False

        def runner():
            try:
                value = fn()
                with self.lock:
                    self.result = value
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.error = str(exc)[:70] or type(exc).__name__
            finally:
                self.done = True

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()

    def wait(self, timeout=60):
        if self.thread is not None:
            self.thread.join(timeout)
        with self.lock:
            return self.kind, self.result, self.error

    def take(self):
        """取走一次性结果并复位。

        只清 done 不清 thread，会让 poll_job() 每一帧都当成「刚完成」重放一遍：
        遥控页 grid_sel 被反复重置成 0（光标按了不动）、空调状态被重置、
        toast 永远不消失、state.json 每秒写 20 次。
        """
        if not self.done:
            return None
        if self.thread is not None:
            self.thread.join(0)
        with self.lock:
            taken = (self.kind, self.result, self.error)
            self.result = None
            self.error = None
        self.kind = None
        self.label = ""
        self.thread = None
        self.done = False
        return taken


# ---------------------------------------------------------------- 状态

def state_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "cp0-ir-remote", "state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("recent", [])
            return data
    except Exception:
        pass
    return {"recent": []}


def save_state(state):
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


# ---------------------------------------------------------------- 应用

class App:
    def __init__(self, fb=None, kb=None):
        self.fb = fb
        self.kb = kb
        self.state = load_state()
        self.job = Job()
        self.running = True
        self.dirty = True
        self.toast = ("", 0.0, FG)
        self.screen = "types"
        self.sel = 0
        self.view = 0
        self.items = []
        self.devices = []
        self.brands = []
        self.groups = []
        self.query = ""            # 品牌搜索词（小写字母/数字）
        self.searching = False     # 品牌页是否处于搜索输入状态
        self.device = None
        self.brand = None
        self.group = None
        self.keyset_pos = 0
        self.model = None
        self.ir_keys = []
        self.ac = None             # kkac.AcConfig，状态型空调专用
        self.ac_state = None
        self.freq = 38000
        self.grid_sel = 0
        self.send_lock = threading.Lock()
        self.sending = 0
        self.hold_key = None
        self.hold_n = 0
        self.error = None
        self.last_frame = None
        self._build_items()
        self.job.start("devices", "读取设备类型", miir.devices)

    # ---------------------------------------------------------- 列表数据

    def _build_items(self):
        items = []
        if self.state.get("recent"):
            items.append({"label": "最近使用", "sub": "%d" % len(self.state["recent"]),
                          "kind": "recent"})
        for dev in self.devices:
            items.append({"label": dev["name"], "sub": "", "kind": "device", "data": dev})
        self.items = items

    def _brand_items(self):
        """品牌页条目：热门置顶 + 搜索过滤。

        检索键在载入时算一次（品牌名小写 + 拼音首字母变体），
        排序按命中强度：名字前缀 > 名字包含 > 常用读音首字母 > 生僻读音首字母，
        同强度保持码库原顺序，所以「md」第一个永远是美的。
        """
        hot = self._hot_names()
        query = (self.query or "").strip().lower()
        scored = []
        for pos, brand in enumerate(self.brands):
            self._brand_keys(brand)
            score = self._brand_score(brand, query)
            if score >= 0:
                scored.append((score, pos, brand))
        pinned, used = [], set()
        for keyword in hot:
            for score, pos, brand in scored:
                name = str(brand["name"])
                if id(brand) in used or not (name == keyword or name.startswith(keyword)):
                    continue
                pinned.append({"label": name, "sub": "", "kind": "brand",
                               "hot": True, "data": brand})
                used.add(id(brand))
                break
        rest = [{"label": str(b["name"]), "sub": "", "kind": "brand", "data": b}
                for score, pos, b in sorted(scored, key=lambda t: (t[0], t[1]))
                if id(b) not in used]
        return pinned + rest

    def _hot_names(self):
        """当前设备类型的热门品牌顺序（按类型名精确匹配）。"""
        name = (self.device or {}).get("name") or ""
        return HOT_BRANDS.get(name, [])

    @staticmethod
    def _brand_keys(brand):
        """给品牌字典挂上检索键（只算一次）。"""
        if "_py" not in brand:
            brand["_ascii"] = str(brand.get("name") or "").lower()
            brand["_py"] = cp0_pinyin.variants(str(brand.get("name") or ""))
        return brand

    @staticmethod
    def _brand_score(brand, query):
        """命中强度，-1 = 不匹配。"""
        if not query:
            return 0
        name = brand.get("_ascii") or str(brand.get("name") or "").lower()
        if name.startswith(query):
            return 0
        if query in name:
            return 1
        pys = brand.get("_py") or []
        if pys and pys[0].startswith(query):
            return 2
        if any(v.startswith(query) for v in pys[1:]):
            return 3
        return -1

    def _group_items(self):
        out = []
        for i, g in enumerate(self.groups):
            if g.get("kind") == "state":
                label, sub = "方案 %d" % (i + 1), "状态型"
            else:
                label, sub = "方案 %d" % (i + 1), "%d 码" % len(g["keysetids"])
            out.append({"label": label, "sub": sub, "kind": "group", "data": g})
        return out

    def toast_show(self, text, color=FG, secs=2.0):
        self.toast = (text, time.time() + secs, color)
        self.dirty = True

    def remember(self, entry):
        recent = [r for r in self.state.get("recent", []) if r.get("mid") != entry["mid"]]
        recent.insert(0, entry)
        self.state["recent"] = recent[:8]
        save_state(self.state)

    # ---------------------------------------------------------- 发射

    def _ac_press(self, action):
        """状态型空调：先改完整状态，再按状态拼一帧发射。"""
        if not self.ac:
            return
        if not self.ac.supports(action):
            # 码表里这个字段只有 1 组规则 = 不可调，别再发一帧和上一键一样的内容
            self.toast_show("该码表不支持「%s」" % AC_LABELS.get(action, action), DIM, 1.4)
            return
        st = dict(self.ac_state or self.ac.initial_state())
        mode = self.ac.mode(st.get("mode", 0))
        temps = mode["temperatures"] or list(range(16, 31))
        speeds = mode["fanSpeeds"] or [0, 1, 2, 3]
        # 只允许跳到码表里真的能拼出的档位，避免「换了但发出去的帧没变」
        usable_temp = set(self.ac.usable_indexes("temp"))
        usable_fan = set(self.ac.usable_indexes("fan"))
        usable_mode = set(self.ac.usable_indexes("mode"))
        temps = [t for index, t in enumerate(temps) if index in usable_temp]
        speeds = [s for s in speeds if s in usable_fan]
        modes = [m for m in self.ac.modes if m["value"] in usable_mode]
        if not temps:
            temps = [st.get("temperature", 26)]
        if not speeds:
            speeds = [st.get("fanSpeed", 0)]
        if not modes:
            modes = self.ac.modes[:1]
        if action == "ac_off":
            st["power"] = 1
        else:
            # 调温/换模式/换风速都先开机，与实体遥控一致
            st["power"] = 0
        if action == "ac_temp_up":
            st["temperature"] = min(temps[-1], st.get("temperature", 26) + 1)
        elif action == "ac_temp_down":
            st["temperature"] = max(temps[0], st.get("temperature", 26) - 1)
        elif action == "ac_mode":
            idx = next((i for i, m in enumerate(modes)
                        if m["value"] == st.get("mode")), 0)
            nxt = modes[(idx + 1) % len(modes)]
            st["mode"] = nxt["value"]
            if nxt["temperatures"]:
                # 换模式时保持温度，只有当前温度不被新模式支持才回落
                keep = st.get("temperature")
                st["temperature"] = keep if keep in nxt["temperatures"] else nxt["temperatures"][0]
            if nxt["fanSpeeds"] and st.get("fanSpeed") not in nxt["fanSpeeds"]:
                st["fanSpeed"] = nxt["fanSpeeds"][0]
        elif action == "ac_fan":
            cur = st.get("fanSpeed")
            idx = speeds.index(cur) if cur in speeds else 0
            st["fanSpeed"] = speeds[(idx + 1) % len(speeds)]
        elif action in ("ac_cool", "ac_heat"):
            st["mode"] = 0 if action == "ac_cool" else 1
        self.ac_state = self.ac.normalize(st, AC_FID.get(action, 1))
        self.dirty = True
        self.send_ir(action)

    def _ac_send(self, action):
        fid = AC_FID.get(action, 1)
        state = dict(self.ac_state)
        freq, cfg = self.freq, self.ac
        label = AC_LABELS.get(action, action)
        self.sending += 1
        self.dirty = True

        def runner():
            try:
                timings = cfg.encode(state, fid)
                with self.send_lock:
                    miir.transmit(freq, timings)
                self.toast = ("已发送 %s · %d 段" % (label, len(timings)),
                              time.time() + 1.6, ACCENT)
            except Exception as exc:  # noqa: BLE001
                self.toast = ("编码/发射失败: %s" % str(exc)[:42],
                              time.time() + 3.0, DANGER)
            finally:
                self.sending -= 1
                self.dirty = True

        threading.Thread(target=runner, daemon=True).start()

    def send_ir(self, name):
        if self.ac:
            self._ac_send(name)
            return
        if not self.model:
            return
        if self.job.running:
            self.toast_show("正在加载，稍候…", DIM, 1.2)
            return
        mid, vendor = self.model["id"], self.model.get("vendor")
        label = IR_LABELS.get(name, name)
        self.sending += 1
        self.dirty = True

        def runner():
            try:
                with self.send_lock:
                    freq, timings = miir.timing(mid, name, vendor)
                    miir.transmit(freq, timings)
                self.toast = ("已发送 %s · %d 段" % (label, len(timings)),
                              time.time() + 1.6, ACCENT)
            except Exception as exc:  # noqa: BLE001
                self.toast = ("发送失败: %s" % str(exc)[:42], time.time() + 3.0, DANGER)
            finally:
                self.sending -= 1
                self.dirty = True

        threading.Thread(target=runner, daemon=True).start()

    # ---------------------------------------------------------- 导航

    def go(self, screen):
        self.screen = screen
        self.sel = 0
        self.view = 0
        self.hold_key, self.hold_n = None, 0
        self.dirty = True

    def open_recent(self):
        rows = []
        for r in self.state.get("recent", []):
            rows.append({"label": r.get("label") or r.get("mid"),
                         "sub": r.get("brand", ""), "kind": "recent_model",
                         "data": {"id": r["mid"], "name": r.get("label") or r["mid"],
                                  "vendor": r.get("vendor"), "kind": r.get("kind")}})
        if not rows:
            self.toast_show("还没有使用记录", DIM)
            return
        self.items = rows
        self.go("recent")

    def open_brands(self, device):
        self.device = device
        self.brands = []
        self.items = []
        self.query, self.searching = "", False
        self.go("brands")
        self.job.start("brands", "读取品牌", lambda: miir.brands(device["deviceid"]))

    def open_groups(self, brand):
        self.brand = brand
        self.groups = []
        self.items = []
        self.go("groups")
        self.job.start("tree", "读取方案",
                       lambda: miir.brand_tree(self.device["deviceid"], brand["brandid"]))

    def open_pair(self, group, test=True):
        """选中方案：先试发该组电源码，再载入组内第一个码表。"""
        if group.get("kind") == "state":
            # 状态型空调：按当前状态拼整帧，走 kkac 编码器
            self.ac = None
            self.group = group
            self.keyset_pos = 0
            self.items = []
            self.go("remote")
            kid = group["keysetids"][0]
            vendor = group.get("vendor")
            self.job.start("ac", "读取空调码表", lambda: self._load_ac(kid, vendor))
            return
        self.ac = None
        self.group = group
        self.keyset_pos = 0
        self.items = []
        self.go("remote")
        label = "试发电源码" if test else "载入码表"
        self.job.start("pair", label, lambda: self._pair(group, 0, test=test))

    def swap_keyset(self):
        if not self.group:
            self.toast_show("该记录没有方案信息", DIM, 1.4)
            return
        kids = self.group["keysetids"]
        if len(kids) < 2:
            self.toast_show("本方案只有 1 个码表", DIM, 1.4)
            return
        pos = (self.keyset_pos + 1) % len(kids)
        self.keyset_pos = pos
        self.job.start("swap", "换下一个码表", lambda: self._pair(self.group, pos, test=False))

    def _load_ac(self, kid, vendor):
        """状态型码表：确认 1002 模板存在，建立可编码的配置对象。"""
        data = miir.remote(kid, vendor) if vendor else self._fetch_remote(kid)[0]
        if not is_state_table(data):
            # 预标记与实际不符（例如空调品牌里混着的 type=1 普通码表）：
            # 按普通码表走，不要硬塞进拼帧器。
            return self._pulse_result(data, kid, data.get("vendor") or vendor)
        return {"ac": True,
                "cfg": kkac.AcConfig(data["keys"], kid),
                "freq": data.get("frequency") or 38000,
                "vendor": data.get("vendor") or vendor,
                "kid": kid}

    @staticmethod
    def _pulse_result(data, kid, vendor, group=None, pos=0, total=1,
                      tested=False, error=None):
        names = list((data.get("keys") or {}).keys())
        if not names:
            raise RuntimeError("该码表没有按键")
        return {"ac": False, "names": names,
                "freq": data.get("frequency") or (group or {}).get("frequency") or 38000,
                "vendor": vendor, "kid": kid, "pos": pos, "total": total,
                "tested": tested, "error": error}

    @staticmethod
    def _fetch_remote(kid):
        last = None
        vendors = [miir._guess_vendor(kid), "mi", "kk", "mx", "yk"]
        seen = []
        for vendor in vendors:
            if vendor in seen:
                continue
            seen.append(vendor)
            try:
                data = miir.remote(kid, vendor)
                if data.get("keys"):
                    return data, vendor
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise RuntimeError("取不到码表 %s: %s" % (kid, last))

    def _pair(self, group, pos, test=True):
        kids = group["keysetids"]
        kid = kids[min(pos, len(kids) - 1)]
        tested = False
        error = None
        if test and group.get("power"):
            try:
                timings = miir.decrypt_pattern(group["power"])
                miir.transmit(group["frequency"], timings)
                tested = True
            except Exception as exc:  # noqa: BLE001
                error = "试发失败: %s" % str(exc)[:40]
        data, vendor = self._fetch_remote(kid)
        if is_state_table(data):
            # 分组树里也可能混着状态型码表，按码表内容而非预标记分流
            return {"ac": True, "cfg": kkac.AcConfig(data["keys"], kid),
                    "freq": data.get("frequency") or group["frequency"],
                    "vendor": vendor, "kid": kid}
        return self._pulse_result(data, kid, vendor, group, pos, len(kids),
                                  tested, error)

    # ---------------------------------------------------------- 按键

    def on_key(self, code, value):
        name = KEYS.get(code)
        if name is None:
            return
        # 屏幕下方的 4~8 五颗键固定对应底栏 slot；
        # 必须先拦截，否则遥控屏会被码表里的数字键名抢走。
        if name in BOTTOM_KEYS:
            self._footer_key(name, value)
            return
        # 品牌搜索状态：字母/数字全归输入，导航交给底栏 4/5（F/X 这时是字母）
        if self.screen == "brands" and self.searching:
            self._key_search(name, value)
            return
        # 遥控页角标字母：按下直接发射该格功能；长按重复事件忽略，避免连发。
        if self.screen == "remote" and name in LETTER_SLOT:
            if value != 2:
                self._letter_press(name)
            return
        if self.screen == "remote":
            self._key_remote(name)
        else:
            self._key_list(name)

    def _footer_key(self, name, value):
        """底栏 4~8：列表屏=上/下/选定/返回，遥控屏=5 个快捷发射。"""
        slot = BOTTOM_KEYS.index(name)
        repeat = value == 2  # 内核长按重复，只让上/下连动
        if self.screen == "remote":
            if not repeat:
                self._footer_action(slot)
            return
        if slot in (0, 1):
            step = self._hold_step(name) * (-1 if slot == 0 else 1)
            last = max(0, len(self.items) - 1)
            self.sel = max(0, min(last, self.sel + step))
            self._clamp_view()
            self.dirty = True
        elif slot == 2:
            if not repeat:
                self._activate()
        elif slot == 3:
            if not repeat:
                self._back()
        elif slot == 4:
            # 键 8：品牌页=进入/清空搜索，其他列表屏保留不用
            if not repeat and self.screen == "brands":
                if self.searching:
                    self.query = ""
                    self._refresh_brands()
                    self.toast_show("已清空关键词", DIM, 1.2)
                else:
                    self._search_start()

    # ---------------------------------------------------------- 品牌搜索

    def _search_start(self):
        if not self.brands:
            self.toast_show("品牌还没读完", DIM, 1.2)
            return
        self.searching = True
        self.query = ""
        self._refresh_brands()

    def _search_stop(self):
        self.searching = False
        self.query = ""
        self._refresh_brands()

    def _refresh_brands(self):
        self.items = self._brand_items()
        self.sel = 0
        self.view = 0
        self.dirty = True

    def _key_search(self, name, value):
        """搜索输入：字母/数字入串，退格删字，ESC/TAB 退出，上下/选定仍走底栏。"""
        if value == 2:
            return                       # 长按重复只用于上下，不重复输入
        if name == "ESC":
            self._search_stop()
        elif name == "TAB":
            self._search_stop()
        elif name == "ENTER":
            self._activate()
        elif name == "BACKSPACE":
            if self.query:
                self.query = self.query[:-1]
                self._refresh_brands()
        elif len(name) == 1 and len(self.query) < SEARCH_MAX:
            self.query += name.lower()
            self._refresh_brands()

    def _hold_step(self, name):
        if name == self.hold_key:
            self.hold_n += 1
        else:
            self.hold_key, self.hold_n = name, 0
        if self.hold_n < 8:
            return 1
        if self.hold_n < 22:
            return 3
        return 8

    def _key_list(self, name):
        # 这块键盘没有方向键键码（设备树 keymap 46 项里没有 103/105/106/108），
        # 方向操作就是 F/X 上下、Z/C 翻页；W/A/S/D 与方向键只是兼容别名，不对外宣传。
        moves = {"UP": -1, "F": -1, "W": -1, "DOWN": 1, "X": 1, "S": 1}
        pages = {"Z": -6, "LEFT": -6, "A": -6, "C": 6, "RIGHT": 6, "D": 6}
        if name in moves:
            step = self._hold_step(name) * moves[name]
            self.sel = max(0, min(len(self.items) - 1, self.sel + step))
            self._clamp_view()
            self.dirty = True
        elif name in pages:
            self.sel = max(0, min(len(self.items) - 1, self.sel + pages[name]))
            self._clamp_view()
            self.dirty = True
        elif name == "ENTER":
            self._activate()
        elif name == "ESC":
            self._back()
        elif name == "TAB" and self.screen == "brands":
            self._search_start()
        else:
            self.hold_key, self.hold_n = None, 0

    def _key_remote(self, name):
        # 4 列网格：F/X 上下跨一行，Z/C 左右移一格（本机方向操作=FXZC）。
        # W/A/S/D 是同义别名；UP/DOWN/LEFT/RIGHT 只作兼容，设备上收不到。
        # 底栏 4~8 归快捷发射，角标字母归直发，都不参与导航。
        moves = {"UP": -GRID_COLS, "F": -GRID_COLS, "W": -GRID_COLS,
                 "DOWN": GRID_COLS, "X": GRID_COLS, "S": GRID_COLS,
                 "LEFT": -1, "Z": -1, "A": -1,
                 "RIGHT": 1, "C": 1, "D": 1}
        if name in moves:
            self._grid_move(moves[name])
        elif name == "ENTER":
            self._press_cell(self.grid_sel)
        elif name == "ESC":
            self._back()
        elif name == "TAB":
            self.swap_keyset()
        elif name == "SPACE":
            if "ok" in self.ir_keys:
                self.send_ir("ok")
        elif name == "BACKSPACE":
            if "back" in self.ir_keys:
                self.send_ir("back")
        elif len(name) == 1 and name.isdigit():
            if self.ac:
                return                 # 状态型遥控没有数字键
            # 4~8 已在 on_key 归底栏，这里只剩 1/2/3/9/0 直接发数字码
            if name in self.ir_keys:
                self.send_ir(name)
            else:
                self.toast_show("该码表没有 %s 键" % name, DIM, 1.2)

    def _footer_action(self, slot):
        if self.ac:
            self._ac_press(AC_FOOTER_ACTIONS[slot])
            return
        present = set(self.ir_keys)
        target = next((n for n in FOOTER_ALIASES[slot] if n in present), None)
        if target:
            self.send_ir(target)
        else:
            self.toast_show("该码表没有「%s」" % FOOTER_REMOTE[slot], DIM, 1.3)

    def _press_cell(self, idx):
        """发射某一格：光标 Enter 与角标字母直发共用同一条路径。"""
        if idx < 0 or idx >= len(self.ir_keys):
            return
        name = self.ir_keys[idx]
        if self.ac:
            self._ac_press(name)          # 状态型：先改完整状态再拼帧
        else:
            self.send_ir(name)

    def _letter_press(self, name):
        """按角标字母直发本页对应格；光标同步过去，WASD 导航仍然可用。"""
        page = self.grid_sel // GRID_PER_PAGE
        idx = page * GRID_PER_PAGE + LETTER_SLOT[name]
        if idx >= len(self.ir_keys):
            return                       # 本页没这一格（短码表），不动作也不动光标
        self.grid_sel = idx
        self.dirty = True
        self._press_cell(idx)

    def _grid_move(self, delta):
        total = len(self.ir_keys)
        if total <= 0:
            return
        page = self.grid_sel // GRID_PER_PAGE
        target = self.grid_sel + delta
        if delta in (1, -1):
            if target < 0:
                target = max(0, page * GRID_PER_PAGE - 1)
            elif target >= total:
                target = min(total - 1, (page + 1) * GRID_PER_PAGE)
        self.grid_sel = max(0, min(total - 1, target))
        self.dirty = True

    def _clamp_view(self):
        rows = 6
        if self.sel < self.view:
            self.view = self.sel
        elif self.sel >= self.view + rows:
            self.view = self.sel - rows + 1
        self.view = max(0, min(self.view, max(0, len(self.items) - rows)))

    def _back(self):
        if self.screen == "brands" and self.searching:
            # 搜索态下「返回」先退搜索，再按一次才离开品牌页
            self._search_stop()
        elif self.screen == "remote":
            if self.group:
                self.items = self._group_items()
                self.go("groups")
            else:
                self._build_items()
                self.go("types")
        elif self.screen == "groups":
            self.items = self._brand_items()
            self.go("brands")
        elif self.screen == "brands":
            self._build_items()
            self.go("types")
        elif self.screen == "recent":
            self._build_items()
            self.go("types")
        else:
            self.running = False

    def _activate(self):
        if not self.items:
            return
        item = self.items[self.sel]
        kind = item.get("kind")
        if kind == "recent":
            self.open_recent()
        elif kind == "device":
            self.open_brands(item["data"])
        elif kind == "brand":
            self.open_groups(item["data"])
        elif kind == "group":
            self.open_pair(item["data"])
        elif kind == "recent_model":
            self.group = None
            self.model = dict(item["data"])
            self.items = []
            self.go("remote")
            kid, vendor = self.model["id"], self.model.get("vendor")
            self.ac = None
            if item["data"].get("kind") == "state":
                self.job.start("ac", "读取空调码表",
                               lambda: self._load_ac(kid, vendor))
            else:
                self.job.start("swap", "载入码表",
                               lambda: self._direct(kid, vendor))

    def _direct(self, kid, vendor):
        data, used = (None, None)
        if vendor:
            try:
                data = miir.remote(kid, vendor)
                used = vendor
            except Exception:  # noqa: BLE001
                data = None
        if not data or not data.get("keys"):
            data, used = self._fetch_remote(kid)
        if is_state_table(data):
            return {"ac": True, "cfg": kkac.AcConfig(data["keys"], kid),
                    "freq": data.get("frequency") or 38000, "vendor": used, "kid": kid}
        return self._pulse_result(data, kid, used)

    # ---------------------------------------------------------- 任务结果

    def poll_job(self):
        taken = self.job.take()
        if taken is None:
            return
        kind, result, error = taken
        self.dirty = True
        if error:
            self.error = error
            self.toast_show(error[:40], DANGER, 4.0)
            return
        self.error = None
        if kind == "devices" and isinstance(result, list):
            self.devices = result
            if self.screen == "types":
                self._build_items()
        elif kind == "brands" and isinstance(result, list):
            self.brands = result
            self.items = self._brand_items()
        elif kind == "tree" and isinstance(result, dict):
            self.groups = result.get("groups") or []
            self.items = self._group_items()
        elif kind == "ac" and isinstance(result, dict):
            if result.get("ac") is False:
                self._apply_table(result)
            else:
                self._install_ac(result)
        elif kind in ("pair", "swap") and isinstance(result, dict):
            if result.get("ac"):
                self._install_ac(result)
                return
            self._apply_table(result)
            if kind == "pair":
                if result.get("error"):
                    self.toast_show(result["error"], DANGER, 3.0)
                elif result.get("tested"):
                    self.toast_show("已试发电源码 · 没反应就 ESC 换下一方案", ACCENT, 2.6)

    def _apply_table(self, result):
        self.ac = None
        names = result["names"]
        order = {n: i for i, n in enumerate(IR_ORDER)}
        self.ir_keys = sorted(names, key=lambda n: (order.get(n, 99), n))
        self.freq = result["freq"]
        self.grid_sel = 0
        self.keyset_pos = result.get("pos", 0)
        kid = result.get("kid")
        brand_name = self.brand["name"] if self.brand else ""
        label = "%s 方案 %d" % (brand_name, self._group_number()) if self.group else kid
        self.model = {"id": kid, "name": label, "vendor": result.get("vendor")}
        self.remember({"mid": kid, "label": label, "vendor": result.get("vendor"),
                       "brand": brand_name})

    def _install_ac(self, result):
        """把状态型码表装进遥控页（网格是功能按钮，不是码库数字键）。"""
        self.ac = result["cfg"]
        self.ac_state = self.ac.initial_state()
        self.freq = result["freq"]
        self.grid_sel = 0
        self.ir_keys = list(AC_GRID)
        brand_name = self.brand["name"] if self.brand else ""
        label = "%s 状态型" % brand_name if brand_name else result["kid"]
        self.model = {"id": result["kid"], "name": label,
                      "vendor": result.get("vendor")}
        self.remember({"mid": result["kid"], "label": label,
                       "vendor": result.get("vendor"), "brand": brand_name,
                       "kind": "state"})

    def _group_number(self):
        if not self.group:
            return 0
        try:
            return self.groups.index(self.group) + 1
        except ValueError:
            return 0

    # ---------------------------------------------------------- 绘制

    def draw(self):
        image = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(image)
        if self.sel >= len(self.items):
            self.sel = max(0, len(self.items) - 1)
        if self.screen == "remote":
            self._draw_remote(d)
        else:
            self._draw_list(d)
        self.last_frame = image
        if self.fb is not None:
            self.fb.show(image)

    def _header(self, d, title, right="", title_color=FG):
        d.rectangle((0, 0, W, 19), fill=PANEL)
        d.text((7, 10), clip(font(13, True), title, 208), font=font(13, True),
               fill=title_color, anchor="lm")
        if right:
            d.text((W - 7, 10), clip(font(10), right, 92), font=font(10),
                   fill=DIM, anchor="rm")
        d.line((0, 19, W, 19), fill="#1d2a35")

    def _footer(self, d, labels):
        d.rectangle((0, 136, W, H), fill=PANEL)
        d.line((0, 136, W, 136), fill="#1d2a35")
        for slot, text in enumerate(labels[:5]):
            if not text:
                continue
            cx = BOTTOM_X[slot]
            d.rounded_rectangle((cx - 23, 142, cx + 23, 164), radius=5,
                                fill="#1b2833", outline="#2b3d4b")
            d.text((cx + 2, 153), clip(font(11), text, 38), font=font(11),
                   fill=FG, anchor="mm")
            d.text((cx - 20, 145), BOTTOM_KEYS[slot], font=font(8), fill=DIM, anchor="la")
            d.line((cx, 165, cx, H - 1), fill=ACCENT)

    def _toast_active(self):
        if not self.toast:          # None / False 都表示「没有提示」
            return False
        text, until, _color = self.toast
        return bool(text) and time.time() <= until

    def _toast_bar(self, d):
        if not self.toast:
            return
        text, until, color = self.toast
        if not text or time.time() > until:
            return
        # 条带 126~136：上沿让出行尾(125)，下沿接底栏(136)，9px 字正好不出边
        d.rectangle((0, 126, W, 136), fill=color)
        d.text((W // 2, 131), clip(font(9, True), text, 302), font=font(9, True),
               fill=SEL_TXT, anchor="mm")

    def _center_text(self, d, text, color=DIM, y=80):
        d.text((W // 2, y), clip(font(12), text, 286), font=font(12), fill=color, anchor="mm")

    # 列表屏 -----------------------------------------------------

    def _draw_list(self, d):
        titles = {"types": "红外遥控", "brands": "选择品牌", "groups": "选择方案",
                  "recent": "最近使用"}
        right = ""
        if self.job.running:
            right = self.job.label + "." * (int(time.time() * 3) % 4)
        elif self.items:
            right = "%d/%d" % (min(self.sel + 1, len(self.items)), len(self.items))
        title = titles.get(self.screen, "")
        if self.screen == "brands" and self.device:
            title = "%s · %s" % (title, self.device["name"])
        elif self.screen == "groups" and self.brand:
            title = "%s · %s" % (title, self.brand["name"])
        if self.screen == "brands" and self.searching:
            title = "搜索：" + (self.query or "输入首字母")
        self._header(d, title, right, ACCENT if self.searching else FG)

        rows, top = 6, 24
        if not self.items:
            if self.job.running:
                self._center_text(d, self.job.label + "…")
            elif self.error:
                self._center_text(d, "读取失败，按 ESC 返回", DANGER)
            elif self.screen == "brands" and self.searching and self.query:
                self._center_text(d, "没有匹配「%s」的品牌" % self.query, DANGER, 76)
                self._center_text(d, "退格删字 · 返回键退出搜索", DIM, 96)
            else:
                self._center_text(d, "码库里没有可用方案" if self.screen == "groups"
                                  else "没有内容")
            self._footer(d, self._footer_labels())
            self._toast_bar(d)
            return

        row_h = 17
        for idx in range(rows):
            pos = self.view + idx
            if pos >= len(self.items):
                break
            item = self.items[pos]
            y = top + idx * row_h
            selected = pos == self.sel
            if selected:
                d.rounded_rectangle((4, y, W - 4, y + row_h - 1), radius=3, fill=ACCENT)
                color, sub_color = SEL_TXT, "#5a4433"
            else:
                color, sub_color = FG, DIM
            face = font(12, selected)
            label_x = 10
            if item.get("hot"):
                # 「热」小标：置顶的热门品牌，选中行用深底反白保证对比度
                chip_fill = SEL_TXT if selected else "#2a1d14"
                chip_txt = ACCENT if selected else "#ff9a6a"
                d.rounded_rectangle((8, y + 3, 25, y + row_h - 4), radius=3, fill=chip_fill)
                d.text((16.5, y + row_h / 2), "热", font=font(8), fill=chip_txt, anchor="mm")
                label_x = 30
            d.text((label_x, y + row_h / 2), clip(face, item["label"], 240 - label_x + 10),
                   font=face, fill=color, anchor="lm")
            if item.get("sub"):
                d.text((W - 9, y + row_h / 2), clip(font(9), item["sub"], 42),
                       font=font(9), fill=sub_color, anchor="rm")

        if len(self.items) > rows:
            track_top, track_bot = top + 2, top + rows * row_h - 5
            d.rectangle((W - 3, track_top, W - 1, track_bot), fill="#1a2530")
            span = track_bot - track_top
            bar = max(6, int(span * rows / len(self.items)))
            offset = int((span - bar) * self.view / max(1, len(self.items) - rows))
            d.rectangle((W - 3, track_top + offset, W - 1, track_top + offset + bar),
                        fill=ACCENT)

        # 提示行与 toast 条同在 y≈127-135，toast 出现时让位，避免两层文字叠在一起
        if not self._toast_active():
            d.text((8, 130), self._list_hint(), font=font(9), fill=DIM, anchor="lm")
        self._footer(d, self._footer_labels())
        self._toast_bar(d)

    def _footer_labels(self):
        """列表屏底栏：品牌页第 5 格给搜索/清空，其余屏留空。"""
        labels = list(FOOTER_LIST)
        if self.screen == "brands":
            labels[4] = "清空" if self.searching else "搜索"
        return labels

    def _list_hint(self):
        if self.screen == "groups":
            # 按光标所在那条方案判定：同一品牌下可能 state / pulse 混排
            cur = self.items[self.sel] if self.items and self.sel < len(self.items) else None
            g = cur["data"] if cur else None
            if g and g.get("kind") == "state":
                return "Enter 载入空调状态遥控 · ESC 返回"
            if g and not g.get("power"):
                return "Enter 载入码表 · ESC 返回"
            return "Enter 试发电源码并载入码表"
        if self.screen == "brands":
            if self.searching:
                # 命中数放在提示行：桌面状态栏会盖住表头右侧，不能只靠表头计数
                if not self.query:
                    return "输入首字母搜索 · 退格删字 · 8 清空"
                return "命中 %d 条 · 退格删字 · 8 清空" % len(self.items)
            hot = sum(1 for it in self.items if it.get("hot"))
            if hot:
                return "带「热」为热门 · TAB 搜索 · F/X 上下"
            return "F/X 上下 · Z/C 翻页 · TAB 搜索"
        if self.screen == "recent":
            return "Enter 直接进入遥控器"
        return "F/X 上下 · Enter 进入 · ESC 返回"

    def _remote_hint(self, pages, page):
        """右上提示：写明设备真实可用的导航键，且不与按键语义矛盾。

        放在右上而不是底部 130 行——那里是 toast 条 (127~135)，会被整条盖住。
        """
        if pages > 1:
            return "%d/%d 页 · 角标直发 · FXZC" % (page + 1, pages)
        if self.group and len(self.group["keysetids"]) > 1:
            return "角标直发 · FXZC · TAB 换码"
        return "角标直发 · FXZC 移动"

    # 遥控器屏 ---------------------------------------------------

    def _draw_remote(self, d):
        if self.model:
            title = self.model["name"]
        elif self.brand:
            title = self.brand["name"]
        else:
            title = "遥控器"
        self._header(d, title, "%dkHz" % (self.freq // 1000))

        if self.job.running:
            self._center_text(d, self.job.label + "…")
            self._footer(d, ["", "", "", "返回", ""])
            self._toast_bar(d)
            return

        total = len(self.ir_keys)
        if total == 0:
            self._center_text(d, "该方案没有可用按键")
            self._footer(d, ["", "", "", "返回", ""])
            self._toast_bar(d)
            return

        page = self.grid_sel // GRID_PER_PAGE
        pages = (total + GRID_PER_PAGE - 1) // GRID_PER_PAGE
        info = self.ac.summary(self.ac_state) if self.ac else "%d 键" % total
        if self.group and len(self.group["keysetids"]) > 1:
            info += " · 码表 %d/%d" % (self.keyset_pos + 1, len(self.group["keysetids"]))
        d.text((8, 25), info, font=font(9), fill=DIM, anchor="lm")
        right_hint = self._remote_hint(pages, page)
        d.text((W - 8, 25), clip(font(9), right_hint, 150), font=font(9),
               fill=DIM, anchor="rm")

        cw, ch, gap = 74, 29, 3
        x0, y0 = 6, 32
        for slot in range(GRID_PER_PAGE):
            idx = page * GRID_PER_PAGE + slot
            if idx >= total:
                break
            col, row = slot % GRID_COLS, slot // GRID_COLS
            x = x0 + col * (cw + gap)
            y = y0 + row * (ch + gap)
            name = self.ir_keys[idx]
            selected = idx == self.grid_sel
            d.rounded_rectangle((x, y, x + cw, y + ch), radius=5,
                                fill=ACCENT if selected else "#16222c",
                                outline=ACCENT if selected else "#243543")
            # 左上角角标 = 该格对应的键盘字母，按下即发射；选中格反色保持可读。
            d.text((x + 4, y + 3), GRID_LETTERS[slot], font=font(9),
                   fill=SEL_TXT if selected else DIM, anchor="lt")
            label = AC_LABELS.get(name, name) if self.ac else IR_LABELS.get(name, name)
            # 文字略右移、收窄，给左上角角标留出不重叠的空间。
            d.text((x + cw / 2 + 3, y + ch / 2),
                   clip(font(12, selected), label, cw - 18),
                   font=font(12, selected),
                   fill=SEL_TXT if selected else FG, anchor="mm")

        if self.sending:
            d.text((W - 8, 25), "发送中…", font=font(9), fill=ACCENT, anchor="ra")

        self._footer(d, AC_FOOTER if self.ac else FOOTER_REMOTE)
        self._toast_bar(d)

    # ---------------------------------------------------------- 主循环

    def run(self):
        while self.running:
            for code, value in self.kb.poll(0.05):
                self.on_key(code, value)
            if self.job.done:
                self.poll_job()
            if self.toast[0] and time.time() > self.toast[1]:
                self.toast = ("", 0.0, FG)
                self.dirty = True
            if self.dirty:
                self.draw()
                self.dirty = False

    def close(self):
        if self.kb is not None:
            self.kb.close()


# ---------------------------------------------------------------- 自检

def shot_mode(outdir):
    """用真实码库数据渲染每一屏成 PNG，用于无摄像头时校验排版。"""
    os.makedirs(outdir, exist_ok=True)
    app = App(fb=None, kb=None)
    _, _, error = app.job.wait(40)
    if error:
        print("devices error:", error)
        return 1
    app.poll_job()
    app.draw()
    app.last_frame.save(os.path.join(outdir, "01-types.png"))

    dev = next((x for x in app.devices if "电视" in x["name"]), app.devices[0])
    app.open_brands(dev)
    _, _, error = app.job.wait(45)
    if error:
        print("brands error:", error)
        return 1
    app.poll_job()
    app.draw()
    app.last_frame.save(os.path.join(outdir, "02-brands.png"))

    brand = next((b for b in app.brands if b["name"] in ("创维", "海信", "TCL")), app.brands[0])
    app.sel = next((i for i, it in enumerate(app.items) if it["data"] is brand), 0)
    app._clamp_view()
    app.draw()
    app.last_frame.save(os.path.join(outdir, "03-brands-sel.png"))

    app._search_start()
    for ch in "XM":
        app._key_search(ch, 1)
    app.draw()
    app.last_frame.save(os.path.join(outdir, "03b-brands-search.png"))
    app.searching, app.query = False, ""

    app.open_groups(brand)
    _, _, error = app.job.wait(45)
    if error:
        print("tree error:", error)
        return 1
    app.poll_job()
    app.draw()
    app.last_frame.save(os.path.join(outdir, "04-groups.png"))

    if not app.groups:
        print("该品牌无方案")
        return 1
    app.sel = 0
    app.draw()
    app.last_frame.save(os.path.join(outdir, "05-groups-sel.png"))

    app.open_pair(app.groups[0], test=False)
    _, _, error = app.job.wait(60)
    if error:
        print("pair error:", error)
        return 1
    app.poll_job()
    app.draw()
    app.last_frame.save(os.path.join(outdir, "06-remote.png"))
    app.grid_sel = min(4, len(app.ir_keys) - 1)
    app.toast = ("已发送 音量+ · 168 段", time.time() + 90, ACCENT)
    app.draw()
    app.last_frame.save(os.path.join(outdir, "07-remote-toast.png"))
    if len(app.ir_keys) > GRID_PER_PAGE:
        app.grid_sel = GRID_PER_PAGE
        app.draw()
        app.last_frame.save(os.path.join(outdir, "08-remote-page2.png"))

    # 空调状态页：品牌/方案组都挑第一个（有缓存码表时基本就是小米），任何一步
    # 失败就跳过，不让截图流程整体失败。
    ac = next((x for x in app.devices if "空调" in x["name"]), None)
    if ac:
        app.toast = None          # 别把上一屏的「已发送…」带到空调页
        app.open_brands(ac)
        if not app.job.wait(45)[2]:
            app.poll_job()
            acb = next((x for x in app.brands if x["name"] == "小米"),
                       app.brands[0] if app.brands else None)
            if acb is not None:
                app.open_groups(acb)
                if not app.job.wait(60)[2]:
                    app.poll_job()
                    if app.groups:
                        app.open_pair(app.groups[0], test=False)
                        if not app.job.wait(60)[2]:
                            app.poll_job()
                            app.draw()
                            app.last_frame.save(os.path.join(outdir, "09-ac.png"))
    print("remote: %d 键 %dHz %s 前12=%s" % (
        len(app.ir_keys), app.freq, app.model, app.ir_keys[:12]))
    print("saved:", outdir)
    return 0


def fbtest():
    """写测试图案 → 读回逐行比对 → 还原原内容，验证 fb 路径而不留痕迹。"""
    fb = Framebuffer()
    n = fb.bpp // 8
    print("fb %dx%d %dbpp stride=%d" % (fb.width, fb.height, fb.bpp, fb.stride))
    backup = fb.snapshot()
    try:
        image = Image.new("RGB", (W, H), "#102030")
        d = ImageDraw.Draw(image)
        d.rectangle((0, 0, 159, H), fill="#ff0000")
        d.rectangle((160, 0, W - 1, H), fill="#00ff00")
        fb.show(image)
        raw, _ = fb._pack(image)
        bad = []
        for y in range(H):
            start = (y + fb.yoff) * fb.stride + fb.xoff * n
            if bytes(fb.mem[start:start + W * n]) != raw[y * W * n:(y + 1) * W * n]:
                bad.append(y)
        print("逐行比对 %d 行:" % H, "PASS" if not bad else "FAIL %s" % bad[:6])
        return 0 if not bad else 1
    finally:
        fb.restore(backup)
        fb.close()


def main(argv):
    if "--shot" in argv:
        idx = argv.index("--shot")
        return shot_mode(argv[idx + 1] if len(argv) > idx + 1 else "/tmp/irremote-shots")
    if "--fbtest" in argv:
        return fbtest()

    fb = kb = app = None
    try:
        fb = Framebuffer()
        kb = Keyboard()
        app = App(fb=fb, kb=kb)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("启动失败: %s\n" % exc)
        if fb:
            fb.close()
        return 1

    def stop(_signum, _frame):
        if app:
            app.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        app.run()
    finally:
        app.close()
        fb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
