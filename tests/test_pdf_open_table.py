"""위아래 선만 있는 표('열린 표')를 글 위치로 읽기 (parsers/pdf/open_table.py).

PDF 없이 글자 위치(Char)와 가로선(Rule)만으로 표를 그려 본다. 글자 크기 10pt, 한글 한 글자 폭 10pt, 줄 간격 15pt.
열은 x = 100, 200, 300에서 시작하고, 열 경계는 각 열 글이 끝난 뒤 빈 띠다.
"""

from korean_parser.parsers.pdf.open_table import text_grid
from korean_parser.parsers.pdf.page import Char
from korean_parser.parsers.pdf.rules import Rule

SIZE = 10.0
COLS = (100, 200, 300)
RIGHT = 380  # 표 오른쪽 끝 (셋째 열 경계)


def _chars(text: str, x: float, y: float) -> list[Char]:
    """기준선 y에 x부터 글자를 늘어놓는다. 공백은 4pt, 한글 10pt, 그 밖의 글자 6pt."""
    out = []
    for ch in text:
        w = 4.0 if ch == " " else (SIZE if "가" <= ch <= "힣" else 6.0)
        out.append(Char(text=ch, box=(x, y - 8, x + w - 0.6, y + 1), size=SIZE, bold=False, italic=False,
                        color="#000000", font="", origin=(x, y), advance=w))
        x += w
    return out


def _row(y: float, *cells: str | None) -> list[Char]:
    out = []
    for x, text in zip(COLS, cells):
        if text:
            out += _chars(text, x, y)
    return out


def _rules(*ys: float) -> list[Rule]:
    return [Rule("h", y, 90, RIGHT) for y in ys]


def _texts(grid, chars):
    """칸마다 (행, 열, 행 수, 열 수, 글)."""
    out = []
    for c in grid.cells:
        x0, y0, x1, y1 = c.box
        inside = [ch for ch in chars if x0 <= (ch.box[0] + ch.box[2]) / 2 <= x1 and y0 < (ch.box[1] + ch.box[3]) / 2 < y1]
        text = "".join(ch.text for ch in sorted(inside, key=lambda ch: (round(ch.origin[1]), ch.origin[0]))).strip()
        out.append((c.r0, c.c0, c.r1 - c.r0 + 1, c.c1 - c.c0 + 1, text))
    return out


def test_columns_rows_and_wrapped_cell():
    # 둘째 행의 둘째 칸은 두 줄로 넘어간다 (앞 줄이 열 끝까지 차서 다음 낱말이 들어갈 자리가 없다)
    chars = (_row(110, "구분", "세부 내용", "배점")
             + _row(135, "설계", "평가 비율은 기술평", "10")
             + _row(150, None, "가 90%", None)
             + _row(175, "분석", "구현", "5"))
    grid = text_grid(chars, _rules(100, 120, 185), (90, 100, RIGHT, 185))
    assert grid is not None and grid.source == "text"
    assert grid.n_cols == 3 and grid.n_rows == 3
    cells = _texts(grid, chars)
    assert [c[4].replace(" ", "") for c in cells] == ["구분", "세부내용", "배점", "설계", "평가비율은기술평가90%", "10",
                                                     "분석", "구현", "5"]
    # 위 선, 머리 행 아래 선, 아래 선만 테두리로 둔다 (본문 행 사이에는 선이 없다)
    assert grid.cells[0].borders["top"] and grid.cells[0].borders["bottom"] and not grid.cells[0].borders["left"]
    last = grid.cells[6]
    assert last.r0 == 2 and not last.borders["top"] and last.borders["bottom"]


def test_short_line_starts_new_row():
    # 앞 줄이 짧게 끝났으면(다음 낱말이 들어갈 자리가 있었으면) 아랫줄은 새 행이다
    chars = (_row(110, "구분", "내용", "배점")
             + _row(135, "설계", "구현", "10")
             + _row(150, "분석", "시험", "5"))
    grid = text_grid(chars, _rules(100, 120, 160), (90, 100, RIGHT, 160))
    assert grid.n_rows == 3


def test_text_crossing_column_boundary_is_merged_cell():
    chars = (_row(110, "구분", "평가항목", "배점")
             + _chars("사업기간: 계약일로부터 8개월", 100, 135) + _chars("2.5", 300, 135))
    grid = text_grid(chars, _rules(100, 120, 145), (90, 100, RIGHT, 145))
    row = [(c.c0, c.c1) for c in grid.cells if c.r0 == 1]
    assert row == [(0, 1), (2, 2)]


def test_empty_first_column_continues_cell_above():
    # 선이 없는 표에서 빈 첫 열은 위 칸이 이어진 것으로 본다 (구분 열 묶음). 다른 열의 빈 칸은 빈 칸으로 둔다
    chars = (_row(110, "구분", "내용", "비고")
             + _row(135, "설계", "구현", "10")
             + _row(150, None, "시험", None))
    grid = text_grid(chars, _rules(100, 120, 160), (90, 100, RIGHT, 160))
    first = next(c for c in grid.cells if c.r0 == 1 and c.c0 == 0)
    assert (first.r0, first.r1) == (1, 2)
    assert any(c.r0 == 2 and c.c0 == 2 and c.r1 == 2 for c in grid.cells)


def test_continuation_uses_known_columns():
    # 다음 쪽으로 넘어간 나머지: 머리 행도 세로선도 없다. 앞 쪽 표의 열 경계로 읽는다
    head = _row(110, "구분", "내용", "비고") + _row(135, "설계", "구현", "10")
    first = text_grid(head, _rules(100, 120, 145), (90, 100, RIGHT, 145))
    rest_chars = _row(60, "분석", "시험", "5") + _row(85, "이행", "교육", "-")
    rest = text_grid(rest_chars, _rules(50, 95), (first.xs[0], 50, first.xs[-1], 95), columns=first.xs)
    assert rest is not None and rest.xs == first.xs and rest.n_rows == 2


def test_vertical_rules_mean_ruled_table():
    chars = _row(110, "구분", "내용", "비고") + _row(135, "설계", "구현", "10")
    rules = _rules(100, 120, 145) + [Rule("v", 190, 100, 145), Rule("v", 290, 100, 145)]
    assert text_grid(chars, rules, (90, 100, RIGHT, 145)) is None  # 괘선 표는 build_grid의 몫


def test_page_order_is_fixed():
    # Docling이 앞 쪽 맨 아래 표를 다음 쪽 앞부분 뒤에 두어도 쪽 순서대로 놓는다
    from korean_parser.ir import Document, PageBreak, Paragraph, Provenance, Text
    from korean_parser.parsers.pdf import PdfParser

    def para(i, page, y):
        return Paragraph(id=f"b{i}", prov=Provenance(parser="t", page=page, bbox=(0, y, 10, y + 5)),
                         inlines=[Text(text=f"p{page}-{y}")])

    parser = PdfParser.__new__(PdfParser)
    parser.doc = Document(blocks=[para(1, 1, 10), PageBreak(id="b2", prov=Provenance(parser="t", page=2)),
                                  para(3, 2, 50), PageBreak(id="b4", prov=Provenance(parser="t", page=1)),
                                  para(5, 1, 700), PageBreak(id="b6", prov=Provenance(parser="t", page=2)),
                                  para(7, 2, 80)])
    parser._seq = 7
    parser.ocr = False
    parser._fix_page_order()
    assert [b.id for b in parser.doc.blocks] == ["b1", "b5", "b2", "b3", "b7"]
