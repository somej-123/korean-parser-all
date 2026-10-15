"""차트 → SVG. 막대(세로/가로, 묶은/누적), 꺾은선, 영역, 원형/도넛을 그린다.

원본 차트 프로그램과 픽셀 단위로 같지는 않지만, 값·범주·계열·색이 원본과 같아서
화면에서 읽는 정보는 동일하다. 지원하지 않는 종류는 None을 돌려주고 데이터 표만 보여준다.
"""

from __future__ import annotations

import math
import re
from html import escape

from ..ir import Chart

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
# Office 기본 테마의 강조색
PALETTE = ["#4472C4", "#ED7D31", "#A5A5A5", "#FFC000", "#5B9BD5", "#70AD47", "#264478", "#9E480E"]
_TEXT = "#595959"
_GRID = "#D9D9D9"


def _c(color, i):
    return color if color and _HEX.match(color) else PALETTE[i % len(PALETTE)]


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _nice(lo: float, hi: float, ticks: int = 5) -> tuple[float, float, float]:
    if hi == lo:
        hi = lo + 1
    raw = (hi - lo) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 or v == int(v) else f"{v:g}"


def render_chart_svg(c: Chart) -> str | None:
    kind = c.chart_type
    if kind not in ("bar", "line", "area", "pie", "doughnut") or not c.series:
        return None
    w, h = c.width_pt or 360.0, c.height_pt or 216.0
    parts: list[str] = []
    top = 8.0
    if c.title:
        parts.append(f'<text x="{_f(w / 2)}" y="18" text-anchor="middle" font-size="12" fill="{_TEXT}">{escape(c.title)}</text>')
        top = 28.0

    if kind in ("pie", "doughnut"):
        body, legend_items = _pie(c, w, h, top)
    else:
        body, legend_items = _axes(c, w, h, top)
    parts.append(body)
    parts.append(_legend(legend_items, w, h))
    label = escape(c.title or "차트")
    return (
        f'<svg class="kp-svg" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{label}" '
        f'viewBox="0 0 {_f(w)} {_f(h)}" width="{_f(w)}pt" height="{_f(h)}pt" '
        f'font-family="inherit"><rect x="0.5" y="0.5" width="{_f(w - 1)}" height="{_f(h - 1)}" fill="#fff" '
        f'stroke="{_GRID}"/>{"".join(parts)}</svg>'
    )


def _legend(items, w, h) -> str:
    if not items:
        return ""
    widths = [14 + 5.5 * len(name) + 10 for name, _ in items]
    x = (w - sum(widths)) / 2
    y = h - 10
    out = []
    for (name, color), iw in zip(items, widths):
        out.append(f'<rect x="{_f(x)}" y="{_f(y - 6)}" width="6" height="6" fill="{color}"/>'
                   f'<text x="{_f(x + 9)}" y="{_f(y)}" font-size="8" fill="{_TEXT}">{escape(name)}</text>')
        x += iw
    return "".join(out)


