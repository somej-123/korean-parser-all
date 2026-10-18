#!/usr/bin/env python3
"""실제 문서에 사람이 단 정답(제목, 표 구조)으로 구조 정확도를 잰다.

    python benchmarks/real_truth/score_real.py --truth samples_rfp/real_truth --out benchmarks/real_truth/report.md

정답 파일(*.truth.json)은 {"source": 원본 경로, "note": 만든 방법, "blocks": [제목·표]} 모양이다. 원본의 글을 담으므로
저장소에 넣지 않는다 (README.md에 만든 방법을 적는다). 우리 파서는 여기서 직접 돌리고, 다른 파서는
benchmarks/compare가 모아 둔 결과(out, out_new)를 쓴다. 지표는 benchmarks/eval/metrics.py 그대로:
제목 재현율·정밀도·단계 일치(정답 제목 목록이 문서의 제목 전부라고 보고), 표 TEDS(정답 표마다 가장 비슷한 예측 표).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "benchmarks" / "eval"), str(ROOT / "benchmarks" / "compare")]

from evaluate import from_ir  # noqa: E402
from metrics import heading_scores, match_blocks, table_scores  # noqa: E402
from score import load, result_pred  # noqa: E402

TOOLS = ["korean-parser", "docling", "kordoc", "markitdown", "pymupdf4llm", "opendataloader-pdf", "hwp-hwpx-parser"]
OUT_DIRS = [ROOT / "benchmarks" / "compare" / "out", ROOT / "benchmarks" / "compare" / "out_new"]


def preds(source: Path) -> dict[str, list[dict]]:
    from korean_parser.parsers import parse

    doc = parse(source)
    out = {"korean-parser": from_ir(doc), "korean-parser (추정 제목 포함)": from_ir(doc, guess=True)}
    key = str(source.resolve())
    for tool in TOOLS[1:]:
        for d in OUT_DIRS:
            entry = load(d, tool).get(key)
            if entry and "error" not in entry:
                out[tool] = result_pred(d, tool, entry)
                break
            if entry:
                out[tool] = None  # 변환 실패
                break
    return out


def score(truth: list[dict], pred: list[dict] | None) -> dict:
    if pred is None:
        return {"failed": True}
    out = {}
    if any(b["type"] == "heading" for b in truth):
        out["heading"] = heading_scores(truth, pred, match_blocks(truth, pred))
    if any(b["type"] == "table" for b in truth):
        out["table"] = table_scores(truth, pred)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--truth", type=Path, default=ROOT / "samples_rfp" / "real_truth")
    p.add_argument("--out", type=Path, default=Path(__file__).with_name("report.md"))
    args = p.parse_args()
    rows = []
    for tf in sorted(args.truth.glob("*.truth.json")):
        spec = json.loads(tf.read_text(encoding="utf-8"))
        source = ROOT / spec["source"]
        truth = spec["blocks"]
        n_head = sum(b["type"] == "heading" for b in truth)
        n_tab = sum(b["type"] == "table" for b in truth)
        for tool, pred in preds(source).items():
            rows.append({"doc": tf.name.split(".")[0], "format": source.suffix.lstrip(".").lower(), "tool": tool,
                         "n_head": n_head, "n_tab": n_tab, **score(truth, pred)})
            print(rows[-1]["doc"], tool, {k: v for k, v in rows[-1].items() if k in ("heading", "table", "failed")},
                  flush=True)
    args.out.write_text(report(rows), encoding="utf-8")
    print(report(rows))
    return 0


def report(rows: list[dict]) -> str:
    lines = ["# 실제 문서 구조 정확도", "", "`benchmarks/real_truth/score_real.py`로 만든 보고서. 정답 만든 방법은 `README.md`.", "",
             "## 문서별", "",
             "| 문서 | 형식 | 정답 제목·표 | 파서 | 제목 재현율 | 제목 정밀도 | 단계 일치 | 표 TEDS | 표 구조만 TEDS | 표 찾음 |",
             "|---|---|---|---|---:|---:|---:|---:|---:|---:|"]

    def f(v):
        return "" if v is None else f"{v:.3f}"

    for r in rows:
        if r.get("failed"):
            lines.append(f"| {r['doc']} | {r['format']} | {r['n_head']}·{r['n_tab']} | {r['tool']} | 실패 | | | | | |")
            continue
        h, t = r.get("heading") or {}, r.get("table") or {}
        lines.append(f"| {r['doc']} | {r['format']} | {r['n_head']}·{r['n_tab']} | {r['tool']} | {f(h.get('recall'))} | "
                     f"{f(h.get('precision'))} | {f(h.get('level'))} | {f(t.get('teds'))} | {f(t.get('teds_s'))} | "
                     f"{f(t.get('found'))} |")
    lines += ["", "## 파서별 평균 (돌린 문서만, 실패는 0으로)", "",
              "| 파서 | 문서 | 제목 재현율 | 제목 정밀도 | 표 TEDS | 표 구조만 TEDS |", "|---|---:|---:|---:|---:|---:|"]
    for tool in dict.fromkeys(r["tool"] for r in rows):
        mine = [r for r in rows if r["tool"] == tool and not r["doc"].endswith("_pages")]

        def mean(key, sub):
            vals = [0.0 if r.get("failed") else r[key][sub] for r in mine if r.get("failed") or key in r]
            return f"{statistics.mean(vals):.3f} ({len(vals)})" if vals else "-"

        lines.append(f"| {tool} | {len(mine)} | {mean('heading', 'recall')} | {mean('heading', 'precision')} | "
                     f"{mean('table', 'teds')} | {mean('table', 'teds_s')} |")
    lines += ["", "평균에서 법무부 표는 '하나의 표' 정답(moj)만 쓴다. '쪽마다 표' 정답(moj_pages) 결과는 문서별 표에 있다.",
              "괄호 안은 그 지표를 잰 문서 수다."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
