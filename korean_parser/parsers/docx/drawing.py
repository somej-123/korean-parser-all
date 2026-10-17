"""DrawingML 도형·그룹·차트 해석.

Word의 도형은 좌표, 크기, 모양(preset), 색, 선, 화살표, 연결 정보가 모두 XML에 있다.
이를 IR의 Shape로 옮기면 HTML에서 SVG로 원본과 같은 모양을 그릴 수 있다.
"""

from __future__ import annotations

import colorsys

from ...ir import Chart, ChartSeries, Shape
from .common import NS, emu_to_pt, hex_color, q


# ---------------------------------------------------------------------------
# 테마 색
# ---------------------------------------------------------------------------


class Theme:
    _ALIASES = {"tx1": "dk1", "bg1": "lt1", "tx2": "dk2", "bg2": "lt2"}

    def __init__(self, root=None):
        self.colors: dict[str, str] = {}
        if root is None:
            return
        scheme = root.find(f".//{q('a:clrScheme')}")
        if scheme is None:
            return
        for el in scheme:
            name = el.tag.split("}")[1]
            srgb = el.find(q("a:srgbClr"))
            sys_ = el.find(q("a:sysClr"))
            if srgb is not None:
                self.colors[name] = "#" + srgb.get("val", "000000").upper()
            elif sys_ is not None:
                self.colors[name] = "#" + sys_.get("lastClr", "000000").upper()

    def resolve(self, name: str) -> str | None:
        return self.colors.get(self._ALIASES.get(name, name))


def _apply_mods(color: str, el) -> str:
    """lumMod, lumOff, shade, tint 색 변환."""
    r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    for mod in el:
        name = mod.tag.split("}")[1]
        try:
            val = int(mod.get("val", "100000")) / 100000
        except ValueError:
            continue
        if name == "lumMod":
            l *= val
        elif name == "lumOff":
            l += val
        elif name == "shade":
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            r, g, b = r * val, g * val, b * val
            h, l, s = colorsys.rgb_to_hls(r, g, b)
        elif name == "tint":
            r, g, b = colorsys.hls_to_rgb(h, l, s)
            r, g, b = r + (1 - r) * (1 - val), g + (1 - g) * (1 - val), b + (1 - b) * (1 - val)
            h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(max(l, 0), 1)
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02X}{:02X}{:02X}".format(*(round(c * 255) for c in (r, g, b)))


def read_color(parent, theme: Theme) -> str | None:
    """solidFill 등 색 요소의 부모를 받아 색을 계산한다."""
    if parent is None:
        return None
    for el in parent:
        name = el.tag.split("}")[1]
        color = None
        if name == "srgbClr":
            color = hex_color(el.get("val"))
        elif name == "schemeClr":
            color = theme.resolve(el.get("val", ""))
        elif name == "sysClr":
            color = hex_color(el.get("lastClr"))
        elif name == "prstClr":
            color = {"black": "#000000", "white": "#FFFFFF", "red": "#FF0000", "blue": "#0000FF",
                     "green": "#008000", "yellow": "#FFFF00", "gray": "#808080"}.get(el.get("val"))
        if color:
            return _apply_mods(color, el)
    return None


# ---------------------------------------------------------------------------
# 도형
# ---------------------------------------------------------------------------

_DASH = {"dash": "dashed", "lgDash": "dashed", "sysDash": "dashed", "dashDot": "dashed",
         "lgDashDot": "dashed", "sysDot": "dotted", "dot": "dotted"}


