"""IR에 한국어 처리 레이어를 적용한다.

- 모든 텍스트 조각에 문자 정규화 (NFC, 특수 공백, 가운뎃점, 전각)
- 자동 번호가 없는 문단에서 개조식 기호·직접 입력한 번호를 찾아 계층(OutlineInfo)을 붙인다
- (선택) 띄어쓰기 교정: PDF·OCR처럼 원문 텍스트를 믿을 수 없는 경우에만 쓴다.
  DOCX/HWPX는 원문 텍스트가 정확하므로 기본으로 끈다.
"""

from __future__ import annotations

from .ir import Document, Heading, OutlineInfo, Paragraph, Text, _walk
from .normalize import normalize_chars
from .outline import OutlineTracker


def enrich_korean(doc: Document, outline: bool = True) -> Document:
    for block in list(doc.iter_blocks()) + list(_iter_extra(doc)):
        inlines = getattr(block, "inlines", None)
        if not inlines:
            continue
        for i, item in enumerate(inlines):
            if isinstance(item, Text):
                new = normalize_chars(item.text)
                if new != item.text:
                    inlines[i] = item.model_copy(update={"text": new})

    if outline:
        _annotate_outline(doc.blocks)
    return doc


def _iter_extra(doc: Document):
    yield from _walk(doc.header)
    yield from _walk(doc.footer)
    for note in doc.notes.values():
        yield from _walk(note.blocks)


def _annotate_outline(blocks) -> None:
    """본문 최상위 문단 흐름에서 개조식 계층을 찾는다. 제목이 나오면 계층을 새로 시작한다."""
    tracker = OutlineTracker()
    for b in blocks:
        if isinstance(b, Heading):
            tracker.reset()
            continue
        if not isinstance(b, Paragraph) or b.list_info is not None:
            continue
        found = tracker.feed(b.text.strip())
        if found:
            level, kind, marker = found
            b.outline = OutlineInfo(level=level, kind=kind, marker=marker)
