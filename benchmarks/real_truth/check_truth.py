#!/usr/bin/env python3
"""정답 파일을 원본과 맞대어 확인한다 (사람이 옮겨 적은 실수 찾기).

    python benchmarks/real_truth/check_truth.py samples_rfp/real_truth2/*.truth.json

- 글: 제목과 표 칸의 글(빈칸 빼고)이 원본 글에 있는가. PDF는 글자 층, HWPX·DOCX는 XML의 글.
- 표: 행·열·병합이 빈틈과 겹침 없이 맞물리는가 (모든 행의 칸 수가 같은 격자).
"""

from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def source_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        text = "".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
        pdf.close()
    else:
        from lxml import etree

        z = zipfile.ZipFile(path)
        text = ""
        for n in z.namelist():
            if n.endswith(".xml") and ("section" in n or "document" in n):
                root = etree.fromstring(z.read(n))
                text += "".join(t.text or "" for t in root.iter() if isinstance(t.tag, str) and etree.QName(t).localname == "t")
    return re.sub(r"\s", "", text)


def grid_ok(rows: list[list[dict]]) -> str | None:
    taken: set[tuple[int, int]] = set()
    width = None
    for r, row in enumerate(rows):
        col = 0
        for cell in row:
            while (r, col) in taken:
                col += 1
            for dr in range(cell.get("rowspan", 1)):
                for dc in range(cell.get("colspan", 1)):
                    if (r + dr, col + dc) in taken:
                        return f"{r}행 칸이 겹친다"
                    taken.add((r + dr, col + dc))
            col += cell.get("colspan", 1)
        while (r, col) in taken:
            col += 1
        width = width if width is not None else col
        if col != width:
            return f"{r}행 폭 {col} (첫 행 {width})"
    if any(rr >= len(rows) for rr, _ in taken):
        return "아래로 넘치는 세로 병합"
    return None


def main() -> int:
    bad = 0
    for tf in sys.argv[1:]:
        spec = json.loads(Path(tf).read_text(encoding="utf-8"))
        text = source_text(ROOT / spec["source"])
        for k, b in enumerate(spec["blocks"]):
            texts = [b["text"]] if b["type"] == "heading" else [c["text"] for r in b["rows"] for c in r]
            for t in texts:
                key = re.sub(r"\s", "", t)
                if key and key not in text:
                    bad += 1
                    print(f"{Path(tf).name} 블록 {k}: 원본에 없는 글 {t[:50]!r}")
            if b["type"] == "table" and (err := grid_ok(b["rows"])):
                bad += 1
                print(f"{Path(tf).name} 블록 {k}: 표 격자 {err}")
    print("문제", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
