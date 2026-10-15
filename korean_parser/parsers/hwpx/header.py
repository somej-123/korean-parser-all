"""HWPX header.xml 해석: 글꼴, 글자 모양, 문단 모양, 테두리/배경, 번호·글머리표, 스타일.

HWP는 Word와 달리 스타일 상속을 쓰지 않는다. 문단과 글 조각이 가리키는 문단 모양(paraPr)과
글자 모양(charPr)에 모든 값이 완전하게 들어 있으므로, id로 찾아 그대로 쓰면 된다.

단위: HWPUNIT = 1/7200 인치 → pt = 값 / 100
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...ir import Border, Borders, ParagraphStyle, TextStyle

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def ln(el) -> str:
    """요소의 로컬 이름 (네임스페이스 버전 차이를 무시하기 위해)."""
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def kid(el, name: str):
    if el is None:
        return None
    for c in el:
        if ln(c) == name:
            return c
    return None


def kids(el, name: str):
    return [] if el is None else [c for c in el if ln(c) == name]


def resolve_switch(el):
    """hp:switch가 있으면 hp:case(최신 형식) 내용을, 없으면 el을 그대로 돌려준다.

    문단 여백의 경우 hp:default에는 구버전 호환용으로 실제의 2배 값이 들어 있어 hp:case를 써야 한다.
    """
    sw = kid(el, "switch")
    if sw is None:
        return el
    case = kid(sw, "case")
    return case if case is not None else kid(sw, "default")


def color(value: str | None) -> str | None:
    if value and _HEX.match(value):
        return value.upper()
    return None


def hwp_pt(value) -> float:
    """HWPUNIT → pt. 음수가 부호 없는 32비트로 저장된 경우도 처리한다."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0.0
    if v >= 2**31:
        v -= 2**32
    return v / 100.0


def mm_to_pt(value: str | None) -> float:
    m = re.match(r"\s*([\d.]+)\s*mm", value or "")
    return float(m.group(1)) * 2.8346 if m else 0.5


_BORDER_STYLE = {
    "SOLID": "solid", "DASH": "dashed", "DASH_DOT": "dashed", "DASH_DOT_DOT": "dashed", "LONG_DASH": "dashed",
    "DOT": "dotted", "CIRCLE": "dotted", "DOUBLE_SLIM": "double", "SLIM_THICK": "double", "THICK_SLIM": "double",
    "SLIM_THICK_SLIM": "double", "WAVE": "solid", "DOUBLE_WAVE": "double", "THICK_3D": "solid",
    "THICK_3D_REVERSAL": "solid", "3D": "solid", "3D_REVERSAL": "solid",
}

_ALIGN = {"LEFT": "left", "RIGHT": "right", "CENTER": "center", "JUSTIFY": "justify",
          "DISTRIBUTE": "justify", "DISTRIBUTE_SPACE": "justify"}


@dataclass
class BorderFill:
    borders: Borders | None = None
    background: str | None = None
    image_ref: str | None = None  # 그림 채우기 (binaryItemIDRef)


@dataclass
class ParaDef:
    style: ParagraphStyle | None
    heading_type: str = "NONE"  # NONE, OUTLINE, NUMBER, BULLET
    heading_ref: str = "0"
    heading_level: int = 0
    page_break_before: bool = False


@dataclass
class NumberingDef:
    start: int = 1
    levels: dict[int, tuple[int, str, str]] = field(default_factory=dict)  # level(1~) → (start, 형식, 표시 문자열)


@dataclass
class StyleDef:
    name: str
    eng_name: str
    para_ref: str
    char_ref: str


