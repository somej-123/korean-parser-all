"""도형(Shape) → SVG. OOXML preset 도형 이름을 SVG 경로로 옮긴다."""

from __future__ import annotations

import re
import zlib
from html import escape

from ..ir import Drawing, Shape

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _safe_color(c: str | None) -> str | None:
    return c if c and _HEX.match(c) else None


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _poly(points) -> str:
    return "M" + " L".join(f"{_f(x)},{_f(y)}" for x, y in points) + " Z"


def preset_path(geo: str, x: float, y: float, w: float, h: float, adjust: dict | None = None) -> tuple[str, bool]:
    """(SVG path d, 근사 여부). adjust: preset 조정값 (100000 = 100%)."""
    r, b = x + w, y + h
    cx, cy = x + w / 2, y + h / 2
    if geo in ("rect", "flowChartProcess", "flowChartPredefinedProcess", "textBox"):
        return _poly([(x, y), (r, y), (r, b), (x, b)]), False
    if geo in ("roundRect", "flowChartAlternateProcess", "flowChartTerminator", "snipRoundRect"):
        # 둥근 모서리 반지름 = 짧은 변 × adj (기본 16667 = 1/6, 최대 50000 = 반)
        adj = min(max((adjust or {}).get("adj", 16667), 0), 50000) / 100000 if geo == "roundRect" else 0.1667
        rad = min(w, h) / 2 if geo == "flowChartTerminator" else min(w, h) * adj
        return (
            f"M{_f(x + rad)},{_f(y)} H{_f(r - rad)} A{_f(rad)},{_f(rad)} 0 0 1 {_f(r)},{_f(y + rad)} "
            f"V{_f(b - rad)} A{_f(rad)},{_f(rad)} 0 0 1 {_f(r - rad)},{_f(b)} H{_f(x + rad)} "
            f"A{_f(rad)},{_f(rad)} 0 0 1 {_f(x)},{_f(b - rad)} V{_f(y + rad)} "
            f"A{_f(rad)},{_f(rad)} 0 0 1 {_f(x + rad)},{_f(y)} Z"
        ), False
    if geo in ("ellipse", "flowChartConnector", "donut"):
        rx, ry = w / 2, h / 2
        return (
            f"M{_f(x)},{_f(cy)} A{_f(rx)},{_f(ry)} 0 1 0 {_f(r)},{_f(cy)} "
            f"A{_f(rx)},{_f(ry)} 0 1 0 {_f(x)},{_f(cy)} Z"
        ), False
    if geo in ("diamond", "flowChartDecision"):
        return _poly([(cx, y), (r, cy), (cx, b), (x, cy)]), False
    if geo in ("triangle", "flowChartExtract"):
        return _poly([(cx, y), (r, b), (x, b)]), False
    if geo == "flowChartMerge":
        return _poly([(x, y), (r, y), (cx, b)]), False
    if geo == "rtTriangle":
        return _poly([(x, y), (r, b), (x, b)]), False
    if geo in ("parallelogram", "flowChartInputOutput"):
        o = w * 0.25 if geo == "parallelogram" else w * 0.2
        return _poly([(x + o, y), (r, y), (r - o, b), (x, b)]), False
    if geo == "trapezoid":
        o = w * 0.25
        return _poly([(x + o, y), (r - o, y), (r, b), (x, b)]), False
    if geo == "flowChartManualOperation":
        o = w * 0.2
        return _poly([(x, y), (r, y), (r - o, b), (x + o, b)]), False
    if geo in ("hexagon", "flowChartPreparation"):
        o = w * 0.25
        return _poly([(x + o, y), (r - o, y), (r, cy), (r - o, b), (x + o, b), (x, cy)]), False
    if geo == "octagon":
        ox, oy = w * 0.29, h * 0.29
        return _poly([(x + ox, y), (r - ox, y), (r, y + oy), (r, b - oy), (r - ox, b), (x + ox, b),
                      (x, b - oy), (x, y + oy)]), False
    if geo == "pentagon" or geo == "homePlate":
        o = min(w, h) * 0.5
        return _poly([(x, y), (r - o, y), (r, cy), (r - o, b), (x, b)]), False
    if geo == "chevron":
        o = min(w, h) * 0.5
        return _poly([(x, y), (r - o, y), (r, cy), (r - o, b), (x, b), (x + o, cy)]), False
    if geo in ("rightArrow", "leftArrow", "upArrow", "downArrow", "leftRightArrow", "notchedRightArrow"):
        return _arrow(geo, x, y, w, h), False
    if geo == "flowChartManualInput":
        return _poly([(x, y + h * 0.2), (r, y), (r, b), (x, b)]), False
    if geo == "flowChartDocument":
        return (
            f"M{_f(x)},{_f(y)} H{_f(r)} V{_f(b - h * 0.17)} "
            f"C{_f(x + w * 0.75)},{_f(b - h * 0.35)} {_f(x + w * 0.4)},{_f(b + h * 0.12)} {_f(x)},{_f(b - h * 0.05)} Z"
        ), False
    if geo in ("can", "flowChartMagneticDisk"):
        ry = h * 0.1
        return (
            f"M{_f(x)},{_f(y + ry)} A{_f(w / 2)},{_f(ry)} 0 0 1 {_f(r)},{_f(y + ry)} V{_f(b - ry)} "
            f"A{_f(w / 2)},{_f(ry)} 0 0 1 {_f(x)},{_f(b - ry)} Z "
            f"M{_f(x)},{_f(y + ry)} A{_f(w / 2)},{_f(ry)} 0 0 0 {_f(r)},{_f(y + ry)}"
        ), False
    if geo == "plus" or geo == "mathPlus":
        o = min(w, h) * 0.25
        return _poly([(x + o, y), (r - o, y), (r - o, y + o), (r, y + o), (r, b - o), (r - o, b - o),
                      (r - o, b), (x + o, b), (x + o, b - o), (x, b - o), (x, y + o), (x + o, y + o)]), False
    # 모르는 도형은 사각형으로 근사한다
    return _poly([(x, y), (r, y), (r, b), (x, b)]), True


