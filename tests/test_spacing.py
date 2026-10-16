"""benchmarks/docling-cpu 실행 결과에서 실제로 나온 사례들."""

import pytest

from korean_parser import spacing_lost


@pytest.mark.parametrize(
    "broken, expected",
    [
        (
            "본문서는 2026 년도사업추진방향과예산계획을설명합니다.",
            "본 문서는 2026년도 사업 추진 방향과 예산 계획을 설명합니다.",
        ),
        (
            "주요목표는문서파싱 품질향상과처리속도개선이며, 각부서는아래일정에따라업무를수행합니다.",
            "주요 목표는 문서 파싱 품질 향상과 처리 속도 개선이며, 각 부서는 아래 일정에 따라 업무를 수행합니다.",
        ),
        ("담당부서는진행상 황을 매주 보고합니다.", "담당 부서는 진행 상황을 매주 보고합니다."),
        ("그림 1. 분기별매출추이", "그림 1. 분기별 매출 추이"),
    ],
)
def test_reset_fixes_lost_spacing(fixer, broken, expected):
    assert fixer.respace(broken, reset=True) == expected


def test_keep_mode_never_removes_original_spaces(fixer):
    # 원래 있던 공백("상 황을")은 지우지 않는다. 이런 오류는 reset 모드나 join_lines로 고친다.
    out = fixer.respace("담당부서는진행상 황을", reset=False)
    assert out.startswith("담당 부서는 ")
    assert out.endswith(" 황을")


def test_keep_mode_leaves_normal_compounds(fixer):
    # 정상 길이의 복합어는 건드리지 않는다
    assert fixer.respace("요구사항 정의서를 작성한다", reset=False) == "요구사항 정의서를 작성한다"


@pytest.mark.parametrize(
    "text",
    [
        "본 문서는 2026년도 사업 추진 방향과 예산 계획을 설명합니다.",
        "1단계: 요구사항 정의, 설계, 구현, 검증을 순서대로 진행합니다.",
        "Docling 2.0 버전에서 PDF 파싱을 테스트했다.",
    ],
)
def test_correct_text_is_untouched(fixer, text):
    assert fixer.respace(text, reset=False) == text


def test_protection_rules(fixer):
    out = fixer.respace("보고합니다 1단계 문서파싱", reset=True)
    assert "보고합니다" in out  # 접미사(하다) 앞은 띄우지 않음
    assert "1단계" in out  # 숫자 뒤는 띄우지 않음
    assert "파 싱" not in out  # 모르는 외래어를 한 글자씩 쪼개지 않음


def test_non_hangul_spaces_are_kept_in_reset(fixer):
    assert fixer.respace("PDF 파일과 HWP 파일", reset=True) == "PDF 파일과 HWP 파일"


def test_join_lines_mid_word_break(fixer):
    # 줄이 어절 중간에서 바뀐 경우: 공백 없이 이어 붙인다
    lines = ["담당 부서는 진행 상", "황을 매주 보고합니다."]
    assert fixer.join_lines(lines) == "담당 부서는 진행 상황을 매주 보고합니다."


def test_join_lines_word_boundary_break(fixer):
    # 줄이 어절 경계에서 바뀐 경우: 공백을 넣는다
    lines = ["본 문서는 2026년도 사업", "추진 방향을 설명합니다."]
    assert fixer.join_lines(lines) == "본 문서는 2026년도 사업 추진 방향을 설명합니다."


def test_join_lines_english_hyphen(fixer):
    assert fixer.join_lines(["document infor-", "mation 추출"]) == "document information 추출"


def test_spacing_lost_detection():
    normal = "본 문서는 2026년도 사업 추진 방향과 예산 계획을 설명합니다. 각 부서는 일정에 따라 업무를 수행합니다."
    lost = "본문서는 2026 년도사업추진방향과예산계획을설명합니다. 주요목표는문서파싱 품질향상과처리속도개선이며, 각부서는아래일정에따라업무를수행합니다."
    assert not spacing_lost(normal)
    assert spacing_lost(lost)


def test_missing_space_after_punctuation(fixer):
    # 스캔 문서 OCR 결과에서 나온 사례
    assert fixer.respace("개선이며,각 부서는 수행합니다.본 문서는") == "개선이며, 각 부서는 수행합니다. 본 문서는"


def test_numbers_with_punctuation_untouched(fixer):
    assert fixer.respace("비율은 3:1이고 1.5배 증가") == "비율은 3:1이고 1.5배 증가"


@pytest.mark.parametrize("line, expected", [
    ("1.산출물제출", "1. 산출물제출"),
    ("Ⅱ.제안서", "Ⅱ. 제안서"),
    ("※증빙 자료", "※ 증빙 자료"),
    ("-데이터 이관", "- 데이터 이관"),
    ("O하자보수", "○ 하자보수"),  # ○를 영문 O로 읽은 경우
    ("ㅇOCR 인식률", "○ OCR 인식률"),
    ("OCR 인식률", "OCR 인식률"),  # 영문 낱말은 그대로
    ("0점 처리", "0점 처리"),  # 숫자 0 바로 뒤의 한글은 단위다
    ("0 하자보수", "○ 하자보수"),
    ("1.5배", "1.5배"),
])
def test_fix_ocr_marker(line, expected):
    from korean_parser.spacing import fix_ocr_marker

    assert fix_ocr_marker(line) == expected


def test_respace_ocr_rules(fixer):
    # OCR 글: 공백을 믿지 않고 다시 띄우되, 숫자와 기호 사이는 띄우지 않고 보조 용언 앞은 띄운다
    out = fixer.respace_ocr("OCR인식률98%이상을목표로함")
    assert "98%" in out and "98 %" not in out
    assert fixer.respace_ocr("인력을투입하여야한다.").endswith("투입하여야 한다.")
    assert fixer.respace_ocr("O산출물은HWPX와PDF형식으로제출").startswith("○ 산출")
    assert "원(VAT" in fixer.respace_ocr("사업예산:1,250,000,000원(VAT포함)")
