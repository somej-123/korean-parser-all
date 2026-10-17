"""v1.0 기준 측정 도구(benchmarks/goal/check.py)의 원문 글자 세기."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "goal"))

from check import source_counter  # noqa: E402


def test_repeated_lines_and_page_numbers_count_once():
    # 쪽마다 되풀이되는 머리말은 한 번만 (파서가 한 번만 남긴다), 숫자만 있는 짧은 줄(쪽 번호)은 뺀다
    pages = [["대외비", "본문 하나", "- 1 -"], ["대외비", "본문 둘", "- 2 -"]]
    counted = source_counter(pages)
    assert counted["대"] == 1 and counted["하"] == 1 and counted["둘"] == 1 and not counted["1"]
    # 한 쪽짜리 문서는 줄을 그대로 센다 (숫자 줄도 내용일 수 있다)
    assert source_counter([["12", "12"]])["1"] == 2
