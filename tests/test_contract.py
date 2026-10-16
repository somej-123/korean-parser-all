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