class Header:
    def __init__(self, root=None):
        self.fonts: dict[str, dict[str, str]] = {}  # 언어 → {id: 글꼴 이름}
        self.border_fills: dict[str, BorderFill] = {}
        self.char_styles: dict[str, TextStyle] = {}
        self.para_defs: dict[str, ParaDef] = {}
        self.numberings: dict[str, NumberingDef] = {}
        self.bullets: dict[str, str] = {}
        self.styles: dict[str, StyleDef] = {}
        if root is not None:
            self._load(root)

    def _load(self, root):
        ref = kid(root, "refList")
        for ff in kids(kid(ref, "fontfaces"), "fontface"):
            self.fonts[ff.get("lang", "")] = {f.get("id"): f.get("face", "") for f in kids(ff, "font")}
        for bf in kids(kid(ref, "borderFills"), "borderFill"):
            self.border_fills[bf.get("id")] = _border_fill(bf)
        for cp in kids(kid(ref, "charProperties"), "charPr"):
            self.char_styles[cp.get("id")] = self._char(cp)
        for pp in kids(kid(ref, "paraProperties"), "paraPr"):
            self.para_defs[pp.get("id")] = self._para(pp)
        for nb in kids(kid(ref, "numberings"), "numbering"):
            nd = NumberingDef(start=int(nb.get("start", "1") or 1))
            for ph in kids(nb, "paraHead"):
                lvl = int(ph.get("level", "1") or 1)
                nd.levels[lvl] = (int(ph.get("start", "1") or 1), ph.get("numFormat", "DIGIT"), ph.text or "")
            self.numberings[nb.get("id")] = nd
        for b in kids(kid(ref, "bullets"), "bullet"):
            self.bullets[b.get("id")] = b.get("char", "")
        styles = kid(ref, "styles")
        for st in kids(styles, "style"):
            self.styles[st.get("id")] = StyleDef(
                name=st.get("name", ""), eng_name=st.get("engName", ""),
                para_ref=st.get("paraPrIDRef", "0"), char_ref=st.get("charPrIDRef", "0"),
            )

    # ------------------------------------------------------------------

    def _char(self, cp) -> TextStyle:
        s: dict = {}
        try:
            s["size_pt"] = int(cp.get("height", "1000")) / 100
        except ValueError:
            pass
        s["color"] = color(cp.get("textColor")) or "#000000"
        bg = color(cp.get("shadeColor"))
        if bg and bg != "#FFFFFF":
            s["background"] = bg
        s["bold"] = kid(cp, "bold") is not None
        s["italic"] = kid(cp, "italic") is not None
        ul = kid(cp, "underline")
        s["underline"] = ul is not None and ul.get("type", "NONE") not in ("NONE",)
        so = kid(cp, "strikeout")
        s["strike"] = so is not None and so.get("shape", "NONE") not in ("NONE", "3D")
        if kid(cp, "supscript") is not None:
            s["vertical"] = "super"
        elif kid(cp, "subscript") is not None:
            s["vertical"] = "sub"
        fref = kid(cp, "fontRef")
        if fref is not None:
            face = self.fonts.get("HANGUL", {}).get(fref.get("hangul"))
            if face:
                s["font"] = face
        sp = kid(cp, "spacing")
        if sp is not None:
            try:
                v = int(sp.get("hangul", "0"))
                if v:
                    s["letter_spacing_em"] = v / 100  # 자간 (%)
            except ValueError:
                pass
        ratio = kid(cp, "ratio")
        if ratio is not None:
            try:
                v = int(ratio.get("hangul", "100"))
                if v != 100:
                    s["width_ratio"] = v / 100  # 장평 (%)
            except ValueError:
                pass
        return TextStyle(**s)

    def _para(self, pp) -> ParaDef:
        body = resolve_switch(pp)
        s: dict = {}
        al = kid(pp, "align")
        if al is not None and al.get("horizontal") in _ALIGN:
            s["align"] = _ALIGN[al.get("horizontal")]
        margin = kid(body, "margin")
        if margin is not None:
            vals = {ln(c): hwp_pt(c.get("value", "0")) for c in margin}
            intent, left = vals.get("intent", 0.0), vals.get("left", 0.0)
            if intent < 0:
                # 내어쓰기: 첫 줄은 왼쪽 여백에서 시작하고 나머지 줄이 들어간다
                s["indent_left_pt"] = left - intent
                s["indent_first_pt"] = intent
            else:
                s["indent_left_pt"] = left or None
                s["indent_first_pt"] = intent or None
            s["indent_right_pt"] = vals.get("right") or None
            s["space_before_pt"] = vals.get("prev") or None
            s["space_after_pt"] = vals.get("next") or None
        ls = kid(body, "lineSpacing")
        if ls is not None:
            kind, value = ls.get("type", "PERCENT"), ls.get("value", "160")
            try:
                if kind == "PERCENT":
                    # HWP의 줄 간격 %는 글자 크기 대비 줄 높이. IR의 line_spacing은 Word '한 줄'(≈1.17) 기준 배수.
                    s["line_spacing"] = int(value) / 100 / 1.17
                elif kind in ("FIXED", "AT_LEAST"):
                    s["line_spacing_pt"] = hwp_pt(value)
            except ValueError:
                pass
        border = kid(pp, "border")
        if border is not None:
            bf = self.border_fills.get(border.get("borderFillIDRef", ""))
            if bf is not None:
                if bf.borders is not None:
                    s["borders"] = bf.borders
                if bf.background:
                    s["background"] = bf.background
        heading = kid(pp, "heading")
        brk = kid(pp, "breakSetting")
        fields = {k: v for k, v in s.items() if v is not None}
        return ParaDef(
            style=ParagraphStyle(**fields) if fields else None,
            heading_type=heading.get("type", "NONE") if heading is not None else "NONE",
            heading_ref=heading.get("idRef", "0") if heading is not None else "0",
            heading_level=int(heading.get("level", "0") or 0) if heading is not None else 0,
            page_break_before=brk is not None and brk.get("pageBreakBefore") == "1",
        )