def _arrow(geo, x, y, w, h) -> str:
    r, b = x + w, y + h
    if geo in ("rightArrow", "leftArrow", "notchedRightArrow", "leftRightArrow"):
        head = min(w * 0.5, h)
        t0, t1 = y + h * 0.25, b - h * 0.25
        cy = y + h / 2
        if geo == "leftArrow":
            return _poly([(x, cy), (x + head, y), (x + head, t0), (r, t0), (r, t1), (x + head, t1), (x + head, b)])
        if geo == "leftRightArrow":
            head = min(w * 0.25, h)
            return _poly([(x, cy), (x + head, y), (x + head, t0), (r - head, t0), (r - head, y), (r, cy),
                          (r - head, b), (r - head, t1), (x + head, t1), (x + head, b)])
        return _poly([(x, t0), (r - head, t0), (r - head, y), (r, cy), (r - head, b), (r - head, t1), (x, t1)])
    head = min(h * 0.5, w)
    l0, l1 = x + w * 0.25, r - w * 0.25
    cx = x + w / 2
    if geo == "upArrow":
        return _poly([(cx, y), (r, y + head), (l1, y + head), (l1, b), (l0, b), (l0, y + head), (x, y + head)])
    return _poly([(l0, y), (l1, y), (l1, b - head), (r, b - head), (cx, b), (x, b - head), (l0, b - head)])


def line_path(s: Shape) -> str:
    x1, y1, x2, y2 = s.x, s.y, s.x + s.width, s.y + s.height
    if s.flip_h:
        x1, x2 = x2, x1
    if s.flip_v:
        y1, y2 = y2, y1
    geo = s.geometry
    if geo.startswith("bentConnector2"):
        return f"M{_f(x1)},{_f(y1)} L{_f(x2)},{_f(y1)} L{_f(x2)},{_f(y2)}"
    if geo.startswith("bentConnector"):
        xm = (x1 + x2) / 2
        return f"M{_f(x1)},{_f(y1)} L{_f(xm)},{_f(y1)} L{_f(xm)},{_f(y2)} L{_f(x2)},{_f(y2)}"
    if geo.startswith("curvedConnector"):
        xm = (x1 + x2) / 2
        return f"M{_f(x1)},{_f(y1)} C{_f(xm)},{_f(y1)} {_f(xm)},{_f(y2)} {_f(x2)},{_f(y2)}"
    return f"M{_f(x1)},{_f(y1)} L{_f(x2)},{_f(y2)}"


