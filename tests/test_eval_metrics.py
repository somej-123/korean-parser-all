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


def test_table_pairing_prefers_whole_table_over_fragment():
    # 쪽을 넘어 나뉜 표의 작은 조각은 낱말이 원래 표의 부분집합이라 낱말 집합 유사도가 100점이다.
    # 짝은 TEDS가 높은 쪽(온전한 표)으로 지어야 한다
    from metrics import table_scores

    rows = [[cell("구분"), cell("내용")], [cell("설계"), cell("설계서")], [cell("구현"), cell("소스 코드")],
            [cell("시험"), cell("시험 결과서")]]
    whole = {"type": "table", "rows": rows}
    fragment = {"type": "table", "rows": rows[2:]}
    assert table_scores([whole], [fragment, whole])["teds"] == 1.0
    assert table_scores([whole], [whole, fragment])["teds"] == 1.0  # 놓인 순서와 상관없다
    # 표 두 개가 서로 다른 예측 표와 짝지어진다 (한 예측 표를 두 번 쓰지 않는다)
    other = {"type": "table", "rows": [[cell("품목"), cell("수량")], [cell("서버"), cell("2")]]}
    out = table_scores([whole, other], [other, whole])
    assert out["teds"] == 1.0 and out["found"] == 1.0