class ShapeReader:
    """wps:wsp / pic:pic / wpg:wgp 요소를 Shape 목록으로 바꾼다.

    read_text_blocks: 글상자 안의 w:txbxContent를 블록 목록으로 바꾸는 콜백 (본문 파서가 제공)
    read_image: r:embed id → 문서 이미지 id 로 바꾸는 콜백
    """

    def __init__(self, theme: Theme, read_text_blocks, read_image):
        self.theme = theme
        self.read_text_blocks = read_text_blocks
        self.read_image = read_image

    def read(self, el, ox: float = 0, oy: float = 0, sx: float = 1, sy: float = 1) -> list[Shape]:
        """el 아래의 도형들을 캔버스 좌표로 읽는다. (ox, oy, sx, sy)는 그룹 좌표 변환."""
        tag = el.tag.split("}")[1]
        if tag in ("wgp", "grpSp", "wpc"):
            return self._read_group(el, ox, oy, sx, sy)
        if tag == "wsp":
            shape = self._read_wsp(el, ox, oy, sx, sy)
            return [shape] if shape else []
        if tag == "pic":
            shape = self._read_pic(el, ox, oy, sx, sy)
            return [shape] if shape else []
        return []

    def _read_group(self, el, ox, oy, sx, sy) -> list[Shape]:
        xfrm = el.find(f"{q('wpg:grpSpPr')}/{q('a:xfrm')}")
        if xfrm is None:
            xfrm = el.find(f"{q('a:grpSpPr')}/{q('a:xfrm')}")
        if xfrm is not None:
            off, ext = xfrm.find(q("a:off")), xfrm.find(q("a:ext"))
            ch_off, ch_ext = xfrm.find(q("a:chOff")), xfrm.find(q("a:chExt"))
            if None not in (off, ext, ch_off, ch_ext):
                gx, gy = _emu(off.get("x")), _emu(off.get("y"))
                gw, gh = _emu(ext.get("cx")), _emu(ext.get("cy"))
                cx, cy = _emu(ch_off.get("x")), _emu(ch_off.get("y"))
                cw, ch = _emu(ch_ext.get("cx")), _emu(ch_ext.get("cy"))
                ksx = gw / cw if cw else 1
                ksy = gh / ch if ch else 1
                # 자식 좌표 c → 부모 좌표: g + (c - chOff) * k
                ox, oy = ox + sx * (gx - cx * ksx), oy + sy * (gy - cy * ksy)
                sx, sy = sx * ksx, sy * ksy
        shapes: list[Shape] = []
        for kid in el:
            shapes.extend(self.read(kid, ox, oy, sx, sy))
        return shapes

    def _geometry(self, sppr, x, y, w, h, sx, sy) -> tuple[str, str | None, bool]:
        prst = sppr.find(q("a:prstGeom"))
        if prst is not None:
            return prst.get("prst", "rect"), None, False
        cust = sppr.find(q("a:custGeom"))
        if cust is not None:
            return "custom", custom_path(cust, x, y, w, h), False
        return "rect", None, False

    def _xfrm(self, sppr, ox, oy, sx, sy):
        xfrm = sppr.find(q("a:xfrm")) if sppr is not None else None
        if xfrm is None:
            return None
        off, ext = xfrm.find(q("a:off")), xfrm.find(q("a:ext"))
        if off is None or ext is None:
            return None
        x = ox + sx * _emu(off.get("x"))
        y = oy + sy * _emu(off.get("y"))
        w = sx * _emu(ext.get("cx"))
        h = sy * _emu(ext.get("cy"))
        rot = int(xfrm.get("rot", "0")) / 60000
        return x, y, w, h, rot, xfrm.get("flipH") == "1", xfrm.get("flipV") == "1"

    def _read_wsp(self, el, ox, oy, sx, sy) -> Shape | None:
        sppr = el.find(q("wps:spPr"))
        geo = self._xfrm(sppr, ox, oy, sx, sy)
        if geo is None:
            return None
        x, y, w, h, rot, fh, fv = geo
        geometry, path, approx = self._geometry(sppr, x, y, w, h, sx, sy)
        fill, stroke, width, dash, head, tail = read_fill_line(sppr, el.find(q("wps:style")), geometry, self.theme)

        nv = el.find(q("wps:cNvPr"))
        cxn = el.find(q("wps:cNvCnPr"))
        conn_from = conn_to = None
        if cxn is not None:
            st, en = cxn.find(q("a:stCxn")), cxn.find(q("a:endCxn"))
            conn_from = st.get("id") if st is not None else None
            conn_to = en.get("id") if en is not None else None

        text_blocks = []
        txbx = el.find(f"{q('wps:txbx')}/{q('w:txbxContent')}")
        if txbx is not None:
            text_blocks = self.read_text_blocks(txbx)
        body = el.find(q("wps:bodyPr"))
        anchor = {"t": "top", "ctr": "middle", "b": "bottom"}.get(body.get("anchor", "t") if body is not None else "t", "top")

        return Shape(
            id=nv.get("id") if nv is not None else None,
            name=nv.get("name") if nv is not None else None,
            geometry=geometry, path=path,
            x=x, y=y, width=w, height=h, rotation=rot, flip_h=fh, flip_v=fv,
            fill=fill, stroke=stroke, stroke_width_pt=width, stroke_dash=dash,
            head_arrow=head, tail_arrow=tail, connect_from=conn_from, connect_to=conn_to,
            text_blocks=text_blocks, text_v_align=anchor, approximate=approx,
        )

    def _read_pic(self, el, ox, oy, sx, sy) -> Shape | None:
        sppr = el.find(q("pic:spPr"))
        geo = self._xfrm(sppr, ox, oy, sx, sy)
        blip = el.find(f".//{q('a:blip')}")
        if geo is None or blip is None:
            return None
        x, y, w, h, rot, fh, fv = geo
        image_id = self.read_image(blip.get(f"{{{NS['r']}}}embed"))
        return Shape(geometry="rect", x=x, y=y, width=w, height=h, rotation=rot,
                     flip_h=fh, flip_v=fv, image_id=image_id)


