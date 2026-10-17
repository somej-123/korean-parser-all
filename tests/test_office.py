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
    # 단위·통화 글자는 값의 일부다 (예전에는 '1,500,000'만 남았다)
    (1500000, '#,##0"원"', False, "1,500,000원"),
    (1500000, '"₩"#,##0', False, "₩1,500,000"),
    (1500000, "[$₩-412]#,##0", False, "₩1,500,000"),
    (5, "0\개", False, "5개"),
    # 음수·0 부분: 괄호 음수, 회계 형식의 0은 '-'
    (-1500, "#,##0;(#,##0)", False, "(1,500)"),
    (0, '_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', False, "-"),
    (-1234, '_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-', False, "-1,234"),
    # 분수, 지수, 천 단위 나눔, 글 자리
    (1.5, "# ?/?", False, "1 1/2"),
    (0.3, "?/8", False, "2/8"),
    (12345.678, "0.00E+00", False, "1.23E+04"),
    (1234567, "#,##0,", False, "1,235"),
    (3, "@", False, "3"),
])
def test_number_formats(value, code, is_date, want):
    assert format_number(value, code, is_date) == want


def test_cached_formula_value_wins():
    parser = XlsxParser.__new__(XlsxParser)
    parser.strings, parser.xf, parser.formats, parser.date1904 = [], [], {}, False
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    c = etree.fromstring(f'<c xmlns="{s}" r="B6"><f>B3+B4</f><v>1050000.5</v></c>')
    assert parser._value(c) == "1050000.5"


# -- 자리 표시자 없이 글상자로 만든 PPTX (PptxGenJS 같은 도구) ------------------------


@pytest.fixture(scope="module")
def plain():
    return parse(FIX / "sample_plain.pptx")


def test_plain_slides_repeated_footer_and_page_numbers(plain):
    # 슬라이드마다 같은 자리의 바닥글은 꼬리말로 한 번만, 쪽 번호('3 / 4')는 버린다
    assert [b.text for b in plain.footer] == ["내부 검토용 자료"] and not plain.header
    texts = [b.text for b in plain.blocks if isinstance(b, Paragraph)]
    assert "내부 검토용 자료" not in texts and not any("/ 4" in t for t in texts)
    # 숫자가 슬라이드 번호와 다른 위쪽 이름표('방법 4')는 내용이라 남는다
    assert [t for t in texts if t.startswith("방법")] == ["방법 4", "방법 3", "방법 2", "방법 1"]


def test_plain_slides_title_guess_and_column_order(plain):
    first = [b for b in plain.blocks if isinstance(b, Paragraph) and b.prov.page == 1]
    guessed = [b for b in first if b.heading_guess]
    assert [b.text for b in guessed] == ["검토 항목 1"] and guessed[0].heading_guess.level == 1
    assert "32pt" in guessed[0].heading_guess.reason
    # 두 단: 왼쪽 단을 다 읽고 오른쪽 단으로 (줄마다 번갈아 읽지 않는다)
    order = [b.text for b in first if b.text in ("장점", "+ 처리가 빠르다", "단점", "- 값이 비싸다")]
    assert order == ["장점", "+ 처리가 빠르다", "단점", "- 값이 비싸다"]


def test_plain_slides_path_alt_becomes_file_name(plain):
    fig = next(b for b in plain.blocks if isinstance(b, Figure))
    assert fig.alt == "icon_check.png"


def test_xy_order_reads_cards_one_by_one():
    from korean_parser.parsers.pptx import xy_order

    def item(name, x, y, w=80, h=20):
        return ((1, (x, y, x + w, y + h)), [name])

    # 제목 한 줄 + 카드 세 장(카드마다 위·아래 두 줄). 줄마다가 아니라 카드마다 읽는다
    items = [item("제목", 10, 10, 400), item("1구간", 10, 60), item("2구간", 150, 60), item("3구간", 290, 60),
             item("200만원", 10, 85), item("1,000만원", 150, 85), item("2,500만원", 290, 85)]
    assert [b for _, (b,) in xy_order(items)] == ["제목", "1구간", "200만원", "2구간", "1,000만원", "3구간", "2,500만원"]


