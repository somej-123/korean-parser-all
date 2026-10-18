#!/usr/bin/env python3
"""실제 전자 PDF를 스캔본처럼 그림으로 바꿔 OCR 품질을 잰다 (정답 = 원래 글자 층).

    python benchmarks/eval/real_scan.py samples_rfp/datagokr/PDF/a.pdf ... --out report.md

실제 스캔 문서에는 정답 글이 없어서 품질을 잴 수 없다. 그래서 글자 층이 있는 실제 공공 PDF의 쪽을 그림으로 그려
글자 층 없는 PDF를 만들고, 원래 글자 층을 정답으로 삼는다. 문서마다 2~3쪽(1쪽은 표지가 많아서 뺀다)을 두 가지로 만든다.
- clean: 200dpi 컬러 (깨끗한 스캔)
- rough: 150dpi 흑백, 0.5도 기울임, JPEG 품질 60 (복사기로 뜬 흔한 스캔)

목차 점선('·····')은 양쪽에서 뺀다. 표는 칸 글을 차례로 이어 비교한다.
지표: 글자 오류율(CER, 공백 빼고 순서대로 편집 거리), 글자 재현율(순서를 보지 않고 정답 글자 중 찾은 비율 — 표·단이
많은 쪽은 글자 층의 순서가 화면 순서와 달라 CER이 실제보다 나빠진다), 어절 F1. 원본 글은 보고서에 넣지 않는다.
"""

from __future__ import annotations

import argparse
import re
import collections
import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks" / "eval")]

from metrics import cer, eojeol_f1, norm  # noqa: E402

_LEADER = re.compile(r"[·.…‥ㆍ∙•\-_]{3,}")  # 목차 점선: 글자 층에는 점이 수백 개 들어 있어 점수를 흐린다


def clean(text: str) -> str:
    return _LEADER.sub(" ", text)

PAGES = (1, 2)  # 0부터: 2·3쪽


def truth_text(pdf_path: Path, pages) -> str:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(pdf_path))
    out = []
    for i in pages:
        left, bottom, right, top = pdf[i].get_cropbox()
        out.append(pdf[i].get_textpage().get_text_bounded(left, bottom, right, top))
    pdf.close()
    return "\n".join(out)


def make_scan(pdf_path: Path, pages, out: Path, rough: bool) -> None:
    import pypdfium2 as pdfium
    from PIL import Image

    pdf = pdfium.PdfDocument(str(pdf_path))
    images = []
    for i in pages:
        img = pdf[i].render(scale=(150 if rough else 200) / 72).to_pil().convert("RGB")
        if rough:
            img = img.convert("L").rotate(0.5, expand=True, fillcolor=255, resample=Image.BICUBIC)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=60)
            img = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
        images.append(img)
    pdf.close()
    images[0].save(out, "PDF", resolution=150 if rough else 200, save_all=True, append_images=images[1:])


def char_recall(truth: str, pred: str) -> float:
    t = collections.Counter(norm(truth).replace(" ", ""))
    p = collections.Counter(norm(pred).replace(" ", ""))
    return sum((t & p).values()) / max(sum(t.values()), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("real_scan_report.md"))
    args = ap.parse_args()
    from evaluate import from_ir  # 표는 칸 글을 차례로 이어 비교한다
    from metrics import full_text

    from korean_parser.parsers import parse

    rows = []
    for src in args.pdfs:
        import pypdfium2 as pdfium

        n = len(pdfium.PdfDocument(str(src)))
        pages = [p for p in PAGES if p < n] or [0]
        truth = clean(truth_text(src, pages))
        if len(norm(truth)) < 50:
            print("글자 층이 거의 없어 건너뜀:", src.name)
            continue
        for kind in ("clean", "rough"):
            with tempfile.TemporaryDirectory() as d:
                scan = Path(d) / "scan.pdf"
                make_scan(src, pages, scan, rough=kind == "rough")
                t0 = time.perf_counter()
                doc = parse(scan)
                sec = time.perf_counter() - t0
            pred = clean(full_text(from_ir(doc)))
            tb, pb = [{"type": "paragraph", "text": truth}], [{"type": "paragraph", "text": pred}]
            row = (src.name[:40], kind, len(pages), cer(tb, pb), char_recall(truth, pred), eojeol_f1(tb, pb), sec)
            rows.append(row)
            print(*row[:2], *(f"{v:.3f}" for v in row[3:6]), f"{sec:.0f}s", flush=True)
    lines = ["# 실제 PDF로 만든 스캔본의 OCR 품질", "",
             "정답은 원래 PDF의 글자 층이다 (`benchmarks/eval/real_scan.py`). 글자 재현율은 순서를 보지 않는다.", "",
             "| 문서 | 스캔 | 쪽 | CER | 글자 재현율 | 어절 F1 | 시간 |", "|---|---|---:|---:|---:|---:|---:|"]
    lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]:.3f} | {r[4]:.3f} | {r[5]:.3f} | {r[6]:.0f}초 |" for r in rows]
    for kind in ("clean", "rough"):
        sel = [r for r in rows if r[1] == kind]
        if sel:
            avg = [sum(r[k] for r in sel) / len(sel) for k in (3, 4, 5)]
            lines.append(f"| **평균 ({kind}, {len(sel)}개)** | | | {avg[0]:.3f} | {avg[1]:.3f} | {avg[2]:.3f} | |")
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("저장:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