def _border(el) -> Border | None:
    if el is None:
        return None
    kind = el.get("type", "NONE")
    if kind == "NONE":
        return Border(style="none", width_pt=0)
    return Border(style=_BORDER_STYLE.get(kind, "solid"), width_pt=max(mm_to_pt(el.get("width")), 0.25),
                  color=color(el.get("color")) or "#000000")


def _border_fill(bf) -> BorderFill:
    sides = {
        "left": _border(kid(bf, "leftBorder")),
        "right": _border(kid(bf, "rightBorder")),
        "top": _border(kid(bf, "topBorder")),
        "bottom": _border(kid(bf, "bottomBorder")),
    }
    visible = {k: v for k, v in sides.items() if v is not None and v.style != "none"}
    background = image_ref = None
    brush = kid(bf, "fillBrush")
    if brush is not None:
        background = fill_color(brush)
        img = kid(kid(brush, "imgBrush"), "img")
        image_ref = img.get("binaryItemIDRef") if img is not None else None
    return BorderFill(borders=Borders(**visible) if visible else None, background=background, image_ref=image_ref)


def fill_color(brush) -> str | None:
    """hc:fillBrush → 단색. 그라데이션은 첫 색으로 근사한다."""
    if brush is None:
        return None
    win = kid(brush, "winBrush")
    if win is not None:
        c = color(win.get("faceColor"))
        if c and win.get("faceColor", "").lower() != "none":
            return c
    grad = kid(brush, "gradation")
    if grad is not None:
        cols = [color(c.get("value")) for c in kids(grad, "color")]
        cols = [c for c in cols if c]
        if cols:
            return cols[0]
    return None


# ---------------------------------------------------------------------------
# 번호 형식
# ---------------------------------------------------------------------------

_GANADA = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
_CIRCLED_DIGIT = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_GANADA = "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻"
_CIRCLED_JAMO = "㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭"
_IDEOGRAPH = "一二三四五六七八九十"
_CIRCLED_IDEOGRAPH = "㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉"


def _cyc(seq: str, n: int) -> str:
    return seq[(n - 1) % len(seq)] if n > 0 else ""


def format_hwp_number(n: int, fmt: str) -> str:
    from ..docx.numbering import _korean_digit, _letters, _roman

    if fmt == "DIGIT":
        return str(n)
    if fmt == "CIRCLED_DIGIT":
        return _CIRCLED_DIGIT[n - 1] if 0 < n <= len(_CIRCLED_DIGIT) else str(n)
    if fmt == "ROMAN_CAPITAL":
        return _roman(n)
    if fmt == "ROMAN_SMALL":
        return _roman(n).lower()
    if fmt == "LATIN_CAPITAL":
        return _letters(n)
    if fmt == "LATIN_SMALL":
        return _letters(n).lower()
    if fmt == "CIRCLED_LATIN_CAPITAL":
        return chr(0x24B6 + (n - 1) % 26)
    if fmt == "CIRCLED_LATIN_SMALL":
        return chr(0x24D0 + (n - 1) % 26)
    if fmt == "HANGUL_SYLLABLE":
        return _cyc(_GANADA, n)
    if fmt == "CIRCLED_HANGUL_SYLLABLE":
        return _cyc(_CIRCLED_GANADA, n)
    if fmt == "HANGUL_JAMO":
        return _cyc(_JAMO, n)
    if fmt == "CIRCLED_HANGUL_JAMO":
        return _cyc(_CIRCLED_JAMO, n)
    if fmt == "HANGUL_PHONETIC":
        return _korean_digit(n)
    if fmt == "IDEOGRAPH":
        return _IDEOGRAPH[n - 1] if 0 < n <= 10 else str(n)
    if fmt == "CIRCLED_IDEOGRAPH":
        return _CIRCLED_IDEOGRAPH[n - 1] if 0 < n <= 10 else str(n)
    return str(n)


class Counters:
    """번호 정의(numbering id)별로 문서 전체에서 이어지는 번호를 센다."""

    def __init__(self, header: Header):
        self.header = header
        self.counts: dict[str, dict[int, int]] = {}

    def next(self, numbering_id: str, level0: int) -> str | None:
        nd = self.header.numberings.get(numbering_id)
        if nd is None:
            return None
        level = level0 + 1
        if level not in nd.levels:
            return None
        counts = self.counts.setdefault(numbering_id, {})
        start, fmt, text = nd.levels[level]
        counts[level] = counts.get(level, start - 1) + 1
        for deeper in [k for k in counts if k > level]:
            del counts[deeper]

        def sub(m):
            lv = int(m.group(1))
            if lv not in nd.levels:
                return ""
            lstart, lfmt, _ = nd.levels[lv]
            # 건너뛴 상위 단계(예: 4. 다음에 바로 4.1.1.)는 시작값으로 확정해 두어야 다음 번호가 이어진다
            counts.setdefault(lv, lstart)
            return format_hwp_number(counts[lv], lfmt)

        return re.sub(r"\^(\d)", sub, text).strip()