def read_fill_line(sppr, style, geometry: str, theme: Theme):
    """spPr(+ 도형 스타일 참조)의 채우기와 선: (채우기, 선 색, 선 굵기 pt, 선 모양, 시작 화살표, 끝 화살표).

    DOCX(wps:spPr)와 PPTX(p:spPr)가 같은 DrawingML이라 함께 쓴다. 스타일 참조(fillRef, lnRef)는 직접 값이 없을 때만.
    """
    fill = None
    if sppr.find(q("a:noFill")) is None:
        fill = read_color(sppr.find(q("a:solidFill")), theme)
        if fill is None and sppr.find(q("a:gradFill")) is not None:
            # 그라데이션은 첫 색으로 근사
            gs = sppr.find(f".//{q('a:gs')}")
            fill = read_color(gs, theme)
        if fill is None and style is not None and geometry not in ("line", "straightConnector1") and "Connector" not in geometry:
            fill = read_color(style.find(q("a:fillRef")), theme)

    ln = sppr.find(q("a:ln"))
    stroke, width, dash, head, tail = None, 0.75, "solid", False, False
    no_line = ln is not None and ln.find(q("a:noFill")) is not None
    if not no_line:
        stroke = read_color(ln.find(q("a:solidFill")), theme) if ln is not None else None
        if stroke is None and style is not None:
            stroke = read_color(style.find(q("a:lnRef")), theme)
        if ln is not None:
            if ln.get("w"):
                width = max(_emu(ln.get("w")), 0.25)
            pd = ln.find(q("a:prstDash"))
            if pd is not None:
                dash = _DASH.get(pd.get("val", ""), "solid")
            he, te = ln.find(q("a:headEnd")), ln.find(q("a:tailEnd"))
            head = he is not None and he.get("type", "none") != "none"
            tail = te is not None and te.get("type", "none") != "none"
    return fill, stroke, width, dash, head, tail


def _emu(v) -> float:
    return emu_to_pt(v) or 0.0


