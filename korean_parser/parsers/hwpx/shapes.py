"""HWPX 그리기 개체(사각형, 타원, 선, 다각형, 곡선, 호, 연결선, 그림, 묶음) → IR Shape.

HWP 개체는 '원래 크기(orgSz)' 좌표로 모양을 저장하고, renderingInfo의 행렬로 현재 위치·크기·회전을 만든다.

    renderingInfo = [이동 T, 확대 S1, 회전 R1, 확대 S2, 회전 R2, ...]   (묶음 단계마다 S, R 한 쌍)
    화면 좌표 = T · S1 · R1 · S2 · R2 · ... · 원래 좌표

묶음(container) 안 개체의 행렬은 이미 묶음 좌표계 기준이므로, 모든 점을 이 행렬로 변환하면
최상위 개체 기준 좌표가 나온다. 회전·뒤집기가 행렬에 들어 있어 별도로 처리할 필요가 없다.
"""

from __future__ import annotations

import math

from ...ir import Shape
from .header import color, fill_color, hwp_pt, kid, kids, ln, shape_comment

Matrix = tuple[float, float, float, float, float, float]  # x' = a x + b y + c, y' = d x + e y + f
IDENTITY: Matrix = (1, 0, 0, 0, 1, 0)

DRAWING_TAGS = {"rect", "ellipse", "arc", "line", "polygon", "curve", "connectLine", "container", "pic", "textart"}

_ARROW_STYLES = {"ARROW", "SPEAR", "CONCAVE_ARROW"}
_DASH = {"DASH": "dashed", "DASH_DOT": "dashed", "DASH_DOT_DOT": "dashed", "LONG_DASH": "dashed",
         "DOT": "dotted", "CIRCLE": "dotted"}


def _matrix(el) -> Matrix:
    def f(k, default):
        try:
            return float(el.get(k, default))
        except (TypeError, ValueError):
            return float(default)

    return (f("e1", 1), f("e2", 0), f("e3", 0), f("e4", 0), f("e5", 1), f("e6", 0))


def _mul(m: Matrix, n: Matrix) -> Matrix:
    """m · n (n을 먼저 적용)."""
    a, b, c, d, e, f = m
    p, q, r, s, t, u = n
    return (a * p + b * s, a * q + b * t, a * r + b * u + c,
            d * p + e * s, d * q + e * t, d * r + e * u + f)


def object_matrix(el) -> Matrix:
    info = kid(el, "renderingInfo")
    if info is None:
        return IDENTITY
    m = IDENTITY
    for part in info:
        if ln(part) in ("transMatrix", "scaMatrix", "rotMatrix"):
            m = _mul(m, _matrix(part))
    return m


def _pt(m: Matrix, el) -> tuple[float, float] | None:
    """점 요소(x, y 속성)를 행렬로 변환하고 pt로 바꾼다."""
    if el is None:
        return None
    x, y = hwp_pt(el.get("x", "0")) * 100, hwp_pt(el.get("y", "0")) * 100  # 부호 처리만 하고 HWPUNIT 유지
    a, b, c, d, e, f = m
    return ((a * x + b * y + c) / 100, (d * x + e * y + f) / 100)


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _bbox(points) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def _line_style(el) -> dict:
    ls = kid(el, "lineShape")
    if ls is None:
        return {"stroke": "#000000"}
    style = ls.get("style", "SOLID")
    out = {
        "stroke": None if style == "NONE" else (color(ls.get("color")) or "#000000"),
        "stroke_width_pt": max(hwp_pt(ls.get("width", "33")), 0.25),
        "stroke_dash": _DASH.get(style, "solid"),
        "head_arrow": ls.get("headStyle", "NORMAL") in _ARROW_STYLES,
        "tail_arrow": ls.get("tailStyle", "NORMAL") in _ARROW_STYLES,
    }
    return out


def _fill(el) -> str | None:
    return fill_color(kid(el, "fillBrush"))


