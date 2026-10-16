"""위아래 선만 있는 표('열린 표')의 행·열을 글 위치로 정한다.

칸마다 선이 있는 표는 선으로 격자를 읽는다 (`rules.build_grid`). 위·아래(와 머리 행 아래)에만 가로선이 있고 세로선이
없는 표는 선만으로는 열을 알 수 없다. 표 구조 인식 모델(TableFormer)에 맡기면 줄이 넘어간 칸을 다른 행으로 나누고,
쪽을 넘어간 나머지(선이 없음)는 표로 찾지 못했다. 그래서 텍스트 레이어의 글자 위치로 격자를 만든다.

- 열: 머리 행(머리 행 아래 선 위의 글)의 글 덩어리 수가 열 수다. 이웃한 머리 글 사이에서 본문 글이 가장 적게 걸친
  자리를 열 경계로 삼는다 (대개 아무 줄도 걸치지 않은 빈 띠의 가운데).
- 행: 본문 줄마다 새 행인지 본다. 앞 줄의 같은 열 글이 열 오른쪽 끝까지 차서 다음 낱말이 들어갈 자리가 없었으면
  줄이 넘어간 것(같은 행)이고, 그 밖의 글이 하나라도 있으면 새 행이다.
- 병합: 열 경계를 가로지른 글은 여러 열을 차지한 칸이다. 첫 열(구분 열)이 비어 있으면 위 칸이 아래로 이어진 것으로
  본다. 선이 없는 표에서는 빈 칸과 병합 칸이 똑같이 보이는데, 첫 열을 비운 행은 한국 문서에서 대개 위 칸의 묶음이다
  (표 구조 인식 모델도 같은 판단을 한다). 다른 열의 빈 칸은 빈 칸으로 둔다.

결과는 괘선 격자와 같은 `rules.Grid`라서 같은 코드(`PdfParser._ruled_table`)로 문서 모델의 표가 된다.
좌표는 '왼쪽 위 원점, pt'.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rules import Grid, GridCell, Rule

# 열 사이로 볼 수 있는 가장 좁은 빈 띠 (글자 크기 배수). 낱말 사이 공백(바탕 11pt에서 잉크 사이 7pt 안팎)보다 넓고,
# 칸 안쪽 여백 두 개(Word 5.8pt×2, 한글 5.1pt×2)보다 좁다
COL_GAP = 0.85


@dataclass
class _Piece:
    """한 줄 안에서 이어진 글 (낱말 사이 공백은 넘고, 열 사이 빈 띠에서 끊긴다)."""

    x0: float
    x1: float
    first_word: float  # 첫 낱말 폭 (줄이 넘어간 것인지 볼 때 쓴다)
    size: float


@dataclass
class _Line:
    top: float
    bottom: float
    pieces: list[_Piece]


def _lines(chars) -> list[_Line]:
    """글자를 기준선으로 줄로 묶고, 줄마다 COL_GAP보다 넓은 빈칸에서 글 조각으로 나눈다.

    공백 글자도 함께 묶는다: 첫 낱말이 어디서 끝나는지는 잉크 간격보다 PDF의 실제 공백 글자로 보는 것이 정확하다
    (숫자·쉼표는 잉크 사이가 넓다).
    """
    rows: list[list] = []
    for c in sorted((c for c in chars if c.text.strip() or not c.generated), key=lambda c: c.origin[1]):
        if rows and abs(c.origin[1] - rows[-1][0].origin[1]) <= max(c.size, rows[-1][0].size, 1.0) * 0.4:
            rows[-1].append(c)
        else:
            rows.append([c])
    out = []
    for row in rows:
        row.sort(key=lambda c: c.origin[0])
        groups: list[list] = []
        for c in row:
            if not c.text.strip():
                if groups:
                    groups[-1].append(c)
                continue
            ink = [g for g in groups[-1] if g.text.strip()] if groups else []
            if ink and c.box[0] - max(g.box[2] for g in ink) <= COL_GAP * max(c.size, ink[-1].size):
                groups[-1].append(c)
            else:
                groups.append([c])
        pieces = []
        for g in groups:
            ink = [c for c in g if c.text.strip()]
            if not ink:
                continue
            # 첫 낱말: 첫 공백 글자(또는 글자 크기 절반이 넘는 빈칸) 앞까지
            end = ink[0].box[2]
            for a, b in zip(g, g[1:]):
                if not b.text.strip() or b.box[0] - a.box[2] > a.size * 0.5:
                    break
                end = max(end, b.box[2])
            pieces.append(_Piece(ink[0].box[0], max(c.box[2] for c in ink), end - ink[0].box[0],
                                 max(c.size for c in ink)))
        if pieces:
            ink = [c for c in row if c.text.strip()]
            out.append(_Line(min(c.box[1] for c in ink), max(c.box[3] for c in ink), pieces))
    return out


def _column_bounds(head: list[_Line], body: list[_Line], left: float, right: float) -> list[float] | None:
    """머리 행 글 덩어리로 열 수를 정하고, 이웃한 머리 글 사이에서 본문 글이 가장 적게 걸친 자리를 경계로."""
    spans: list[list[float]] = []
    for p in sorted((p for ln in head for p in ln.pieces), key=lambda p: p.x0):
        if spans and p.x0 - spans[-1][1] <= COL_GAP * p.size:
            spans[-1][1] = max(spans[-1][1], p.x1)
        else:
            spans.append([p.x0, p.x1])
    if len(spans) < 2:
        return None
    xs = [left]
    for (_, a1), (b0, _) in zip(spans, spans[1:]):
        # a1~b0 사이를 0.5pt 간격으로 훑어, 본문 조각이 덮는 줄 수가 가장 적은 구간의 가운데
        best: tuple[int, float, float] | None = None
        x = a1
        run_start, run_cnt = None, None
        while x <= b0:
            cnt = sum(1 for ln in body for p in ln.pieces if p.x0 - 0.5 <= x <= p.x1 + 0.5)
            if run_cnt is None or cnt != run_cnt:
                run_start, run_cnt = x, cnt
            width = x - run_start
            if best is None or (cnt, -width) < (best[0], -(best[2] - best[1])):
                best = (cnt, run_start, x)
            x += 0.5
        xs.append((best[1] + best[2]) / 2 if best else (a1 + b0) / 2)
    xs.append(right)
    return xs


def _col_of(x: float, xs: list[float]) -> int:
    for j in range(len(xs) - 1):
        if x < xs[j + 1]:
            return j
    return len(xs) - 2


def _continues(prev: _Piece, cur: _Piece, col_right: float) -> bool:
    """cur가 prev 칸의 넘어간 줄인가: prev 뒤에 cur의 첫 낱말이 들어갈 자리가 없었고, 왼쪽 끝이 비슷하다."""
    room = col_right - prev.x1
    return room < cur.first_word + cur.size * 0.5 and abs(prev.x0 - cur.x0) <= cur.size * 1.5


def text_grid(chars, rules: list[Rule], box, columns: list[float] | None = None) -> Grid | None:
    """열린 표의 격자. box는 표가 있을 만한 영역(Docling 표 영역, 이어진 쪽이면 쪽 위~가로선).

    columns: 열 경계를 이미 알 때(앞 쪽에서 이어진 표) 그 값. 없으면 머리 행으로 정한다.
    표가 아니면(가로선이 모자라거나, 세로선이 있거나, 열이 하나) None.
    """
    x0, y0, x1, y1 = box
    width = x1 - x0
    hs = sorted((r for r in rules if r.axis == "h" and y0 - 6 <= r.pos <= y1 + 6
                 and min(r.b, x1) - max(r.a, x0) >= width * 0.6), key=lambda r: r.pos)
    hs = [r for i, r in enumerate(hs) if i == 0 or r.pos - hs[i - 1].pos > 2]  # 겹선은 하나로
    if len(hs) < (1 if columns else 2):
        return None
    left = min(r.a for r in hs) if not columns else columns[0]
    right = max(r.b for r in hs) if not columns else columns[-1]
    top = hs[0].pos if not columns else y0
    bottom = hs[-1].pos
    if any(r.axis == "v" and left + 2 < r.pos < right - 2 and min(r.b, bottom) - max(r.a, top) > 4 for r in rules):
        return None  # 세로선이 있으면 괘선 표다 (build_grid의 몫)
    inside = [c for c in chars if left - 1 <= (c.box[0] + c.box[2]) / 2 <= right + 1
              and top < (c.box[1] + c.box[3]) / 2 < bottom]
    lines = _lines(inside)
    if len(lines) < 2:
        return None
    if columns:
        head, body, head_rule = [], lines, None
        xs = list(columns)
    else:
        # 머리 행: 위에서 두 번째 가로선 위의 줄. 그 선이 없으면 첫 줄
        head_rule = hs[1].pos if len(hs) >= 3 else None
        head = [ln for ln in lines if head_rule is not None and ln.bottom <= head_rule + 1] or lines[:1]
        body = lines[len(head):]
        if not body:
            return None
        xs = _column_bounds(head, body, left, right)
        if xs is None:
            return None

    # 줄 → 행. 줄의 조각마다 차지한 열 범위 (열 경계를 가로지르면 여러 열)
    def placed(ln: _Line) -> list[tuple[int, int, _Piece]]:
        out: list[tuple[int, int, _Piece]] = []
        for p in ln.pieces:
            a, b = _col_of(p.x0 + 0.5, xs), _col_of(p.x1 - 0.5, xs)
            if out and out[-1][0] == a and out[-1][1] == b:  # 같은 칸 안의 넓은 낱말 사이: 한 조각으로
                q = out[-1][2]
                out[-1] = (a, b, _Piece(q.x0, p.x1, q.first_word, max(q.size, p.size)))
            else:
                out.append((a, b, p))
        return out

    rows: list[list[_Line]] = [list(head)] if head else []  # 머리 행은 줄이 여럿이어도 한 행
    prev: list[tuple[int, int, _Piece]] = []
    body_rows: list[list[_Line]] = []
    for ln in body:
        cur = placed(ln)
        same = bool(body_rows) and all(
            any(pa == a and pb == b and _continues(pp, p, xs[b + 1]) for pa, pb, pp in prev) for a, b, p in cur)
        if same:
            body_rows[-1].append(ln)
        else:
            body_rows.append([ln])
        prev = cur
    rows += body_rows
    n_head = 1 if head else 0

    # 행 경계: 첫 행 위는 위 가로선, 머리 행 아래는 머리 행 선, 나머지는 행 사이의 가운데
    ys = [top]
    for r in range(len(rows) - 1):
        if r == 0 and head and head_rule is not None:
            ys.append(head_rule)
        else:
            ys.append((max(ln.bottom for ln in rows[r]) + min(ln.top for ln in rows[r + 1])) / 2)
    ys.append(bottom)
    nc = len(xs) - 1

    # 칸: 행마다 글이 있는 열 범위. 가로지른 글은 여러 열을 합친다
    cells: list[GridCell] = []
    owner: dict[tuple[int, int], GridCell] = {}
    for r, group in enumerate(rows):
        spans: list[list[int]] = []
        for ln in group:
            for a, b, _ in placed(ln):
                for s in spans:
                    if a <= s[1] and b >= s[0]:
                        s[0], s[1] = min(s[0], a), max(s[1], b)
                        break
                else:
                    spans.append([a, b])
        filled = {c for a, b in spans for c in range(a, b + 1)}
        for a, b in spans:
            cells.append(GridCell(r, a, r, b, (xs[a], ys[r], xs[b + 1], ys[r + 1])))
            for c in range(a, b + 1):
                owner[(r, c)] = cells[-1]
        for c in range(nc):
            if c in filled:
                continue
            above = owner.get((r - 1, c))
            if c == 0 and r > n_head and above is not None and above.c0 == 0 and above.c1 == 0 and filled:
                above.r1 = r  # 첫 열이 빈 행: 위 칸이 이어진 것으로 본다
                above.box = (above.box[0], above.box[1], above.box[2], ys[r + 1])
                owner[(r, c)] = above
                continue
            cells.append(GridCell(r, c, r, c, (xs[c], ys[r], xs[c + 1], ys[r + 1])))
            owner[(r, c)] = cells[-1]
    if nc < 2 or len(rows) < 2:
        return None

    rule_ys = [r.pos for r in hs]
    for gc in cells:
        gc.borders = {"top": any(abs(y - ys[gc.r0]) <= 2 for y in rule_ys),
                      "bottom": any(abs(y - ys[gc.r1 + 1]) <= 2 for y in rule_ys), "left": False, "right": False}
    cells.sort(key=lambda c: (c.r0, c.c0))
    return Grid(xs, ys, cells, source="text")