def _axes(c: Chart, w, h, top):
    n = max([len(c.categories)] + [len(s.values) for s in c.series])
    cats = [c.categories[i] if i < len(c.categories) else str(i + 1) for i in range(n)]
    stacked = c.grouping in ("stacked", "percentStacked") and c.chart_type in ("bar", "area")
    percent = c.grouping == "percentStacked"
    horizontal = c.chart_type == "bar" and c.bar_dir == "bar"

    def val(s, i):
        return (s.values[i] if i < len(s.values) and s.values[i] is not None else 0.0)

    if stacked:
        sums = [sum(val(s, i) for s in c.series) for i in range(n)]
        hi = 100.0 if percent else max(sums + [0])
        lo = min([0.0] + [val(s, i) for s in c.series for i in range(n)])
    else:
        vals = [v for s in c.series for v in s.values if v is not None]
        hi, lo = max(vals + [0]), min(vals + [0])
    lo, hi, step = _nice(lo, hi)

    left, right, bottom = (60.0 if horizontal else 36.0), 12.0, 40.0
    x0, x1, y0, y1 = left, w - right, top + 6, h - bottom
    pw, ph = x1 - x0, y1 - y0
    out = []

    def vpos(v):  # 값 → 좌표
        t = (v - lo) / (hi - lo)
        return x0 + t * pw if horizontal else y1 - t * ph

    v = lo
    while v <= hi + step / 2:
        p = vpos(v)
        if horizontal:
            out.append(f'<line x1="{_f(p)}" y1="{_f(y0)}" x2="{_f(p)}" y2="{_f(y1)}" stroke="{_GRID}" stroke-width="0.5"/>'
                       f'<text x="{_f(p)}" y="{_f(y1 + 10)}" text-anchor="middle" font-size="7" fill="{_TEXT}">{_num(v)}</text>')
        else:
            out.append(f'<line x1="{_f(x0)}" y1="{_f(p)}" x2="{_f(x1)}" y2="{_f(p)}" stroke="{_GRID}" stroke-width="0.5"/>'
                       f'<text x="{_f(x0 - 4)}" y="{_f(p + 2.5)}" text-anchor="end" font-size="7" fill="{_TEXT}">{_num(v)}</text>')
        v += step

    band = (ph if horizontal else pw) / max(n, 1)
    for i, cat in enumerate(cats):
        if horizontal:  # Office는 가로 막대의 첫 범주를 아래에 그린다
            cy = y1 - band * (i + 0.5)
            out.append(f'<text x="{_f(x0 - 4)}" y="{_f(cy + 2.5)}" text-anchor="end" font-size="7" fill="{_TEXT}">{escape(cat)}</text>')
        else:
            cx = x0 + band * (i + 0.5)
            out.append(f'<text x="{_f(cx)}" y="{_f(y1 + 10)}" text-anchor="middle" font-size="7" fill="{_TEXT}">{escape(cat)}</text>')

    base = vpos(max(lo, 0.0))
    if c.chart_type == "bar":
        k = len(c.series)
        for i in range(n):
            acc_pos = acc_neg = 0.0
            for j, s in enumerate(c.series):
                v = val(s, i)
                if percent and sums[i]:
                    v = v / sums[i] * 100
                color = _c(s.color, j)
                if stacked:
                    start = acc_pos if v >= 0 else acc_neg
                    a, b = vpos(start), vpos(start + v)
                    if v >= 0:
                        acc_pos += v
                    else:
                        acc_neg += v
                    size = band * 0.6
                    off = band * 0.2
                else:
                    a, b = base, vpos(v)
                    size = band * 0.7 / k
                    off = band * 0.15 + size * j
                if horizontal:
                    if not stacked:  # Office는 가로 막대에서 첫 계열을 범주의 아래쪽에 그린다
                        off = band * 0.15 + size * (k - 1 - j)
                    yy = y1 - band * (i + 1) + off
                    out.append(f'<rect x="{_f(min(a, b))}" y="{_f(yy)}" width="{_f(abs(b - a))}" height="{_f(size)}" fill="{color}"/>')
                else:
                    xx = x0 + band * i + off
                    out.append(f'<rect x="{_f(xx)}" y="{_f(min(a, b))}" width="{_f(size)}" height="{_f(abs(b - a))}" fill="{color}"/>')
    else:  # line, area
        for j, s in enumerate(c.series):
            color = _c(s.color, j)
            pts = [(x0 + band * (i + 0.5), vpos(val(s, i))) for i in range(n)]
            path = " ".join(f"{_f(x)},{_f(y)}" for x, y in pts)
            if c.chart_type == "area":
                out.append(f'<polygon points="{_f(pts[0][0])},{_f(base)} {path} {_f(pts[-1][0])},{_f(base)}" '
                           f'fill="{color}" fill-opacity="0.75"/>')
            else:
                out.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
                out.extend(f'<circle cx="{_f(x)}" cy="{_f(y)}" r="2.2" fill="{color}"/>' for x, y in pts)

    axis = (f'<line x1="{_f(base)}" y1="{_f(y0)}" x2="{_f(base)}" y2="{_f(y1)}" stroke="#BFBFBF"/>' if horizontal
            else f'<line x1="{_f(x0)}" y1="{_f(base)}" x2="{_f(x1)}" y2="{_f(base)}" stroke="#BFBFBF"/>')
    out.append(axis)
    legend = [(s.name or f"계열 {j + 1}", _c(s.color, j)) for j, s in enumerate(c.series)]
    return "".join(out), legend


def _pie(c: Chart, w, h, top):
    s = c.series[0]
    vals = [max(v or 0.0, 0.0) for v in s.values]
    total = sum(vals) or 1.0
    cx, cy = w / 2, (top + h - 24) / 2
    r = max(min(w / 2 - 16, (h - 24 - top) / 2 - 4), 10)
    hole = r * 0.5 if c.chart_type == "doughnut" else 0
    out, legend = [], []
    angle = -math.pi / 2
    for i, v in enumerate(vals):
        color = _c(s.point_colors[i] if i < len(s.point_colors) else None, i)
        name = c.categories[i] if i < len(c.categories) else str(i + 1)
        legend.append((name, color))
        if v <= 0:
            continue
        sweep = v / total * 2 * math.pi
        a2 = angle + sweep
        large = 1 if sweep > math.pi else 0
        p1 = (cx + r * math.cos(angle), cy + r * math.sin(angle))
        p2 = (cx + r * math.cos(a2), cy + r * math.sin(a2))
        if sweep >= 2 * math.pi - 1e-6:
            out.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" fill="{color}"/>')
        else:
            out.append(f'<path d="M{_f(cx)},{_f(cy)} L{_f(p1[0])},{_f(p1[1])} A{_f(r)},{_f(r)} 0 {large} 1 '
                       f'{_f(p2[0])},{_f(p2[1])} Z" fill="{color}" stroke="#fff" stroke-width="1"/>')
        angle = a2
    if hole:
        out.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(hole)}" fill="#fff"/>')
    return "".join(out), legend