def _side(pt_el) -> int | None:
    try:
        return int(pt_el.get("subjectIdx")) if pt_el is not None and pt_el.get("subjectIDRef") not in (None, "0") else None
    except (TypeError, ValueError):
        return None


def _connector_path(a, b, side_a, side_b, curved: bool) -> str:
    horiz = {1, 3}
    a_h = side_a in horiz if side_a is not None else abs(b[0] - a[0]) >= abs(b[1] - a[1])
    b_h = side_b in horiz if side_b is not None else a_h
    if curved:
        dx, dy = (b[0] - a[0]) / 2, (b[1] - a[1]) / 2
        c1 = (a[0] + dx, a[1]) if a_h else (a[0], a[1] + dy)
        c2 = (b[0] - dx, b[1]) if b_h else (b[0], b[1] - dy)
        return f"M{_f(a[0])},{_f(a[1])} C{_f(c1[0])},{_f(c1[1])} {_f(c2[0])},{_f(c2[1])} {_f(b[0])},{_f(b[1])}"
    if a_h and not b_h:
        pts = [a, (b[0], a[1]), b]
    elif not a_h and b_h:
        pts = [a, (a[0], b[1]), b]
    elif a_h:
        mx = (a[0] + b[0]) / 2
        pts = [a, (mx, a[1]), (mx, b[1]), b]
    else:
        my = (a[1] + b[1]) / 2
        pts = [a, (a[0], my), (b[0], my), b]
    return "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in pts)


def _image_fill(el, image_id) -> str | None:
    """hc:fillBrush/hc:imgBrush 그림 채우기."""
    brush = kid(el, "fillBrush")
    img = kid(kid(brush, "imgBrush"), "img")
    return image_id(img.get("binaryItemIDRef")) if img is not None else None