def test_xlsx_chart_without_cache_reads_cells(book):
    chart = next(b for b in book.blocks if isinstance(b, Chart))
    assert chart.title == "단계별 진행률" and chart.categories == ["분석", "설계"]
    assert chart.series[0].name == "진행률" and chart.series[0].values == [1.0, 0.45]
    assert chart.prov.page == 2  # '일정' 시트의 표 뒤


# -- 연결선으로 이은 도형 묶음 → 도식 ----------------------------------------------


def test_connected_shapes_become_one_drawing():
    # 실제 화면설계서의 '메뉴구조도'(연결선 18개)가 상자 글만 흩어져 나와 위아래 관계가 사라졌다
    from korean_parser.ir import Drawing
    from korean_parser.render.llm import render_llm

    doc = parse(FIX / "sample_diagram.pptx")
    chart, flow = [b for b in doc.blocks if isinstance(b, Drawing)]
    labels = {s.text_blocks[0].text for s in chart.shapes if s.text_blocks}
    assert labels == {"산업안전", "안전교육", "안전점검", "교육현황"}  # 연결선 없이 쌓은 '교육현황'도 같은 열
    assert sum(s.is_line for s in chart.shapes) == 2 and all(s.id.startswith("s1:") for s in chart.shapes)
    assert min(s.x for s in chart.shapes) == 0 and chart.prov.bbox[0] == pytest.approx(140, abs=1)
    text = render_llm(doc, with_ids=False)
    assert "- 산업안전 — 안전교육" in text and "- 산업안전 — 안전점검" in text
    assert "- 접수 → 처리" in text  # 연결 정보 없이 끝점만 닿은 화살표
    # 도식에 들어간 글은 따로 문단으로 나오지 않고, 이어지지 않은 장식 사각형은 경고만
    assert [b.text for b in doc.blocks if isinstance(b, Paragraph)] == ["메뉴구조도"]
    assert any("장식" in w for w in doc.warnings) and flow.shapes


# -- 원래 배치 화면 (고정 쪽) ------------------------------------------------------


@pytest.fixture(scope="module")
def layout():
    return parse(FIX / "sample_layout.pptx")


def test_fixed_slide_draws_shapes_in_order_with_background(layout):
    # 슬라이드마다 고정 쪽 하나. 마스터의 장식 띠가 맨 아래, 그다음 슬라이드 도형을 원본 그리는 차례대로
    assert len(layout.fixed_pages) == layout.meta.page_count == 1
    fp = layout.fixed_pages[0]
    assert (fp.page, fp.width_pt, fp.height_pt, fp.background) == (1, 720.0, 540.0, "#F5F3EE")
    band, card, box, clear, table = fp.shapes
    assert band.fill == "#1E2761" and band.width == 720 and not band.block_ids  # 마스터 도형은 본문에 넣지 않는다
    assert card.geometry == "roundRect" and card.adjust == {"adj": 5000} and card.fill == "#FBEAEA"
    # 글이 있는 도형은 본문 블록을 id로 가리킨다 (같은 글을 두 번 담지 않는다)
    by_id = {b.id: b for b in layout.blocks}
    assert [by_id[i].text for i in box.block_ids] == ["클라우드 AI", "외부 서버로 전송"]
    assert isinstance(by_id[table.block_ids[0]], Table)
    assert clear.fill is None  # 완전히 투명한(alpha 0) 채우기는 없는 것으로


