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


def test_header_lines_that_differ_only_by_page_number_count_once():
    # '… 국어 책형 2쪽', '… 3쪽'처럼 쪽 번호만 다른 머리말도 파서는 한 번만 남긴다
    pages = [["시험 국어 2쪽", "본문"], ["시험 국어 3쪽", "본문 둘"]]
    counted = source_counter(pages)
    assert counted["시"] == 1 and counted["둘"] == 1


def test_running_heads_at_page_edges_count_once():
    # 장마다 바뀌는 머리말은 절반 넘는 쪽에 나오지 않아도, 쪽 끝 줄이고 두 쪽 이상이면 한 번만 센다
    pages = [["2장 기준 9", "본문"], ["2장 기준 11", "본문 둘"], ["3장 정의 13", "끝"], ["다른", "더"], ["또", "끝 둘"]]
    assert source_counter(pages)["기"] == 2
    assert source_counter(pages, edges={"2장기준9", "2장기준11", "3장정의13"})["기"] == 1


def test_size_limit_warning_is_an_exception_not_a_failure():
    # 칸 수 제한처럼 일부러 둔 제한에 걸려 경고를 남긴 파일은 기준 1의 예외다 (docs/GOAL.md)
    from check import report

    rows = [{"file": Path("big.xlsx"), "format": "xlsx", "set": "실제 문서", "chars": 100, "kept": 0.3, "missing": "",
             "warnings": ["칸이 너무 많아 앞부분 200,000칸만 넣음: data"]}]
    text = report(rows, [])
    assert "- 판정: **통과**" in text and "예외 (의도한 크기 제한에 걸려 경고를 남김): 1개" in text
    rows[0]["warnings"] = ["다른 경고"]
    assert "- 판정: **미달**" in report(rows, [])


def test_running_head_piece_inside_a_longer_line_counts_once():
    # 두 쪽을 펼친 PDF: pdfium이 장마다 바뀌는 머리글('Part 1 · 가')과 늘 같은 머리글('우리가 가면 건널목')을 한 줄로
    # 이어 읽었다. 쪽 끝 줄을 크게 벌어진 자리에서 나눈 조각(edges)이 더 긴 줄 안에 있으면 처음 한 번만 센다 (수기공모집 0.988)
    head = "우리가가면건널목"
    pages = [["Part1·가" + head, "본문"], ["Part1·가" + head], ["Part2·나" + head], ["Part2·나" + head, head]]
    counted = source_counter(pages, edges={"Part1·가" + head, "Part2·나" + head, "Part1·가", "Part2·나", head})
    assert counted["널"] == 1 and counted["나"] == 1 and counted["가"] == 3  # 'Part1·가' 한 번 + '우리가가면' 한 번
