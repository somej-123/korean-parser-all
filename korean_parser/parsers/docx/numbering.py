"""numbering.xml 해석: 자동 번호/글머리표를 화면에 보이는 문자열로 계산한다.

Word는 번호를 저장하지 않고 "규칙"만 저장한다. (예: 수준 1은 '가.' 형식, 1부터 시작)
그래서 문서를 처음부터 읽으며 번호를 직접 세야 화면과 같은 번호("다.")를 얻을 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .common import child, q, wattr
from .styles import parse_ppr
from .symbols import map_symbol

_GANADA = "가나다라마바사아자차카타파하"
_CHOSUNG = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_KOREAN_DIGITS = "영일이삼사오육칠팔구"
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# Symbol/Wingdings 글꼴의 사용자 정의 영역 문자 → 일반 유니코드
_SYMBOL_BULLETS = {
    "": "•", "": "▪", "": "o", "": "➢", "": "✓",
    "": "❖", "": "▫", "": "➔", "": "○", "": "■",
    "": "◆", "": "◇",
}


def _roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def _letters(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _korean_digit(n: int) -> str:
    if n < 10:
        return _KOREAN_DIGITS[n]
    tens, ones = divmod(n, 10)
    return (_KOREAN_DIGITS[tens] if tens > 1 else "") + "십" + (_KOREAN_DIGITS[ones] if ones else "")


def format_number(n: int, fmt: str) -> str:
    if fmt in ("decimal", "decimalHalfWidth", "decimalFullWidth"):
        return str(n)
    if fmt == "decimalZero":
        return f"{n:02d}"
    if fmt == "upperRoman":
        return _roman(n)
    if fmt == "lowerRoman":
        return _roman(n).lower()
    if fmt == "upperLetter":
        return _letters(n)
    if fmt == "lowerLetter":
        return _letters(n).lower()
    if fmt in ("ganada", "koreanLegal"):
        return _GANADA[(n - 1) % len(_GANADA)]
    if fmt == "chosung":
        return _CHOSUNG[(n - 1) % len(_CHOSUNG)]
    if fmt in ("koreanDigital", "koreanCounting", "koreanDigital2"):
        return _korean_digit(n)
    if fmt == "decimalEnclosedCircle":
        return _CIRCLED[n - 1] if 0 < n <= len(_CIRCLED) else str(n)
    if fmt == "none":
        return ""
    return str(n)


@dataclass
class Level:
    fmt: str
    text: str
    start: int
    ppr: dict
    font: str = ""  # 표지 글꼴 (기호 글꼴이면 글꼴별 표로 바꾼다)


class Numbering:
    def __init__(self, root=None):
        self.abstract: dict[str, dict[int, Level]] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}  # numId → (abstractId, startOverrides)
        self.counters: dict[str, dict[int, int]] = {}
        if root is not None:
            self._load(root)

    def _load(self, root):
        for an in root.findall(q("w:abstractNum")):
            levels: dict[int, Level] = {}
            for lvl in an.findall(q("w:lvl")):
                ilvl = int(wattr(lvl, "ilvl", "0"))
                start = child(lvl, "w:start")
                levels[ilvl] = Level(
                    fmt=wattr(child(lvl, "w:numFmt"), "val", "decimal"),
                    text=wattr(child(lvl, "w:lvlText"), "val", "") or "",
                    start=int(wattr(start, "val", "1")) if start is not None else 1,
                    ppr=parse_ppr(child(lvl, "w:pPr")),
                    font=wattr(child(child(lvl, "w:rPr"), "w:rFonts"), "ascii", "") if child(lvl, "w:rPr") is not None
                    else "",
                )
            self.abstract[wattr(an, "abstractNumId")] = levels
        for num in root.findall(q("w:num")):
            overrides = {}
            for ov in num.findall(q("w:lvlOverride")):
                so = child(ov, "w:startOverride")
                if so is not None:
                    overrides[int(wattr(ov, "ilvl", "0"))] = int(wattr(so, "val", "1"))
            self.nums[wattr(num, "numId")] = (wattr(child(num, "w:abstractNumId"), "val"), overrides)

    def level(self, num_id: str, ilvl: int) -> Level | None:
        entry = self.nums.get(num_id)
        if not entry:
            return None
        return self.abstract.get(entry[0], {}).get(ilvl)

    def next_marker(self, num_id: str, ilvl: int) -> tuple[str, bool, dict] | None:
        """번호를 하나 증가시키고 (표시 문자열, 순서 목록 여부, 수준의 문단 서식)을 돌려준다."""
        if num_id in (None, "0"):
            return None
        entry = self.nums.get(num_id)
        if not entry:
            return None
        abstract_id, overrides = entry
        levels = self.abstract.get(abstract_id, {})
        lvl = levels.get(ilvl)
        if lvl is None:
            return None

        counters = self.counters.setdefault(num_id, {})
        start = overrides.get(ilvl, lvl.start)
        counters[ilvl] = counters.get(ilvl, start - 1) + 1
        for deeper in [k for k in counters if k > ilvl]:
            del counters[deeper]  # 상위 번호가 바뀌면 하위 번호는 처음부터

        if lvl.fmt == "bullet":
            # 기호 글꼴(Wingdings 1·2·3, Symbol, MT Extra)의 글꼴 고유 코드는 글꼴별 표로, 그 밖에는 자주 쓰는 것만
            text = "".join(map_symbol(lvl.font, ch) if lvl.font and "\uf000" <= ch <= "\uf0ff"
                           else _SYMBOL_BULLETS.get(ch, ch) for ch in lvl.text) or "•"
            return text, False, lvl.ppr

        text = lvl.text
        for i in range(9):
            token = f"%{i + 1}"
            if token in text:
                li = levels.get(i)
                # 건너뛴 상위 단계는 시작값으로 확정해 두어야 다음 번호가 이어진다
                n = counters.setdefault(i, overrides.get(i, li.start) if li else 1)
                text = text.replace(token, format_number(n, li.fmt if li else "decimal"))
        return text, True, lvl.ppr
