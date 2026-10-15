#!/usr/bin/env python3
"""여러 문서(DOCX, HWPX)를 한꺼번에 파싱·렌더링해서 실패, 경고, 처리 시간, 추출 요소 수를 집계한다.

    ./fetch_samples.sh samples_web
    python run.py samples_web --out corpus_report.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from korean_parser.ir import Chart, Drawing, Heading, ImageRef, Paragraph, Table  # noqa: E402
from korean_parser.parsers import parse  # noqa: E402
from korean_parser.render.html import render_html  # noqa: E402
from korean_parser.render.llm import render_llm  # noqa: E402


def stats(doc) -> Counter:
    c = Counter()
    for b in doc.iter_blocks():
        c[type(b).__name__] += 1
        if isinstance(b, Table) and b.has_merged_cells:
            c["merged_table"] += 1
        if isinstance(b, (Paragraph, Heading)):
            if b.list_info:
                c["list_item"] += 1
            c["image"] += sum(isinstance(i, ImageRef) for i in b.inlines)
        if isinstance(b, Drawing):
            c["shape"] += len(b.shapes)
    c["note"] = len(doc.notes)
    return c


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out", default="corpus_report.md")
    args = p.parse_args()

    root = Path(args.folder)
    files = sorted(
        f for f in root.rglob("*")
        if f.suffix.lower() in (".docx", ".hwpx")
        and not any(part.startswith(".") for part in f.relative_to(root).parts)  # 내려받기용 원본 저장소 제외
    )
    ok, failed, times = [], [], []
    total = Counter()
    warnings = Counter()
    for f in files:
        t0 = time.perf_counter()
        try:
            doc = parse(f)
            render_html(doc)
            render_llm(doc)
        except Exception as exc:  # noqa: BLE001
            failed.append((f, exc, traceback.format_exc(limit=3)))
            continue
        times.append(time.perf_counter() - t0)
        ok.append(f)
        total += stats(doc)
        for w in doc.warnings:
            warnings[w.split(":")[0]] += 1

    lines = [
        "# 문서 코퍼스 실행 결과",
        "",
        f"- 문서 {len(files)}개 (DOCX {sum(f.suffix.lower() == '.docx' for f in files)}, "
        f"HWPX {sum(f.suffix.lower() == '.hwpx' for f in files)}) 중 성공 {len(ok)}개, 실패 {len(failed)}개",
        f"- 문서당 처리 시간(파싱+HTML+LLM): 중앙값 {statistics.median(times) * 1000:.0f}ms, "
        f"최대 {max(times) * 1000:.0f}ms" if times else "",
        "",
        "## 추출한 요소",
        "",
        "| 요소 | 개수 |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(total.items())],
        "",
        "## 경고 종류",
        "",
        *[f"- {k}: {v}건" for k, v in warnings.most_common()],
    ]
    if failed:
        lines += ["", "## 실패", ""]
        for f, exc, tb in failed:
            lines += [f"### {f.name}", "", f"`{exc!r}`", "", "```", tb.strip(), "```", ""]
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:4]))
    print(f"리포트: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