class ShapeReader:
    """read_text_blocks: hp:subList → 블록 목록, image_id: binaryItemIDRef → 문서 이미지 id"""

    def __init__(self, read_text_blocks, image_id):
        self.read_text_blocks = read_text_blocks
        self.image_id = image_id

    def read(self, el) -> list[Shape]:
        name = ln(el)
        if name == "container":
            out: list[Shape] = []
            for c in el:
                if ln(c) in DRAWING_TAGS:
                    out.extend(self.read(c))
            return out
        m = object_matrix(el)
        reader = getattr(self, f"_{name}", None)
        if reader is None:
            return []
        shape = reader(el, m)
        if shape is None:
            return []
        if name in ("rect", "ellipse", "polygon", "curve") and shape.image_id is None:
            shape.image_id = _image_fill(el, self.image_id)
        shape.id = el.get("instid") or el.get("id")
        shape.name = shape_comment(el)
        draw = kid(el, "drawText")
        if draw is not None:
            sub = kid(draw, "subList")
            shape.text_blocks = self.read_text_blocks(sub) if sub is not None else []
            va = sub.get("vertAlign", "CENTER") if sub is not None else "CENTER"
            shape.text_v_align = {"TOP": "top", "BOTTOM": "bottom"}.get(va, "middle")
        return [shape]

    # ------------------------------------------------------------------

    def _rect(self, el, m) -> Shape:
        pts = [_pt(m, kid(el, f"pt{i}")) for i in range(4)]
        if None in pts:
            sz = kid(el, "sz")
            w, h = hwp_pt(sz.get("width")) if sz is not None else 0, hwp_pt(sz.get("height")) if sz is not None else 0
            pts = [(0, 0), (w, 0), (w, h), (0, h)]
        x, y, w, h = _bbox(pts)
        axis_aligned = abs(pts[0][1] - pts[1][1]) < 0.5 and abs(pts[0][0] - pts[3][0]) < 0.5
        ratio = int(el.get("ratio", "0") or 0)
        common = dict(x=x, y=y, width=w, height=h, fill=_fill(el), **_line_style(el))
        common["head_arrow"] = common["tail_arrow"] = False
        if axis_aligned:
            return Shape(geometry="roundRect" if ratio else "rect", **common)
        path = "M" + " L".join(f"{_f(px)},{_f(py)}" for px, py in pts) + " Z"
        return Shape(geometry="custom", path=path, **common)

    def _ellipse(self, el, m) -> Shape:
        c, a1, a2 = _pt(m, kid(el, "center")), _pt(m, kid(el, "ax1")), _pt(m, kid(el, "ax2"))
        if None in (c, a1, a2):
            return self._rect(el, m).model_copy(update={"geometry": "ellipse"})
        rx, ry = math.dist(c, a1), math.dist(c, a2)
        angle = math.degrees(math.atan2(a1[1] - c[1], a1[0] - c[0]))
        style = _line_style(el)
        style["head_arrow"] = style["tail_arrow"] = False
        base = dict(x=c[0] - rx, y=c[1] - ry, width=2 * rx, height=2 * ry, fill=_fill(el), **style)
        if abs(angle) < 0.5:
            return Shape(geometry="ellipse", **base)
        p1 = (c[0] - (a1[0] - c[0]), c[1] - (a1[1] - c[1]))
        path = (f"M{_f(a1[0])},{_f(a1[1])} A{_f(rx)},{_f(ry)} {_f(angle)} 1 1 {_f(p1[0])},{_f(p1[1])} "
                f"A{_f(rx)},{_f(ry)} {_f(angle)} 1 1 {_f(a1[0])},{_f(a1[1])} Z")
        return Shape(geometry="custom", path=path, **base)

    def _arc(self, el, m) -> Shape:
        c, a1, a2 = _pt(m, kid(el, "center")), _pt(m, kid(el, "ax1")), _pt(m, kid(el, "ax2"))
        style = _line_style(el)
        if None in (c, a1, a2):
            return Shape(geometry="custom", path="", **style)
        rx, ry = math.dist(c, a1), math.dist(c, a2)
        kind = el.get("type", "NORMAL")
        path = f"M{_f(a1[0])},{_f(a1[1])} A{_f(rx)},{_f(ry)} 0 0 1 {_f(a2[0])},{_f(a2[1])}"
        if kind == "PIE":
            path += f" L{_f(c[0])},{_f(c[1])} Z"
        elif kind == "CHORD":
            path += " Z"
        x, y, w, h = _bbox([c, a1, a2])
        return Shape(geometry="custom", path=path, x=x, y=y, width=w, height=h,
                     fill=_fill(el) if kind != "NORMAL" else None, **style)

    def _polygon(self, el, m) -> Shape | None:
        pts = [_pt(m, p) for p in kids(el, "pt")]
        if len(pts) < 2:
            return None
        x, y, w, h = _bbox(pts)
        path = "M" + " L".join(f"{_f(px)},{_f(py)}" for px, py in pts) + " Z"
        style = _line_style(el)
        style["head_arrow"] = style["tail_arrow"] = False
        return Shape(geometry="custom", path=path, x=x, y=y, width=w, height=h, fill=_fill(el), **style)

    def _curve(self, el, m) -> Shape | None:
        segs = kids(el, "seg")
        pts = []
        for s in segs:
            p1 = _pt(m, _XY(s.get("x1"), s.get("y1")))
            if not pts:
                pts.append(p1)
            pts.append(_pt(m, _XY(s.get("x2"), s.get("y2"))))
        if len(pts) < 2:
            return None
        closed = math.dist(pts[0], pts[-1]) < 0.5
        path = _catmull_rom(pts[:-1] if closed else pts, closed)
        x, y, w, h = _bbox(pts)
        return Shape(geometry="custom", path=path, x=x, y=y, width=w, height=h,
                     fill=_fill(el) if closed else None, **_line_style(el))

    def _line(self, el, m) -> Shape | None:
        a = _pt(m, kid(el, "startPt"))
        b = _pt(m, kid(el, "endPt"))
        if a is None or b is None:
            return None
        return _line_shape("line", a, b, _line_style(el))

    def _connectLine(self, el, m) -> Shape | None:  # noqa: N802 - HWPX 요소 이름
        st, en = kid(el, "startPt"), kid(el, "endPt")
        a, b = _pt(m, st), _pt(m, en)
        if a is None or b is None:
            return None
        kind = el.get("type", "STRAIGHT_NOARROW")
        geo = "bentConnector3" if kind.startswith("STROKE") else "curvedConnector3" if kind.startswith("ARC") else "straightConnector1"
        style = _line_style(el)
        # 꺾인/곡선 연결선은 양 끝이 도형의 어느 변(0 위, 1 오른쪽, 2 아래, 3 왼쪽)에 붙었는지에 따라 경로가 정해진다
        path = None
        if not kind.startswith("STRAIGHT"):
            path = _connector_path(a, b, _side(st), _side(en), curved=kind.startswith("ARC"))
        if kind.endswith("ONEWAY"):
            style["tail_arrow"] = True
        elif kind.endswith("BOTH"):
            style["head_arrow"] = style["tail_arrow"] = True
        shape = _line_shape(geo, a, b, style)
        shape.path = path
        shape.connect_from = st.get("subjectIDRef") if st.get("subjectIDRef") not in (None, "0") else None
        shape.connect_to = en.get("subjectIDRef") if en.get("subjectIDRef") not in (None, "0") else None
        return shape

    def _pic(self, el, m) -> Shape | None:
        img = next((c for c in el.iter() if ln(c) == "img"), None)
        image_id = self.image_id(img.get("binaryItemIDRef")) if img is not None else None
        rect = kid(el, "imgRect")
        pts = [_pt(m, kid(rect, f"pt{i}")) for i in range(4)] if rect is not None else []
        if not pts or None in pts:
            sz = kid(el, "sz")
            w, h = hwp_pt(sz.get("width")), hwp_pt(sz.get("height"))
            pts = [(0, 0), (w, 0), (w, h), (0, h)]
        x, y, w, h = _bbox(pts)
        return Shape(geometry="rect", x=x, y=y, width=w, height=h, image_id=image_id, stroke=None)

    def _textart(self, el, m) -> Shape | None:
        from ...ir import Paragraph, ParagraphStyle, Text

        shape = self._rect(el, m)
        text = kid(el, "text")
        if text is not None and text.text:
            shape.text_blocks = [Paragraph(inlines=[Text(text=text.text)], style=ParagraphStyle(align="center"))]
        shape.stroke = None
        return shape