def test_fixed_slide_text_box_properties(layout):
    box = layout.fixed_pages[0].shapes[2]
    assert box.text_insets_pt == (0, 0, 0, 0) and box.text_wrap is False
    # 크기가 없는 런은 마스터 otherStyle(18pt)과 테마 글꼴을 물려받는다. 본문 블록에는 넣지 않는다
    assert box.text_style.size_pt == 18 and box.text_style.font == "맑은 고딕"
    first, second = (b for b in layout.blocks if isinstance(b, Paragraph) and b.prov.bbox[1] == 120)
    assert first.inlines[0].style is None or first.inlines[0].style.size_pt is None
    # 슬라이드에 적힌 문단 모양(줄 간격, 문단 앞 간격)과 자간은 본문 블록에 그대로
    assert (second.style.line_spacing, second.style.space_before_pt) == (1.2, 6)
    assert second.inlines[0].style.letter_spacing_em == pytest.approx(0.1)


def test_table_style_borders_come_from_file(layout):
    # 칸에 선이 없으면 표 스타일(tableStyles.xml)의 전체 표 선을 쓴다 (실제 화면설계서의 표 43개)
    t = next(b for b in layout.blocks if isinstance(b, Table))
    b = t.rows[0].cells[0].borders
    assert (b.top.color, b.left.color, b.right.color) == ("#9E9E9E",) * 3
    assert t.cell_padding_pt == (3.6, 7.2, 3.6, 7.2)  # PowerPoint 기본 칸 여백


def test_fixed_slide_html_and_flow_unchanged(layout):
    from korean_parser.render.html import render_html

    html = render_html(layout)  # 고정 쪽이 있으면 기본이 원래 배치
    assert 'class="kp-fixed-page kp-fixed-slide" data-page="1"' in html and 'fill="#F5F3EE"' in html
    box = layout.fixed_pages[0].shapes[2]
    assert f'data-kp-id="{box.block_ids[0]}"' in html  # 화면 강조가 본문 블록 id로 이어진다
    assert "kp-nowrap" in html and "padding:0pt 0pt 0pt 0pt" in html
    flow = render_html(layout, layout="flow")
    assert 'class="kp-fixed-page' not in flow and 'data-kp-id="b1"' in flow


def test_fixed_slide_keeps_text_dropped_from_body(plain):
    # 되풀이되어 꼬리말로 한 번만 옮긴 바닥글과 버린 쪽 번호도 원래 배치 화면에는 슬라이드마다 그린다
    def texts(fp):
        by_id = {b.id: b for b in plain.blocks + plain.footer}
        out = []
        for s in fp.shapes:
            out += [getattr(by_id[i], "text", "") for i in s.block_ids] + [b.text for b in s.text_blocks]
        return out

    assert all("내부 검토용 자료" in texts(fp) for fp in plain.fixed_pages)
    assert [t for fp in plain.fixed_pages for t in texts(fp) if "/ 4" in t] == ["1 / 4", "2 / 4", "3 / 4", "4 / 4"]
    # 본문에 없는 글은 id 없이 담는다 (본문 블록 id와 겹치지 않게)
    assert all(not b.id for fp in plain.fixed_pages for s in fp.shapes for b in s.text_blocks)


def test_fixed_slide_draws_diagram_members_in_place():
    # 도식은 한 덩어리가 아니라 구성 도형마다 원래 차례에 그린다 (사이에 놓인 배경 도형이 글을 덮지 않게).
    # 상자 글은 도식 안 문단 id를 가리킨다
    from korean_parser.ir import Drawing, walk_blocks

    doc = parse(FIX / "sample_diagram.pptx")
    drawing_ids = {b.id for b in doc.blocks if isinstance(b, Drawing)}
    shapes = doc.fixed_pages[0].shapes
    assert not any(i in drawing_ids for s in shapes for i in s.block_ids)
    inner = {b.id: b for b in walk_blocks(doc.blocks)}
    labels = [inner[i].text for s in shapes for i in s.block_ids]
    assert {"산업안전", "안전교육", "교육현황", "접수", "처리"} <= set(labels)
    assert sum(s.is_line for s in shapes) == 3 and any(s.tail_arrow for s in shapes)
