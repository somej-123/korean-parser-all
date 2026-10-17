"""DOCX → IR → HTML/LLM 파이프라인 테스트 (tests/fixtures/korean_report.docx 기준)."""

from pathlib import Path

import pytest

from korean_parser.ir import Chart, Document, Drawing, Heading, ImageRef, Paragraph, Table
from korean_parser.parsers import DocumentError, parse
from korean_parser.render.html import render_html
from korean_parser.render.llm import render_llm

FIXTURE = Path(__file__).parent / "fixtures" / "korean_report.docx"


@pytest.fixture(scope="module")
def doc() -> Document:
    return parse(FIXTURE)


def blocks_of(doc, kind):
    return [b for b in doc.iter_blocks() if isinstance(b, kind)]


def test_metadata_and_page(doc):
    assert doc.meta.title == "2026년 문서 파싱 고도화 계획"
    assert doc.meta.source_format == "docx"
    assert doc.page.width_pt > 500
    assert doc.header and "대외비" in doc.header[0].text


def test_headings(doc):
    headings = [(h.level, h.text) for h in blocks_of(doc, Heading)]
    assert headings[0] == (1, "2026년 문서 파싱 고도화 계획")
    assert (1, "3. 예산 계획") in headings


def test_korean_numbering_markers(doc):
    markers = [(p.list_info.level, p.list_info.marker, p.text) for p in blocks_of(doc, Paragraph) if p.list_info]
    assert markers[:5] == [
        (0, "1.", "통합 문서 모델 구축"),
        (1, "가.", "DOCX 파서 개발"),
        (2, "1)", "문단과 서식"),
        (3, "가)", "자동 번호"),
        (3, "나)", "셀 병합"),
    ]
    assert (1, "나.", "HWPX 파서 개발") in markers
    assert (0, "2.", "화면 표시 품질 검증") in markers


def test_outline_from_manual_bullets(doc):
    outline = [(p.outline.level, p.outline.marker) for p in blocks_of(doc, Paragraph) if p.outline]
    assert outline[:4] == [(0, "□"), (1, "○"), (2, "-"), (2, "-")]
    assert (2, "※") in outline


def test_merged_table(doc):
    table = blocks_of(doc, Table)[0]
    first = table.rows[0].cells
    assert (first[0].row_span, first[0].col_span) == (2, 1)
    assert (first[1].row_span, first[1].col_span) == (1, 3)
    assert first[0].background == "#D9E2F3"
    assert len(table.rows) == 5 and len(table.rows[1].cells) == 3  # 세로 병합된 칸은 빠진다
    assert first[0].borders and first[0].borders.top


def test_image_and_hyperlink(doc):
    images = [i for p in blocks_of(doc, Paragraph) for i in p.inlines if isinstance(i, ImageRef)]
    assert len(images) == 1 and doc.images[images[0].image_id].mime == "image/png"
    links = [i.link for p in blocks_of(doc, Paragraph) for i in p.inlines if getattr(i, "link", None)]
    assert "https://github.com/docling-project/docling" in links


def test_flowchart_shapes_and_connections(doc):
    drawing = blocks_of(doc, Drawing)[0]
    boxes = {s.id: s for s in drawing.shapes if not s.is_line}
    lines = [s for s in drawing.shapes if s.is_line]
    assert {s.geometry for s in boxes.values()} >= {"flowChartTerminator", "flowChartProcess", "flowChartDecision"}
    assert len(lines) == 4 and all(ln.tail_arrow for ln in lines)
    assert ("11", "12") in {(ln.connect_from, ln.connect_to) for ln in lines}
    texts = {" ".join(b.text for b in s.text_blocks) for s in boxes.values()}
    assert {"신청", "부서 검토", "적합?", "승인", "반려"} <= texts


def test_html_rendering(doc):
    html = render_html(doc)
    assert html.startswith("<!doctype html>")
    assert 'rowspan="2"' in html and 'colspan="3"' in html
    assert "<svg" in html and "foreignObject" in html
    assert "data:image/png;base64," in html
    assert 'class="kp-marker"' in html and ">가.</span>" in html
    assert "<script" not in html.lower()


def test_llm_rendering(doc):
    text = render_llm(doc)
    assert "# 2026년 문서 파싱 고도화 계획" in text
    assert '<td rowspan="2">구분</td><td colspan="3">2026년</td>' in text  # 병합 표는 HTML
    assert "  가. DOCX 파서 개발" in text
    assert "- 신청 → 부서 검토" in text and "- 적합? → 반려" in text
    assert "<!-- b" in text  # 블록 id


def test_json_roundtrip(doc):
    again = Document.model_validate_json(doc.model_dump_json())
    assert render_llm(again) == render_llm(doc)


def test_html_escapes_text_and_blocks_unsafe_links():
    from korean_parser.ir import Text

    d = Document(blocks=[Paragraph(id="b1", inlines=[
        Text(text="<script>alert(1)</script>"),
        Text(text="링크", link="javascript:alert(1)"),
    ])])
    html = render_html(d)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "javascript:" not in html


def test_errors_are_readable(tmp_path):
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(DocumentError, match="손상된 파일"):
        parse(broken)
    ole = tmp_path / "encrypted.docx"
    ole.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    with pytest.raises(DocumentError, match="암호화"):
        parse(ole)


def test_chartex_data():
    # Office 2016 신형 차트(cx:chartSpace)는 예전에 '지원하지 않는 그래픽 요소: chartex' 경고만 남겼다.
    # 데이터 묶음(분류·값)과 계열(이름, 데이터 번호, 종류)을 읽는다. 여러 단계 분류는 위 단계부터 ' / '로 잇는다
    from lxml import etree

    from korean_parser.parsers.docx.drawing import read_chart

    cx = "http://schemas.microsoft.com/office/drawing/2014/chartex"
    xml = f"""<cx:chartSpace xmlns:cx="{cx}"><cx:chartData><cx:data id="0">
      <cx:strDim type="cat"><cx:lvl ptCount="2"><cx:pt idx="0">잎1</cx:pt><cx:pt idx="1">잎2</cx:pt></cx:lvl>
        <cx:lvl ptCount="2"><cx:pt idx="0">줄기</cx:pt><cx:pt idx="1">줄기</cx:pt></cx:lvl></cx:strDim>
      <cx:numDim type="size"><cx:lvl ptCount="2"><cx:pt idx="0">22</cx:pt><cx:pt idx="1">12.5</cx:pt></cx:lvl></cx:numDim>
    </cx:data></cx:chartData><cx:chart><cx:title><cx:tx><cx:txData><cx:v>예산 구성</cx:v></cx:txData></cx:tx></cx:title>
    <cx:plotArea><cx:plotAreaRegion><cx:series layoutId="sunburst"><cx:tx><cx:txData><cx:v>금액</cx:v></cx:txData></cx:tx>
    <cx:dataId val="0"/></cx:series></cx:plotAreaRegion></cx:plotArea></cx:chart></cx:chartSpace>"""
    chart = read_chart(etree.fromstring(xml), 300, 200)
    assert (chart.chart_type, chart.title) == ("sunburst", "예산 구성")
    assert chart.categories == ["줄기 / 잎1", "줄기 / 잎2"]
    assert [(s.name, s.values) for s in chart.series] == [("금액", [22.0, 12.5])]
