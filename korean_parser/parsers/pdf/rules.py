"""PDF 괘선(표의 선) 읽기와, 괘선으로 표 구조(행·열·병합 칸)를 찾는 계산.

한국 공공 문서의 표는 거의 모두 칸마다 선이 그어진 '괘선 표'다. 선의 위치를 그대로 읽으면 행·열 경계와
병합 칸이 정확히 나온다 (표 인식 모델은 병합 칸이 많은 표에서 칸을 섞는 경우가 많다).

좌표는 '왼쪽 위 원점, pt'.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field

THIN = 2.5  # 이보다 얇은 사각형·도형은 선으로 본다


@dataclass
class Rule:
    """수평(h) 또는 수직(v) 선. pos는 y(수평) 또는 x(수직), a~b는 선이 걸친 범위."""

    axis: str
    pos: float
    a: float
    b: float
    width: float = 0.5


def read_rules(obj, page_height: float) -> list[Rule]:
    """path 개체 하나에서 수평·수직 선을 읽는다.

    - 얇은 개체(선, 얇게 채운 사각형): 범위의 가운데 선
    - 윤곽선을 그린 개체: 축에 나란한 선분 (사각형 테두리, 꺾은선)
    - 면만 채운 두꺼운 사각형(칸 배경 등)은 선이 아니다
    """
    import pypdfium2.raw as raw

    left, bottom, right, top = obj.get_bounds()
    w, h = right - left, top - bottom
    if w < THIN and h < THIN:
        # 점선을 이루는 작은 조각 (1.2×0.7 등). 긴 쪽 방향의 짧은 선으로 보고, 나중에 이어 붙인다
        if max(w, h) < 0.5 or max(w, h) < min(w, h) * 1.4:
            return []
    if h < THIN and h <= w:
        y = page_height - (bottom + top) / 2
        return [Rule("h", y, left, right, max(h, 0.3))]
    if w < THIN and w < h:
        x = (left + right) / 2
        return [Rule("v", x, page_height - top, page_height - bottom, max(w, 0.3))]

    mode, stroke = ctypes.c_int(), ctypes.c_int()
    if not raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(mode), ctypes.byref(stroke)) or not stroke.value:
        return []
    if obj.level != 0:
        return []  # 양식(Form) 안의 개체는 좌표 변환이 겹쳐 있어 건너뛴다 (드묾)
    m = raw.FS_MATRIX()
    if not raw.FPDFPageObj_GetMatrix(obj.raw, ctypes.byref(m)):
        return []
    sw = ctypes.c_float()
    width = sw.value if raw.FPDFPageObj_GetStrokeWidth(obj.raw, ctypes.byref(sw)) else 0.5

    def to_page(px: float, py: float) -> tuple[float, float]:
        return m.a * px + m.c * py + m.e, page_height - (m.b * px + m.d * py + m.f)

    out: list[Rule] = []
    x, y = ctypes.c_float(), ctypes.c_float()
    start = prev = None
    for i in range(raw.FPDFPath_CountSegments(obj.raw)):
        seg = raw.FPDFPath_GetPathSegment(obj.raw, i)
        if not raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y)):
            continue
        pt = to_page(x.value, y.value)
        kind = raw.FPDFPathSegment_GetType(seg)
        if kind == raw.FPDF_SEGMENT_MOVETO:
            start = prev = pt
            continue
        if kind == raw.FPDF_SEGMENT_LINETO and prev is not None:
            out += _axis_rule(prev, pt, width)
        prev = pt
        if raw.FPDFPathSegment_GetClose(seg) and start is not None:
            out += _axis_rule(pt, start, width)
            prev = start
    return out


def _axis_rule(p, q, width: float) -> list[Rule]:
    (x0, y0), (x1, y1) = p, q
    if abs(y1 - y0) < 0.5 and abs(x1 - x0) >= 1:
        return [Rule("h", (y0 + y1) / 2, min(x0, x1), max(x0, x1), width)]
    if abs(x1 - x0) < 0.5 and abs(y1 - y0) >= 1:
        return [Rule("v", (x0 + x1) / 2, min(y0, y1), max(y0, y1), width)]
    return []


# ---------------------------------------------------------------------------
# 괘선 → 격자
# ---------------------------------------------------------------------------


@dataclass
class GridCell:
    r0: int
    c0: int
    r1: int  # 끝 행 (포함)
    c1: int  # 끝 열 (포함)
    box: tuple[float, float, float, float]
    borders: dict[str, bool] = field(default_factory=dict)  # top/bottom/left/right 선이 있는지


@dataclass
class Grid:
    xs: list[float]
    ys: list[float]
    cells: list[GridCell]

    @property
    def n_rows(self) -> int:
        return len(self.ys) - 1

    @property
    def n_cols(self) -> int:
        return len(self.xs) - 1


def _merge_lines(rules: list[Rule], tol: float = 1.5, gap: float = 2.5) -> dict[float, list[tuple[float, float]]]:
    """같은 위치(±tol)의 선을 모으고, 끊어진 조각(점선, 칸마다 따로 그린 선)을 잇는다. 위치 → 구간 목록."""
    groups: list[list[Rule]] = []
    for r in sorted(rules, key=lambda r: r.pos):
        if groups and r.pos - groups[-1][-1].pos <= tol:
            groups[-1].append(r)
        else:
            groups.append([r])
    out: dict[float, list[tuple[float, float]]] = {}
    for g in groups:
        pos = sum(r.pos for r in g) / len(g)
        spans: list[list[float]] = []
        for r in sorted(g, key=lambda r: r.a):
            if spans and r.a <= spans[-1][1] + gap:
                spans[-1][1] = max(spans[-1][1], r.b)
            else:
                spans.append([r.a, r.b])
        out[round(pos, 2)] = [(a, b) for a, b in spans]
    return out


def _covered(spans: list[tuple[float, float]], a: float, b: float, need: float = 0.6) -> bool:
    if b - a <= 0:
        return False
    total = sum(max(0.0, min(e, b) - max(s, a)) for s, e in spans)
    return total >= (b - a) * need


def _near(lines: dict[float, list], pos: float, tol: float = 2.0):
    best = None
    for p, spans in lines.items():
        if abs(p - pos) <= tol and (best is None or abs(p - pos) < abs(best[0] - pos)):
            best = (p, spans)
    return best[1] if best else []


def build_grid(rules: list[Rule], box: tuple[float, float, float, float], pad: float = 4.0,
               min_gap: float = 3.0) -> Grid | None:
    """표 영역(box) 근처의 괘선으로 격자와 병합 칸을 만든다. 괘선 표가 아니면 None."""
    bw, bh = box[2] - box[0], box[3] - box[1]
    # 쪽 테두리처럼 표보다 훨씬 긴 선은 뺀다
    rules = [r for r in rules if (r.b - r.a) <= 3 * (bw if r.axis == "h" else bh) + 20]
    # 표 영역에 닿는 선을 따라 영역을 넓힌다 (Docling 영역이 표의 일부만 덮는 경우)
    x0, y0, x1, y1 = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
    for _ in range(4):
        hs = [r for r in rules if r.axis == "h" and y0 <= r.pos <= y1 and r.b > x0 and r.a < x1]
        vs = [r for r in rules if r.axis == "v" and x0 <= r.pos <= x1 and r.b > y0 and r.a < y1]
        long_h = [r for r in hs if r.b - r.a > 8]
        long_v = [r for r in vs if r.b - r.a > 5]
        nx0 = min([x0] + [r.a - 1 for r in long_h] + [r.pos - 1 for r in long_v])
        nx1 = max([x1] + [r.b + 1 for r in long_h] + [r.pos + 1 for r in long_v])
        ny0 = min([y0] + [r.a - 1 for r in long_v] + [r.pos - 1 for r in long_h])
        ny1 = max([y1] + [r.b + 1 for r in long_v] + [r.pos + 1 for r in long_h])
        if (nx0, ny0, nx1, ny1) == (x0, y0, x1, y1):
            break
        x0, y0, x1, y1 = nx0, ny0, nx1, ny1
    # 점선 조각을 이은 뒤 짧은 선(글자 밑줄, 장식)은 뺀다
    H = {p: sp for p, sp in _merge_lines(hs).items() if max(b - a for a, b in sp) > 8}
    V = {p: sp for p, sp in _merge_lines(vs).items() if max(b - a for a, b in sp) > 5}
    if len(H) < 2:
        return None

    # 표의 범위: 수평선이 걸친 가로 범위 (좌우 바깥선이 없는 '열린 표'가 흔하다)
    left = min(a for spans in H.values() for a, _ in spans)
    right = max(b for spans in H.values() for _, b in spans)
    top, bottom = min(H), max(H)
    xs = sorted([left, right] + [p for p, spans in V.items() if left + min_gap < p < right - min_gap
                                 and _covered(spans, top, bottom, need=0.05)])
    ys = sorted(H)
    xs, ys = _dedupe(xs, min_gap), _dedupe(ys, min_gap)
    nr, nc = len(ys) - 1, len(xs) - 1
    if nr < 1 or nc < 2:
        return None

    # 칸 경계에 선이 있는가
    def vline(j: int, i: int) -> bool:  # 행 i에서 열 경계 xs[j]
        return _covered(_near(V, xs[j]), ys[i], ys[i + 1])

    def hline(i: int, j: int) -> bool:  # 열 j에서 행 경계 ys[i]
        return _covered(_near(H, ys[i]), xs[j], xs[j + 1])

    # 선이 없는 경계를 사이에 둔 칸끼리 합친다 (union-find)
    parent = list(range(nr * nc))

    def find(k: int) -> int:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i in range(nr):
        for j in range(nc):
            if j + 1 < nc and not vline(j + 1, i):
                parent[find(i * nc + j)] = find(i * nc + j + 1)
            if i + 1 < nr and not hline(i + 1, j):
                parent[find(i * nc + j)] = find((i + 1) * nc + j)
    groups: dict[int, list[tuple[int, int]]] = {}
    for i in range(nr):
        for j in range(nc):
            groups.setdefault(find(i * nc + j), []).append((i, j))

    cells = []
    for members in groups.values():
        r0, r1 = min(i for i, _ in members), max(i for i, _ in members)
        c0, c1 = min(j for _, j in members), max(j for _, j in members)
        if len(members) != (r1 - r0 + 1) * (c1 - c0 + 1):
            return None  # 사각형이 아닌 병합 = 선을 잘못 읽었거나 괘선 표가 아님
        cells.append(GridCell(r0, c0, r1, c1, (xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1]), {
            "top": all(hline(r0, j) for j in range(c0, c1 + 1)),
            "bottom": all(hline(r1 + 1, j) for j in range(c0, c1 + 1)),
            "left": c0 > 0 and all(vline(c0, i) for i in range(r0, r1 + 1)) or (
                c0 == 0 and all(_covered(_near(V, xs[0]), ys[i], ys[i + 1]) for i in range(r0, r1 + 1))),
            "right": c1 + 1 < nc and all(vline(c1 + 1, i) for i in range(r0, r1 + 1)) or (
                c1 + 1 == nc and all(_covered(_near(V, xs[-1]), ys[i], ys[i + 1]) for i in range(r0, r1 + 1))),
        }))
    cells.sort(key=lambda c: (c.r0, c.c0))
    return Grid(xs, ys, cells)


def _dedupe(vals: list[float], gap: float) -> list[float]:
    """간격이 gap보다 좁은 이웃 값은 하나로 (겹선, 굵은 선을 두 번 그린 경우)."""
    out: list[float] = []
    for v in sorted(vals):
        if out and v - out[-1] < gap:
            continue
        out.append(v)
    return out
