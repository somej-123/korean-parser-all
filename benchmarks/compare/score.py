#!/usr/bin/env python3
"""다른 파서와 같은 기준으로 비교한다 (프로젝트 환경에서 돌린다).

    python benchmarks/compare/score.py --out benchmarks/compare/report.md

먼저 비교용 환경에서 run_tools.py로 다른 파서의 결과를 모아 둔다 (README.md). 이 도구는 우리 파서와 Docling을
직접 돌리고(결과는 out/korean-parser, out/docling에 같은 모양으로 저장), 모든 파서를 두 가지로 잰다.

1. 정답 세트(benchmarks/eval/data, 합성 제안요청서 PDF·DOCX 12개씩, 스캔 4개): 글자 오류율, 어절 F1, 표 TEDS,
   제목 재현율, 읽기 순서. 다른 파서의 마크다운은 제목(#)·표(| 또는 <table>)·문단으로 나눠 같은 평가에 넣는다.
2. 기준 문서 묶음(benchmarks/goal, 264개): 원문 글자 보존율. 원문 글자는 v1.0 기준 1과 같은 방법으로 파서와 따로
   읽는다. 마크다운 기호·그림 주소(base64 포함)는 빼고 센다.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import html
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks" / "eval"), str(ROOT / "benchmarks" / "goal")]

from check import edge_lines, norm, our_text, source_counter, source_pages  # noqa: E402
from evaluate import from_docling, from_ir, truth_for  # noqa: E402
from metrics import evaluate  # noqa: E402

from korean_parser.parsers.pdf.page import _PDFIUM_LOCK  # noqa: E402

TOOLS = ["korean-parser", "docling", "kordoc", "markitdown", "pymupdf4llm", "opendataloader-pdf", "hwp-hwpx-parser"]
DOCLING_FORMATS = {".pdf", ".docx", ".pptx", ".xlsx"}


# ---------------------------------------------------------------------------
# 마크다운 → 평가 블록
# ---------------------------------------------------------------------------

_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>|data:[\w/+.-]+;base64,[A-Za-z0-9+/=\s]+")
_HTML_TABLE = re.compile(r"<table.*?</table>", re.S | re.I)


def clean_text(md: str) -> str:
    """보존율용 글: 그림 주소·HTML 태그·마크다운 기호를 뺀다."""
    md = _IMG.sub(" ", md)
    md = re.sub(r"<[^>]+>", " ", md)
    md = html.unescape(md)
    md = re.sub(r"^\s{0,3}#{1,6}\s+", "", md, flags=re.M)
    md = re.sub(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", " ", md, flags=re.M)  # 표 구분 줄
    md = md.replace("|", " ").replace("**", "").replace("__", "")
    md = re.sub(r"^\s*[-*+]\s+", "", md, flags=re.M)
    return md


def _html_table(fragment: str) -> dict | None:
    from lxml import html as lh

    try:
        t = lh.fromstring(fragment)
    except Exception:  # noqa: BLE001
        return None
    rows = []
    for tr in t.iter("tr"):
        cells = [{"text": " ".join(td.text_content().split()), "rowspan": int(td.get("rowspan") or 1),
                  "colspan": int(td.get("colspan") or 1)} for td in tr if td.tag in ("td", "th")]
        if cells:
            rows.append(cells)
    return {"type": "table", "rows": rows} if rows else None


def from_markdown(md: str) -> list[dict]:
    md = _IMG.sub(" ", md)
    out: list[dict] = []
    pieces = _HTML_TABLE.split(md)
    tables = _HTML_TABLE.findall(md)
    for k, piece in enumerate(pieces):
        para: list[str] = []
        rows: list[list[dict]] = []

        def flush_para():
            if para:
                text = " ".join(" ".join(para).split())
                text = html.unescape(re.sub(r"<[^>]+>", " ", text)).replace("**", "").strip()
                if text:
                    out.append({"type": "paragraph", "text": text})
                para.clear()

        def flush_rows():
            if rows:
                out.append({"type": "table", "rows": [r[:] for r in rows]})
                rows.clear()

        for line in piece.splitlines():
            s = line.strip()
            if s.startswith("|") and s.count("|") >= 2:
                flush_para()
                if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", s):
                    continue
                cells = [c.strip() for c in s.strip("|").split("|")]
                rows.append([{"text": html.unescape(re.sub(r"<[^>]+>", " ", c)).replace("**", "").strip(),
                              "rowspan": 1, "colspan": 1} for c in cells])
                continue
            flush_rows()
            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                flush_para()
                out.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).replace("**", "").strip()})
            elif not s:
                flush_para()
            else:
                para.append(re.sub(r"^[-*+]\s+", "", s))
        flush_rows()
        flush_para()
        if k < len(tables):
            t = _html_table(tables[k])
            if t:
                out.append(t)
    return out


# ---------------------------------------------------------------------------
# 우리 파서·Docling 결과 모으기 (다른 파서와 같은 모양으로 저장)
# ---------------------------------------------------------------------------


def _name(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def collect_ours_and_docling(items: list[tuple[Path, bool]], out: Path, tools: set[str]) -> None:
    from korean_parser.parsers import parse

    docling_conv = {}
    for tool in ("korean-parser", "docling"):
        if tool not in tools:
            continue
        folder = out / tool
        folder.mkdir(parents=True, exist_ok=True)
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        for path, ocr in items:
            key = str(path)
            if key in index or (tool == "docling" and path.suffix.lower() not in DOCLING_FORMATS):
                continue
            name = _name(key)
            t0 = time.perf_counter()
            try:
                if tool == "korean-parser":
                    doc = parse(path)
                    text, pred = our_text(doc), from_ir(doc)
                else:
                    if path.suffix.lower() == ".pdf":
                        from evaluate import docling_convert

                        ddoc = docling_convert(path, ocr=ocr)
                    else:
                        if "office" not in docling_conv:
                            from docling.document_converter import DocumentConverter

                            docling_conv["office"] = DocumentConverter()
                        ddoc = docling_conv["office"].convert(path).document
                    text, pred = ddoc.export_to_markdown(), from_docling(ddoc)
                seconds = time.perf_counter() - t0
                (folder / f"{name}.txt").write_text(text, encoding="utf-8")
                (folder / f"{name}.pred.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
                index[key] = {"txt": f"{name}.txt", "pred": f"{name}.pred.json", "seconds": round(seconds, 3)}
            except Exception as exc:  # noqa: BLE001
                index[key] = {"error": f"{type(exc).__name__}: {exc}"[:300], "seconds": round(time.perf_counter() - t0, 3)}
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{tool:14s} {path.name[:40]:40s} {index[key].get('seconds')}s {index[key].get('error', '')[:60]}",
                  flush=True)


def load(out: Path, tool: str) -> dict:
    p = out / tool / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def result_text(out: Path, tool: str, entry: dict) -> str:
    if "txt" in entry:
        return (out / tool / entry["txt"]).read_text(encoding="utf-8")
    return (out / tool / entry["md"]).read_text(encoding="utf-8")


def result_pred(out: Path, tool: str, entry: dict) -> list[dict]:
    if "pred" in entry:
        return json.loads((out / tool / entry["pred"]).read_text(encoding="utf-8"))
    return from_markdown((out / tool / entry["md"]).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------


def truth_scores(items, out: Path) -> dict:
    """{(파서, 형식): [점수]}"""
    res: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for path, _ in items:
        if "eval" not in path.parts or "data" not in path.parts:
            continue
        fmt = "scan" if path.name.endswith(".scan.pdf") else path.suffix.lstrip(".")
        spec = json.loads(path.with_name(path.name.split(".")[0] + ".truth.json").read_text(encoding="utf-8"))
        with _PDFIUM_LOCK:
            truth = truth_for(spec, path)
        for tool in TOOLS:
            entry = load(out, tool).get(str(path))
            if entry is None:
                continue
            if "error" in entry:
                res[(tool, fmt)].append({"error": True})
                continue
            score = evaluate(truth, result_pred(out, tool, entry))
            res[(tool, fmt)].append(score | {"seconds": entry["seconds"]})
    return res


def retention_scores(items, out: Path) -> dict:
    """{(파서, 형식): [행]}"""
    res: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    sources: dict[str, collections.Counter | None] = {}
    for path, _ in items:
        if "eval" in path.parts and "data" in path.parts:
            continue
        key = str(path)
        if key not in sources:
            try:
                # source_pages·edge_lines는 pdfium 잠금을 스스로 잡는다 (겹쳐 잡으면 멈춘다: 다시 잡을 수 없는 잠금)
                pages = source_pages(path)
                edges = edge_lines(path)
                sources[key] = source_counter(pages, edges) if pages is not None else None
            except Exception:  # noqa: BLE001 - 원문을 못 읽는 파일(암호 등)은 빼고 잰다
                sources[key] = None
        src = sources[key]
        if not src:
            continue
        total = sum(src.values())
        fmt = path.suffix.lower().lstrip(".")
        for tool in TOOLS:
            entry = load(out, tool).get(key)
            if entry is None:
                continue
            if "error" in entry:
                res[(tool, fmt)].append({"file": path.name, "kept": 0.0, "error": entry["error"], "seconds": entry["seconds"]})
                continue
            text = result_text(out, tool, entry)
            ours = collections.Counter(norm(text if tool == "korean-parser" else clean_text(text)))
            res[(tool, fmt)].append({"file": path.name, "kept": sum((src & ours).values()) / total,
                                     "seconds": entry["seconds"]})
    return res


def report(truth: dict, ret: dict) -> str:
    lines = ["# 다른 파서와 비교", "", "`benchmarks/compare/score.py`로 만든 보고서. 방법은 `benchmarks/compare/README.md`.", "",
             "## 1. 정답 세트 (구조 정확도)", "",
             "| 형식 | 파서 | 문서 | 실패 | 글자 오류율 ↓ | 어절 F1 | 표 TEDS | 제목 재현율 | 읽기 순서 | 초/문서 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for fmt in ("pdf", "scan", "docx"):
        for tool in TOOLS:
            rows = truth.get((tool, fmt))
            if not rows:
                continue
            ok = [r for r in rows if "error" not in r]
            if not ok:
                lines.append(f"| {fmt} | {tool} | {len(rows)} | {len(rows)} | | | | | | |")
                continue

            def mean(key, sub=None):
                vals = [(r[key][sub] if sub else r[key]) for r in ok if key in r and r[key] is not None]
                return statistics.mean(vals) if vals else float("nan")

            lines.append(f"| {fmt} | {tool} | {len(rows)} | {len(rows) - len(ok)} | {mean('cer'):.1%} | "
                         f"{mean('eojeol_f1'):.3f} | {mean('table_teds'):.3f} | {mean('heading', 'recall'):.3f} | "
                         f"{mean('reading_order'):.3f} | {statistics.median(r['seconds'] for r in ok):.2f} |")
    lines += ["", "## 2. 기준 문서 묶음 (원문 글자 보존율)", "",
              "| 형식 | 파서 | 파일 | 실패 | 보존율 평균 | 0.99 이상 | 가장 낮은 값 | 초/문서(중앙값) |",
              "|---|---|---:|---:|---:|---:|---:|---:|"]
    for fmt in ("hwp", "hwpx", "pdf", "docx", "pptx", "xlsx"):
        for tool in TOOLS:
            rows = ret.get((tool, fmt))
            if not rows:
                continue
            errors = sum(1 for r in rows if "error" in r)
            kept = [r["kept"] for r in rows]
            worst = min(rows, key=lambda r: r["kept"])
            lines.append(f"| {fmt} | {tool} | {len(rows)} | {errors} | {statistics.mean(kept):.3f} | "
                         f"{sum(k >= 0.99 for k in kept)} | {worst['kept']:.3f} | "
                         f"{statistics.median(r['seconds'] for r in rows):.2f} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--files", type=Path, default=HERE / "out" / "files.txt")
    p.add_argument("--out-dir", type=Path, default=HERE / "out")
    p.add_argument("--out", type=Path, default=HERE / "report.md")
    p.add_argument("--run", default="korean-parser,docling", help="여기서 직접 돌릴 파서 (빈 값이면 모아 둔 결과만 씀)")
    args = p.parse_args()
    items = []
    for line in args.files.read_text(encoding="utf-8").splitlines():
        if line.strip():
            path, _, flag = line.strip().partition("|")
            items.append((Path(path), flag == "ocr"))
    collect_ours_and_docling(items, args.out_dir, set(filter(None, args.run.split(","))))
    text = report(truth_scores(items, args.out_dir), retention_scores(items, args.out_dir))
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
