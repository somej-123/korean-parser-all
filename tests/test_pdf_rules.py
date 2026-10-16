"""괘선으로 표 구조 찾기 (pdfium 없이 선 목록만으로 시험)."""

from korean_parser.parsers.pdf.rules import Rule, build_grid


def h(y, a, b):
    return Rule("h", y, a, b)


def v(x, a, b):
    return Rule("v", x, a, b)


def spans(grid):
    return {(c.r0, c.c0): (c.r1 - c.r0 + 1, c.c1 - c.c0 + 1) for c in grid.cells}


def test_merged_cells_from_missing_lines():
    # 3행 3열. 첫 열은 세 행 병합(행 사이 선이 첫 열에 없음), 첫 행 2·3열은 가로 병합
    rules = [h(0, 0, 300), h(100, 0, 300)]
    rules += [h(30, 100, 300), h(60, 100, 300)]       # 행 경계: 1열에는 없음
    rules += [v(0, 0, 100), v(300, 0, 100), v(100, 0, 100)]
    rules += [v(200, 30, 100)]                        # 2·3열 경계: 첫 행에는 없음
    grid = build_grid(rules, (0, 0, 300, 100))
    assert (grid.n_rows, grid.n_cols) == (3, 3)
    assert spans(grid) == {(0, 0): (3, 1), (0, 1): (1, 2), (1, 1): (1, 1), (1, 2): (1, 1),
                           (2, 1): (1, 1), (2, 2): (1, 1)}


def test_dotted_lines_are_joined():
    # 행 경계가 점선(짧은 조각 여러 개)이어도 하나의 선으로 본다
    rules = [h(0, 0, 200), h(80, 0, 200), v(0, 0, 80), v(100, 0, 80), v(200, 0, 80)]
    rules += [h(40, x, x + 1.2) for x in range(0, 200, 2)]
    grid = build_grid(rules, (0, 0, 200, 80))
    assert (grid.n_rows, grid.n_cols) == (2, 2)
    assert all(rs == 1 and cs == 1 for rs, cs in spans(grid).values())


def test_open_sided_table():
    # 한국 문서에 흔한 좌우 바깥선 없는 표: 범위는 수평선이 걸친 가로 범위로 정한다
    rules = [h(0, 0, 200), h(40, 0, 200), h(80, 0, 200), v(100, 0, 80)]
    grid = build_grid(rules, (0, 0, 200, 80))
    assert (grid.n_rows, grid.n_cols) == (2, 2)
    left_cell = next(c for c in grid.cells if (c.r0, c.c0) == (0, 0))
    assert left_cell.borders["right"] and not left_cell.borders["left"]


def test_region_grows_along_lines():
    # 표 인식 영역이 표의 일부(오른쪽 아래)만 덮어도 이어진 선을 따라 표 전체를 찾는다
    rules = [h(y, 0, 300) for y in (0, 50, 100)] + [v(x, 0, 100) for x in (0, 150, 300)]
    grid = build_grid(rules, (140, 45, 290, 95))
    assert (grid.xs[0], grid.xs[-1], grid.ys[0], grid.ys[-1]) == (0, 300, 0, 100)


def test_not_a_ruled_table():
    assert build_grid([h(0, 0, 200)], (0, 0, 200, 50)) is None          # 선 하나
    assert build_grid([h(0, 0, 200), h(50, 0, 200)], (0, 0, 200, 50)) is None  # 열 구분 없음
