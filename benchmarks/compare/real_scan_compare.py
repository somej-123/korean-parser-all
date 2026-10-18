#!/usr/bin/env python3
"""실제 PDF로 만든 스캔본으로 OCR 품질을 다른 파서와 비교한다 (프로젝트 환경에서 돌린다).

    python benchmarks/compare/real_scan_compare.py --samples C:/korean-parser-all/samples_rfp --out benchmarks/compare/report_real_scan.md

스캔본은 `benchmarks/eval/real_scan.py`와 같은 방법으로 만든다 (문서마다 2·3쪽, clean 200dpi 컬러 / rough 150dpi 흑백·
0.5도 기울임·JPEG 60). 한 번 만들어 out/real_scan/scans에 두고, 모든 파서가 같은 파일을 읽는다.

- korean-parser: real_scan.py와 똑같이 본문 블록 + 머리말·꼬리말 글
- Docling: 기본 설정에 OCR만 켬 (RapidOCR 한국어, 쪽 전체, score.py와 같음). 본문 + 머리말·꼬리말 층 글
- kordoc: `--ocr` (run_tools.py의 run_kordoc). 마크다운은 score.py의 clean_text처럼 기호를 빼고 글만 비교한다

지표도 real_scan.py와 같다: CER, 글자 재현율, 어절 F1 (정답의 한글-한글 줄바꿈은 예측에 맞춤). 목차 점선은 양쪽에서 뺀다.
결과는 out/real_scan/<파서>/index.json에 쌓고, 이미 있는 결과는 건너뛴다. 보고서에는 숫자만 넣는다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks" / "eval"), str(HERE)]

from metrics import cer, eojeol_f1, full_text, norm  # noqa: E402
from real_scan import PAGES, char_recall, clean, make_scan, resolve_breaks, truth_text  # noqa: E402

TOOLS = ["korean-parser", "docling", "kordoc"]

# 문서 묶음: (이름, samples_rfp 아래 폴더, 문서 번호 목록). OCR이 CPU로 느려 10개만 고른다: 띄어쓰기를 고칠 때 본 문서
# (eval/real_scan_report.md의 7개)에서 4개, 고칠 때 보지 않은 두 묶음에서 3개씩. 기관이 겹치지 않게 골랐다
SETS = [
    ("고칠 때 본 문서", ("datagokr/PDF", "datagokr_new/PDF"), ["15068570", "15161182", "15165208", "3038333"]),
    ("datagokr_scan", ("datagokr_scan/PDF",), ["15053277", "15088524", "15165858"]),
    ("datagokr_scan2", ("datagokr_scan2/PDF",), ["15104232", "15162234", "3068964"]),
]
KINDS = ("clean", "rough")


def doc_id(path: Path) -> str:
    return path.name.split("_")[0]


def find_sources(samples: Path) -> list[tuple[str, Path]]:
    out = []
    for name, folders, ids in SETS:
        by_id = {doc_id(p): p for f in folders for p in (samples / f).glob("*.pdf")}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"{name}: 문서를 찾지 못함 {missing}")
        out += [(name, by_id[i]) for i in ids]
    return out


def make_scans(sources, work: Path) -> list[dict]:
    """스캔본과 정답 글을 만든다 (이미 있으면 그대로 쓴다). 항목: 묶음, 문서, 종류, 쪽 수, 스캔 경로, 정답 경로."""
    import pypdfium2 as pdfium

    (work / "scans").mkdir(parents=True, exist_ok=True)
    (work / "truth").mkdir(parents=True, exist_ok=True)
    items = []
    for set_name, src in sources:
        n = len(pdfium.PdfDocument(str(src)))
        pages = [p for p in PAGES if p < n] or [0]
        truth_path = work / "truth" / f"{doc_id(src)}.txt"
        if not truth_path.exists():
            truth_path.write_text(clean(truth_text(src, pages)), encoding="utf-8")
        if len(norm(truth_path.read_text(encoding="utf-8"))) < 50:
            print("글자 층이 거의 없어 건너뜀:", src.name)
            continue
        for kind in KINDS:
            scan = work / "scans" / f"{doc_id(src)}.{kind}.pdf"
            if not scan.exists():
                make_scan(src, pages, scan, rough=kind == "rough")
            items.append({"set": set_name, "doc": doc_id(src), "kind": kind, "pages": len(pages),
                          "scan": scan, "truth": truth_path})
    return items


# ---------------------------------------------------------------------------
# 파서 돌리기 (결과 글을 out/real_scan/<파서>/<문서>.<종류>.txt 로)
# ---------------------------------------------------------------------------


def ours_text(path: Path) -> str:
    from evaluate import from_ir

    from korean_parser.parsers import parse

    doc = parse(path)
    # real_scan.py와 같다: 정답(글자 층 전체)에는 머리말·꼬리말도 있으므로 파서가 머리말로 알아본 글도 넣는다
    furniture = " ".join(getattr(b, "text", "") or "" for b in doc.header + doc.footer)
    return full_text(from_ir(doc)) + " " + furniture


def docling_text(path: Path) -> str:
    from docling_core.types.doc import ContentLayer, TableItem, TextItem
    from evaluate import docling_convert, from_docling

    ddoc = docling_convert(path, ocr=True)
    # 우리 파서와 같게 머리말·꼬리말 층(FURNITURE)의 글도 넣는다
    furniture = []
    for item, _ in ddoc.iterate_items(included_content_layers={ContentLayer.FURNITURE}):
        if isinstance(item, TableItem):
            furniture += [c.text for c in item.data.table_cells]
        elif isinstance(item, TextItem):
            furniture.append(item.text)
    return full_text(from_docling(ddoc)) + " " + " ".join(furniture)


def kordoc_text(path: Path) -> str:
    from run_tools import run_kordoc
    from score import clean_text

    return clean_text(run_kordoc(path, ocr=True))


RUNNERS = {"korean-parser": ours_text, "docling": docling_text, "kordoc": kordoc_text}


def run(items: list[dict], work: Path, tools: list[str]) -> None:
    for tool in tools:
        folder = work / tool
        folder.mkdir(parents=True, exist_ok=True)
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        for it in items:
            key = it["scan"].name
            if key in index:
                continue
            t0 = time.perf_counter()
            try:
                text = RUNNERS[tool](it["scan"])
                name = key.removesuffix(".pdf") + ".txt"
                (folder / name).write_text(text, encoding="utf-8")
                index[key] = {"txt": name, "seconds": round(time.perf_counter() - t0, 3)}
            except Exception as exc:  # noqa: BLE001 - 실패도 결과다
                index[key] = {"error": f"{type(exc).__name__}: {exc}"[:300], "seconds": round(time.perf_counter() - t0, 3)}
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{tool:14s} {key:24s} {index[key]['seconds']:7.1f}s {index[key].get('error', '')[:80]}", flush=True)


# ---------------------------------------------------------------------------
# 채점
# ---------------------------------------------------------------------------


def score_one(truth: str, pred: str) -> dict:
    pred = clean(pred)
    tb, pb = [{"type": "paragraph", "text": truth}], [{"type": "paragraph", "text": pred}]
    eb = [{"type": "paragraph", "text": resolve_breaks(truth, pred)}]
    return {"cer": cer(tb, pb), "recall": char_recall(truth, pred), "f1": eojeol_f1(eb, pb)}


def score(items: list[dict], work: Path) -> list[dict]:
    rows = []
    for tool in TOOLS:
        index_path = work / tool / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        for it in items:
            entry = index.get(it["scan"].name)
            if entry is None:
                continue
            row = {"tool": tool, "set": it["set"], "doc": it["doc"], "kind": it["kind"], "seconds": entry["seconds"]}
            if "error" in entry:
                # 실패는 아무것도 못 읽은 것으로 센다 (평균에서 빼면 실패한 파서가 유리해진다)
                row |= {"cer": 1.0, "recall": 0.0, "f1": 0.0, "error": entry["error"]}
            else:
                truth = it["truth"].read_text(encoding="utf-8")
                row |= score_one(truth, (work / tool / entry["txt"]).read_text(encoding="utf-8"))
            rows.append(row)
    return rows


def report(rows: list[dict]) -> str:
    lines = ["# 실제 PDF로 만든 스캔본의 OCR 품질: 다른 파서와 비교", "",
             "`benchmarks/compare/real_scan_compare.py`로 만든 보고서. 정답은 원래 PDF의 글자 층이다. 글자 재현율은 순서를 보지 않는다.",
             "실패한 변환은 CER 1, 재현율·F1 0으로 넣었다. 시간은 중앙값이다.", "",
             "## 묶음별 평균", "",
             "| 묶음 | 스캔 | 파서 | 문서 | 실패 | CER ↓ | 글자 재현율 | 어절 F1 | 초/문서 |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for set_name, _, _ in SETS + [("전체", None, None)]:
        for kind in KINDS:
            for tool in TOOLS:
                sel = [r for r in rows if r["tool"] == tool and r["kind"] == kind and set_name in (r["set"], "전체")]
                if not sel:
                    continue
                errors = sum("error" in r for r in sel)
                avg = {k: statistics.mean(r[k] for r in sel) for k in ("cer", "recall", "f1")}
                lines.append(f"| {set_name} | {kind} | {tool} | {len(sel)} | {errors} | {avg['cer']:.3f} | "
                             f"{avg['recall']:.3f} | {avg['f1']:.3f} | {statistics.median(r['seconds'] for r in sel):.0f} |")
    lines += ["", "## 문서별 (CER / 글자 재현율 / 어절 F1)", "",
              "| 묶음 | 문서 | 스캔 | " + " | ".join(TOOLS) + " |", "|---|---|---|" + "---|" * len(TOOLS)]
    keys = list(dict.fromkeys((r["set"], r["doc"], r["kind"]) for r in rows))
    for key in keys:
        cells = []
        for tool in TOOLS:
            r = next((r for r in rows if (r["set"], r["doc"], r["kind"]) == key and r["tool"] == tool), None)
            cells.append("" if r is None else "실패" if "error" in r else f"{r['cer']:.3f} / {r['recall']:.3f} / {r['f1']:.3f}")
        lines.append(f"| {key[0]} | {key[1]} | {key[2]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--samples", type=Path, default=ROOT / "samples_rfp", help="samples_rfp 폴더 (저장소에 없다)")
    p.add_argument("--work", type=Path, default=HERE / "out" / "real_scan", help="스캔본·정답·파서 결과를 둘 곳")
    p.add_argument("--out", type=Path, default=HERE / "report_real_scan.md")
    p.add_argument("--run", default=",".join(TOOLS), help="돌릴 파서 (빈 값이면 모아 둔 결과로 채점만)")
    args = p.parse_args()
    items = make_scans(find_sources(args.samples), args.work)
    print(f"스캔본 {len(items)}개", flush=True)
    run(items, args.work, [t for t in args.run.split(",") if t])
    text = report(score(items, args.work))
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