def custom_path(cust, x, y, w, h) -> str | None:
    """a:custGeom → SVG path. 경로 좌표계(w, h)를 도형 상자에 맞춰 늘린다."""
    parts = []
    for path in cust.findall(f"{q('a:pathLst')}/{q('a:path')}"):
        pw = float(path.get("w", "0") or 0) or None
        ph = float(path.get("h", "0") or 0) or None

        def pt(el):
            px, py = float(el.get("x", "0")), float(el.get("y", "0"))
            return (x + (px / pw * w if pw else 0), y + (py / ph * h if ph else 0))

        for cmd in path:
            name = cmd.tag.split("}")[1]
            pts = [pt(p) for p in cmd.findall(q("a:pt"))]
            if name == "moveTo" and pts:
                parts.append("M{:.2f},{:.2f}".format(*pts[0]))
            elif name == "lnTo" and pts:
                parts.append("L{:.2f},{:.2f}".format(*pts[0]))
            elif name == "cubicBezTo" and len(pts) == 3:
                parts.append("C" + " ".join("{:.2f},{:.2f}".format(*p) for p in pts))
            elif name == "quadBezTo" and len(pts) == 2:
                parts.append("Q" + " ".join("{:.2f},{:.2f}".format(*p) for p in pts))
            elif name == "close":
                parts.append("Z")
    return " ".join(parts) or None


# ---------------------------------------------------------------------------
# 차트
# ---------------------------------------------------------------------------


CX = "{http://schemas.microsoft.com/office/drawing/2014/chartex}"


def chart_ref(graphic_data):
    """graphicData 안의 차트 참조 요소 (c:chart 또는 신형 cx:chart). 없으면 None."""
    return next((el for el in graphic_data.iter() if isinstance(el.tag, str) and el.tag.split("}")[-1] == "chart"
                 and any(k.endswith("}id") for k in el.attrib)), None)


def read_chart(root, width_pt, height_pt, theme: Theme | None = None) -> Chart | None:
    """chartN.xml에서 차트 종류, 제목, 범주, 계열 값과 색을 읽는다. 신형 차트(cx:chartSpace)는 read_chartex."""
    if root is not None and root.tag == f"{CX}chartSpace":
        return read_chartex(root, width_pt, height_pt)
    theme = theme or Theme()
    plot = root.find(f".//{q('c:plotArea')}")
    if plot is None:
        return None
    kind_el = next((el for el in plot if isinstance(el.tag, str) and el.tag.endswith("Chart")), None)
    if kind_el is None:
        return None
    chart_type = kind_el.tag.split("}")[1].replace("Chart", "").replace("3D", "")

    title = None
    t = root.find(f"{q('c:chart')}/{q('c:title')}")
    if t is not None:
        # 제목 요소만 있고 글이 없으면 Office는 기본 제목을 표시한다
        title = "".join(x.text or "" for x in t.iter(q("a:t"))) or "차트 제목"
    bar_dir = kind_el.find(q("c:barDir"))
    grouping = kind_el.find(q("c:grouping"))

    categories: list[str] = []
    series: list[ChartSeries] = []
    for ser in kind_el.findall(q("c:ser")):
        name_el = ser.find(f"{q('c:tx')}//{q('c:v')}")
        if not categories:
            cat = ser.find(q("c:cat"))
            if cat is not None:
                pts = sorted(cat.iter(q("c:pt")), key=lambda p: int(p.get("idx", "0")))
                categories = [(p.find(q("c:v")).text or "") if p.find(q("c:v")) is not None else "" for p in pts]
        values: list[float | None] = []
        val = ser.find(q("c:val")) if ser.find(q("c:val")) is not None else ser.find(q("c:yVal"))
        if val is not None:
            count = val.find(f".//{q('c:ptCount')}")
            n = int(count.get("val", "0")) if count is not None else 0
            values = [None] * n
            for p in val.iter(q("c:pt")):
                idx = int(p.get("idx", "0"))
                v = p.find(q("c:v"))
                while len(values) <= idx:
                    values.append(None)
                try:
                    values[idx] = float(v.text) if v is not None else None
                except (TypeError, ValueError):
                    values[idx] = None
        sppr = ser.find(q("c:spPr"))
        color = None
        if sppr is not None:
            color = read_color(sppr.find(q("a:solidFill")), theme)
            if color is None:
                ln = sppr.find(q("a:ln"))
                color = read_color(ln.find(q("a:solidFill")), theme) if ln is not None else None
        point_colors: list[str | None] = []
        for dpt in ser.findall(q("c:dPt")):
            idx = int(dpt.find(q("c:idx")).get("val", "0")) if dpt.find(q("c:idx")) is not None else len(point_colors)
            fill = dpt.find(f"{q('c:spPr')}/{q('a:solidFill')}")
            while len(point_colors) <= idx:
                point_colors.append(None)
            point_colors[idx] = read_color(fill, theme)
        series.append(ChartSeries(name=name_el.text if name_el is not None else None, values=values,
                                  color=color, point_colors=point_colors))
    return Chart(chart_type=chart_type, title=title, categories=categories, series=series,
                 bar_dir=bar_dir.get("val") if bar_dir is not None else None,
                 grouping=grouping.get("val") if grouping is not None else None,
                 width_pt=width_pt, height_pt=height_pt)


