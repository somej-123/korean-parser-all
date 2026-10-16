"""PPTX·XLSX 파서. 시험 문서는 tests/fixtures/make_office.py로 만든다."""

from pathlib import Path

import pytest
from lxml import etree

from korean_parser.ir import Chart, Figure, Heading, PageBreak, Paragraph, Table
from korean_parser.parsers import detect_format, parse
from korean_parser.parsers.xlsx import XlsxParser, format_number

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def deck():
    return parse(FIX / "sample.pptx")


@pytest.fixture(scope="module")
def book():
    return parse(FIX / "sample.xlsx")


def cell_texts(t: Table):
    return [[" ".join(p.text for p in c.blocks) for c in r.cells] for r in t.rows]


# -- PPTX ---------------------------------------------------------------------


def test_pptx_detect_and_meta(deck):
    assert detect_format(FIX / "sample.pptx") == "pptx"
    assert deck.meta.page_count == 4 and deck.meta.page_count_source == "layout"
    assert deck.page.width_pt == 720.0
    assert sum(isinstance(b, PageBreak) for b in deck.blocks) == 3


def test_pptx_titles_are_headings_with_slide_page(deck):
    hs = [b for b in deck.blocks if isinstance(b, Heading)]
    assert [h.text for h in hs] == ["2026년 문서 파싱 고도화 계획", "추진 배경", "예산 계획", "분기별 처리량"]
    assert [h.prov.page for h in hs] == [1, 2, 3, 4]
    assert hs[0].prov.bbox is not None  # 자리 표시자 위치는 레이아웃에서 물려받는다


def test_pptx_bullets_with_levels(deck):
    items = [(b.list_info.level, b.text) for b in deck.blocks
             if isinstance(b, Paragraph) and b.list_info and b.prov.page == 2]
    assert items == [(0, "문서 파싱 품질 문제"), (1, "표 구조 손실"), (1, "띄어쓰기 소실"), (0, "처리 속도 문제")]


def test_pptx_table_merges(deck):
    t = next(b for b in deck.blocks if isinstance(b, Table))
    assert cell_texts(t)[:2] == [["구분", "2026년"], ["상반기", "하반기"]]
    assert (t.rows[0].cells[0].row_span, t.rows[0].cells[1].col_span) == (2, 2)


def test_pptx_chart_picture_group_and_notes(deck):
    chart = next(b for b in deck.blocks if isinstance(b, Chart))
    assert chart.categories == ["1분기", "2분기", "3분기"] and chart.series[0].values == [120, 180, 240]
    fig = next(b for b in deck.blocks if isinstance(b, Figure))
    assert fig.alt == "처리 흐름 그림" and fig.image_id in deck.images
    texts = [b.text for b in deck.blocks if isinstance(b, Paragraph)]
    assert "※ 자료: 내부 집계" in texts  # 묶음 안 글상자
    notes = [b for b in deck.blocks if isinstance(b, Paragraph) and b.style_name == "slide_notes"]
    assert [n.text for n in notes] == ["발표 시 띄어쓰기 사례를 함께 보여 줄 것"] and notes[0].prov.page == 2


# -- XLSX ---------------------------------------------------------------------


def test_xlsx_sheets_tables_and_display_values(book):
    assert detect_format(FIX / "sample.xlsx") == "xlsx"
    assert [b.text for b in book.blocks if isinstance(b, Heading)] == ["예산", "일정", "숨김"]
    assert book.warnings == ["숨긴 시트도 넣음: 숨김"]
    t = next(b for b in book.blocks if isinstance(b, Table))
    rows = cell_texts(t)
    assert rows[0] == ["구분", "2026년", "비고"] and rows[1] == ["상반기", "하반기"]
    assert rows[2] == ["인건비", "1,000,000", "1,200,000", "단위: 원"]
    assert rows[3][:3] == ["장비비", "50,000.50", "25%"]
    assert rows[4][:2] == ["착수일", "2026-03-02"]
    assert rows[5][:2] == ["합계", "=B3+B4"]  # 계산 결과가 저장되지 않은 수식은 수식 그대로
    first = t.rows[0].cells
    assert (first[0].row_span, first[1].col_span, first[2].row_span) == (2, 2, 2)
    assert first[0].background == "#DDEBF7" and first[0].blocks[0].inlines[0].style.bold


@pytest.mark.parametrize("value, code, is_date, want", [
    (1234.5, "#,##0", False, "1,235"),
    (0.125, "0.0%", False, "12.5%"),
    (3.0, None, False, "3"),
    (0.1 + 0.2, None, False, "0.3"),
    (46083.0, "yyyy-mm-dd", True, "2026-03-02"),
    (46083.5, "yyyy-mm-dd hh:mm", True, "2026-03-02 12:00"),
])
def test_number_formats(value, code, is_date, want):
    assert format_number(value, code, is_date) == want


def test_cached_formula_value_wins():
    parser = XlsxParser.__new__(XlsxParser)
    parser.strings, parser.xf, parser.formats, parser.date1904 = [], [], {}, False
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    c = etree.fromstring(f'<c xmlns="{s}" r="B6"><f>B3+B4</f><v>1050000.5</v></c>')
    assert parser._value(c) == "1050000.5"
