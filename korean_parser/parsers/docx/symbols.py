"""Symbol·Wingdings 글꼴 문자 → 유니코드.

Word는 기호 글꼴의 문자를 글꼴 고유 코드(U+F020~U+F0FF)로 저장한다. 그 글꼴이 없는 브라우저에서는
깨져 보이므로, 자주 쓰이는 기호를 일반 유니코드로 바꾼다. 공문서의 □ ○ ▪ ➢ ✓ 등이 여기에 해당한다.
"""

from __future__ import annotations

# Adobe Symbol 인코딩 (자주 쓰는 것)
_SYMBOL = {
    0xB7: "•", 0xB4: "×", 0xB8: "÷", 0xB1: "±", 0xA3: "≤", 0xB3: "≥", 0xB9: "≠", 0xBB: "≈",
    0xA5: "∞", 0xD6: "√", 0xE5: "∑", 0xF2: "∫", 0xB0: "°", 0xAE: "→", 0xAC: "←", 0xAD: "↑",
    0xAF: "↓", 0xAB: "↔", 0xDE: "⇒", 0xDB: "⇔", 0xD0: "∠", 0xA2: "′", 0xB2: "″", 0xBE: "—",
    0xBC: "…", 0xC6: "∅", 0xCE: "∈", 0xC7: "∩", 0xC8: "∪", 0xA0: "€", 0x22: "∀", 0x24: "∃",
}
_GREEK_UPPER = "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"
_GREEK_LOWER = "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"
for _i, _c in enumerate(_GREEK_UPPER):
    _SYMBOL[0x41 + _i] = _c
for _i, _c in enumerate(_GREEK_LOWER):
    _SYMBOL[0x61 + _i] = _c

_WINGDINGS = {
    0x6C: "●", 0x6E: "■", 0x6F: "□", 0x70: "◻", 0x71: "❑", 0x72: "❒", 0x73: "⬧", 0x74: "⧫",
    0x75: "◆", 0x76: "❖", 0x77: "⬥", 0xA1: "○", 0xA2: "◯", 0xA4: "◉", 0xA7: "▪", 0xA8: "◻",
    0x9F: "•", 0xD8: "➢", 0xDF: "←", 0xE0: "→", 0xE1: "↑", 0xE2: "↓", 0xE8: "➔", 0xF0: "⇨",
    0xFB: "✗", 0xFC: "✓", 0xFD: "☒", 0xFE: "☑", 0x4A: "☺", 0x4C: "☹", 0x46: "☞", 0x2A: "✉",
    0x28: "☎", 0x3F: "✍", 0x4E: "☠", 0xAB: "★", 0xB2: "⬩", 0x81: "①", 0x82: "②", 0x83: "③",
    0x84: "④", 0x85: "⑤", 0x86: "⑥", 0x87: "⑦", 0x88: "⑧", 0x89: "⑨", 0x8A: "⑩",
}


def map_symbol(font: str, ch: str) -> str:
    cp = ord(ch)
    if not 0xF000 <= cp <= 0xF0FF:
        return ch
    code = cp - 0xF000
    name = font.lower()
    if "wingdings" in name or "webdings" in name:
        return _WINGDINGS.get(code, "•")
    if "symbol" in name:
        if 0x80 <= code <= 0x9F:
            return "•"  # Symbol 글꼴의 미정의 영역은 대부분 글머리표로 쓰인다
        return _SYMBOL.get(code, chr(code) if 0x20 <= code < 0x7F else "•")
    return chr(code) if 0x20 <= code < 0x7F else "•"
