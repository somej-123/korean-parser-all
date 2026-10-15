"""문자 단위 정규화.

파서마다, 문서를 만든 도구마다 같은 글자가 다른 코드로 들어오는 문제를 정리한다.

- NFD로 분리된 한글 자모를 NFC로 합친다 (맥에서 만든 파일에서 흔함).
- 보이지 않는 문자와 특수 공백을 정리한다.
- 가운뎃점 변형을 하나로 통일한다.
- 전각 영숫자를 반각으로 바꾼다. (①, ㎡ 같은 다른 호환 문자는 의미가 바뀌므로 건드리지 않는다.)
- 사용자 정의 영역(PUA) 문자를 찾아낸다. HWP 옛한글 등은 매핑표를 넘겨 변환할 수 있다.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

# 폭이 없는 문자: 복사·붙여넣기나 웹 문서에서 섞여 들어와 검색을 깨뜨린다.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)

# 일반 공백으로 바꿀 특수 공백 (NBSP, 전각 공백, 여러 폭의 공백 등)
_SPACES = dict.fromkeys(
    map(ord, "            　"),
    " ",
)

# 가운뎃점으로 쓰이는 비슷한 글자들 → U+00B7. 글머리표(•)는 의미가 달라 제외한다.
_MIDDLE_DOTS = dict.fromkeys(map(ord, "ㆍ∙⋅・･"), "·")

# 전각 ASCII(！~～) → 반각
_FULLWIDTH = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)}

_TABLE = {**_ZERO_WIDTH, **_SPACES, **_MIDDLE_DOTS, **_FULLWIDTH}

_MULTI_SPACE = re.compile(r"[ \t]{2,}")

PUA_RANGES = ((0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD))


def is_pua(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in PUA_RANGES)


def is_hangul_syllable(ch: str) -> bool:
    return "가" <= ch <= "힣"


def normalize_chars(text: str, pua_map: Mapping[str, str] | None = None) -> str:
    """글자만 바꾸고 공백 개수나 앞뒤 공백은 건드리지 않는다.

    문단이 여러 서식 조각(run)으로 나뉜 경우 조각 사이 공백을 지키기 위해 쓴다.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_TABLE)
    if pua_map:
        text = "".join(pua_map.get(ch, ch) for ch in text)
    return text


def normalize_text(text: str, pua_map: Mapping[str, str] | None = None) -> str:
    """문자 단위로 텍스트를 정규화한다. 줄바꿈은 유지한다."""
    text = normalize_chars(text, pua_map)
    text = _MULTI_SPACE.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n"))


@dataclass
class TextHealth:
    """텍스트가 정상적으로 추출됐는지 판단하기 위한 지표."""

    length: int
    hangul_ratio: float
    pua_count: int
    replacement_count: int
    control_count: int

    @property
    def garbled(self) -> bool:
        """글자 깨짐이 의심되면 True. PDF라면 OCR로 다시 처리할 후보다."""
        if self.length == 0:
            return False
        bad = self.pua_count + self.replacement_count + self.control_count
        return bad / self.length > 0.05


def inspect_text(text: str) -> TextHealth:
    chars = [c for c in text if not c.isspace()]
    n = len(chars)
    return TextHealth(
        length=n,
        hangul_ratio=(sum(is_hangul_syllable(c) for c in chars) / n) if n else 0.0,
        pua_count=sum(is_pua(c) for c in chars),
        replacement_count=chars.count("�"),
        control_count=sum(unicodedata.category(c) == "Cc" for c in chars),
    )
