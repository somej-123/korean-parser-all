"""구조 기반 청크 도구 테스트."""

from pathlib import Path

from korean_parser.chunk import chunk, estimate_tokens
from korean_parser.ir import (
    Cell, Document, Heading, Note, NoteRef, Paragraph, Provenance, Row, Table, Text, TextStyle,
)
from korean_parser.parsers import parse

FIX = Path(__file__).parent / "fixtures"


def para(text, id, **style):
    return Paragraph(id=id, inlines=[Text(text=text, style=TextStyle(**style) if style else None)])


def cell(text, row_span=1):
    return Cell(blocks=[para(text, "")], row_span=row_span)


def test_rfp_heading_paths_from_numbered_titles():
    doc = parse(FIX / "hwpx" / "rfp_example.hwpx")
    chunks = chunk(doc, max_tokens=400)
    paths = [tuple(c.heading_path) for c in chunks]
    # 제목 스타일 없이 굵고 큰 번호 문단으로 쓴 장·절 제목이 경로가 된다
    assert ("Ⅰ. 사업 개요",) in paths
    assert ("Ⅳ. 제안요청 내용", "4. 상세 요구사항", "① 기능 요구사항 (FUN)") in paths
    # 목차 줄("Ⅰ. 사업개요<탭>#")은 제목이 아니다
    assert not any("\t" in p for path in paths for p in path)
    # 장이 바뀌면 조각도 바뀐다: 한 조각에 두 장의 제목이 함께 들어가지 않는다
    for c in chunks:
        assert sum(c.text.lstrip().startswith(r) or f"\n{r}" in c.text for r in ("Ⅰ. 사업 개요", "Ⅱ. 시스템 현황")) <= 1
    # 모든 블록 id는 문서에 있는 블록이다 (HTML data-kp-id와 같은 값)
    ids = {b.id for b in doc.iter_blocks()}
    assert all(i in ids for c in chunks for i in c.block_ids)


def test_rfp_large_table_split_with_header_repeated():
    doc = parse(FIX / "hwpx" / "rfp_example.hwpx")
    chunks = chunk(doc, max_tokens=400)
    parts = [c for c in chunks if any(s.part and s.kind == "table" for s in c.sources) and "평가부문" in c.text]
    assert len(parts) >= 3
    assert all(c.text.startswith("<table><tr><td>평가부문</td>") for c in parts)
    assert all(c.tokens <= 400 for c in parts)


def test_table_split_carries_vertically_merged_cell():
    rows = [Row(cells=[cell("구분"), cell("항목"), cell("배점")])]
    rows.append(Row(cells=[cell("기술평가", row_span=6), cell("항목 0 " + "가" * 40), cell("10")]))
    rows += [Row(cells=[cell(f"항목 {i} " + "가" * 40), cell("10")]) for i in range(1, 6)]
    doc = Document(blocks=[Table(id="t1", rows=rows)])
    chunks = chunk(doc, max_tokens=150)
    assert len(chunks) >= 2
    for c in chunks:
        assert c.text.startswith("<table><tr><td>구분</td><td>항목</td><td>배점</td></tr>")
        assert "기술평가" in c.text  # 세로 병합 칸이 조각마다 남는다
        assert [s.part for s in c.sources][0].endswith(f"/{len(chunks)}")


def test_sections_merge_when_small_and_break_at_headings():
    doc = Document(blocks=[
        Heading(id="h1", level=1, inlines=[Text(text="1. 개요")]),
        para("짧은 문단입니다.", "p1"),
        Heading(id="h2", level=2, inlines=[Text(text="가. 목적")]),
        para("목적을 설명합니다.", "p2"),
        Heading(id="h3", level=1, inlines=[Text(text="2. 계획")]),
        para("계획을 설명합니다.", "p3"),
    ])
    chunks = chunk(doc, max_tokens=200)
    assert [c.block_ids for c in chunks] == [["h1", "p1", "h2", "p2"], ["h3", "p3"]]
    assert chunks[0].heading_path == ["1. 개요"]  # 합친 조각의 경로는 공통 부분
    # 조각이 제목으로 시작하면 경로를 다시 붙이지 않고, 중간부터 시작하면 경로를 붙인다
    assert chunks[1].contextual_text() == chunks[1].text
    doc.blocks.insert(4, para("가" * 190, "p2b"))
    later = [c for c in chunk(doc, max_tokens=200) if c.block_ids[0] == "p2b"]
    assert later and later[0].contextual_text().startswith("[1. 개요 > 가. 목적]\n")


def test_long_paragraph_split_by_sentence_and_notes_attached():
    sentence = "이 사업은 공공 문서의 구조를 보존하는 파서를 만든다. "
    p = Paragraph(id="p1", prov=Provenance(parser="test", page=3, bbox=(0, 0, 10, 10)),
                  inlines=[Text(text=sentence * 12), NoteRef(note_id="n1", label="1")])
    doc = Document(blocks=[p], notes={"n1": Note(kind="footnote", label="1", blocks=[para("각주 내용", "")])})
    chunks = chunk(doc, max_tokens=100)
    assert len(chunks) > 1
    assert all(c.tokens <= 100 + estimate_tokens("[^1]: 각주 내용") + 5 for c in chunks)
    assert chunks[0].pages == [3] and chunks[0].sources[0].bbox == (0, 0, 10, 10)
    assert "[^1]: 각주 내용" in chunks[-1].text


def test_custom_token_counter_and_json():
    doc = parse(FIX / "korean_report.docx")
    chunks = chunk(doc, max_tokens=80, count_tokens=len)
    assert all(c.tokens == len(c.text) for c in chunks)
    data = chunks[0].model_dump()
    assert {"id", "text", "heading_path", "block_ids", "pages", "sources", "doc"} <= data.keys()
    assert chunks[0].metadata()["doc_source_format"] == "docx"
