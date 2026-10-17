"""기준 3(필수 요소) 확인: 표본 문서의 '들어 있어야 할 요소 목록'(manifest JSON)을 파서 결과와 맞춰 본다.

목록은 표본을 만드는 스크립트(benchmarks/goal/samples/make_*.py)가 문서와 함께 쓴다. 항목:

    headings    [[글, 단계], …]            Heading 블록 (번호 표지를 붙인 글이나 본문 글이 같고 단계가 같다)
    list_items  [[표지, 글], …]            목록 문단 (자동 번호 표지 또는 글 앞의 기호가 표지와 같다)
    tables      [{rows, cols, spans}, …]   표 크기와 병합 칸 [행, 열, 행 수, 열 수]
    footnotes   [글, …]                    각주·미주 (doc.notes)
    header, footer  [글, …]                머리말·꼬리말
    links       [[글, 주소], …]            링크가 달린 글
    images      개수                        그림 (ImageRef·Figure·그림 도형)
    equations   [글, …]                    수식 스크립트 (공백 빼고 포함)
    texts       [글, …]                    어디든 나와야 하는 글 (글상자 안 글, 캡션 등)
    charts      [{categories, series}, …]  차트 분류와 계열 이름
"""

from __future__ import annotations

from korean_parser.ir import (
    Chart, Drawing, Equation, Figure, Heading, ImageRef, Paragraph, Table, Text, walk_blocks,
)


def _flat(s: str) -> str:
    return "".join((s or "").split())


def _all_blocks(doc):
    for blocks in [doc.header, doc.blocks, doc.footer] + [n.blocks for n in doc.notes.values()]:
        yield from walk_blocks(blocks)


def _texts(doc) -> str:
    parts = []
    for b in _all_blocks(doc):
        if hasattr(b, "inlines"):
            marker = b.list_info.marker + " " if getattr(b, "list_info", None) else ""
            parts.append(marker + "".join(getattr(i, "text", "") or "" for i in b.inlines))
        if isinstance(b, Figure):
            parts += [b.caption or "", b.inner_text or "", b.alt or ""]
    return _flat(" ".join(parts))


def _grid(t: Table) -> list[tuple[int, int, int, int]]:
    """병합 칸의 (행, 열, 행 수, 열 수). 앞 행의 세로 병합이 덮는 자리는 건너뛴다."""
    taken: set[tuple[int, int]] = set()
    out = []
    for r, row in enumerate(t.rows):
        c = 0
        for cell in row.cells:
            while (r, c) in taken:
                c += 1
            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    taken.add((r + dr, c + dc))
            if cell.row_span > 1 or cell.col_span > 1:
                out.append((r, c, cell.row_span, cell.col_span))
            c += cell.col_span
    return out


def check(doc, m: dict) -> list[tuple[str, bool, str]]:
    """(요소, 통과, 설명) 목록."""
    out: list[tuple[str, bool, str]] = []
    blocks = list(_all_blocks(doc))
    if "headings" in m:
        hs = [(_flat(h.text), _flat((h.list_info.marker + " " if h.list_info else "") + h.text), h.level)
              for h in blocks if isinstance(h, Heading)]
        for text, level in m["headings"]:
            hit = next((lv for a, b, lv in hs if _flat(text) in (a, b)), None)
            out.append(("제목", hit == level, f"{text} (단계 {level}, 결과 {hit})"))
    if "list_items" in m:
        items = []
        for p in blocks:
            if isinstance(p, Paragraph):
                marker = p.list_info.marker if p.list_info else ""
                items.append((marker, _flat(p.text)))
        for marker, text in m["list_items"]:
            ok = any(_flat(text) in t and (mk == marker or t.startswith(_flat(marker))) for mk, t in items)
            out.append(("목록", ok, f"{marker} {text}"))
    if "tables" in m:
        tables = [t for t in blocks if isinstance(t, Table)]
        for spec in m["tables"]:
            want = sorted(tuple(x) for x in spec.get("spans", []))
            hit = next((t for t in tables if len(t.rows) == spec["rows"]
                        and max((sum(c.col_span for c in r.cells) for r in t.rows[:1]), default=0) == spec["cols"]
                        and sorted(_grid(t)) == want), None)
            out.append(("표", hit is not None, f"{spec['rows']}×{spec['cols']} 병합 {want}"))
    if "footnotes" in m:
        notes = [_flat(" ".join(getattr(b, "text", "") for b in n.blocks)) for n in doc.notes.values()]
        for text in m["footnotes"]:
            out.append(("각주", any(_flat(text) in n for n in notes), text))
    for key, name in (("header", "머리말"), ("footer", "꼬리말")):
        got = _flat(" ".join(getattr(b, "text", "") for b in walk_blocks(getattr(doc, key))))
        for text in m.get(key, []):
            out.append((name, _flat(text) in got, text))
    if "links" in m:
        links = [(_flat(i.text), i.link) for b in blocks for i in getattr(b, "inlines", []) if isinstance(i, Text) and i.link]
        for text, url in m["links"]:
            # 주소 끝의 '/'는 만든 프로그램이 붙이기도 한다 (LibreOffice: https://www.g2b.go.kr → …/)
            ok = any(_flat(text) in t and (u or "").rstrip("/") == url.rstrip("/") for t, u in links)
            out.append(("링크", ok, f"{text} → {url}"))
    if "images" in m:
        n = sum(isinstance(i, ImageRef) for b in blocks for i in getattr(b, "inlines", []))
        n += sum(isinstance(b, Figure) for b in blocks)
        n += sum(1 for b in blocks if isinstance(b, Drawing) for s in b.shapes if s.image_id)
        out.append(("그림", n >= m["images"], f"{m['images']}개 (결과 {n})"))
    if "equations" in m:
        eqs = [_flat(i.script) for b in blocks for i in getattr(b, "inlines", []) if isinstance(i, Equation)]
        for text in m["equations"]:
            out.append(("수식", any(_flat(text) in e for e in eqs), text))
    if "charts" in m:
        charts = [c for c in blocks if isinstance(c, Chart)]
        for spec in m["charts"]:
            ok = any(c.categories == spec["categories"] and [s.name for s in c.series] == spec["series"] for c in charts)
            out.append(("차트", ok, f"{spec['series']} × {spec['categories']}"))
    if "texts" in m:
        all_text = _texts(doc)
        for text in m["texts"]:
            out.append(("글", _flat(text) in all_text, text))
    return out
