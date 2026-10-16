"""PDF 파서 테스트. tests/fixtures/korean_report.pdf는 korean_report.docx를 LibreOffice로 변환한 것이다.

Docling(레이아웃 모델)이 필요하므로 설치되지 않은 환경에서는 건너뛴다.
"""

import re
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


def test_scanned_pdf_spacing(doc, tmp_path):
    # 쪽 그림만 있는 PDF(스캔본)를 OCR로 읽어도, 쪽 그림의 글자 사이 빈칸으로 띄어쓰기를 되살린다.
    # 텍스트 레이어로 읽은 결과와 어절이 대부분 같아야 한다 (OCR 글자 오류만큼은 다를 수 있다)
    pytest.importorskip("onnxruntime")
    import pypdfium2 as pdfium
    from collections import Counter

    src = pdfium.PdfDocument(PDF)
    images = [src[i].render(scale=150 / 72).to_pil().convert("RGB") for i in range(len(src))]
    scan = tmp_path / "scan.pdf"
    images[0].save(scan, "PDF", resolution=150, save_all=True, append_images=images[1:])
    ocr = parse(scan)
    assert ocr.blocks and "OCR로 읽었습니다" in " ".join(ocr.warnings)

    def words(d):
        return Counter(" ".join(b.text for b in d.iter_blocks() if isinstance(b, (Paragraph, Heading))).split())

    truth, pred = words(doc), words(ocr)
    common = sum((truth & pred).values())
    f1 = 2 * common / (sum(truth.values()) + sum(pred.values()))
    assert f1 >= 0.85, f1  # 그림 빈칸을 쓰기 전에는 0.74


def test_real_scan_structure():
    # 실제 스캔본 (benchmarks/docling-cpu/samples, 같은 내용의 DOCX가 정답). 고치기 전에는
    # 표지 제목이 머리말로 분류되어 '2026사업계획서' 문단이 되었고, 그림의 자식으로 분류된 캡션이 사라졌으며,
    # 차트 제목 '분기별 매출(억원)'이 절 제목이 되고, 쪽을 넘는 문단이 둘로 나뉘었다
    pytest.importorskip("onnxruntime")
    scan = parse(Path(__file__).parents[1] / "benchmarks" / "docling-cpu" / "samples" / "사업계획서_스캔.pdf")
    assert [h.text for h in of(scan, Heading)] == ["2026 사업계획서", "1. 개요", "2. 예산 계획", "3. 실적 추이", "4. 추진 일정"]
    fig = of(scan, Figure)[0]
    assert fig.caption == "그림 1. 분기별 매출 추이" and fig.inner_text.startswith("분기별 매출(억원)")
    body = [b.text for b in scan.blocks if isinstance(b, Paragraph) and b.text.startswith("본 문서는")]
    assert len(body) == 6 and body[-1].endswith("업무를 수행합니다.")
    # 표는 쪽 그림의 괘선으로 읽는다 (표 구조 모델은 두 줄 합친 '구분' 칸을 한 줄로 읽었다)
    table = of(scan, Table)[0]
    first = table.rows[0].cells
    assert [c.blocks[0].text for c in first] == ["구분", "2026년"]
    assert (first[0].row_span, first[1].col_span, len(table.rows)) == (2, 3, 4)


def test_table_of_contents_lines_stay_paragraphs():
    # 목차(Docling document_index)는 표 구조 모델에 맡기지 않는다. 맡기면 번호와 제목이 다른 칸으로 갈라졌다
    toc = parse(Path(__file__).parent / "fixtures" / "toc.pdf")
    assert not [b for b in toc.iter_blocks() if isinstance(b, Table)]
    lines = [re.sub("·+", " ", b.text).split() for b in toc.blocks if isinstance(b, Paragraph) and "·" in b.text]
    assert [" ".join(w) for w in lines][:3] == ["I. 사업 개요 1", "1. 사업 개요 1", "2. 사업목표 및 필요성 1"]
    assert len(lines) == 12
