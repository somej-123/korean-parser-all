"""스캔(OCR) 글의 공백·개조식 기호를 쪽 그림의 빈칸으로 되살리기 (parsers/pdf/ocr_spacing.py).

글꼴에 따라 결과가 달라지지 않도록 글자 대신 '잉크 덩어리'를 숫자 배열로 그린다. 글자 폭은 char_width,
낱말 안 빈칸은 0.1em, 공백은 0.55em(한글 사이) 또는 0.3em(숫자·영문·문장부호 옆)으로 둔다.
"""

import numpy as np
import pytest

from korean_parser.parsers.pdf.ocr_spacing import (
    OcrCell, bullet_shape, char_width, group_lines, respace_line,
)

EM = 30  # 글자 높이(px)
PAD = 6  # 줄 위아래 여백


def _blank(width: int) -> np.ndarray:
    return np.zeros((EM + 2 * PAD, width), dtype=bool)


def _glyph(ch: str) -> np.ndarray:
    blob = _blank(max(2, int(char_width(ch) * EM * 0.9)))
    blob[PAD:PAD + EM, :] = True
    return blob


def _ring(square: bool) -> np.ndarray:
    size = int(EM * 0.8)
    blob = _blank(size)
    yy, xx = np.mgrid[0:size, 0:size]
    if square:
        ring = (yy < 2) | (yy >= size - 2) | (xx < 2) | (xx >= size - 2)
    else:
        r = (size - 1) / 2
        d = np.sqrt((yy - r) ** 2 + (xx - r) ** 2)
        ring = (d <= r) & (d >= r - 2)
    top = PAD + (EM - size) // 2
    blob[top:top + size, :] = ring
    return blob


def _line(spaced: str, bullet: str | None = None, mixed_gap: float = 0.3) -> np.ndarray:
    """정답 띄어쓰기(spaced)대로 덩어리를 늘어놓은 줄 그림."""
    parts = []
    if bullet:
        parts += [_ring(bullet == "□"), _blank(int(0.6 * EM))]
    prev = None
    for i, ch in enumerate(spaced):
        if ch == " ":
            continue
        if prev is not None:
            if spaced[i - 1] == " ":
                hangul = "가" <= prev <= "힣" and "가" <= ch <= "힣"
                parts.append(_blank(int((0.55 if hangul else mixed_gap) * EM)))
            else:
                parts.append(_blank(int(0.1 * EM)))
        parts.append(_glyph(ch))
        prev = ch
    return np.hstack(parts)


def _ocr(spaced: str) -> str:
    return spaced.replace(" ", "")


H = EM + 2 * PAD


@pytest.mark.parametrize("spaced", [
    "계약방법: 협상에 의한 계약",
    "하자보수 기간은 검수 완료일로부터 1년",
    "평가 비율은 기술평가 90%, 가격평가 10%로 함",
    "사업예산: 1,250,000,000원(VAT 포함)",
])
def test_spaces_from_image_gaps(spaced):
    assert respace_line(_line(spaced), _ocr(spaced), H) == spaced


def test_ocr_spaces_are_ignored():
    # OCR이 남긴 공백은 믿지 않고 그림의 빈칸을 따른다
    spaced = "제안서 제출 부수는 원본 1부, 사본 5부"
    assert respace_line(_line(spaced), "제안서제출 부수는원본1부,사본 5부", H) == spaced


def test_no_space_before_closing_punctuation():
    # 마침표·쉼표·닫는 괄호 앞, 여는 괄호 뒤에는 빈칸이 넓어도 띄우지 않는다
    img = np.hstack([_line("제출한다"), _blank(int(0.6 * EM)), _glyph("."), _blank(int(0.6 * EM)), _line("본")])
    assert respace_line(img, "제출한다.본", H) == "제출한다. 본"


@pytest.mark.parametrize("ocr_text, want", [("2026사업계획서", "2026 사업계획서"), ("2026년도사업", "2026년도 사업"),
                                             ("2단계요구사항", "2단계 요구사항")])
