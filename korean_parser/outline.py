"""개조식·번호 체계 계층 인식.

한국 공문서와 보고서는 기호와 번호로 계층을 표현한다.

    □ 추진 배경                 1. 개요
      ○ 문서 파싱 품질 문제        가. 목적
        - 표 구조 손실               1) 세부 목표
        - 띄어쓰기 소실                가) 품질

어떤 기호가 몇 단계인지는 문서마다 다르므로 고정 순서를 쓰지 않는다.
문서에서 **처음 나타난 순서**대로 단계를 정하고, 이미 나온 기호가 다시 나오면
그 단계로 돌아간 것으로 본다. 사람이 문서를 읽을 때와 같은 방식이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HANGUL_SEQ = "가나다라마바사아자차카타파하"
_CIRCLED_NUM = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_CIRCLED_HANGUL = "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭"
_ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ"

# (종류, 정규식). 위에서부터 먼저 맞는 것을 쓴다. 표지 뒤에는 반드시 공백이 있어야 한다.
_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("roman", re.compile(rf"^([{_ROMAN}]|[IVX]{{1,4}})\.\s+")),
    ("num_dot", re.compile(r"^(\d{1,3})\.\s+")),
    ("hangul_dot", re.compile(rf"^([{_HANGUL_SEQ}])\.\s+")),
    ("num_paren", re.compile(r"^(\d{1,3})\)\s+")),
    ("hangul_paren", re.compile(rf"^([{_HANGUL_SEQ}])\)\s+")),
    ("num_wrap", re.compile(r"^\((\d{1,3})\)\s+")),
    ("hangul_wrap", re.compile(rf"^\(([{_HANGUL_SEQ}])\)\s+")),
    ("circled_num", re.compile(rf"^([{_CIRCLED_NUM}])\s*")),
    ("circled_hangul", re.compile(rf"^([{_CIRCLED_HANGUL}])\s*")),
    ("square", re.compile(r"^([□■◇◆▣])\s*")),
    ("circle", re.compile(r"^([○●◦◎])\s*")),
    ("dash", re.compile(r"^([-–—])\s+")),
    ("dot", re.compile(r"^([·•∙▪])\s*")),
    ("note", re.compile(r"^(※|\*)\s*")),
]


@dataclass
class OutlineItem:
    level: int | None  # 0부터 시작. 표지가 없는 일반 문단은 None
    kind: str | None  # 표지 종류 (num_dot, square, ...)
    marker: str  # 원문 표지 그대로 ("1.", "□", "가)")
    text: str  # 표지를 뗀 본문


def match_marker(line: str) -> tuple[str, str, str] | None:
    """줄 앞의 개조식/번호 표지를 찾는다. (종류, 표지, 나머지 본문)"""
    for kind, pattern in _MARKERS:
        m = pattern.match(line)
        if m and line[m.end() :].strip():
            return kind, line[: m.end()].strip(), line[m.end() :].strip()
    return None


class OutlineTracker:
    """문단을 순서대로 넣으면 개조식 계층을 계산한다.

    처음 보는 표지 종류는 한 단계 아래, 이미 나온 종류가 다시 나오면 그 단계로 돌아간 것으로 본다.
    ※(참고) 표지는 계층에 넣지 않고 바로 앞 항목보다 한 단계 아래로 둔다.
    """

    def __init__(self):
        self.stack: list[str] = []  # 현재 열려 있는 표지 종류들 (바깥 → 안쪽)
        self.last_level: int | None = None

    def reset(self) -> None:
        self.stack.clear()
        self.last_level = None

    def feed(self, line: str) -> tuple[int, str, str] | None:
        """(단계, 종류, 표지) 또는 표지가 없으면 None."""
        found = match_marker(line.strip())
        if found is None:
            return None
        kind, marker, _ = found
        if kind == "note":
            level = (self.last_level + 1) if self.last_level is not None else 0
            return level, kind, marker
        if kind in self.stack:
            del self.stack[self.stack.index(kind) + 1 :]  # 위 단계로 돌아감
        else:
            self.stack.append(kind)  # 처음 보는 표지 → 한 단계 아래
        self.last_level = len(self.stack) - 1
        return self.last_level, kind, marker


def detect_outline(lines: list[str]) -> list[OutlineItem]:
    """줄 목록에서 계층을 인식한다. 표지가 없는 줄은 바로 앞 항목에 이어 붙인다."""
    tracker = OutlineTracker()
    items: list[OutlineItem] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        found = tracker.feed(line)
        if found is None:
            if items and items[-1].kind is not None:
                items[-1].text += " " + line  # 앞 항목의 줄바꿈된 내용
            else:
                items.append(OutlineItem(None, None, "", line))
            continue
        level, kind, marker = found
        items.append(OutlineItem(level, kind, marker, match_marker(line)[2]))
    return items


def outline_to_markdown(items: list[OutlineItem], indent: str = "  ") -> str:
    """원래 표지를 살린 채 들여쓰기로 계층을 표현한다 (LLM 입력용)."""
    out = []
    for it in items:
        if it.level is None:
            out.append(it.text)
        else:
            out.append(f"{indent * it.level}- {it.marker} {it.text}")
    return "\n".join(out)
