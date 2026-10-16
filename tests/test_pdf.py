"""PDF 파서 테스트. tests/fixtures/korean_report.pdf는 korean_report.docx를 LibreOffice로 변환한 것이다.

Docling(레이아웃 모델)이 필요하므로 설치되지 않은 환경에서는 건너뛴다.
"""

from pathlib import Path

import pytest

pytest.importorskip("docling")
pytest.importorskip("pypdfium2")

from korean_parser.ir import Figure, Heading, Paragraph, Table  # noqa: E402
from korean_parser.parsers import parse  # noqa: E402
from korean_parser.render.html import render_html  # noqa: E402
from korean_parser.render.llm import render_llm  # noqa: E402

PDF = Path(__file__).parent / "fixtures" / "korean_report.pdf"


@pytest.fixture(scope="module")
def doc():
    return parse(PDF)


def of(doc, kind):
    return [b for b in doc.iter_blocks() if isinstance(b, kind)]


def test_metadata_and_provenance(doc):
    assert doc.meta.source_format == "pdf" and doc.meta.title == "2026년 문서 파싱 고도화 계획"
    paras = of(doc, Paragraph)
    assert all(p.prov and p.prov.page and p.prov.bbox for p in paras)  # 쪽과 좌표


def test_heading_levels_from_font_size(doc):
    levels = {h.text: h.level for h in of(doc, Heading)}
    title = next(t for t in levels if t.startswith("2026"))
    assert levels[title] < levels["3. 예산 계획"]  # 큰 글자의 제목이 더 높은 단계


def test_korean_outline_from_list_markers(doc):
    outline = [(p.outline.level, p.outline.marker) for p in of(doc, Paragraph) if p.outline]
    assert (0, "□") in outline and (1, "○") in outline and (2, "-") in outline
    assert (1, "가.") in outline and (2, "1)") in outline and (3, "가)") in outline


def test_merged_table_and_cell_background(doc):
    table = of(doc, Table)[0]
    first = table.rows[0].cells
    assert (first[0].row_span, first[1].col_span) == (2, 3)
    assert first[0].background and first[0].background.upper() == "#D9E2F3"


def test_picture_region_is_split(doc):
    """Docling이 차트·캡션·제목·순서도를 한 그림으로 묶은 것을 그래픽 단위로 나눈다."""
    figures = of(doc, Figure)
    assert len(figures) >= 2
    assert any(f.caption and f.caption.startswith("그림 1") for f in figures)
    flow = next(f for f in figures if f.inner_text)
    for word in ("신청", "승인", "반려"):
        assert word in flow.inner_text
    assert any(h.text.startswith("5.") for h in of(doc, Heading))  # 그림 안에 묶였던 제목을 되살림


def test_misclassified_header_returns_to_body(doc):
    assert any("단위" in p.text for p in of(doc, Paragraph))
    assert doc.header and "대외비" in doc.header[0].text


def test_fixed_layout(doc):
    assert len(doc.fixed_pages) == 2
    page = doc.fixed_pages[0]
    assert page.background_image_id in doc.images
    ids = {b.id for b in doc.iter_blocks()}
    linked = [r for r in page.runs if r.block_id]
    assert linked and all(r.block_id in ids for r in linked)
    html = render_html(doc)
    assert 'class="kp-fixed-page"' in html and 'lengthAdjust="spacingAndGlyphs"' in html
    assert 'class="kp-page' in render_html(doc, layout="flow")


def test_llm_text(doc):
    text = render_llm(doc)
    assert '<td rowspan="2">구분</td><td colspan="3">2026년</td>' in text
    assert "[그림: 그림 1." in text and "그림 안 글자:" in text


def test_page_count_from_layout(doc):
    assert doc.meta.page_count == 2 and doc.meta.page_count_source == "layout"


def test_same_file_same_result(doc):
    # 레이아웃 모델을 써도 결과(블록 id, 좌표 포함)는 매번 같아야 한다
    assert parse(PDF).model_dump_json() == doc.model_dump_json()


def test_text_and_spacing_from_text_layer(doc):
    # Docling 글은 공백이 빠지지만, 텍스트 레이어에서 다시 읽으면 원문(DOCX)과 같은 띄어쓰기가 된다
    texts = [b.text for b in doc.iter_blocks() if isinstance(b, (Paragraph, Heading))]
    assert "2026년 문서 파싱 고도화 계획" in texts  # 숫자-한글 사이 자동 간격은 붙인다
    assert any("스캔 문서 OCR이 전체 처리 시간" in t for t in texts)
    assert any("병합 셀이 Markdown 변환 과정에서 사라짐" in t for t in texts)
    assert any(t.endswith("돌아갑니다.") for t in texts)  # 마침표 앞 조판 공백 제거


def test_ruled_table_structure(doc):
    # 괘선으로 읽은 표: 병합 칸이 원본(DOCX)과 같고, 칸마다 선이 있다
    t = of(doc, Table)[0]
    assert t.prov.confidence == 1.0
    first = [("".join(p.text for p in c.blocks), c.row_span, c.col_span) for c in t.rows[0].cells]
    assert first == [("구분", 2, 1), ("2026년", 1, 3)]
    assert ["".join(p.text for p in c.blocks) for c in t.rows[-1].cells] == ["합계", "15", "19", "34"]
    assert t.rows[2].cells[1].borders.top is not None


def test_split_paragraph_merge_rules():
    # 쪽·줄에서 끊긴 문단 잇기: 문장으로 끝났거나 다음 줄이 개조식 기호로 시작하면 잇지 않는다
    from korean_parser.parsers.pdf import _SENTENCE_END
    from korean_parser.outline import match_marker

    assert _SENTENCE_END.search("비용은 별도 청구하지 아니한다.")
    assert _SENTENCE_END.search("협상순위를 부여함")
    assert not _SENTENCE_END.search("기준으로 지급하며 병원과")
    assert match_marker("◦ 순찰(26코스)을 통해 시설물 점검") and not match_marker("사전에 협의하여 착용하고")
