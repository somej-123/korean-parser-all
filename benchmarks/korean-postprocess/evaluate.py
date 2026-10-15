#!/usr/bin/env python3
"""한국어 후처리 효과 평가.

같은 내용의 DOCX(정답)와 PDF(평가 대상)를 Docling으로 변환하고,
PDF 결과의 띄어쓰기가 후처리 전후로 정답에 얼마나 가까운지 측정한다.

    python evaluate.py --reference ../docling-cpu/samples/사업계획서.docx \\
                       --target ../docling-cpu/samples/사업계획서.pdf \\
                       --target ../docling-cpu/samples/사업계획서_스캔.pdf

지표
- 띄어쓰기 정밀도/재현율/F1: 공백을 뺀 글자열을 정렬한 뒤, 각 글자 앞에 공백이
  있어야 하는지를 정답과 비교한다. 글자 자체가 다른 부분(OCR 오인식 등)은 제외한다.
- 어절 일치율: 정답 어절 중 결과에 그대로 나타난 비율 (중복 고려).
"""

from __future__ import annotations

import argparse
import copy
import difflib
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from korean_parser import SpacingFixer  # noqa: E402
from korean_parser.docling_adapter import postprocess_docling  # noqa: E402


@dataclass
class Score:
    precision: float
    recall: float
    f1: float
    eojeol_match: float
    aligned_chars: int


def doc_text(doc) -> str:
    texts = [t.text for t in doc.texts]
    cells = [c.text for table in doc.tables for c in table.data.table_cells]
    return "\n".join(texts + cells)


def strip_with_flags(text: str) -> tuple[str, list[bool]]:
    """공백을 뺀 글자열과, 각 글자 앞에 공백(또는 줄바꿈)이 있었는지 여부."""
    chars, flags, pending = [], [], False
    for ch in text:
        if ch.isspace():
            pending = True
        else:
            chars.append(ch)
            flags.append(pending)
            pending = False
    return "".join(chars), flags


def score(reference: str, target: str) -> Score:
    ref_s, ref_f = strip_with_flags(reference)
    tgt_s, tgt_f = strip_with_flags(target)
    tp = fp = fn = aligned = 0
    matcher = difflib.SequenceMatcher(None, ref_s, tgt_s, autojunk=False)
    for block in matcher.get_matching_blocks():
        # 블록 첫 글자는 앞 문맥이 달라 공백 비교가 의미 없으므로 제외한다.
        for k in range(1, block.size):
            r, t = ref_f[block.a + k], tgt_f[block.b + k]
            aligned += 1
            tp += r and t
            fp += (not r) and t
            fn += r and not t
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    ref_words, tgt_words = Counter(reference.split()), Counter(target.split())
    matched = sum((ref_words & tgt_words).values())
    return Score(precision, recall, f1, matched / max(1, sum(ref_words.values())), aligned)


def build_converter(ocr: bool):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.table_structure_options.mode = TableFormerMode.FAST
    opts.do_ocr = ocr
    if ocr:
        opts.ocr_options = RapidOcrOptions(lang=["korean"], mode=OcrMode.FULL_PAGE)
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def needs_ocr(path: Path) -> bool:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[0]
        text = page.get_textpage().get_text_range().strip()
        return len(text) < 30
    finally:
        pdf.close()


def show_examples(before: str, after: str, limit: int) -> None:
    shown = 0
    for b, a in zip(before.split("\n"), after.split("\n")):
        if b != a and len(b) > 10:
            print(f"    전: {b[:90]}")
            print(f"    후: {a[:90]}\n")
            shown += 1
            if shown >= limit:
                break


def main() -> int:
    p = argparse.ArgumentParser(description="한국어 후처리 효과 평가")
    p.add_argument("--reference", required=True, help="정답 문서 (DOCX 등 원본)")
    p.add_argument("--target", action="append", required=True, help="평가할 PDF (여러 개 가능)")
    p.add_argument("--examples", type=int, default=3, help="출력할 변경 예시 수")
    args = p.parse_args()

    fixer = SpacingFixer()
    reference = doc_text(build_converter(ocr=False).convert(args.reference).document)

    rows = []
    for target in map(Path, args.target):
        ocr = needs_ocr(target)
        raw_doc = build_converter(ocr=ocr).convert(target).document
        before = doc_text(raw_doc)

        fixed_doc = copy.deepcopy(raw_doc)
        report = postprocess_docling(fixed_doc, fixer=fixer)
        after = doc_text(fixed_doc)

        s_before, s_after = score(reference, before), score(reference, after)
        rows.append((target.name, "OCR" if ocr else "텍스트", s_before, s_after))

        print(f"\n## {target.name} ({'OCR' if ocr else '텍스트 레이어'})")
        print(f"  {report}")
        show_examples(before, after, args.examples)

    print("\n| 문서 | 경로 | 띄어쓰기 F1 (전 → 후) | 정밀도 (전 → 후) | 재현율 (전 → 후) | 어절 일치율 (전 → 후) |")
    print("|---|---|---|---|---|---|")
    for name, route, b, a in rows:
        print(
            f"| {name} | {route} | {b.f1:.3f} → **{a.f1:.3f}** | {b.precision:.3f} → {a.precision:.3f} | "
            f"{b.recall:.3f} → {a.recall:.3f} | {b.eojeol_match:.1%} → **{a.eojeol_match:.1%}** |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
