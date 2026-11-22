#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 src/cp0_pinyin.py（品牌名 -> 拼音首字母表）。

只在开发机上跑，设备上不需要 pypinyin：
    python3 tools/gen_pinyin.py > src/cp0_pinyin.py

字表范围：GB2312 一级汉字（3755 个常用字）+ 米家码库里品牌名实际用到的其余字。
多音字收录全部读音（长 -> c/z，厦 -> s/x），匹配时按前缀命中任意一个即可。
"""

import json
import os
import sys

try:
    from pypinyin import pinyin, Style
except ImportError:  # pragma: no cover
    sys.exit("需要 pypinyin：pip install pypinyin")

HERE = os.path.dirname(os.path.abspath(__file__))
BRANDS = os.path.join(HERE, "brands.json")


def gb2312_level1():
    out = []
    for hi in range(0xB0, 0xD8):          # 一级汉字 0xB0A1-0xD7F9
        for lo in range(0xA1, 0xFF):
            try:
                out.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return out


def extra_chars():
    """码库品牌名里出现的、一级字表之外的字（繁体、二级字等）。"""
    if not os.path.exists(BRANDS):
        return set()
    try:
        data = json.load(open(BRANDS, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    out = set()
    for value in data.values():
        if not isinstance(value, list):
            continue
        for name in value:
            out.update(c for c in str(name) if ord(c) > 127)
    return out


def main():
    chars = list(dict.fromkeys(gb2312_level1()))
    seen = set(chars)
    for ch in sorted(extra_chars()):
        if ch not in seen:
            chars.append(ch)
            seen.add(ch)

    by_letter = {}
    for ch in chars:
        readings = pinyin(ch, style=Style.FIRST_LETTER, heteronym=True, errors="ignore")
        if not readings or not readings[0]:
            continue
        letters = sorted({r[0].lower() for r in readings[0] if r and r[0].isalpha()})
        for letter in letters:
            by_letter.setdefault(letter, []).append(ch)

    lines = [
        "#!/usr/bin/env python3",
        "# -*- coding: utf-8 -*-",
        '"""中文 -> 拼音首字母表（由 tools/gen_pinyin.py 生成，勿手改）。',
        "",
        "覆盖 GB2312 一级汉字 + 米家码库品牌用字，多音字收录全部读音，",
        "所以「长虹」既能用 ch 也能用 zh 命中。生成方式：",
        "    python3 tools/gen_pinyin.py > src/cp0_pinyin.py",
        '"""',
        "",
        "_BY_LETTER = {",
    ]
    for letter in sorted(by_letter):
        chars_str = "".join(by_letter[letter])
        chunks = [chars_str[i:i + 40] for i in range(0, len(chars_str), 40)]
        body = "\n".join('        "%s"' % c for c in chunks)
        lines.append('    "%s": (' % letter)
        lines.append(body)
        lines.append("    ),")
    lines.append("}")
    lines += [
        "",
        "_TABLE = {}",
        "for _letter, _chars in _BY_LETTER.items():",
        "    for _ch in _chars:",
        '        _TABLE.setdefault(_ch, []).append(_letter)',
        '_TABLE = {k: tuple(v) for k, v in _TABLE.items()}',
        "",
        "",
        "def letters_of(ch):",
        '    """单字的可能首字母（多音字返回多个）。"""',
        "    return _TABLE.get(ch, ())",
        "",
        "",
        "def variants(text, cap=6):",
        '    """整串的可能首字母串（多音字组合，最多 cap 个）。',
        "",
        "    第一个永远是各字最常用读音拼出的结果，其余是生僻读音的排列，",
        "    搜索时用它们做「次要命中」，排序时排在常用读音后面。",
        '    """',
        "    combos = [\"\"]",
        "    for ch in text:",
        "        if ch.isascii() and ch.isalnum():",
        "            opts = [ch.lower()]",
        "        else:",
        "            opts = _TABLE.get(ch)",
        "            if not opts:",
        "                continue",
        "        combos = [prev + opt for prev in combos for opt in opts][:cap]",
        "    out = []",
        "    for item in combos:",
        "        if item and item not in out:",
        "            out.append(item)",
        "    return out[:cap]",
        "",
    ]
    sys.stdout.write("\n".join(lines))


if __name__ == "__main__":
    main()