def render_drawing(drawing: Drawing, render_blocks, image_src) -> str:
    """Drawing → <svg>. render_blocks: 글상자 블록 → HTML, image_src: 이미지 id → URL."""
    w, h = max(drawing.width_pt, 1), max(drawing.height_pt, 1)
    # 캔버스 밖으로 나간 도형이 잘리지 않도록 실제 범위로 viewBox를 넓힌다.
    min_x = min([0.0] + [s.x for s in drawing.shapes])
    min_y = min([0.0] + [s.y for s in drawing.shapes])
    max_x = max([w] + [s.x + s.width for s in drawing.shapes])
    max_y = max([h] + [s.y + s.height for s in drawing.shapes])
    vw, vh = max_x - min_x, max_y - min_y

    markers: dict[str, str] = {}
    body: list[str] = []
    for i, s in enumerate(drawing.shapes):
        body.append(_render_shape(s, i, markers, render_blocks, image_src))

    label = escape(drawing.alt or "도식")
    return (
        f'<svg class="kp-svg" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{label}" '
        f'viewBox="{_f(min_x)} {_f(min_y)} {_f(vw)} {_f(vh)}" width="{_f(vw)}pt" height="{_f(vh)}pt">'
        f"<defs>{_marker_defs(markers)}</defs>{''.join(body)}</svg>"
    )


def render_slide(shapes, width: float, height: float, render_blocks, image_src, background: str | None = None,
                 background_src: str | None = None, label: str = "") -> str:
    """고정 쪽(PPTX 슬라이드) → <svg>. shapes: (Shape, 화면 강조용 블록 id 또는 None) 목록, 그리는 차례대로.

    도식과 달리 캔버스를 넓히지 않는다: 슬라이드 밖으로 나간 부분은 PowerPoint처럼 잘린다.
    """
    markers: dict[str, str] = {}
    body: list[str] = []
    if _safe_color(background):
        body.append(f'<rect x="0" y="0" width="{_f(width)}" height="{_f(height)}" fill="{background}"/>')
    if background_src:
        body.append(f'<image href="{escape(background_src)}" x="0" y="0" width="{_f(width)}" height="{_f(height)}" '
                    'preserveAspectRatio="none"/>')
    for i, (s, link) in enumerate(shapes):
        g = _render_shape(s, i, markers, render_blocks, image_src)
        body.append(f'<g data-kp-id="{escape(link)}">{g}</g>' if link else g)
    return (
        f'<svg class="kp-fixed-svg" xmlns="http://www.w3.org/2000/svg" role="document" aria-label="{escape(label)}" '
        f'viewBox="0 0 {_f(width)} {_f(height)}" width="{_f(width)}pt" height="{_f(height)}pt">'
        f"<defs>{_marker_defs(markers)}</defs>{''.join(body)}</svg>"
    )


def _marker_defs(markers: dict[str, str]) -> str:
    return "".join(
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
        f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>'
        for color, mid in markers.items()
    )


