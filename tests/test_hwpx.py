"""HWPX 파서 테스트. 파일 출처는 tests/fixtures/hwpx/NOTICE.md (hwpxlib Apache-2.0, 공공누리 제1유형)."""

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


def test_real_rfp_example():
    """실제 공공SW사업 제안요청서 작성 예시 (과기정통부·NIPA, 공공누리 제1유형)."""
    doc = parse(FIX / "rfp_example.hwpx")
    text = render_llm(doc, with_ids=False)
    # 한글이 자동으로 넣은 그림 설명은 원본 파일 이름만 남는다
    assert "[이미지: NIPA_로고.jpg" in text
    assert "그림입니다" not in text
    # 칸 대부분이 빈 표지 배치용 표는 글로 풀린다
    assert "공공SW사업 제안요청서\n\n작성 예시" in text
    # 장 제목과 그 아래 번호가 계층대로 들여 쓰인다
    assert "Ⅰ. 사업 개요\n\n  1. 제안개요" in text
    # 세로 병합 표 (HW 품목 11행)
    tables = [b for b in doc.blocks if isinstance(b, Table)]
    assert any(c.row_span == 11 and "HW" in "".join(p.text for p in c.blocks if hasattr(p, "text"))
               for t in tables for r in t.rows for c in r.cells)
    html = render_html(doc)
    assert "rowspan=\"11\"" in html


def test_caption_memo_and_master_page(tmp_path):
    # v1.0 기준 1(빠짐없이)에서 찾은 것: 개체 캡션 글은 개체 안이 아니라 hp:caption에 있어 경고 없이 빠졌고
    # (hwpxlib SimpleRectangle '그림'), 메모(fieldBegin MEMO)와 바탕쪽의 글도 알리지 않고 빠졌다.
    # 캡션은 개체 뒤 문단으로 읽는다. 메모는 v1.4.0부터 메모 목록으로, 바탕쪽 글은 v1.4.1부터 머리말 뒤로 읽는다
    import re
    import zipfile

    src = FIX / "simple_table.hwpx"
    out = tmp_path / "caption.hwpx"
    hp = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Contents/section0.xml":
                xml = data.decode("utf-8")
                cap = ('<hp:caption side="BOTTOM"><hp:subList><hp:p><hp:run><hp:t>표 1. 시험 표</hp:t></hp:run></hp:p>'
                       '</hp:subList></hp:caption>')
                xml = re.sub(r"(<hp:tbl\b[^>]*>)", r"\1" + cap, xml, count=1)
                memo = ('<hp:ctrl><hp:fieldBegin type="MEMO"><hp:subList><hp:p><hp:run><hp:t>메모 글</hp:t></hp:run>'
                        '</hp:p></hp:subList></hp:fieldBegin></hp:ctrl>')
                xml = xml.replace("<hp:t>", memo + "<hp:t>", 1)
                data = xml.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("Contents/masterpage0.xml",
                      f'<hm:masterPage xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" {hp}>'
                      '<hp:subList><hp:p><hp:run><hp:t>바탕쪽 글</hp:t></hp:run></hp:p></hp:subList></hm:masterPage>')
    doc = parse(out)
    i = next(k for k, b in enumerate(doc.blocks) if isinstance(b, Table))
    assert doc.blocks[i + 1].text == "표 1. 시험 표"
    assert [(b.style_name, b.text) for b in doc.header] == [("master_page", "바탕쪽 글")]
    assert not any("바탕쪽" in w for w in doc.warnings)
    # 메모는 v1.4.0부터 읽는다: Note(kind="comment")와 붙은 자리의 NoteRef
    assert [n.blocks[0].text for n in doc.notes.values() if n.kind == "comment"] == ["메모 글"]


