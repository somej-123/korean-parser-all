from korean_parser import detect_outline, outline_to_markdown


def levels(items):
    return [(it.level, it.marker) for it in items]


def test_public_document_bullets():
    lines = [
        "□ 추진 배경",
        "○ 문서 파싱 품질 문제",
        "- 표 구조 손실",
        "- 띄어쓰기 소실",
        "○ 처리 속도 문제",
        "□ 추진 계획",
        "○ 한국어 후처리 모듈 개발",
    ]
    assert levels(detect_outline(lines)) == [
        (0, "□"), (1, "○"), (2, "-"), (2, "-"), (1, "○"), (0, "□"), (1, "○"),
    ]


def test_numbering_system():
    lines = ["1. 개요", "가. 목적", "1) 세부 목표", "가) 품질", "나) 속도", "나. 범위", "2. 예산"]
    assert levels(detect_outline(lines)) == [
        (0, "1."), (1, "가."), (2, "1)"), (3, "가)"), (3, "나)"), (1, "나."), (0, "2."),
    ]


def test_order_comes_from_document_not_fixed_table():
    # 이 문서는 ○ 가 최상위다
    lines = ["○ 상위", "- 하위", "○ 상위2"]
    assert levels(detect_outline(lines)) == [(0, "○"), (1, "-"), (0, "○")]


def test_continuation_lines_are_merged():
    items = detect_outline(["□ 추진 배경은 다음과", "같습니다", "○ 세부"])
    assert items[0].text == "추진 배경은 다음과 같습니다"
    assert len(items) == 2


def test_note_marker_goes_under_previous_item():
    items = detect_outline(["□ 예산", "○ 인건비", "※ 상반기 기준"])
    assert levels(items) == [(0, "□"), (1, "○"), (2, "※")]


def test_decimal_number_is_not_a_marker():
    items = detect_outline(["1.5배 증가했다"])
    assert items[0].level is None


def test_markdown_keeps_original_markers():
    md = outline_to_markdown(detect_outline(["□ 배경", "○ 문제", "- 세부"]))
    assert md == "- □ 배경\n  - ○ 문제\n    - - 세부"
