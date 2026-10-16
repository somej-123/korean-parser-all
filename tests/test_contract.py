"""파서가 쓰는 쪽에 지키는 약속: 형식 버전, 결정성(같은 파일 → 같은 결과), JSON 왕복."""

from pathlib import Path

import pytest

from korean_parser.ir import IR_VERSION, Document
from korean_parser.parsers import parse

FIX = Path(__file__).parent / "fixtures"
FILES = [FIX / "korean_report.docx", FIX / "hwpx" / "rfp_example.hwpx", FIX / "hwpx" / "project_plan.hwpx",
         FIX / "hwpx" / "container.hwpx"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_same_file_same_result(path):
    # 블록 id까지 같아야 검색 결과(조각의 블록 id)와 화면(data-kp-id)이 다시 파싱해도 맞는다
    a = parse(path).model_dump_json()
    b = parse(path).model_dump_json()
    assert a == b


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_json_round_trip_and_version(path):
    doc = parse(path)
    assert doc.schema_version == IR_VERSION
    again = Document.model_validate_json(doc.model_dump_json())
    assert again == doc


def test_block_ids_unique():
    for path in FILES:
        ids = [b.id for b in parse(path).iter_blocks() if b.id]
        assert len(ids) == len(set(ids)), path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_source_identity_and_parser_version(path):
    import hashlib

    from korean_parser import __version__

    m = parse(path).meta
    assert m.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert m.source_size_bytes == path.stat().st_size
    assert m.parser_version == __version__


def test_page_count_comes_with_its_source():
    # DOCX: 저장한 프로그램이 docProps/app.xml에 적어 둔 값 (마지막 저장 때의 값이라 실제와 다를 수 있다)
    m = parse(FIX / "korean_report.docx").meta
    assert m.page_count == 1 and m.page_count_source == "document_property"
    # HWPX: 파일에 쪽 수가 없으므로 추정하지 않고 비워 둔다
    m = parse(FIX / "hwpx" / "rfp_example.hwpx").meta
    assert m.page_count is None and m.page_count_source is None


def test_chunks_carry_source_identity():
    from korean_parser.chunk import chunk

    doc = parse(FIX / "hwpx" / "rfp_example.hwpx")
    meta = chunk(doc, max_tokens=500)[0].metadata()
    assert meta["doc_source_sha256"] == doc.meta.source_sha256
    assert meta["doc_parser_version"] == doc.meta.parser_version
