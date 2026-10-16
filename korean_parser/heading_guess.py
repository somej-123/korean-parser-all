"""제목처럼 쓰인 번호 문단 찾기 (추정 제목).

DOCX·HWPX·HWP 문서는 제목 스타일·개요 번호 대신 '굵고 큰 번호 문단'으로 제목을 쓰는 경우가 많다
("Ⅰ. 사업 개요"를 18pt 굵게). 원본에는 제목이라는 정보가 없으므로 문서 모델에서 Heading으로 바꾸지 않고,
문단은 그대로 둔 채 `Paragraph.heading_guess`(추정 단계와 근거)만 붙인다. 쓰는 쪽이 쓸지 말지 고른다.

기준 (모두 만족):
- 번호·기호로 시작한다: 자동 번호(list_info), 직접 입력한 개조식(outline), 또는 제목 상자 안의 번호
- 표지를 뺀 글이 2~40자다
- 문장으로 끝나지 않는다 ('.' ':' ',' 또는 '~다', '~음', '~함' …)
- 목차 줄이 아니다 ('Ⅰ. 개요 ···· 3')
- 번호(Ⅰ. 1. 가. 1) ① …)면 본문보다 굵거나 크다. 기호(□ ○ ◦ ㅇ - · •, 자동 글머리표, 기호 글꼴 문자)면 본문보다
  크다 (굵기만으로는 안 된다: 강조하려고 굵게 쓴 개조식 항목이 흔하다). ※(참고)는 제목으로 보지 않는다.
  본문 = 최상위 문단 글자 크기의 중앙값(글자 수 가중). 번호는 1.05배, 기호는 1.1배 이상이고 0.9pt 넘게 크면 크다.
- ':'가 없다 ('사업기간 : 7개월'처럼 항목: 값을 적은 줄은 제목이 아니다)

단계는 바로 앞 Heading의 단계에, 그 아래에서 추정 제목의 표지 종류가 처음 나온 순서(Ⅰ. → 1, 1. → 2 …)를 더한
값이다 (1~6). 청크 도구(chunk.py)도 같은 판단(제목인지 아닌지)으로 조각의 제목 경로를 만든다.

실제 제안요청서(HWP)에서 확인한 것: 굵게 쓴 '◦ 가상 환경 기반 합성 데이터 구축', '- 청렴옴부즈만 …'이 기호만으로
제목이 되었다 → 기호는 큰 글씨일 때만.
"""

from __future__ import annotations

import re
import statistics

from .ir import Document, Drawing, Heading, HeadingGuess, PageBreak, Paragraph, Table, Text
from .outline import match_marker

_BULLET_KINDS = {"square", "circle", "dash", "dot"}  # 기호: 큰 글씨일 때만 제목
_NEVER_KINDS = {"note"}  # ※ 참고
_TOC_LINE = re.compile(r"(\t|\.{3,}|·{3,}|…|-{3,})\s*[\d#ivxIVX\-~ ]*\s*$")  # 목차 줄: "Ⅰ. 개요 ···· 3"
_TITLE_TAIL = re.compile(r"([.。:;,]|[다음함임됨]\.?)$")  # 문장으로 끝나는 문단은 제목이 아니다


def body_size(doc: Document) -> float | None:
    """본문 글자 크기 (최상위 문단 글자 크기의 중앙값, 글자 수 가중)."""
    sizes = []
    for b in doc.blocks:
        if isinstance(b, Paragraph):
            for i in b.inlines:
                if isinstance(i, Text) and i.style is not None and i.style.size_pt:
                    sizes += [i.style.size_pt] * min(len(i.text.strip()), 200)
    if sizes:
        return statistics.median(sizes)
    return doc.default_text_style.size_pt


def title_text(b) -> str:
    """번호(자동 번호 표지 포함)를 붙인 한 줄 글."""
    marker = f"{b.list_info.marker} " if getattr(b, "list_info", None) else ""
    return " ".join((marker + b.text).split())


