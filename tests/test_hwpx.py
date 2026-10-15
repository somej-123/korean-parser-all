"""HWPX 파서 테스트. 파일 출처는 tests/fixtures/hwpx/NOTICE.md (hwpxlib, Apache-2.0)."""

from pathlib import Path

import pytest

from korean_parser.ir import Drawing, Equation, Heading, Paragraph, Table
from korean_parser.parsers import detect_format, parse
from korean_parser.render.html import render_html
from korean_parser.render.llm import render_llm

FIX = Path(__file__).parent / "fixtures" / "hwpx"


def blocks_of(doc, kind):
    return [b for b in doc.iter_blocks() if isinstance(b, kind)]


def test_format_detection():
    assert detect_format(FIX / "para_head.hwpx") == "hwpx"


def test_numbering_bullets_and_outline():
    doc = parse(FIX / "para_head.hwpx")
    items = [(b.list_info.marker, b.text) for b in doc.iter_blocks()
             if isinstance(b, (Paragraph, Heading)) and b.list_info and b.text]
    assert ("●", "글머리1-1") in items and ("■", "글머리2-1") in items
    # 중간 단계를 건너뛴 번호도 한컴오피스와 같게 이어진다
    assert ("4.1.1.1.", "번호-4-1-1-1") in items
    assert ("4.1.2.", "번호-4-1-2") in items
    assert ("4.1.2.1.", "번호-4-1-2-1") in items
    # 개요 문단은 제목 계층이 된다 (1. → 가. → 1))
    headings = [(h.level, h.list_info.marker if h.list_info else "", h.text) for h in blocks_of(doc, Heading)]
    assert headings[:3] == [(1, "1.", "개요1"), (2, "가.", "개요1-1"), (2, "나.", "개요1-2")]
    assert (1, "2.", "개요2") in headings and (2, "가.", "개요2-1") in headings and (3, "1)", "개요2-1-1") in headings


def test_merged_table():
    doc = parse(FIX / "simple_table.hwpx")
    table = blocks_of(doc, Table)[0]
    first = table.rows[0].cells[0]
    assert (first.row_span, first.col_span) == (2, 2)
    assert [len(r.cells) for r in table.rows] == [2, 1, 2]
    assert len(table.col_widths_pt) == 3 and all(w > 0 for w in table.col_widths_pt)
    html = render_html(doc)
    assert 'rowspan="2" colspan="2"' in html


def test_connect_line_links_shapes():
    doc = parse(FIX / "connect_line.hwpx")
    shapes = [s for d in blocks_of(doc, Drawing) for s in d.shapes]
    line = next(s for s in shapes if s.is_line)
    ids = {s.id for s in shapes if not s.is_line}
    assert line.connect_from in ids and line.connect_to in ids
    assert line.path and line.path.count("L") == 2  # 오른쪽 변에서 나와 아래 변으로 들어가는 ㄱ자 경로


def test_group_transforms():
    doc = parse(FIX / "container.hwpx")
    drawing = blocks_of(doc, Drawing)[0]
    images = [s for s in drawing.shapes if s.image_id]
    assert len(images) == 2
    # 묶음 안 그림은 묶음 크기 안에 들어와야 한다
    for s in images:
        assert -1 <= s.x and s.x + s.width <= drawing.width_pt + 1
        assert -1 <= s.y and s.y + s.height <= drawing.height_pt + 1


def test_equation():
    doc = parse(FIX / "equation.hwpx")
    eqs = [i for b in doc.iter_blocks() for i in getattr(b, "inlines", []) if isinstance(i, Equation)]
    assert eqs and "over" in eqs[0].script
    assert "[수식:" in render_llm(doc)
    assert 'class="kp-equation"' in render_html(doc)


def test_page_background_and_floating_table():
    doc = parse(FIX / "project_plan.hwpx")
    assert doc.meta.title == "업무제안서" and doc.meta.language == "ko"
    assert doc.page.background_image_id in doc.images  # 왼쪽 남색 띠는 쪽 배경 그림
    floating = [t for t in blocks_of(doc, Table) if t.anchor is not None]
    assert floating and floating[0].anchor.x_pt == pytest.approx(143.6)
    html = render_html(doc)
    assert "background-image:url(" in html and "kp-anchor-host" in html
    assert "작성일자" in render_llm(doc)


def test_margins_use_real_values_not_doubled_compat_values():
    """hp:switch의 hp:default(구버전 호환)에는 문단 여백이 2배로 들어 있다. hp:case 값을 써야 한다."""
    from lxml import etree

    from korean_parser.parsers.hwpx.header import Header

    xml = b"""<hh:head xmlns:hh="h" xmlns:hp="p" xmlns:hc="c"><hh:refList><hh:paraProperties>
      <hh:paraPr id="0"><hh:align horizontal="LEFT"/><hp:switch>
        <hp:case><hh:margin><hc:intent value="0"/><hc:left value="1500"/></hh:margin></hp:case>
        <hp:default><hh:margin><hc:intent value="0"/><hc:left value="3000"/></hh:margin></hp:default>
      </hp:switch></hh:paraPr></hh:paraProperties></hh:refList></hh:head>"""
    header = Header(etree.fromstring(xml))
    assert header.para_defs["0"].style.indent_left_pt == 15.0


def test_all_fixtures_render():
    for f in FIX.glob("*.hwpx"):
        doc = parse(f)
        assert render_html(doc).startswith("<!doctype html>")
        render_llm(doc)
