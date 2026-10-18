#!/usr/bin/env python3
"""다른 파서로 문서를 변환해 결과(마크다운)와 걸린 시간을 모은다. 비교용 가상 환경(.venv-compare)에서 돌린다.

    .venv-compare/Scripts/python benchmarks/compare/run_tools.py --list files.txt --out benchmarks/compare/out

파서마다 지원하는 형식만 돌린다. 결과는 out/<파서>/<번호>.md, 목록은 out/<파서>/index.json
({원본 경로: {"md": 파일 이름, "seconds": 초, "error": 오류}}). 이미 있는 결과는 건너뛴다 (--force로 다시).
점수는 프로젝트 환경에서 score.py로 낸다 (우리 파서·Docling도 거기서 돌린다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
KORDOC = HERE / "node_modules" / "kordoc" / "dist" / "cli.js"

# 파서 → 지원 형식 (각 파서 문서 기준)
TOOLS = {
    "markitdown": {".pdf", ".docx", ".pptx", ".xlsx"},
    "pymupdf4llm": {".pdf"},
    "opendataloader-pdf": {".pdf"},
    "hwp-hwpx-parser": {".hwp", ".hwpx"},
    "kordoc": {".hwp", ".hwpx", ".pdf", ".docx", ".xlsx", ".pptx"},
}


def run_markitdown(path: Path, ocr: bool) -> str:
    from markitdown import MarkItDown

    global _MD
    try:
        _MD
    except NameError:
        _MD = MarkItDown()
    return _MD.convert(str(path)).text_content


def run_pymupdf4llm(path: Path, ocr: bool) -> str:
    import pymupdf4llm

    return pymupdf4llm.to_markdown(str(path))


def run_opendataloader(path: Path, ocr: bool) -> str:
    import opendataloader_pdf

    with tempfile.TemporaryDirectory() as tmp:
        opendataloader_pdf.convert(input_path=str(path), output_dir=tmp, format="markdown", quiet=True)
        mds = list(Path(tmp).rglob("*.md"))
        return mds[0].read_text(encoding="utf-8") if mds else ""


def run_hwp_hwpx_parser(path: Path, ocr: bool) -> str:
    from hwp_hwpx_parser import Reader

    with Reader(str(path)) as r:
        res = r.extract_text_with_notes()
        text = getattr(res, "text", "") or ""
        notes = [getattr(n, "text", "") or "" for n in (getattr(res, "footnotes", None) or []) + (getattr(res, "endnotes", None) or [])]
        return text + ("\n\n" + "\n".join(notes) if notes else "")


def run_kordoc(path: Path, ocr: bool) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.md"
        cmd = ["node", str(KORDOC), str(path), "-o", str(out)] + (["--ocr"] if ocr else [])
        p = subprocess.run(cmd, capture_output=True, timeout=900)
        if p.returncode != 0 or not out.exists():
            raise RuntimeError(p.stderr.decode("utf-8", "replace").strip()[-300:] or f"exit {p.returncode}")
        return out.read_text(encoding="utf-8")


RUNNERS = {"markitdown": run_markitdown, "pymupdf4llm": run_pymupdf4llm, "opendataloader-pdf": run_opendataloader,
           "hwp-hwpx-parser": run_hwp_hwpx_parser, "kordoc": run_kordoc}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--list", type=Path, required=True, help="원본 파일 경로 목록 (한 줄에 하나, '|ocr'를 붙이면 스캔)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--tools", default=",".join(TOOLS))
    p.add_argument("--force", action="store_true")
    args = p.parse_args()
    items = []
    for line in args.list.read_text(encoding="utf-8").splitlines():
        if line.strip():
            path, _, flag = line.strip().partition("|")
            items.append((Path(path), flag == "ocr"))
    for tool in args.tools.split(","):
        folder = args.out / tool
        folder.mkdir(parents=True, exist_ok=True)
        index_path = folder / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        for path, ocr in items:
            if path.suffix.lower() not in TOOLS[tool] or (str(path) in index and not args.force):
                continue
            name = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:16] + ".md"
            t0 = time.perf_counter()
            try:
                text = RUNNERS[tool](path, ocr)
                (folder / name).write_text(text or "", encoding="utf-8")
                index[str(path)] = {"md": name, "seconds": round(time.perf_counter() - t0, 3)}
            except Exception as exc:  # noqa: BLE001 - 실패도 결과다
                index[str(path)] = {"error": f"{type(exc).__name__}: {exc}"[:300],
                                    "seconds": round(time.perf_counter() - t0, 3)}
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
            r = index[str(path)]
            print(f"{tool:20s} {path.name[:40]:40s} {r.get('seconds')}s {'오류 ' + r['error'][:60] if 'error' in r else ''}",
                  flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