def read_chartex(root, width_pt, height_pt) -> Chart | None:
    """Office 2016 신형 차트(cx:chartSpace: 폭포, 상자 수염, 선버스트, 트리맵, 깔때기, 히스토그램 …)의 데이터.

    cx:chartData의 데이터 묶음(cx:data id)마다 분류 글(cx:strDim type=cat, 여러 단계면 ' / '로 이음)과 값(cx:numDim)이
    있고, cx:series가 이름(cx:tx)·데이터 번호(cx:dataId)·종류(layoutId)를 가리킨다. 종류는 layoutId 그대로 둔다
    (화면에는 데이터 표로 보인다). 색과 통계 설정(사분위 계산 방법 등)은 읽지 않는다. Apache POI chartex.docx로 확인.
    """
    data: dict[str, tuple[list[str], list[float | None]]] = {}
    for d in root.iter(f"{CX}data"):
        cats: list[str] = []
        str_dim = next((s for s in d.findall(f"{CX}strDim") if s.get("type") == "cat"), None)
        if str_dim is not None:
            levels = [{int(p.get("idx", "0")): (p.text or "") for p in lvl.findall(f"{CX}pt")}
                      for lvl in str_dim.findall(f"{CX}lvl")]
            n = max((max(lv) + 1 for lv in levels if lv), default=0)
            # 여러 단계(선버스트·트리맵의 상위 분류)는 위 단계부터 이어 쓴다. 파일에는 아래 단계가 먼저 나온다
            cats = [" / ".join(lv[i] for lv in reversed(levels) if lv.get(i)) for i in range(n)]
        num_dim = next((s for s in d.findall(f"{CX}numDim") if s.get("type") in ("val", "size", "y")), None)
        vals: list[float | None] = []
        if num_dim is not None:
            lvl = num_dim.find(f"{CX}lvl")
            pts = {int(p.get("idx", "0")): p.text for p in (lvl.findall(f"{CX}pt") if lvl is not None else [])}
            count = int(lvl.get("ptCount", "0") or 0) if lvl is not None else 0
            for i in range(max(count, max(pts) + 1 if pts else 0)):
                try:
                    vals.append(float(pts[i]) if pts.get(i) not in (None, "") else None)
                except ValueError:
                    vals.append(None)
        data[d.get("id", "")] = (cats, vals)
    series: list[ChartSeries] = []
    categories: list[str] = []
    kinds: list[str] = []
    for ser in root.iter(f"{CX}series"):
        ref = ser.find(f"{CX}dataId")
        cats, vals = data.get(ref.get("val", "") if ref is not None else "", ([], []))
        if not vals:
            continue
        name = ser.find(f"{CX}tx/{CX}txData/{CX}v")
        series.append(ChartSeries(name=name.text if name is not None else None, values=vals))
        categories = categories or cats
        kinds.append(ser.get("layoutId") or "chartex")
    if not series:
        return None
    t = root.find(f"{CX}chart/{CX}title")
    title = None
    if t is not None:
        v = t.find(f".//{CX}v")
        title = (v.text if v is not None else "".join(x.text or "" for x in t.iter(q("a:t")))) or None
    return Chart(chart_type=kinds[0], title=title, categories=categories, series=series,
                 width_pt=width_pt, height_pt=height_pt)


