"""평가 지표 (benchmarks/eval/metrics.py) 시험."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("rapidfuzz")
pytest.importorskip("apted")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "eval"))

from metrics import cer, eojeol_f1, evaluate, match_blocks, reading_order, teds  # noqa: E402


def para(t):
    return {"type": "paragraph", "text": t}


def cell(t, rs=1, cs=1):
    return {"text": t, "rowspan": rs, "colspan": cs}


TABLE = {"type": "table", "rows": [[cell("구분", 2), cell("2026년", 1, 2)], [cell("상반기"), cell("하반기")]]}


def test_text_metrics():
    truth = [para("본 사업은 문서를 관리한다.")]
    assert cer(truth, truth) == 0 and eojeol_f1(truth, truth) == 1
    assert eojeol_f1(truth, [para("본사업은 문서를 관리한다.")]) < 1  # 띄어쓰기만 틀림
    assert cer(truth, [para("본사업은 문서를 관리한다.")]) == 0  # 글자 오류율은 공백을 보지 않는다


def test_reading_order():
    truth = [para("가 문단입니다"), para("나 문단입니다"), para("다 문단입니다")]
    pred = [truth[2], truth[0], truth[1]]
    pairs = match_blocks(truth, pred)
    assert len(pairs) == 3
    assert reading_order(pairs) == pytest.approx(1 / 3)


def test_teds():
    assert teds(TABLE, TABLE) == 1.0
    flat = {"type": "table", "rows": [[cell("구분"), cell("2026년"), cell("")], [cell(""), cell("상반기"), cell("하반기")]]}
    assert teds(TABLE, flat, structure_only=True) < 1.0  # 병합을 잃으면 구조 점수가 떨어진다
    typo = {"type": "table", "rows": [[cell("구분", 2), cell("2026", 1, 2)], [cell("상반기"), cell("하반기")]]}
    assert teds(TABLE, typo, structure_only=True) == 1.0 and teds(TABLE, typo) < 1.0


def test_evaluate_report_fields():
    truth = [{"type": "heading", "level": 1, "text": "Ⅰ. 사업 개요"}, para("본문입니다 충분히 긴 문장"), TABLE]
    out = evaluate(truth, truth)
    assert out["heading"]["recall"] == 1 and out["table_teds"] == 1 and out["reading_order"] == 1