def test_space_after_digit_only_before_non_unit(ocr_text, want):
    # 숫자 글꼴은 옆 여백이 넓어 '2026년도'도 공백만큼 벌어진다. 빈칸 폭은 같으므로 뒤가 단위(년, 단계 …)면 붙이고,
    # 단위가 아닌 낱말이면 띄운다 (실제 스캔 '2026 사업계획서'가 '2026사업계획서'로 붙었다)
    spaced = want.replace("2026년도", "2026 년도").replace("2단계", "2 단계")
    assert respace_line(_line(spaced, mixed_gap=0.5), ocr_text, H) == want


@pytest.mark.parametrize("bullet, ocr_text", [("○", "하자보수기간은"), ("○", "O하자보수기간은"),
                                               ("○", "ㅇ하자보수기간은"), ("□", "하자보수기간은")])
def test_bullet_recovered_from_shape(bullet, ocr_text):
    # OCR이 빠뜨리거나 'O'·'ㅇ'으로 읽은 개조식 기호를 모양으로 되살린다
    assert respace_line(_line("하자보수 기간은", bullet=bullet), ocr_text, H) == f"{bullet} 하자보수 기간은"


def test_bullet_shapes():
    assert bullet_shape(_ring(square=False), EM) == "○"
    assert bullet_shape(_ring(square=True), EM) == "□"
    assert bullet_shape(_glyph("가"), EM) == "■"  # 꽉 찬 사각형
    dash = _blank(int(0.4 * EM))
    dash[PAD + EM // 2:PAD + EM // 2 + 2, :] = True
    assert bullet_shape(dash, EM) == "-"
    word = _line("본")  # 글자 덩어리처럼 속이 찬 것도 테두리만 있는 것도 아닌 모양
    word[PAD + EM // 3:PAD + 2 * EM // 3, 3:-3] = False
    word[PAD + EM // 2, :] = True
    assert bullet_shape(word, EM) is None


def test_table_rules_are_ignored():
    # 표 칸 그림의 위아래 괘선(줄 전체를 가로지르는 잉크)은 빈칸 계산에서 뺀다
    spaced = "하자보수 기간은 검수"
    img = _line(spaced)
    img[0, :] = True
    img[-1, :] = True
    assert respace_line(img, _ocr(spaced), H) == spaced


def test_mismatch_returns_none():
    # OCR 글자 수가 그림과 크게 다르면(글자를 빠뜨림) 위치를 믿을 수 없다 → 형태소 분석에 맡긴다
    assert respace_line(_line("하자보수 기간은 검수 완료일로부터 1년"), "하자보수", H) is None


def test_group_lines():
    cells = [OcrCell("둘째 줄", (10, 30, 60, 42)), OcrCell("오른쪽", (70, 11, 120, 23)),
             OcrCell("첫 줄", (10, 10, 60, 22))]
    assert [[c.text for c in line] for line in group_lines(cells)] == [["첫 줄", "오른쪽"], ["둘째 줄"]]


def test_scan_rules_keep_cell_borders_not_glyph_strokes():
    # 스캔 쪽 그림의 괘선: 가로선 3개, 세로선 3개(양 끝이 가로선에 닿음)인 2×2 표. 칸 안 글자의 세로 획(10pt)은
    # 길이로는 세로선과 비슷하지만 가로선에 닿지 않아 선이 아니다 (실제 스캔: 세로선 후보 97개 중 진짜는 6개)
    from korean_parser.parsers.pdf.rules import build_grid
    from korean_parser.parsers.pdf.scan_rules import image_rules

    scale = 3.0
    ink = np.zeros((int(120 * scale), int(320 * scale)), dtype=bool)

    def px(v):
        return int(v * scale)

    for y in (20, 50, 80):  # 가로선 (20pt~300pt)
        ink[px(y):px(y) + 2, px(20):px(300)] = True
    for x in (20, 160, 300):  # 세로선 (20pt~80pt)
        ink[px(20):px(80) + 2, px(x):px(x) + 2] = True
    ink[px(23):px(33), px(60):px(60) + 2] = True  # 칸 안 글자의 세로 획
    rules = image_rules(ink, scale)
    assert sorted(round(r.pos) for r in rules if r.axis == "v") == [20, 160, 300]
    grid = build_grid(rules, (20, 20, 300, 80))
    assert (grid.n_rows, grid.n_cols) == (2, 2)
