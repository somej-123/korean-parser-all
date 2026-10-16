"""Docling 변환 결과(DoclingDocument)에 한국어 후처리를 적용한다.

    from docling.document_converter import DocumentConverter
    from korean_parser.docling_adapter import postprocess_docling

    doc = DocumentConverter().convert("문서.pdf").document
    report = postprocess_docling(doc)   # doc을 직접 수정한다
    print(report)
    doc.export_to_markdown()
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import inspect_text, normalize_text
from .spacing import SpacingFixer, spacing_lost, spacing_stats


@dataclass
class PostprocessReport:
    texts_changed: int
    cells_changed: int
    spacing_reset: bool  # 문서 전체에서 띄어쓰기 소실이 감지되어 재띄어쓰기를 했는지
    long_eojeol_ratio_before: float
    long_eojeol_ratio_after: float
    garbled: bool  # 글자 깨짐 의심 (PDF라면 OCR 재처리 후보)


def postprocess_docling(doc, fixer: SpacingFixer | None = None, respace: bool = True,
                        keep_spacing: set[int] = frozenset(), ocr: bool = False) -> PostprocessReport:
    """본문 텍스트와 표 셀에 정규화와 띄어쓰기 교정을 적용한다.

    띄어쓰기 소실 여부는 문서 전체 텍스트로 한 번 판단한다.
    소실이 감지되면 기존 공백을 무시하고 다시 띄우고(reset),
    아니면 빠진 공백만 채운다. 원문은 각 항목의 `orig`에 남아 있다.
    keep_spacing: 띄어쓰기를 믿을 수 있는 항목의 id() (PDF 텍스트 레이어에서 다시 읽은 글). 정규화만 한다.
    ocr: OCR 글이면 판단 없이 OCR용 규칙으로 다시 띄운다 (`SpacingFixer.respace_ocr`). OCR은 공백 대부분을 빠뜨리지만
         일부는 남겨서 긴 어절 비율로 재는 '띄어쓰기 소실' 판단에 걸리지 않는다.
    """
    fixer = fixer or SpacingFixer()

    cells = [cell for table in doc.tables for cell in table.data.table_cells]
    full_text = "\n".join([t.text for t in doc.texts] + [c.text for c in cells])
    before = spacing_stats(full_text)
    # 띄어쓰기 소실 판단은 믿을 수 없는 글(keep_spacing 밖)로만 한다
    loose = "\n".join([t.text for t in doc.texts if id(t) not in keep_spacing]
                      + [c.text for c in cells if id(c) not in keep_spacing])
    reset = ocr or (spacing_lost(loose) if loose.strip() else False)

    def fix(text: str, keep: bool = False) -> str:
        text = normalize_text(text)
        if not respace or keep:
            return text
        return fixer.respace_ocr(text) if ocr else fixer.respace(text, reset=reset)

    texts_changed = 0
    for item in doc.texts:
        new = fix(item.text, id(item) in keep_spacing)
        if new != item.text:
            item.text = new
            texts_changed += 1

    cells_changed = 0
    for cell in cells:
        new = fix(cell.text, id(cell) in keep_spacing)
        if new != cell.text:
            cell.text = new
            cells_changed += 1

    after_text = "\n".join([t.text for t in doc.texts] + [c.text for c in cells])
    return PostprocessReport(
        texts_changed=texts_changed,
        cells_changed=cells_changed,
        spacing_reset=reset,
        long_eojeol_ratio_before=round(before.long_ratio, 4),
        long_eojeol_ratio_after=round(spacing_stats(after_text).long_ratio, 4),
        garbled=inspect_text(full_text).garbled,
    )