def test_even_page_header_is_kept(tmp_path):
    # 짝수 쪽 머리말(applyPageType="EVEN")을 버려서 글이 경고 없이 빠졌다 (한글로 만든 표본에서 찾음).
    # 화면용은 첫 양쪽 머리말이고, 글이 다른 머리말은 뒤에 함께 담는다
    import re
    import zipfile

    out = tmp_path / "even.hwpx"
    hdr = ('<hp:ctrl><hp:header id="1" applyPageType="{t}"><hp:subList><hp:p><hp:run><hp:t>{x}</hp:t></hp:run></hp:p>'
           "</hp:subList></hp:header></hp:ctrl>")
    with zipfile.ZipFile(FIX / "simple_table.hwpx") as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Contents/section0.xml":
                both, even = hdr.format(t="BOTH", x="양쪽 머리말"), hdr.format(t="EVEN", x="짝수 쪽 머리말")
                data = re.sub("<hp:t>", even + both + "<hp:t>", data.decode("utf-8"), count=1).encode("utf-8")
            zout.writestr(item, data)
    doc = parse(out)
    assert [b.text for b in doc.header] == ["짝수 쪽 머리말", "양쪽 머리말"] or         [b.text for b in doc.header] == ["양쪽 머리말", "짝수 쪽 머리말"]


def test_hwpx_memo_becomes_comment_note():
    # 한글 메모(fieldBegin type="MEMO")의 글은 예전에 경고만 남겼다 (hwpxlib 표본에 메모 56개)
    from pathlib import Path as _P

    sample = _P(__file__).resolve().parents[1] / "benchmarks/corpus/samples_web/hwpxlib/error_20251107_test.hwpx"
    if not sample.exists():
        pytest.skip("hwpxlib 표본이 없다 (benchmarks/corpus/fetch_samples.sh)")
    doc = parse(sample)
    memos = [n for n in doc.notes.values() if n.kind == "comment"]
    assert len(memos) == 56 and memos[0].blocks[0].text.startswith("★교과군명")
    assert not any("메모" in w for w in doc.warnings)


def test_hidden_comment_and_caption_auto_number(tmp_path):
    # HWP·HWPX 점검(v1.4.1)에서 찾은 것: 캡션 '그림 1'의 번호는 글이 아니라 자동 번호 컨트롤(hp:autoNum)이라 '그림 '만
    # 남았고, 숨은 설명(hp:hiddenComment)의 글은 경고 없이 빠졌다
    import zipfile

    out = tmp_path / "autonum.hwpx"
    hidden = ('<hp:ctrl><hp:hiddenComment><hp:subList><hp:p><hp:run><hp:t>숨은 글</hp:t></hp:run></hp:p></hp:subList>'
              '</hp:hiddenComment></hp:ctrl>')
    num = ('<hp:t>표 </hp:t><hp:ctrl><hp:autoNum num="3" numType="TABLE"><hp:autoNumFormat type="ROMAN_CAPITAL" '
           'userChar="" prefixChar="" suffixChar="." supscript="0"/></hp:autoNum></hp:ctrl>'
           '<hp:ctrl><hp:autoNum num="7" numType="PAGE"><hp:autoNumFormat type="DIGIT"/></hp:autoNum></hp:ctrl>')
    with zipfile.ZipFile(FIX / "simple_table.hwpx") as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "Contents/section0.xml":
                data = data.decode("utf-8").replace("<hp:t>", hidden + num + "<hp:t>", 1).encode("utf-8")
            zout.writestr(item, data)
    doc = parse(out)
    assert any(b.text.startswith("표 III.") for b in blocks_of(doc, Paragraph))  # 쪽 번호(PAGE)는 넣지 않는다
    assert [(k, n.label, n.blocks[0].text) for k, n in doc.notes.items()] == [("hc1", "숨은 설명1", "숨은 글")]


@pytest.mark.parametrize("name,check", [
    ("basic_바탕쪽.hwp", lambda d: [b.style_name for b in d.header] == ["master_page"] and d.header[0].text),
    ("basic_숨은설명.hwp", lambda d: [b.text for n in d.notes.values() for b in n.blocks]
     == ["우리는 우리다.", "그것은 그것이다."]),
    ("basic_표.hwp", lambda d: any(getattr(b, "text", "").startswith("표 1") for b in d.iter_blocks())),
])
def test_hwp_master_page_hidden_comment_auto_number(name, check):
    # HWP 바이너리: 바탕쪽(구역 정의 아래 글 목록), 숨은 설명(tcmt), 자동 번호(atno)를 HWPX와 같은 모양으로 옮긴다
    sample = Path(__file__).resolve().parents[1] / "benchmarks/corpus/samples_web/hwplib" / name
    if not sample.exists():
        pytest.skip("hwplib 표본이 없다 (benchmarks/corpus/fetch_samples.sh)")
    doc = parse(sample)
    assert check(doc)
    assert not any("바탕쪽" in w or "숨은 설명" in w for w in doc.warnings)
