#!/usr/bin/env python3
"""정답 세트로 파서를 평가한다.

    python synth.py --out data              # 정답이 있는 문서 만들기 (한 번)
    python evaluate.py data --out report.md # korean-parser 평가
    python evaluate.py data --baseline      # Docling 기본 출력도 같은 지표로 잰다 (비교 기준)
    python evaluate.py --hwpx 폴더           # HWPX: 한컴오피스가 파일에 넣어 둔 미리보기 글과 비교

형식별(DOCX, PDF, 스캔 PDF)로 글자 오류율, 어절 F1, 읽기 순서, 블록 재현율, 제목, 표 TEDS를 모아 보고서를 쓴다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import evaluate, norm  # noqa: E402

from korean_parser.ir import Heading, Paragraph, Table  # noqa: E402


# ---------------------------------------------------------------------------
# 예측 → 평가용 블록
# ---------------------------------------------------------------------------


def from_ir(doc) -> list[dict]:
    """korean-parser 결과를 평가용 블록으로. 본문 최상위 블록만 (머리말·꼬리말 제외)."""
    out = []
    for b in doc.blocks:
        if isinstance(b, Heading):
            marker = f"{b.list_info.marker} " if b.list_info else ""
            out.append({"type": "heading", "level": b.level, "text": marker + b.text})
        elif isinstance(b, Paragraph):
            marker = f"{b.list_info.marker} " if b.list_info else ""
            if b.text.strip():
                out.append({"type": "paragraph", "text": marker + b.text})
        elif isinstance(b, Table):
            out.append(_ir_table(b))
    return out


def _ir_table(t: Table) -> dict:
    def cell_text(c):
        parts = []
        for x in c.blocks:
            if isinstance(x, Table):
                parts.append(" ".join(cell_text(cc) for r in x.rows for cc in r.cells))
            elif hasattr(x, "text"):
                parts.append(x.text)
        return " ".join(parts)

    return {"type": "table", "rows": [[{"text": cell_text(c), "rowspan": c.row_span, "colspan": c.col_span}
                                       for c in r.cells] for r in t.rows]}


def from_docling(ddoc) -> list[dict]:
    """Docling 기본 출력 (비교 기준)."""
    from docling_core.types.doc import ContentLayer, DocItemLabel, TableItem, TextItem

    out = []
    for item, level in ddoc.iterate_items(included_content_layers={ContentLayer.BODY}):
        if isinstance(item, TableItem):
            data = item.data
            origins: dict[int, list] = {}
            for c in data.table_cells:
                origins.setdefault(c.start_row_offset_idx, []).append(c)
            rows = [[{"text": c.text, "rowspan": c.row_span or 1, "colspan": c.col_span or 1}
                     for c in sorted(origins.get(r, []), key=lambda c: c.start_col_offset_idx)]
                    for r in range(data.num_rows)]
            out.append({"type": "table", "rows": rows})
        elif isinstance(item, TextItem) and item.text.strip():
            if item.label in (DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER):
                out.append({"type": "heading", "level": getattr(item, "level", 1), "text": item.text})
            else:
                marker = (getattr(item, "marker", "") or "").strip()
                text = item.text if not marker or item.text.startswith(marker) else f"{marker} {item.text}"
                out.append({"type": "paragraph", "text": text})
    return out


_DOCLING = None


def docling_convert(path: Path, ocr: bool):
    global _DOCLING
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    if _DOCLING is None:
        _DOCLING = {}
    if ocr not in _DOCLING:
        opts = PdfPipelineOptions()  # Docling 기본값 (표 구조 인식 켬)
        opts.do_ocr = ocr
        if ocr:
            opts.ocr_options = RapidOcrOptions(lang=["korean"], mode=OcrMode.FULL_PAGE)
        _DOCLING[ocr] = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _DOCLING[ocr].convert(path).document


# ---------------------------------------------------------------------------
# 정답
# ---------------------------------------------------------------------------


def truth_for(spec: dict, path: Path) -> list[dict]:
    """정답 블록. 스캔 PDF는 앞 몇 쪽만 그렸으므로, 원본 PDF의 그 쪽들에 들어 있는 블록만 남긴다."""
    blocks = spec["blocks"]
    if not path.name.endswith(".scan.pdf"):
        return blocks
    import pypdfium2 as pdfium

    n = len(pdfium.PdfDocument(path))
    src = pdfium.PdfDocument(path.with_name(path.name.replace(".scan.pdf", ".pdf")))
    shown = "".join(src[i].get_textpage().get_text_range() for i in range(n)).replace(" ", "").replace("\r\n", "")
    shown = norm(shown).replace(" ", "")

    def visible(b):
        text = norm(b["rows"][0][0]["text"] if b["type"] == "table" else b["text"]).replace(" ", "")
        return text[:20] in shown

    # 마지막으로 보이는 블록까지 (쪽 끝에서 잘린 블록은 포함)
    last = max((i for i, b in enumerate(blocks) if visible(b)), default=-1)
    return blocks[: last + 1]


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------


FORMATS = [("docx", ".docx"), ("pdf", ".pdf"), ("scan", ".scan.pdf")]


def run(data: Path, baseline: bool, formats: set[str] | None = None) -> dict:
    from korean_parser.parsers import parse

    results: dict[str, list[dict]] = {}
    for truth_file in sorted(data.glob("*.truth.json")):
        spec = json.loads(truth_file.read_text(encoding="utf-8"))
        stem = truth_file.name[: -len(".truth.json")]
        for fmt, suffix in FORMATS:
            path = data / f"{stem}{suffix}"
            if not path.exists() or (formats and fmt not in formats):
                continue
            truth = truth_for(spec, path)
            t0 = time.perf_counter()
            pred = from_ir(parse(path))
            score = evaluate(truth, pred) | {"seconds": time.perf_counter() - t0, "doc": stem}
            results.setdefault(f"korean-parser · {fmt}", []).append(score)
            print(f"{stem} {fmt:5s} CER {score['cer']:.3f}  어절F1 {score['eojeol_f1']:.3f}  "
                  f"TEDS {score.get('table_teds', float('nan')):.3f}", flush=True)
            if baseline and fmt != "docx":
                t0 = time.perf_counter()
                pred = from_docling(docling_convert(path, ocr=fmt == "scan"))
                score = evaluate(truth, pred) | {"seconds": time.perf_counter() - t0, "doc": stem}
                results.setdefault(f"Docling 기본 · {fmt}", []).append(score)
    return results


def hwpx_preview(folder: Path) -> list[dict]:
    """HWPX 안의 미리보기 글(Preview/PrvText.txt, 한컴오피스가 저장)과 우리 결과의 앞부분을 비교한다.

    미리보기는 문서 앞부분(약 1~2쪽)만 담는다. 미리보기 글자가 우리 결과에 같은 순서로 나오는 비율(글자 재현율)을 잰다.
    """
    from rapidfuzz.distance import Indel

    from korean_parser.parsers import parse

    import hashlib

    from korean_parser.ir import Heading as _H, Paragraph as _P

    out, seen = [], set()
    for path in sorted(folder.rglob("*.hwpx")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:  # 같은 파일의 사본
            continue
        seen.add(digest)
        try:
            with zipfile.ZipFile(path) as z:
                preview = z.read("Preview/PrvText.txt").decode("utf-8", "ignore")
        except (KeyError, zipfile.BadZipFile):
            continue
        truth = norm(preview.replace("<", " ").replace(">", " ")).replace(" ", "")
        if len(truth) < 20:
            continue
        try:
            doc = parse(path)
        except Exception:  # noqa: BLE001
            continue
        # 미리보기에는 표 칸과 글상자(도형) 안 글도 들어 있으므로 모든 문단을 문서 순서대로 모은다
        pred = norm(" ".join(b.text for b in doc.iter_blocks() if isinstance(b, (_H, _P)))).replace(" ", "")
        window = pred[: int(len(truth) * 1.3) + 200]
        lcs = (len(truth) + len(window) - Indel.distance(truth, window)) / 2
        out.append({"file": path.name, "chars": len(truth), "recall": lcs / len(truth)})
    return out


def _plain(b: dict) -> str:
    if b["type"] == "table":
        return " ".join(c["text"] for row in b["rows"] for c in row)
    return b["text"]


def report(results: dict, hwpx: list[dict]) -> str:
    def avg(rows, key, sub=None):
        vals = [(r[key][sub] if sub else r[key]) for r in rows if key in r]
        return statistics.mean(vals) if vals else None

    def f(x, pct=False):
        return "-" if x is None else (f"{x * 100:.1f}%" if pct else f"{x:.3f}")

    lines = ["# 정답 세트 평가 결과", "",
             "| 대상 | 문서 | 글자 오류율 ↓ | 어절 F1 | 블록 재현율 | 읽기 순서 | 제목 재현율 | 제목 정밀도 | 제목 단계 | 표 TEDS | 표 TEDS-S | 문서당 시간 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, rows in results.items():
        lines.append(" | ".join([
            f"| {name}", str(len(rows)), f(avg(rows, "cer"), True), f(avg(rows, "eojeol_f1")),
            f(avg(rows, "block_recall")), f(avg(rows, "reading_order")), f(avg(rows, "heading", "recall")),
            f(avg(rows, "heading", "precision")), f(avg(rows, "heading", "level")), f(avg(rows, "table_teds")),
            f(avg(rows, "table_teds_s")), f"{avg(rows, 'seconds'):.1f}초 |"]))
    if hwpx:
        lines += ["", "## HWPX: 한컴오피스 미리보기 글과 비교", "",
                  f"- 문서 {len(hwpx)}개, 미리보기 글자 재현율 평균 {statistics.mean(r['recall'] for r in hwpx):.3f}, "
                  f"최저 {min(r['recall'] for r in hwpx):.3f}", "",
                  "| 파일 | 미리보기 글자 수 | 재현율 |", "|---|---:|---:|"]
        for r in sorted(hwpx, key=lambda r: r["recall"])[:15]:
            lines.append(f"| {r['file']} | {r['chars']} | {r['recall']:.3f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="정답 세트로 파서 평가")
    p.add_argument("data", nargs="?", help="synth.py로 만든 폴더")
    p.add_argument("--baseline", action="store_true", help="Docling 기본 출력도 평가")
    p.add_argument("--hwpx", help="HWPX 미리보기 비교에 쓸 폴더")
    p.add_argument("--formats", help="평가할 형식만 (쉼표로 구분: docx,pdf,scan)")
    p.add_argument("--out", default="report.md")
    p.add_argument("--json", help="문서별 점수를 JSON으로도 저장")
    args = p.parse_args()
    formats = set(args.formats.split(",")) if args.formats else None
    results = run(Path(args.data), args.baseline, formats) if args.data else {}
    hwpx = hwpx_preview(Path(args.hwpx)) if args.hwpx else []
    Path(args.out).write_text(report(results, hwpx), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps({"results": results, "hwpx": hwpx}, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    print(f"보고서: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