def _render_shape(s: Shape, index: int, markers: dict, render_blocks, image_src) -> str:
    cx, cy = s.x + s.width / 2, s.y + s.height / 2
    transforms = []
    if s.rotation:
        transforms.append(f"rotate({_f(s.rotation)} {_f(cx)} {_f(cy)})")
    if not s.is_line and (s.flip_h or s.flip_v):
        sx, sy = (-1 if s.flip_h else 1), (-1 if s.flip_v else 1)
        transforms.append(f"translate({_f(cx)} {_f(cy)}) scale({sx} {sy}) translate({_f(-cx)} {_f(-cy)})")
    tf = f' transform="{" ".join(transforms)}"' if transforms else ""

    dash = {"dashed": ' stroke-dasharray="4 3"', "dotted": ' stroke-dasharray="1 2"'}.get(s.stroke_dash, "")
    attrs = f' data-shape="{escape(s.geometry)}"' + (' data-approximate="true"' if s.approximate else "")
    if s.id:
        attrs += f' data-shape-id="{escape(s.id)}"'
    parts = [f"<g{tf}{attrs}>"]

    if s.is_line:
        color = _safe_color(s.stroke) or "#000000"
        d = s.path or line_path(s)
        # id를 색으로 정한다: 한 HTML에 도식·슬라이드가 여럿이어도 같은 id는 같은 색 화살표라 서로 섞이지 않는다
        mid = markers.setdefault(color, f"kp-arrow-{color[1:].lower()}")
        m = ""
        if s.tail_arrow:
            m += f' marker-end="url(#{mid})"'
        if s.head_arrow:
            m += f' marker-start="url(#{mid})"'
        parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{_f(max(s.stroke_width_pt, 0.75))}"{dash}{m}/>'
        )
    elif s.image_id and (s.path or s.geometry not in ("rect",)):
        # 그림으로 채운 도형: 도형 모양으로 잘라 그림을 넣고 테두리를 그린다
        d = s.path or preset_path(s.geometry, s.x, s.y, s.width, s.height, s.adjust)[0]
        src = image_src(s.image_id)
        # 같은 경로면 같은 id가 되어도 잘리는 모양이 같으므로 문제없다 (hash()는 실행마다 달라 쓰지 않는다)
        cid = f"kp-clip-{zlib.crc32(d.encode()):08x}"
        if _safe_color(s.fill):  # 그림 아래 바탕색
            parts.append(f'<path d="{d}" fill="{s.fill}" stroke="none"/>')
        if src:
            parts.append(f'<clipPath id="{cid}"><path d="{d}"/></clipPath>'
                         f'<image href="{escape(src)}" x="{_f(s.x)}" y="{_f(s.y)}" width="{_f(s.width)}" '
                         f'height="{_f(s.height)}" preserveAspectRatio="none" clip-path="url(#{cid})"/>')
        stroke = _safe_color(s.stroke) or "none"
        parts.append(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{_f(max(s.stroke_width_pt, 0.75))}"{dash}/>')
    elif s.image_id:
        src = image_src(s.image_id)
        if src:
            parts.append(
                f'<image href="{escape(src)}" x="{_f(s.x)}" y="{_f(s.y)}" width="{_f(s.width)}" '
                f'height="{_f(s.height)}" preserveAspectRatio="none"/>'
            )
    else:
        d = s.path
        approx = False
        if not d:
            d, approx = preset_path(s.geometry, s.x, s.y, s.width, s.height, s.adjust)
        if approx and not s.approximate:
            parts[0] = parts[0].replace(">", ' data-approximate="true">', 1)
        fill = _safe_color(s.fill) or "none"
        opacity = f' fill-opacity="{_f(s.fill_opacity)}"' if fill != "none" and s.fill_opacity is not None else ""
        stroke = _safe_color(s.stroke) or "none"
        # 0.75pt(1px)보다 얇은 선은 화면에서 흐려지므로 최소 1px로 그린다 (한컴·Word도 그렇게 표시)
        parts.append(f'<path d="{d}" fill="{fill}"{opacity} stroke="{stroke}" '
                     f'stroke-width="{_f(max(s.stroke_width_pt, 0.75))}"{dash}/>')
        if s.geometry == "flowChartPredefinedProcess":
            o = s.width * 0.1
            for xx in (s.x + o, s.x + s.width - o):
                parts.append(f'<path d="M{_f(xx)},{_f(s.y)} V{_f(s.y + s.height)}" stroke="{stroke}" '
                             f'stroke-width="{_f(s.stroke_width_pt)}"/>')

    if s.text_blocks:
        justify = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}[s.text_v_align]
        css = [f"justify-content:{justify}"]
        if s.text_insets_pt:
            css.append("padding:" + " ".join(f"{_f(v)}pt" for v in s.text_insets_pt))
        if s.text_style:
            from .html import text_style_css  # html.py가 이 모듈을 불러오므로 여기서 불러온다

            css.append(text_style_css(s.text_style))
        cls = "kp-shape-text" if s.text_wrap else "kp-shape-text kp-nowrap"
        # SVG 좌표 1단위 = 1pt 이고 화면에서는 1pt = 4/3px 로 늘어난다. 글상자 안 HTML의 pt 글꼴이
        # 다시 4/3배 커지지 않도록, 3/4로 줄인 좌표계에서 4/3배 큰 상자를 그린다.
        k = 4 / 3
        parts.append(
            f'<foreignObject x="{_f(s.x)}" y="{_f(s.y)}" width="{_f(max(s.width, 1) * k)}" '
            f'height="{_f(max(s.height, 1) * k)}" '
            f'transform="translate({_f(s.x)} {_f(s.y)}) scale(0.75) translate({_f(-s.x)} {_f(-s.y)})">'
            f'<div xmlns="http://www.w3.org/1999/xhtml" class="{cls}" style="{escape(";".join(c for c in css if c))}">'
            f"{render_blocks(s.text_blocks)}</div></foreignObject>"
        )
    parts.append("</g>")
    return "".join(parts)
