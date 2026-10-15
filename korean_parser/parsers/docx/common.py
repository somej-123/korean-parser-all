"""DOCX(OOXML) 파싱 공통: 네임스페이스, 단위 변환, 속성 읽기."""

from __future__ import annotations

from functools import lru_cache

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "v": "urn:schemas-microsoft-com:vml",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}


@lru_cache(maxsize=None)
def q(tag: str) -> str:
    """'w:p' → '{namespace}p'"""
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


W = NS["w"]


def wattr(el, name: str, default=None):
    """w: 네임스페이스 속성 읽기."""
    if el is None:
        return default
    return el.get(f"{{{W}}}{name}", default)


def child(el, tag: str):
    # iterchildren(tag)은 find()보다 빠르다 (대형 문서에서 수십만 번 호출됨)
    return None if el is None else next(el.iterchildren(q(tag)), None)


def on_off(el) -> bool | None:
    """<w:b/>, <w:b w:val="0"/> 같은 켜고 끄는 속성."""
    if el is None:
        return None
    val = wattr(el, "val")
    return val not in ("0", "false", "off", "none")


def twips_to_pt(value) -> float | None:
    try:
        return int(float(value)) / 20.0
    except (TypeError, ValueError):
        return None


def emu_to_pt(value) -> float | None:
    try:
        return int(value) / 12700.0
    except (TypeError, ValueError):
        return None


def hex_color(value: str | None) -> str | None:
    if not value or value.lower() in ("auto", "none"):
        return None
    value = value.strip().lstrip("#")
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value.upper()
    return None


# w:highlight 값 → 색
HIGHLIGHT_COLORS = {
    "yellow": "#FFFF00", "green": "#00FF00", "cyan": "#00FFFF", "magenta": "#FF00FF",
    "blue": "#0000FF", "red": "#FF0000", "darkBlue": "#000080", "darkCyan": "#008080",
    "darkGreen": "#008000", "darkMagenta": "#800080", "darkRed": "#800000",
    "darkYellow": "#808000", "darkGray": "#808080", "lightGray": "#C0C0C0", "black": "#000000",
    "white": "#FFFFFF",
}
