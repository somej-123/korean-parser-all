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

굵거나 크지 않아도 제목으로 보는 경우 (실제 문서 구조 정답, benchmarks/real_truth):
- 점이 여러 개인 십진 번호('2.2 가스기술기준', '5.2.1 수소 튜브'): 규격서·지침의 절 번호다. 번호 자체가 제목의 표시다.
- 번호 문단 바로 다음이 표('2. PM 주차구역 설치 현황' 다음에 표): 표 제목이다 (표지를 뺀 글 30자 이하).
사용자 정의 영역 글자(한컴 기호 글꼴)로 시작하는 문단은, 같은 문서에 바로 앞·뒤 글자로 시작하는 문단이 있으면
(❶ ❷ ❸처럼 차례로 이어지면) 번호로 본다. 보도자료 부록의 번호 제목이 이렇게 쓰여 후보에서 빠졌다.

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
_DECIMAL = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){1,3})\.?\s+(?=\D)")  # 2.2 / 5.2.1 / 6.7.10 (점이 여러 개인 절 번호)


def _private_use(ch: str) -> bool:
    cp = ord(ch) if ch else 0
    return 0xE000 <= cp <= 0xF8FF or cp >= 0xF0000


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
        from .ir import walk_blocks

        self.body_size = body_size(doc)
        self.heading_level = 0
        self.kind_level: dict[str, int] = {}
        # 사용자 정의 영역 글자 중 '번호'로 쓰인 것: 같은 문서에 바로 앞·뒤 글자로 시작하는 문단이 있다 (❶ ❷ ❸ …)
        firsts = {ord(b.text.lstrip()[0]) for b in walk_blocks(doc.blocks)
                  if isinstance(b, Paragraph) and b.text.strip() and _private_use(b.text.lstrip()[0])}
        self.pua_numbers = {c for c in firsts if c - 1 in firsts or c + 1 in firsts}

    def heading(self, h: Heading) -> None:
        self.heading_level = h.level
        self.kind_level.clear()

    def paragraph(self, p: Paragraph, before_table: bool = False) -> tuple[int, str, str] | None:
        """제목처럼 쓰인 번호 문단이면 (개조식 단계 0부터, 근거, 표지 종류), 아니면 None.

        before_table: 바로 다음 블록이 표다 (표 제목이면 굵거나 크지 않아도 제목으로 본다).
        """
        if p.outline is not None:
            self.kind_level.setdefault(p.outline.kind, p.outline.level)
        body = title_text(p)
        m = match_marker(body)
        if p.outline is not None:
            level, marker, kind = p.outline.level, p.outline.marker, p.outline.kind
        elif p.list_info is not None:
            level, marker = p.list_info.level, p.list_info.marker
            # 자동 번호도 보이는 번호 모양으로 종류를 정한다: 자동 번호 '1.'과 직접 친 '2.'가 같은 단계여야 한다
            # (동래구 현황: 첫 제목만 자동 번호라 다른 단계가 되었다)
            shown = match_marker(f"{marker} 가") if p.list_info.ordered else None
            if shown is not None:
                kind = shown[0]
            else:
                kind = f"list:{p.list_info.list_id}:{p.list_info.level}" if p.list_info.ordered else "dot"
        elif m and m[0] in self.kind_level:  # 제목 상자(한 칸 표) 안의 "Ⅱ. 시스템 현황" 등
            level, marker, kind = self.kind_level[m[0]], m[1], m[0]
        elif d := _DECIMAL.match(body):  # 2.2 가스기술기준
            marker = d.group(1)
            level, kind = marker.count("."), f"decimal{marker.count('.') + 1}"
            m = (kind, marker, body[d.end():].strip())
        elif body and ord(body[0]) in self.pua_numbers and body[1:2].isspace():  # 한컴 기호 글꼴의 ❶ ❷ …
            level, marker, kind = 0, body[0], "pua_number"
            m = (kind, marker, body[1:].strip())
        else:
            return None
        if kind in _NEVER_KINDS:
            return None
        if kind != "pua_number" and (not marker.strip() or _private_use(body[:1])):
            kind = "dot"  # 보이는 번호가 없거나 기호 글꼴 문자(사용자 정의 영역)로 시작: 글머리 기호로 본다
        if _TOC_LINE.search(p.text):
            return None
        rest = m[2] if m else body
        # 절 번호 줄은 영문 제목('2.4 ISO (International Organization for Standardization)')이 길어 60자까지 본다
        limit = 60 if kind.startswith("decimal") else 40
        if not (2 <= len(rest) <= limit) or _TITLE_TAIL.search(rest) or ":" in rest:
            return None  # 문장, 긴 글, '사업기간 : 7개월' 같은 항목: 값 줄은 제목이 아니다
        styles = [i.style for i in p.inlines if isinstance(i, Text) and i.text.strip()]
        bold = any(s is not None and s.bold for s in styles)
        sizes = [s.size_pt for s in styles if s is not None and s.size_pt]
        # 번호: 본문보다 1.05배 이상이고 0.9pt 넘게 크면 '크다' (본문 13pt에 14pt 절 제목을 쓰는 문서가 흔하다).
        # 기호: 1.1배 이상 (본문 14pt에 개조식 항목을 15pt로 쓰는 문서가 있다)
        ratio = 1.1 if kind in _BULLET_KINDS else 1.05
        bigger = bool(sizes and self.body_size and max(sizes) >= max(self.body_size * ratio, self.body_size + 0.9))
        decimal = kind.startswith("decimal")
        table_title = before_table and kind not in _BULLET_KINDS and len(rest) <= 30
        if not (bigger if kind in _BULLET_KINDS else bold or bigger or decimal or table_title):
            return None
        why = [f"{'기호' if kind in _BULLET_KINDS else '번호'} '{marker}'"]
        if decimal:
            why.append("절 번호")
        if table_title and not (bold or bigger or decimal):
            why.append("바로 아래 표")
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


