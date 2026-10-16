"""PDF 제목 단계 정하기 (PdfParser._assign_heading_levels). Docling 없이 돈다."""

from korean_parser.ir import Heading, Text
from korean_parser.parsers.pdf import PdfParser


def run(ocr: bool, items: list[tuple[str, float]]) -> list[int]:
    parser = PdfParser.__new__(PdfParser)
    parser.ocr = ocr
    parser._heading_sizes = [(Heading(level=1, inlines=[Text(text=t)]), s) for t, s in items]
    parser._assign_heading_levels()
    return [h.level for h, _ in parser._heading_sizes]


def test_text_layer_levels_follow_font_size():
    assert run(False, [("Ⅰ. 사업 개요", 16), ("1. 추진 목표", 13), ("2. 범위", 13), ("Ⅱ. 내용", 16)]) == [1, 2, 2, 1]


def test_scan_levels_follow_number_kind_not_noisy_size():
    # OCR은 글 상자 높이로 크기를 짐작해 같은 단계도 크기가 흩어진다: 번호 종류가 처음 나온 순서로 정한다
    items = [("제 안 요 청 서", 17), ("I. 사업 개요", 14), ("1. 산출물 제출", 12), ("2. 테스트 요구사항", 13),
             ("Ⅱ. 제안서 작성요령", 13), ("1. 하자보수", 11.5), ("가. 세부 사항", 11)]
    assert run(True, items) == [1, 1, 2, 2, 1, 2, 3]