# ---------------------------------------------------------------------------
# SmartArt (Word가 저장해 둔 그리기 결과)
# ---------------------------------------------------------------------------

_DSP = "http://schemas.microsoft.com/office/drawing/2008/diagram"


def _dsp(tag: str) -> str:
    return f"{{{_DSP}}}{tag}"


def read_dsp_drawing(root, theme: Theme) -> list[Shape]:
    """diagrams/drawingN.xml(dsp:drawing)의 도형들을 읽는다."""
    tree = root.find(_dsp("spTree"))
    if tree is None:
        return []
    shapes: list[Shape] = []
    for sp in tree.iter(_dsp("sp")):
        sppr = sp.find(_dsp("spPr"))
        xfrm = sppr.find(q("a:xfrm")) if sppr is not None else None
        if xfrm is None or xfrm.find(q("a:off")) is None or xfrm.find(q("a:ext")) is None:
            continue
        off, ext = xfrm.find(q("a:off")), xfrm.find(q("a:ext"))
        style = sp.find(_dsp("style"))
        prst = sppr.find(q("a:prstGeom"))

        fill = None
        if sppr.find(q("a:noFill")) is None:
            fill = read_color(sppr.find(q("a:solidFill")), theme)
            if fill is None and style is not None:
                fill = read_color(style.find(q("a:fillRef")), theme)
        ln = sppr.find(q("a:ln"))
        stroke = None
        if ln is None or ln.find(q("a:noFill")) is None:
            stroke = read_color(ln.find(q("a:solidFill")), theme) if ln is not None else None
            if stroke is None and style is not None:
                stroke = read_color(style.find(q("a:lnRef")), theme)
        font_color = read_color(style.find(q("a:fontRef")), theme) if style is not None else None

        body = sp.find(_dsp("txBody"))
        blocks = _drawingml_text(body, theme, font_color) if body is not None else []
        bpr = body.find(q("a:bodyPr")) if body is not None else None
        anchor = {"t": "top", "ctr": "middle", "b": "bottom"}.get(
            bpr.get("anchor", "ctr") if bpr is not None else "ctr", "middle"
        )
        shapes.append(Shape(
            geometry=prst.get("prst", "rect") if prst is not None else "rect",
            x=_emu(off.get("x")), y=_emu(off.get("y")),
            width=_emu(ext.get("cx")), height=_emu(ext.get("cy")),
            rotation=int(xfrm.get("rot", "0")) / 60000,
            fill=fill, stroke=stroke,
            stroke_width_pt=max(_emu(ln.get("w")), 0.25) if ln is not None and ln.get("w") else 0.75,
            text_blocks=blocks, text_v_align=anchor,
        ))
    return shapes


def _drawingml_text(body, theme: Theme, default_color: str | None) -> list:
    """a:p / a:r / a:t (DrawingML 텍스트) → Paragraph 목록."""
    from ...ir import Paragraph, ParagraphStyle, Text, TextStyle

    paragraphs = []
    for p in body.findall(q("a:p")):
        inlines = []
        for r in p.findall(q("a:r")):
            t = r.find(q("a:t"))
            if t is None or not t.text:
                continue
            rpr = r.find(q("a:rPr"))
            style = {}
            if rpr is not None:
                if rpr.get("sz"):
                    style["size_pt"] = int(rpr.get("sz")) / 100
                if rpr.get("b") == "1":
                    style["bold"] = True
                if rpr.get("i") == "1":
                    style["italic"] = True
                color = read_color(rpr.find(q("a:solidFill")), theme)
                if color:
                    style["color"] = color
            if "color" not in style and default_color:
                style["color"] = default_color
            inlines.append(Text(text=t.text, style=TextStyle(**style) if style else None))
        if not inlines:
            continue
        ppr = p.find(q("a:pPr"))
        align = {"ctr": "center", "r": "right", "just": "justify"}.get(ppr.get("algn", "ctr") if ppr is not None else "ctr", "center")
        paragraphs.append(Paragraph(inlines=inlines, style=ParagraphStyle(align=align)))
    return paragraphs
