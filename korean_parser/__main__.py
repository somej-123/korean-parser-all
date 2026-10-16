"""명령줄 도구.

    python -m korean_parser convert 문서.docx -o out/
    python -m korean_parser convert 문서.docx -o out/ --formats html,llm,json
    python -m korean_parser convert 문서.hwpx -o out/ --formats chunks --max-tokens 500
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .chunk import chunk
from .parsers import parse, supported_formats
from .render.html import render_html
from .render.llm import render_llm


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="korean_parser", description="한국어 문서 파서")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help=f"문서를 HTML/LLM 텍스트/JSON으로 변환 (지원: {', '.join(supported_formats())})")
    c.add_argument("inputs", nargs="+", help="문서 파일")
    c.add_argument("-o", "--out", default="out", help="결과 폴더")
    c.add_argument("--formats", default="html,llm,json",
                   help="html, llm, json, chunks(검색용 조각, JSON Lines) 중 쉼표로 구분")
    c.add_argument("--max-tokens", type=int, default=500, help="chunks: 조각 하나의 토큰 상한 (어림값)")
    c.add_argument("--no-korean", action="store_true", help="한국어 처리 레이어 끄기")
    c.add_argument("--layout", choices=["auto", "flow", "fixed"], default="auto",
                   help="HTML 배치: auto(기본), flow(문서 흐름형), fixed(원본 쪽 모양, PDF만)")
    args = p.parse_args(argv)

    formats = {f.strip() for f in args.formats.split(",") if f.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    for src in map(Path, args.inputs):
        t0 = time.perf_counter()
        try:
            doc = parse(src, korean=not args.no_korean)
        except Exception as exc:  # noqa: BLE001 - 여러 파일을 처리하므로 계속 진행한다
            failed += 1
            print(f"실패  {src.name}: {exc}", file=sys.stderr)
            continue
        # with_suffix는 쓰지 않는다: '붙임. 3.(제안요청서) …'처럼 이름 가운데 점이 있으면 그 뒤를 확장자로 보고 잘라 버려
        # 같은 공고의 붙임 파일들이 서로 덮어썼다
        def target(ext: str, src=src) -> Path:
            return out / f"{src.stem}{ext}"

        if "html" in formats:
            target(".html").write_text(render_html(doc, layout=args.layout), encoding="utf-8")
        if "llm" in formats:
            target(".llm.md").write_text(render_llm(doc), encoding="utf-8")
        if "json" in formats:
            target(".json").write_text(doc.model_dump_json(indent=1, exclude_none=True), encoding="utf-8")
        if "chunks" in formats:
            lines = (c.model_dump_json(exclude_none=True) for c in chunk(doc, max_tokens=args.max_tokens))
            target(".chunks.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        warn = f" (경고 {len(doc.warnings)}건)" if doc.warnings else ""
        print(f"완료  {src.name}  {time.perf_counter() - t0:.2f}s  블록 {len(doc.blocks)}개{warn}")
        for w in doc.warnings:
            print(f"      - {w}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
