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


def _dot(size: float = 0.15) -> np.ndarray:
    """점 하나 (가운데 높이, 가로·세로 size em)."""
    px = max(2, int(size * EM))
    blob = _blank(px)
    top = PAD + EM // 2 - px // 2
    blob[top:top + px, :] = True
    return blob


def _leader(length_em: float = 4.0) -> np.ndarray:
    parts = []
    for _ in range(int(length_em / 0.5)):
        parts += [_dot(), _blank(int(0.35 * EM))]
    return np.hstack(parts)


def test_space_threshold_follows_gothic_gaps():
    # 공공기관 고딕 문서: 낱말 안 빈칸 0.05~0.2em, 공백 0.27~0.42em. 예전 하한 0.3em으로는 공백을 대부분 놓쳤다
    from korean_parser.parsers.pdf.ocr_spacing import space_threshold

    gaps = [0.05 + 0.15 * i / 99 for i in range(100)] + [0.27 + 0.15 * i / 29 for i in range(30)] + [0.9, 1.1, 2.5]
    assert 0.2 <= space_threshold(gaps) <= 0.27
    # 바탕체 합성 문서처럼 낱말 안이 0.38em까지 벌어지면 기준도 올라간다
    gaps = [0.1 + 0.28 * i / 99 for i in range(100)] + [0.46 + 0.3 * i / 29 for i in range(30)]
    assert 0.38 <= space_threshold(gaps) <= 0.46


@pytest.mark.parametrize("ocr_text, want", [("사망재해분석결과…13", "사망재해 분석결과 … 13"),
                                             ("사망재해분석결과13", "사망재해 분석결과 13")])
def test_toc_leader_is_set_aside(ocr_text, want):
    # 목차 점선: 그림의 점 수십 개와 OCR의 '…'(또는 없음)이 맞지 않아 줄 전체를 형태소 분석에 넘기고 있었다
    img = np.hstack([_line("사망재해 분석결과"), _blank(int(0.4 * EM)), _leader(), _line("13")])
    assert respace_line(img, ocr_text, H) == want


def test_trailing_dot_segment_left_empty():
    # 줄 끝에 남은 점 하나(점선의 일부)는 OCR 글에 없으면 비워 둔다. 예전에는 글자를 그 점에 나눠 담아 '안전관 리대 상'
    img = np.hstack([_line("안전관리 대상 사업"), _blank(int(0.6 * EM)), _dot()])
    assert respace_line(img, "안전관리대상사업", H) == "안전관리 대상 사업"


def test_middle_dot_stays_attached():
    # 가운뎃점 둘레는 여백이 넓어 섞인 자리 기준(0.6배)을 넘지만, 앞뒤 낱말에 붙여 쓴다
    img = np.hstack([_line("대내"), _blank(int(0.3 * EM)), _glyph("·"), _blank(int(0.3 * EM)), _line("외 안전경영")])
    assert respace_line(img, "대내·외안전경영", H) == "대내·외 안전경영"


def test_open_paren_needs_full_space():
    # 여는 괄호 앞은 괄호의 왼쪽 여백 때문에 넓어 보인다. 섞인 자리의 낮은 기준을 쓰면 '방문취업 (H-2)'
    img = np.hstack([_line("방문취업"), _blank(int(0.3 * EM)), _line("(H-2) 자격")])
    assert respace_line(img, "방문취업(H-2)자격", H) == "방문취업(H-2) 자격"


def test_digits_need_wide_gap():
    # 고딕 문서는 공백 기준이 0.25em 안팎인데 숫자 '1' 옆은 0.35em까지 빈다 → 숫자 옆은 0.5em 넘어야 띄운다
    parts = []
    for ch in "전-예-":
        parts += [_glyph(ch), _blank(int(0.1 * EM))]
    parts += [_blank(int(0.25 * EM)), _glyph("1"), _blank(int(0.35 * EM)), _glyph("2")]
    assert respace_line(np.hstack(parts), "전-예-12", H, space_gap=0.25) == "전-예-12"


