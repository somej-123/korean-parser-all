"""구조 기반 청크 도구.

임베딩·검색(RAG)용으로 문서를 조각낸다. 글자 수로 자르지 않고 문서 구조를 따른다.

- 제목 경로: 제목 블록과, 제목처럼 쓰인 번호 문단(Ⅰ. 사업 개요 / 1. 제안개요 / □ 추진 배경)으로
  "Ⅰ. 사업 개요 > 1. 제안개요" 같은 경로를 만든다. 한글 문서는 제목 스타일 대신 직접 입력한 번호에
  굵고 큰 글씨를 쓰는 경우가 많기 때문이다.
- 장(맨 위 단계)이 바뀌면 반드시 끊고, 작은 절은 같은 장 안에서 이웃과 합친다.
- 표는 통째로 둔다. 너무 큰 표만 병합 셀이 걸치지 않는 행 경계에서 나누고, 머리 행을 조각마다 반복한다.
- 너무 긴 문단은 문장 단위로 나눈다.
- 조각마다 블록 id, 쪽 번호, 좌표가 붙는다. HTML의 data-kp-id와 같으므로 검색 결과를 화면에서 바로 강조할 수 있다.

조각의 크기를 정하는 것은 쓰는 쪽(임베딩 모델, 검색 방식)의 몫이다. max_tokens와 토큰 세는 함수를 넘겨 맞춘다.

    from korean_parser.chunk import chunk
    for c in chunk(doc, max_tokens=500):
        index(c.contextual_text(), metadata=c.metadata())
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, computed_field

from .ir import Block, Document, Drawing, Heading, NoteRef, PageBreak, Paragraph, Table, Text, _walk
from .outline import match_marker
from .render.llm import LlmRenderer

TokenCounter = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """토큰 수 어림값. 한글 음절은 1토큰, 그 밖의 글자는 3.5자당 1토큰으로 센다.

    실제 토크나이저마다 다르므로(한글 음절당 0.6~1.5토큰), 정확히 맞추려면 count_tokens를 넘긴다.
    """
    hangul = sum(1 for ch in text if "가" <= ch <= "힣")
    other = sum(1 for ch in text if not ch.isspace()) - hangul
    return math.ceil(hangul + other / 3.5)


class ChunkSource(BaseModel):
    """조각에 들어간 원문 블록 하나. 쪽·좌표는 PDF처럼 쪽 배치가 있는 형식에만 있다."""

    block_id: str
    kind: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    part: str | None = None  # 블록 하나를 나눴을 때 "2/3" (표의 행 묶음, 긴 문단의 문장 묶음)


class Chunk(BaseModel):
    id: str
    text: str  # LLM 텍스트 형식 (병합 표는 HTML, 도식은 연결 관계)
    heading_path: list[str] = Field(default_factory=list)
    tokens: int = 0
    sources: list[ChunkSource] = Field(default_factory=list)
    doc: dict = Field(default_factory=dict)  # 문서 메타데이터 (제목, 파일 이름, 형식 ...)

    @computed_field
    @property
    def block_ids(self) -> list[str]:
        return list(dict.fromkeys(s.block_id for s in self.sources))

    @computed_field
    @property
    def pages(self) -> list[int]:
        return sorted({s.page for s in self.sources if s.page is not None})

    def contextual_text(self) -> str:
        """임베딩용: 제목 경로를 앞에 붙인 글. 조각만 떼어 봐도 어느 장·절의 내용인지 알 수 있다."""
        path = self.heading_path
        if path and self.text.lstrip().lstrip("#").lstrip().startswith(path[-1]):
            path = path[:-1]  # 조각이 그 제목으로 시작하면 경로에서 겹치는 마지막 제목은 뺀다
        if not path:
            return self.text
        return "[" + " > ".join(path) + "]\n" + self.text

    def metadata(self) -> dict:
        """벡터 DB 등에 함께 저장할 평평한 메타데이터."""
        return {
            "chunk_id": self.id, "heading_path": self.heading_path, "block_ids": self.block_ids,
            "pages": self.pages, "kinds": sorted({s.kind for s in self.sources}), "tokens": self.tokens,
            **{f"doc_{k}": v for k, v in self.doc.items()},
        }


def chunk(
    doc: Document,
    max_tokens: int = 500,
    min_tokens: int | None = None,
    count_tokens: TokenCounter | None = None,
    include_notes: bool = True,
) -> list[Chunk]:
    """문서를 구조 단위 조각으로 나눈다.

    max_tokens: 조각 하나의 상한 (나눌 수 없는 큰 그림·병합 표 묶음만 넘을 수 있다)
    min_tokens: 이보다 작은 조각은 같은 장 안의 다음 절과 합친다 (기본 max_tokens의 1/4)
    count_tokens: 토큰 세는 함수 (기본은 어림값 estimate_tokens)
    include_notes: 조각 안에서 참조한 각주 내용을 조각 끝에 붙인다
    """
    return _Chunker(doc, max_tokens, min_tokens, count_tokens or estimate_tokens, include_notes).run()


# ---------------------------------------------------------------------------


@dataclass
class _Unit:
    text: str
    path: tuple[str, ...]
    sources: list[ChunkSource]
    tokens: int
    note_ids: list[str] = field(default_factory=list)
    starts_section: bool = False


_SENTENCE_END = re.compile(r"(?<=[.?!。])\s+")
_TOC_LINE = re.compile(r"(\t|\.{3,}|·{3,}|…|-{3,})\s*[\d#ivxIVX\-~ ]*\s*$")  # 목차 줄: "Ⅰ. 개요 ···· 3"
_TITLE_TAIL = re.compile(r"([.。:;,]|[다음함임됨]\.?)$")  # 문장으로 끝나는 문단은 제목이 아니다


class _Chunker:
    def __init__(self, doc: Document, max_tokens: int, min_tokens: int | None, count: TokenCounter,
                 include_notes: bool):
        self.doc = doc
        self.max = max_tokens
        self.min = max_tokens // 4 if min_tokens is None else min_tokens
        self.count = count
        self.include_notes = include_notes
        self.llm = LlmRenderer(doc, with_ids=False)
        self.body_size = _body_size(doc)
        self.meta = {k: v for k, v in doc.meta.model_dump().items() if v is not None}

    # -- 블록 → 단위 --------------------------------------------------------

    def units(self) -> list[_Unit]:
        out: list[_Unit] = []
        stack: list[tuple[tuple[int, int], str]] = []  # (단계, 제목)
        heading_level = 0
        self.kind_level: dict[str, int] = {}  # 본문 개조식에서 본 표지 종류 → 단계 (상자 안 제목에 쓴다)
        for b in self._stream(self.doc.blocks):
            if isinstance(b, PageBreak):
                continue
            title_rank = None
            if isinstance(b, Heading):
                heading_level = b.level
                self.kind_level.clear()
                title_rank = (b.level, -1)
            elif isinstance(b, Paragraph):
                if b.outline is not None:
                    self.kind_level.setdefault(b.outline.kind, b.outline.level)
                lvl = self._title_level(b)
                if lvl is not None:
                    title_rank = (heading_level, lvl)
            if title_rank is not None:
                while stack and stack[-1][0] >= title_rank:
                    stack.pop()
                stack.append((title_rank, _title_text(b)))

            path = tuple(t for _, t in stack)
            for text, part in self._render(b):
                if not text.strip():
                    continue
                src = ChunkSource(block_id=b.id, kind=b.type, page=b.prov.page if b.prov else None,
                                  bbox=b.prov.bbox if b.prov else None, part=part)
                notes = _note_ids(b) if self.include_notes else []
                tokens = self.count(text) + sum(self._note_tokens(n) for n in notes)  # 조각 끝에 붙일 각주 몫
                out.append(_Unit(text, path, [src], tokens, notes,
                                 starts_section=title_rank is not None and part in (None, "1/1")))
        return out

    def _note_tokens(self, note_id: str) -> int:
        note = self.doc.notes.get(note_id)
        return self.count(f"[^{note.label}]: {self.llm._plain(note.blocks)}") if note else 0

    def _stream(self, blocks):
        """본문 블록 흐름. 글로 풀어 쓰는 표(제목 상자, 배치용 틀)와 글상자 하나뿐인 그리기 개체는
        안쪽 블록을 본문처럼 꺼낸다."""
        for b in blocks:
            if isinstance(b, Table) and (flat := self.llm.flattened_cells(b)) is not None:
                for c in flat:
                    yield from self._stream(c.blocks)
            elif isinstance(b, Drawing) and (frame := self.llm.text_frame_blocks(b)) is not None:
                yield from self._stream(frame)
            else:
                yield b

    def _render(self, b: Block) -> list[tuple[str, str | None]]:
        """블록 하나의 글. 상한을 넘는 표와 문단만 여러 조각으로 나눈다."""
        text = self.llm.block(b)
        if self.count(text) <= self.max:
            return [(text, None)]
        if isinstance(b, Table):
            parts = self._split_table(b)
        elif isinstance(b, (Paragraph, Heading)):
            parts = self._split_sentences(text)
        else:  # 그림·도식·차트는 나누지 않는다
            return [(text, None)]
        if len(parts) <= 1:
            return [(text, None)]
        return [(p, f"{i}/{len(parts)}") for i, p in enumerate(parts, 1)]

    def _split_table(self, t: Table) -> list[str]:
        """큰 표를 행 묶음으로 나눈다. 머리 행은 조각마다 반복하고, 경계에 걸친 병합 칸(세로 병합)은
        다음 조각에도 복사해 넣는다 (예: '평가부문' 칸이 조각마다 남아 문맥이 끊기지 않는다)."""
        grid = _TableGrid(t)
        rows = t.rows
        n_head = 0
        while n_head < len(rows) and rows[n_head].header:
            n_head += 1
        if n_head == 0 and len(rows) > 2 and all(c.row_span == 1 for c in rows[0].cells):
            n_head = 1  # 머리 행 표시가 없으면 첫 행을 머리로 본다 (한국 문서 표는 대부분 첫 행이 머리)
        ranges, a = [], n_head
        for i in range(n_head + 1, len(rows) + 1):
            # 행을 하나씩 더해 보고 상한을 넘으면 그 앞에서 끊는다
            if i - a > 1 and self.count(self.llm._table(grid.slice(a, i, head=n_head))) > self.max:
                ranges.append((a, i - 1))
                a = i - 1
        if a < len(rows):
            ranges.append((a, len(rows)))

        out = []
        for a, b in ranges:
            piece = self.llm._table(grid.slice(a, b, head=n_head))
            if b - a == 1 and self.count(piece) > self.max:
                # 행 하나가 상한을 넘으면(칸 안에 긴 글·표가 든 양식) 칸 내용을 글로 풀어 나눈다
                out += self._pack([self.llm.blocks(c.blocks) for c in rows[a].cells])
            else:
                out.append(piece)
        return out

    def _pack(self, texts: list[str]) -> list[str]:
        """글 목록을 상한 안에서 묶는다. 긴 글은 문장 단위로 나눈다."""
        pieces = []
        for x in texts:
            if x.strip():
                pieces += self._split_sentences(x) if self.count(x) > self.max else [x]
        out, cur = [], ""
        for x in pieces:
            cand = f"{cur}\n\n{x}" if cur else x
            if cur and self.count(cand) > self.max:
                out.append(cur)
                cand = x
            cur = cand
        if cur:
            out.append(cur)
        return out

    def _split_sentences(self, text: str) -> list[str]:
        """문장 단위로 묶어 나눈다. 문장 하나가 상한을 넘으면 어절 단위로 나눈다."""
        indent = text[: len(text) - len(text.lstrip())]
        pieces = []
        for sent in _SENTENCE_END.split(text.strip()):
            if not sent.strip():
                continue
            pieces += sent.split() if self.count(sent) > self.max else [sent.strip()]
        out, cur = [], ""
        for s in pieces:
            cand = f"{cur} {s}".strip()
            if cur and self.count(cand) > self.max:
                out.append(indent + cur)
                cand = s
            cur = cand
        if cur:
            out.append(indent + cur)
        return out

    def _title_level(self, p: Paragraph) -> int | None:
        """제목처럼 쓰인 번호 문단이면 그 단계, 아니면 None.

        번호·기호로 시작하고, 짧고, 문장으로 끝나지 않고, 본문보다 굵거나 큰 글씨인 문단.
        """
        body = _title_text(p)
        m = match_marker(body)
        if p.outline is not None:
            level = p.outline.level
        elif p.list_info is not None:
            level = p.list_info.level
        elif m and m[0] in self.kind_level:  # 제목 상자(한 칸 표) 안의 "Ⅱ. 시스템 현황" 등
            level = self.kind_level[m[0]]
        else:
            return None
        if _TOC_LINE.search(p.text):
            return None
        rest = m[2] if m else body
        if not (2 <= len(rest) <= 40) or _TITLE_TAIL.search(rest):
            return None
        styles = [i.style for i in p.inlines if isinstance(i, Text) and i.text.strip()]
        bold = any(s is not None and s.bold for s in styles)
        sizes = [s.size_pt for s in styles if s is not None and s.size_pt]
        bigger = bool(sizes and self.body_size and max(sizes) >= self.body_size * 1.1)
        return level if (bold or bigger) else None

    # -- 단위 → 조각 --------------------------------------------------------

    def run(self) -> list[Chunk]:
        chunks: list[Chunk] = []
        cur: list[_Unit] = []

        def flush():
            if cur:
                chunks.append(self._make(len(chunks) + 1, cur))
                cur.clear()

        for u in self.units():
            if cur:
                size = sum(x.tokens for x in cur)
                if size + u.tokens > self.max:
                    flush()
                elif u.starts_section:
                    # 새 절은 원칙적으로 새 조각. 지금 조각이 작고, 합쳐도 같은 장 안에 있을 때만 합친다
                    # (앞부분·목차와 첫 장, 서로 다른 장은 합치지 않는다)
                    same_chapter = bool(_common_prefix([x.path for x in cur] + [u.path]))
                    if not (size < self.min and same_chapter):
                        flush()
            cur.append(u)
        flush()
        return chunks

    def _make(self, n: int, units: list[_Unit]) -> Chunk:
        text = "\n\n".join(u.text for u in units)
        if self.include_notes:
            seen = list(dict.fromkeys(nid for u in units for nid in u.note_ids))
            notes = [self.doc.notes[nid] for nid in seen if nid in self.doc.notes]
            if notes:
                text += "\n\n" + "\n".join(f"[^{nt.label}]: {self.llm._plain(nt.blocks)}" for nt in notes)
        return Chunk(
            id=f"c{n}", text=text, heading_path=list(_common_prefix([u.path for u in units])),
            tokens=self.count(text), sources=[s for u in units for s in u.sources], doc=self.meta,
        )


# ---------------------------------------------------------------------------


class _TableGrid:
    """병합 칸의 실제 위치(행, 열)를 계산해 두고, 행 범위로 잘라낸 표를 만든다."""

    def __init__(self, t: Table):
        self.t = t
        self.placed: list[tuple[int, int, object]] = []  # (행, 열, 칸)
        occupied: set[tuple[int, int]] = set()
        for r, row in enumerate(t.rows):
            c = 0
            for cell in row.cells:
                while (r, c) in occupied:
                    c += 1
                self.placed.append((r, c, cell))
                for rr in range(r, r + max(cell.row_span, 1)):
                    for cc in range(c, c + max(cell.col_span, 1)):
                        occupied.add((rr, cc))
                c += max(cell.col_span, 1)

    def _rows(self, a: int, b: int) -> list:
        by_row: dict[int, list[tuple[int, object]]] = {r: [] for r in range(a, b)}
        for r, c, cell in self.placed:
            end = r + max(cell.row_span, 1)
            if a <= r < b:
                by_row[r].append((c, cell.model_copy(update={"row_span": min(end, b) - r})))
            elif r < a < end:  # 위 조각에서 이어지는 세로 병합 칸
                by_row[a].append((c, cell.model_copy(update={"row_span": min(end, b) - a})))
        return [self.t.rows[r].model_copy(update={"cells": [cell for _, cell in sorted(by_row[r], key=lambda x: x[0])]})
                for r in range(a, b)]

    def slice(self, a: int, b: int, head: int = 0) -> Table:
        rows = (self._rows(0, head) if head else []) + self._rows(a, b)
        return self.t.model_copy(update={"rows": rows})


def _title_text(b) -> str:
    marker = f"{b.list_info.marker} " if getattr(b, "list_info", None) else ""
    return " ".join((marker + b.text).split())


def _note_ids(b: Block) -> list[str]:
    out = []
    for x in _walk([b]):
        for i in getattr(x, "inlines", None) or []:
            if isinstance(i, NoteRef):
                out.append(i.note_id)
    return out


def _common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    first = paths[0]
    n = len(first)
    for p in paths[1:]:
        n = min(n, len(p))
        for i in range(n):
            if p[i] != first[i]:
                n = i
                break
    return first[:n]


def _body_size(doc: Document) -> float | None:
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


__all__ = ["Chunk", "ChunkSource", "chunk", "estimate_tokens"]
