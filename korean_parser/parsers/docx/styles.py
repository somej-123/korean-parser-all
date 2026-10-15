"""styles.xml 해석: 문서 기본값 → 스타일 상속(basedOn) → 직접 서식 순서로 서식을 계산한다."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ...ir import Border, Borders, ParagraphStyle, TextStyle
from .common import HIGHLIGHT_COLORS, child, hex_color, on_off, q, twips_to_pt, wattr

_HEADING_NAME = re.compile(r"^(?:heading|제목)\s*(\d)$", re.IGNORECASE)


def parse_rpr(rpr) -> dict:
    """<w:rPr> → TextStyle 필드 dict (지정된 값만)."""
    if rpr is None:
        return {}
    out: dict = {}
    for tag, key in (("w:b", "bold"), ("w:i", "italic")):
        v = on_off(child(rpr, tag))
        if v is not None:
            out[key] = v
    strike = on_off(child(rpr, "w:strike"))
    dstrike = on_off(child(rpr, "w:dstrike"))
    if strike is not None or dstrike is not None:
        out["strike"] = bool(strike or dstrike)
    u = child(rpr, "w:u")
    if u is not None:
        out["underline"] = wattr(u, "val", "single") not in ("none", "0")
    color = child(rpr, "w:color")
    if color is not None:
        out["color"] = hex_color(wattr(color, "val"))
    sz = child(rpr, "w:sz")
    if sz is not None and wattr(sz, "val"):
        try:
            out["size_pt"] = int(wattr(sz, "val")) / 2.0
        except ValueError:
            pass
    hl = child(rpr, "w:highlight")
    if hl is not None and wattr(hl, "val") in HIGHLIGHT_COLORS:
        out["background"] = HIGHLIGHT_COLORS[wattr(hl, "val")]
    shd = child(rpr, "w:shd")
    if shd is not None and "background" not in out:
        fill = hex_color(wattr(shd, "fill"))
        if fill:
            out["background"] = fill
    va = child(rpr, "w:vertAlign")
    if va is not None:
        out["vertical"] = {"superscript": "super", "subscript": "sub"}.get(wattr(va, "val"))
    fonts = child(rpr, "w:rFonts")
    if fonts is not None:
        font = wattr(fonts, "eastAsia") or wattr(fonts, "ascii") or wattr(fonts, "hAnsi")
        if font:
            out["font"] = font
    caps, small = on_off(child(rpr, "w:caps")), on_off(child(rpr, "w:smallCaps"))
    if caps is not None or small is not None:
        out["caps"] = "all" if caps else "small" if small else None
    vanish = on_off(child(rpr, "w:vanish"))
    if vanish is not None:
        out["_hidden"] = vanish
    return out


def parse_ppr(ppr) -> dict:
    """<w:pPr> → ParagraphStyle 필드 dict + 번호(numPr), 개요 수준."""
    if ppr is None:
        return {}
    out: dict = {}
    jc = child(ppr, "w:jc")
    if jc is not None:
        align = {
            "left": "left", "start": "left", "center": "center", "right": "right",
            "end": "right", "both": "justify", "distribute": "justify",
        }.get(wattr(jc, "val"))
        if align:
            out["align"] = align
    ind = child(ppr, "w:ind")
    if ind is not None:
        left = wattr(ind, "left") or wattr(ind, "start")
        right = wattr(ind, "right") or wattr(ind, "end")
        if left is not None:
            out["indent_left_pt"] = twips_to_pt(left)
        if right is not None:
            out["indent_right_pt"] = twips_to_pt(right)
        if wattr(ind, "hanging") is not None:
            out["indent_first_pt"] = -(twips_to_pt(wattr(ind, "hanging")) or 0)
        elif wattr(ind, "firstLine") is not None:
            out["indent_first_pt"] = twips_to_pt(wattr(ind, "firstLine"))
    sp = child(ppr, "w:spacing")
    if sp is not None:
        if wattr(sp, "before") is not None:
            out["space_before_pt"] = twips_to_pt(wattr(sp, "before"))
        if wattr(sp, "after") is not None:
            out["space_after_pt"] = twips_to_pt(wattr(sp, "after"))
        line, rule = wattr(sp, "line"), wattr(sp, "lineRule", "auto")
        if line is not None:
            try:
                if rule == "auto":
                    out["line_spacing"] = int(line) / 240.0
                    out["line_spacing_pt"] = None
                else:
                    out["line_spacing_pt"] = int(line) / 20.0
                    out["line_spacing"] = None
            except ValueError:
                pass
    shd = child(ppr, "w:shd")
    if shd is not None:
        fill = hex_color(wattr(shd, "fill"))
        if fill:
            out["background"] = fill
    bdr = child(ppr, "w:pBdr")
    if bdr is not None:
        borders = parse_borders(bdr, names=("top", "bottom", "left", "right"))
        if borders:
            out["borders"] = borders
    num = child(ppr, "w:numPr")
    if num is not None:
        ilvl, num_id = child(num, "w:ilvl"), child(num, "w:numId")
        out["_num"] = (wattr(num_id, "val"), int(wattr(ilvl, "val", "0") or 0))
    cs = on_off(child(ppr, "w:contextualSpacing"))
    if cs is not None:
        out["_contextual"] = cs
    ol = child(ppr, "w:outlineLvl")
    if ol is not None:
        try:
            out["_outline"] = int(wattr(ol, "val"))
        except (TypeError, ValueError):
            pass
    return out


def parse_border(el) -> Border | None:
    if el is None:
        return None
    val = wattr(el, "val", "single")
    if val in ("nil", "none"):
        return Border(style="none", width_pt=0)
    style = "solid"
    if val.startswith("double") or val in ("triple", "thinThickSmallGap", "thickThinSmallGap"):
        style = "double"
    elif "dash" in val.lower():
        style = "dashed"
    elif "dot" in val.lower():
        style = "dotted"
    try:
        width = int(wattr(el, "sz", "4")) / 8.0
    except ValueError:
        width = 0.5
    return Border(style=style, width_pt=max(width, 0.25), color=hex_color(wattr(el, "color")) or "#000000")


def parse_borders(el, names=("top", "bottom", "left", "right", "insideH", "insideV")) -> dict:
    if el is None:
        return {}
    out = {}
    for name in names:
        b = child(el, f"w:{name}")
        if b is None and name in ("left", "right"):
            b = child(el, "w:start" if name == "left" else "w:end")
        if b is not None:
            out[name] = parse_border(b)
    return out


@dataclass
class StyleDef:
    style_id: str
    name: str
    kind: str
    based_on: str | None
    rpr: dict
    ppr: dict
    table_borders: dict = field(default_factory=dict)
    table_cell_margins: dict = field(default_factory=dict)


class StyleSheet:
    def __init__(self, root=None):
        self.styles: dict[str, StyleDef] = {}
        self.default_rpr: dict = {}
        self.default_ppr: dict = {}
        self.default_para_style: str | None = None
        self.default_table_style: str | None = None
        self._cache: dict = {}
        if root is not None:
            self._load(root)

    def _load(self, root):
        defaults = child(root, "w:docDefaults")
        if defaults is not None:
            self.default_rpr = parse_rpr(defaults.find(f"{q('w:rPrDefault')}/{q('w:rPr')}"))
            self.default_ppr = parse_ppr(defaults.find(f"{q('w:pPrDefault')}/{q('w:pPr')}"))
        for st in root.findall(q("w:style")):
            sid = wattr(st, "styleId")
            if not sid:
                continue
            name_el = child(st, "w:name")
            based = child(st, "w:basedOn")
            tblpr = child(st, "w:tblPr")
            d = StyleDef(
                style_id=sid,
                name=wattr(name_el, "val", sid) or sid,
                kind=wattr(st, "type", "paragraph"),
                based_on=wattr(based, "val") if based is not None else None,
                rpr=parse_rpr(child(st, "w:rPr")),
                ppr=parse_ppr(child(st, "w:pPr")),
                table_borders=parse_borders(child(tblpr, "w:tblBorders")),
                table_cell_margins=cell_margins(child(tblpr, "w:tblCellMar")),
            )
            self.styles[sid] = d
            if wattr(st, "default") in ("1", "true"):
                if d.kind == "paragraph":
                    self.default_para_style = sid
                elif d.kind == "table":
                    self.default_table_style = sid

    def chain(self, style_id: str | None) -> list[StyleDef]:
        """기본 스타일부터 style_id까지 상속 순서대로 (먼저 나온 것이 바깥)."""
        out: list[StyleDef] = []
        seen: set[str] = set()
        while style_id and style_id in self.styles and style_id not in seen:
            seen.add(style_id)
            st = self.styles[style_id]
            out.append(st)
            style_id = st.based_on
        return list(reversed(out))

    def paragraph_props(self, style_id: str | None) -> tuple[dict, dict]:
        """문단 스타일의 (pPr, rPr) 누적값. 돌려받은 dict는 수정하지 말 것 (캐시됨)."""
        key = ("p", style_id)
        if key not in self._cache:
            self._cache[key] = self._paragraph_props(style_id)
        return self._cache[key]

    def _paragraph_props(self, style_id: str | None) -> tuple[dict, dict]:
        style_id = style_id or self.default_para_style
        ppr, rpr = dict(self.default_ppr), dict(self.default_rpr)
        for st in self.chain(style_id):
            ppr.update(st.ppr)
            rpr.update(st.rpr)
        return ppr, rpr

    def run_style_props(self, style_id: str | None) -> dict:
        if not style_id:
            return {}
        key = ("r", style_id)
        if key not in self._cache:
            self._cache[key] = self._run_style_props(style_id)
        return self._cache[key]

    def _run_style_props(self, style_id: str | None) -> dict:
        rpr: dict = {}
        for st in self.chain(style_id):
            rpr.update(st.rpr)
        return rpr

    def table_props(self, style_id: str | None) -> tuple[dict, dict]:
        borders: dict = {}
        margins: dict = {}
        for st in self.chain(style_id or self.default_table_style):
            borders.update(st.table_borders)
            margins.update(st.table_cell_margins)
        return borders, margins

    def table_text_props(self, style_id: str | None) -> tuple[dict, dict]:
        """표 스타일에 정의된 셀 안 문단·글자 서식 (예: Table Grid는 문단 뒤 간격 0)."""
        ppr: dict = {}
        rpr: dict = {}
        for st in self.chain(style_id or self.default_table_style):
            ppr.update(st.ppr)
            rpr.update(st.rpr)
        return ppr, rpr

    def is_default_paragraph_style(self, style_id: str | None) -> bool:
        return style_id is None or style_id == self.default_para_style

    def heading_level(self, style_id: str | None, ppr: dict) -> int | None:
        """제목 수준. 스타일 이름(Heading 1, 제목 1, Title)이나 개요 수준으로 판단한다."""
        for st in reversed(self.chain(style_id)):
            m = _HEADING_NAME.match(st.name.strip())
            if m:
                return min(int(m.group(1)), 6)
            if st.name.strip().lower() in ("title", "제목"):
                return 1
        lvl = ppr.get("_outline")
        if lvl is not None and 0 <= lvl < 9:
            return min(lvl + 1, 6)
        return None

    def name(self, style_id: str | None) -> str | None:
        st = self.styles.get(style_id or "")
        return st.name if st else None


def cell_margins(el) -> dict:
    if el is None:
        return {}
    out = {}
    for side in ("top", "bottom", "left", "right", "start", "end"):
        m = child(el, f"w:{side}")
        if m is not None and wattr(m, "type", "dxa") == "dxa":
            key = {"start": "left", "end": "right"}.get(side, side)
            out[key] = twips_to_pt(wattr(m, "w"))
    return out


def to_text_style(props: dict) -> TextStyle | None:
    fields = {k: v for k, v in props.items() if not k.startswith("_") and v is not None}
    return TextStyle(**fields) if fields else None


def to_paragraph_style(props: dict) -> ParagraphStyle | None:
    fields = {k: v for k, v in props.items() if not k.startswith("_") and v is not None}
    if "borders" in fields:
        fields["borders"] = to_borders(fields["borders"])
        if fields["borders"] is None:
            del fields["borders"]
    return ParagraphStyle(**fields) if fields else None


def to_borders(d: dict) -> Borders | None:
    keys = {k: d[k] for k in ("top", "bottom", "left", "right") if d.get(k) is not None}
    return Borders(**keys) if keys else None
