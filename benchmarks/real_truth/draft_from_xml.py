#!/usr/bin/env python3
"""HWPX·DOCX의 표 정의와 문단을 파일 XML에서 직접 읽어 정답 초안을 만든다 (우리 파서를 쓰지 않는다).

    python benchmarks/real_truth/draft_from_xml.py <문서> > 초안.json

표: 행·열·병합(HWPX cellAddr·cellSpan, DOCX gridSpan·vMerge)과 칸 글. 문단: 글과 문단 모양·스타일 이름(제목 판단용).
초안은 사람이 원본을 보며 고친다 (제목은 사람이 정한다). 표 안의 표는 바깥 칸의 글로만 넣는다.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

from lxml import etree

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text(el, t_tag) -> str:
    return " ".join("".join(t.text or "" for t in el.iter(t_tag)).split())


def hwpx(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    names = sorted(n for n in z.namelist() if n.startswith("Contents/section") and n.endswith(".xml"))
    header = etree.fromstring(z.read("Contents/header.xml"))
    styles = {s.get("id"): s.get("name") for s in header.iter("{http://www.hancom.co.kr/hwpml/2011/head}style")}
    blocks = []
    for n in names:
        root = etree.fromstring(z.read(n))
        for p in root.iter(HP + "p"):
            if any(a.tag == HP + "tbl" for a in p.iterancestors()):
                continue  # 표 안 문단은 표에서
            for tbl in p.iter(HP + "tbl"):
                if any(a.tag == HP + "tbl" for a in tbl.iterancestors()):
                    continue
                cells = {}
                for tc in tbl.iter(HP + "tc"):
                    if next((a for a in tc.iterancestors() if a.tag == HP + "tbl"), None) is not tbl:
                        continue
                    addr, span = tc.find(HP + "cellAddr"), tc.find(HP + "cellSpan")
                    r, c = int(addr.get("rowAddr")), int(addr.get("colAddr"))
                    cells[(r, c)] = {"text": _text(tc, HP + "t"), "rowspan": int(span.get("rowSpan", 1)),
                                     "colspan": int(span.get("colSpan", 1))}
                rows = [[cells[k] for k in sorted(cells) if k[0] == r] for r in sorted({k[0] for k in cells})]
                blocks.append({"type": "table", "rows": rows})
            text = "".join(t.text or "" for run in p.findall(HP + "run") for t in run.findall(HP + "t")).strip()
            if text:
                blocks.append({"type": "paragraph", "text": " ".join(text.split()),
                               "style": styles.get(p.get("styleIDRef"))})
    return {"source": path.name, "blocks": blocks}


def docx(path: Path) -> dict:
    z = zipfile.ZipFile(path)
    root = etree.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    blocks = []
    for el in body:
        if el.tag == W + "p":
            text = _text(el, W + "t")
            style = el.find(f"{W}pPr/{W}pStyle")
            if text:
                blocks.append({"type": "paragraph", "text": text, "style": style.get(W + "val") if style is not None else None})
        elif el.tag == W + "tbl":
            grid = []  # 행마다 [(시작 열, 칸, vMerge)]
            for tr in el.findall(W + "tr"):
                col, row = 0, []
                for tc in tr.findall(W + "tc"):
                    pr = tc.find(W + "tcPr")
                    span = pr.find(W + "gridSpan") if pr is not None else None
                    vm = pr.find(W + "vMerge") if pr is not None else None
                    n = int(span.get(W + "val")) if span is not None else 1
                    row.append((col, {"text": _text(tc, W + "t"), "rowspan": 1, "colspan": n},
                                None if vm is None else (vm.get(W + "val") or "continue")))
                    col += n
                grid.append(row)
            rows = []
            for i, row in enumerate(grid):
                out = []
                for col, cell, vm in row:
                    if vm == "continue":
                        continue
                    if vm == "restart":
                        k = i + 1
                        while k < len(grid) and any(c == col and v == "continue" for c, _, v in grid[k]):
                            k += 1
                        cell["rowspan"] = k - i
                    out.append(cell)
                rows.append(out)
            blocks.append({"type": "table", "rows": rows})
    return {"source": path.name, "blocks": blocks}


if __name__ == "__main__":
    p = Path(sys.argv[1])
    data = hwpx(p) if p.suffix.lower() == ".hwpx" else docx(p)
    json.dump(data, sys.stdout, ensure_ascii=False, indent=1)