def test_light_gray_text_uses_local_contrast():
    # 연한 회색 제목(밝기 186~193, 바탕 250)은 잉크 기준(160)에 걸리지 않아 그림으로 띄우지 못했다
    from korean_parser.parsers.pdf.ocr_spacing import SCALE, PageImage

    spaced = "2026년도 교육훈련계획"
    img = _line(spaced)
    page = PageImage.__new__(PageImage)
    page.gray = np.where(img, 190, 250).astype(np.uint8)
    page.ink = page.gray < 160
    box = (0, 0, img.shape[1] / SCALE, img.shape[0] / SCALE)
    assert respace_line(page.crop(box), _ocr(spaced), H) == spaced


def test_ink_between_ocr_boxes_is_ignored():
    # 줄이 표의 두 칸에 걸치면 칸 사이 세로 괘선 조각이 글자 조각으로 잡혔다 → 상자 사이 잉크는 지운다
    from korean_parser.parsers.pdf.ocr_spacing import SCALE, _between_boxes_cleared

    left, right = _line("구분"), _line("제품 기준")
    bar = _blank(int(2.0 * EM))
    bar[PAD - 2:PAD + EM - 4, EM:EM + 3] = True  # 위아래가 끊긴 괘선 (괘선으로 지워지지 않음)
    img = np.hstack([left, bar, right])
    w0, w1 = left.shape[1] / SCALE, (left.shape[1] + bar.shape[1]) / SCALE
    line = [OcrCell("구분", (0, 0, w0, H / SCALE)), OcrCell("제품기준", (w1, 0, img.shape[1] / SCALE, H / SCALE))]
    assert respace_line(img, "구분제품기준", H) != "구분 제품 기준"
    cleared = _between_boxes_cleared(img, line, (0, 0, img.shape[1] / SCALE, H / SCALE))
    assert respace_line(cleared, "구분제품기준", H) == "구분 제품 기준"


def test_unread_icon_at_line_start():
    # 줄 맨 앞의 그림 기호(체크 표시)를 OCR이 읽지 않았다. 예전에는 첫 글자를 기호에 짝지어 '오 시는 길'
    icon = _blank(int(0.9 * EM))
    icon[PAD:PAD + EM - 4, 2:-2] = True
    yy, xx = np.mgrid[0:icon.shape[0], 0:icon.shape[1]]
    icon[(np.abs(yy - PAD - xx) < 3) | (np.abs(yy - PAD - (icon.shape[1] - xx)) < 3)] = False  # 속에 흰 무늬
    assert bullet_shape(icon, EM) is None  # 개조식 기호 모양이 아니다
    img = np.hstack([icon, _blank(int(0.6 * EM)), _line("오시는 길")])
    assert respace_line(img, "오시는길", H) == "오시는 길"


def test_white_text_on_dark_band():
    # 어두운 띠 위의 흰 글자: 상자의 절반 넘게 잉크이면 밝은 화소를 글자로 본다
    from korean_parser.parsers.pdf.ocr_spacing import SCALE, PageImage

    spaced = "이용 프로세스"
    strokes = _line(spaced)
    strokes[:, ::3] = False  # 글자 획은 가늘다 (상자의 대부분은 띠 색)
    strokes[PAD + 4:PAD + EM - 4, :] &= np.arange(strokes.shape[1]) % 3 == 1
    page = PageImage.__new__(PageImage)
    page.gray = np.where(strokes, 255, 60).astype(np.uint8)
    page.ink = page.gray < 160
    box = (0, 0, strokes.shape[1] / SCALE, strokes.shape[0] / SCALE)
    assert page.crop(box).mean() < 0.5
    assert respace_line(page.crop(box), _ocr(spaced), H) == spaced


@pytest.mark.parametrize("ocr_text", ["0액화석유가스의안전관리", "O액화석유가스의안전관리"])
def test_bullet_misread_as_digit_keeps_its_segment(ocr_text):
    # 'ㅇ' 기호를 OCR이 '0'·'O'로 읽었다. 기호 모양으로는 알아보지 못해도 그 조각에 짝지어야 뒤 낱말이 밀리지 않는다
    bullet = _blank(int(0.95 * EM))
    bullet[PAD + 4:PAD + EM - 4, 3:-3] = True
    bullet[PAD + 8:PAD + EM - 8, 7:-7] = False
    bullet[PAD + 4:PAD + 6, :] = False  # 위가 트인 모양 (□로 보지 않게)
    img = np.hstack([bullet, _blank(int(1.0 * EM)), _line("액화석유가스의 안전관리")])
    got = respace_line(img, ocr_text, H)
    assert got.endswith(" 액화석유가스의 안전관리")