class TitleFinder:
    """본문 블록 흐름을 차례로 넣으면 제목처럼 쓰인 번호 문단의 (단계, 근거)를 알려 준다.

    Heading을 만나면 그 단계를 기준으로 삼고 개조식 표지 기억을 지운다. 본문 개조식에서 본 표지 종류와 단계를
    기억해 두었다가, 제목 상자(한 칸 표) 안의 번호 문단('Ⅱ. 시스템 현황')에 같은 단계를 쓴다.
    """

    def __init__(self, doc: Document):
        self.body_size = body_size(doc)
        self.heading_level = 0
        self.kind_level: dict[str, int] = {}

    def heading(self, h: Heading) -> None:
        self.heading_level = h.level
        self.kind_level.clear()

    def paragraph(self, p: Paragraph) -> tuple[int, str, str] | None:
        """제목처럼 쓰인 번호 문단이면 (개조식 단계 0부터, 근거, 표지 종류), 아니면 None."""
        if p.outline is not None:
            self.kind_level.setdefault(p.outline.kind, p.outline.level)
        body = title_text(p)
        m = match_marker(body)
        if p.outline is not None:
            level, marker, kind = p.outline.level, p.outline.marker, p.outline.kind
        elif p.list_info is not None:
            level, marker = p.list_info.level, p.list_info.marker
            kind = f"list:{p.list_info.list_id}:{p.list_info.level}" if p.list_info.ordered else "dot"
        elif m and m[0] in self.kind_level:  # 제목 상자(한 칸 표) 안의 "Ⅱ. 시스템 현황" 등
            level, marker, kind = self.kind_level[m[0]], m[1], m[0]
        else:
            return None
        if kind in _NEVER_KINDS:
            return None
        if not marker.strip() or "" <= body[:1] <= "":
            kind = "dot"  # 보이는 번호가 없거나 기호 글꼴 문자(사용자 정의 영역)로 시작: 글머리 기호로 본다
        if _TOC_LINE.search(p.text):
            return None
        rest = m[2] if m else body
        if not (2 <= len(rest) <= 40) or _TITLE_TAIL.search(rest) or ":" in rest:
            return None  # 문장, 긴 글, '사업기간 : 7개월' 같은 항목: 값 줄은 제목이 아니다
        styles = [i.style for i in p.inlines if isinstance(i, Text) and i.text.strip()]
        bold = any(s is not None and s.bold for s in styles)
        sizes = [s.size_pt for s in styles if s is not None and s.size_pt]
        # 번호: 본문보다 1.05배 이상이고 0.9pt 넘게 크면 '크다' (본문 13pt에 14pt 절 제목을 쓰는 문서가 흔하다).
        # 기호: 1.1배 이상 (본문 14pt에 개조식 항목을 15pt로 쓰는 문서가 있다)
        ratio = 1.1 if kind in _BULLET_KINDS else 1.05
        bigger = bool(sizes and self.body_size and max(sizes) >= max(self.body_size * ratio, self.body_size + 0.9))
        if not (bigger if kind in _BULLET_KINDS else bold or bigger):
            return None
        why = [f"{'기호' if kind in _BULLET_KINDS else '번호'} '{marker}'"]
        if bold:
            why.append("굵게")
        if bigger:
            why.append(f"{max(sizes):g}pt (본문 {self.body_size:g}pt)")
        return level, " + ".join(why), kind


def stream(blocks, llm):
    """본문 블록 흐름. 글로 풀어 쓰는 표(제목 상자, 배치용 틀)와 글상자 하나뿐인 그리기 개체는 안쪽 블록을 꺼낸다.

    llm: LlmRenderer (풀어 쓸 표·그리기 개체인지 판단). 청크 도구와 같은 흐름을 쓰기 위해 받는다.
    """
    for b in blocks:
        if isinstance(b, Table) and (flat := llm.flattened_cells(b)) is not None:
            for c in flat:
                yield from stream(c.blocks, llm)
        elif isinstance(b, Drawing) and (frame := llm.text_frame_blocks(b)) is not None:
            yield from stream(frame, llm)
        else:
            yield b


def annotate_heading_guesses(doc: Document) -> None:
    """제목처럼 쓰인 번호 문단에 heading_guess를 붙인다 (문단은 그대로)."""
    from .render.llm import LlmRenderer

    finder = TitleFinder(doc)
    ranks: dict[str, int] = {}  # 바로 앞 Heading 아래에서 추정 제목의 표지 종류 → 처음 나온 순서
    for b in stream(doc.blocks, LlmRenderer(doc, with_ids=False)):
        if isinstance(b, PageBreak):
            continue
        if isinstance(b, Heading):
            finder.heading(b)
            ranks.clear()
        elif isinstance(b, Paragraph):
            found = finder.paragraph(b)
            if found is not None:
                _, why, kind = found
                rank = ranks.setdefault(kind, len(ranks))
                b.heading_guess = HeadingGuess(level=min(finder.heading_level + rank + 1, 6), reason=why)