class _XY:
    """seg의 x1/y1 같은 속성을 점 요소처럼 다루기 위한 작은 객체."""

    def __init__(self, x, y):
        self._d = {"x": x or "0", "y": y or "0"}

    def get(self, k, default=None):
        return self._d.get(k, default)


def _line_shape(geometry: str, a, b, style: dict) -> Shape:
    x, y = min(a[0], b[0]), min(a[1], b[1])
    return Shape(geometry=geometry, x=x, y=y, width=abs(b[0] - a[0]), height=abs(b[1] - a[1]),
                 flip_h=a[0] > b[0], flip_v=a[1] > b[1], **style)


def _catmull_rom(pts, closed: bool) -> str:
    """점들을 지나는 부드러운 곡선 (Catmull-Rom → 3차 베지어)."""
    n = len(pts)
    if n < 2:
        return ""
    out = [f"M{_f(pts[0][0])},{_f(pts[0][1])}"]
    count = n if closed else n - 1
    for i in range(count):
        p0 = pts[(i - 1) % n] if (closed or i > 0) else pts[i]
        p1, p2 = pts[i], pts[(i + 1) % n]
        p3 = pts[(i + 2) % n] if (closed or i + 2 < n) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        out.append(f"C{_f(c1[0])},{_f(c1[1])} {_f(c2[0])},{_f(c2[1])} {_f(p2[0])},{_f(p2[1])}")
    if closed:
        out.append("Z")
    return " ".join(out)