def _guess_level(kind: str, heading_level: int, ranks: dict[str, int]) -> int:
    """추정 제목의 단계. 표지 종류가 처음 나온 순서를 바로 앞 Heading 단계에 더한다.

    절 번호(2.2, 6.6.1)는 처음 나온 순서가 아니라 점 개수로 정한다. '6.6'이 문장이라 제목이 아니면 6장 아래 첫 소제목이
    '6.6.1'인데, 순서로 매기면 6.6.1이 2단계, 뒤의 6.7이 3단계로 뒤집혔다 (가스공사 규격표준). '1.'(num_dot)이 같은 흐름에
    추정 제목으로 나왔으면 그 단계가 점 0개다.
    """
    if kind.startswith("decimal"):
        dots = int(kind[len("decimal"):]) - 1
        family = ranks.setdefault("decimal", len(ranks))
        if "num_dot" in ranks:
            return heading_level + ranks["num_dot"] + 1 + dots
        return heading_level + family + dots
    return heading_level + ranks.setdefault(kind, len(ranks)) + 1


def annotate_heading_guesses(doc: Document) -> None:
    """제목처럼 쓰인 번호 문단에 heading_guess를 붙인다 (문단은 그대로)."""
    from .render.llm import LlmRenderer

    finder = TitleFinder(doc)
    ranks: dict[str, int] = {}  # 바로 앞 Heading 아래에서 추정 제목의 표지 종류 → 처음 나온 순서
    flow = [b for b in stream(doc.blocks, LlmRenderer(doc, with_ids=False)) if not isinstance(b, PageBreak)]
    for k, b in enumerate(flow):
        if isinstance(b, Heading):
            finder.heading(b)
            ranks.clear()
        elif isinstance(b, Paragraph):
            found = finder.paragraph(b, before_table=k + 1 < len(flow) and isinstance(flow[k + 1], Table))
            if found is not None and b.heading_guess is None:  # 파서가 먼저 붙인 추정(슬라이드 제목 등)은 그대로
                _, why, kind = found
                b.heading_guess = HeadingGuess(level=min(_guess_level(kind, finder.heading_level, ranks), 6), reason=why)
