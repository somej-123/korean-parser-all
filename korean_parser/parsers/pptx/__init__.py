"""PPTX 파서: PowerPoint 슬라이드를 AI 없이 XML 규칙만으로 IR(Document)로 변환한다.

꾸러미 구조
    ppt/presentation.xml       슬라이드 순서(sldIdLst), 슬라이드 크기(sldSz)
    ppt/slides/slideN.xml      슬라이드: 도형 나무(spTree) 안의 글상자·표·그림·차트·묶음
    ppt/slideLayouts, slideMasters  자리 표시자(placeholder)의 위치·서식을 물려준다
    ppt/notesSlides/…          발표자 메모
    ppt/theme/theme1.xml       테마 색

변환 규칙
- 슬라이드마다 쪽 나누기. 출처(prov)의 쪽 = 슬라이드 번호, 좌표 = 도형 위치(pt)
- 제목 자리 표시자(title, ctrTitle) → Heading(1단계). 원본 구조에 있는 정보다
- 내용 자리 표시자(body, obj 등)의 문단 → 글머리 목록(ListInfo, 단계 = a:pPr lvl). 글머리 기호·자동 번호는 슬라이드에
  적힌 값(buChar, buAutoNum, buNone)을 먼저 쓰고, 없으면 '•'
- 읽는 순서: 제목 → 나머지는 XY 자르기(빈 띠로 위아래·좌우를 나누되 더 넓은 빈 띠부터)로. 두 단 비교, 카드 세 장 같은
  배치를 단(카드)마다 읽는다. 자리 표시자 위치가 슬라이드에 없으면 레이아웃·마스터 것
- 자리 표시자 없이 글상자로 만든 슬라이드: 여러 슬라이드의 같은 자리에 되풀이되는 글(바닥글, 쪽 번호)은 머리말·꼬리말로
  옮기고, 슬라이드 위쪽의 가장 큰 글씨는 제목 추정(heading_guess)을 붙인다 (원본에 제목 정보가 없으므로 Heading으로 바꾸지 않는다)
- 그림 대체 텍스트가 파일 경로이면('/home/…/icon.png', 만든 프로그램이 넣은 값) 파일 이름만 남긴다
- 표(a:tbl): 가로·세로 병합, 칸 배경, 테두리, 세로 정렬. 차트: DOCX 차트 해석을 그대로 쓴다
- 그림 → Figure. SmartArt → 안의 글만 문단으로
- 연결선·선이 도형 둘 이상을 잇는 묶음(조직도, 흐름도) → Drawing 하나 (상자 글, 연결선, 연결 관계). 연결선 끝은
  연결 정보(stCxn/endCxn)로, 없으면 끝점에서 8pt 안의 도형으로 찾는다. 묶음 범위 안에 놓인 글상자(선 위의 이름표)도
  함께 넣는다. 도형 id는 슬라이드마다 1부터 다시 시작하므로 's슬라이드번호:id'로 바꾼다
- 이어지지 않은 글 없는 도형(카드 배경, 색 띠, 아이콘 모양 등 장식)은 그리지 않는다 (경고)
- 발표자 메모 → 슬라이드 끝에 문단 (style_name='slide_notes')
- 날짜·슬라이드 번호 자리 표시자는 버리고, 바닥글(ftr)은 문서 꼬리말로
"""

from __future__ import annotations

import base64
import mimetypes
import posixpath
import re
import statistics
from collections import Counter
from pathlib import Path

from ...ir import (
    LINE_GEOMETRIES, Block, Border, Borders, Break, Cell, Document, Drawing, Figure, Heading, HeadingGuess, Image,
    ListInfo, Meta, PageBreak, PageSetup, Paragraph, ParagraphStyle, Provenance, Row, Shape, Table, Text, TextStyle,
)
from ..docx.drawing import Theme, custom_path, read_chart, read_color, read_fill_line
from ..ooxml import R_EMBED, R_ID, Package, ln

PARSER_NAME = "pptx-native"
NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}
EMU = 12700.0
_TITLE = {"title", "ctrTitle"}
_SKIP_PH = {"dt", "sldNum"}  # 날짜·슬라이드 번호: 자동 값
_BODY_PH = {"body", "obj", None}  # 종류가 없고 idx만 있는 자리 표시자는 내용(body)이다
_ALIGN = {"l": "left", "ctr": "center", "r": "right", "just": "justify", "dist": "justify"}
_ANCHOR = {"t": "top", "ctr": "middle", "b": "bottom"}


def q(tag: str) -> str:
    prefix, local = tag.split(":")
    return f"{{{NS[prefix]}}}{local}"


def parse_pptx(path: str | Path) -> Document:
    return PptxParser(path).parse()


def _pt(v) -> float:
    try:
        return int(v) / EMU
    except (TypeError, ValueError):
        return 0.0


def _auto_number(kind: str, n: int) -> str:
    """a:buAutoNum type → 표시 문자열 (자주 쓰는 것만)."""
    from ..docx.numbering import _letters, _roman

    circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
    if kind.startswith("circleNum"):
        return circled[n - 1] if 0 < n <= len(circled) else str(n)
    if kind.startswith("roman"):
        body = _roman(n) if "Uc" in kind else _roman(n).lower()
    elif kind.startswith("alpha"):
        body = _letters(n) if "Uc" in kind else _letters(n).lower()
    elif kind.startswith("hangul") or kind.startswith("ea1Jpn") or kind.startswith("ganada"):
        body = "가나다라마바사아자차카타파하"[(n - 1) % 14]
    else:
        body = str(n)
    if kind.endswith("ParenBoth"):
        return f"({body})"
    if kind.endswith("ParenR"):
        return f"{body})"
    if kind.endswith("Period"):
        return f"{body}."
    return body


class PptxParser:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.pkg = Package(self.path)
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="pptx"))
        self._seq = 0
        self._images: dict[str, str] = {}
        self.theme = Theme()

    def _new_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _prov(self, slide: int, box=None) -> Provenance:
        return Provenance(parser=PARSER_NAME, page=slide,
                          bbox=tuple(round(v, 1) for v in box) if box else None)

    def _warn(self, msg: str) -> None:
        if msg not in self.doc.warnings:
            self.doc.warnings.append(msg)

    # ------------------------------------------------------------------

    def parse(self) -> Document:
        pres_part = "ppt/presentation.xml"
        pres = self.pkg.xml(pres_part)
        if pres is None:
            raise ValueError("ppt/presentation.xml이 없습니다")
        theme_part = self.pkg.rel_of_type(pres_part, "theme")
        if theme_part:
            self.theme = Theme(self.pkg.xml(theme_part))
        size = pres.find(q("p:sldSz"))
        if size is not None:
            self.doc.page = PageSetup(width_pt=_pt(size.get("cx")), height_pt=_pt(size.get("cy")),
                                      margin_left_pt=0, margin_right_pt=0, margin_top_pt=0, margin_bottom_pt=0)
        rels = self.pkg.rels(pres_part)
        slides = [rels[s.get(R_ID)][0] for s in pres.iter(q("p:sldId")) if s.get(R_ID) in rels]
        for n, part in enumerate(slides, 1):
            if n > 1:
                self.doc.blocks.append(PageBreak(id=self._new_id(), prov=self._prov(n)))
            self.doc.blocks.extend(self._slide(part, n))
        self._move_repeated_text(len(slides))
        self._guess_slide_titles()
        core = self.pkg.core()
        m = self.doc.meta
        m.title, m.author = core.get("title"), core.get("author")
        m.created, m.modified, m.language = core.get("created"), core.get("modified"), core.get("language")
        m.page_count, m.page_count_source = len(slides), "layout"  # 슬라이드 수는 파일 구조 그대로다
        self.pkg.close()
        return self.doc

    # ------------------------------------------------------------------
    # 슬라이드
    # ------------------------------------------------------------------

    def _slide(self, part: str, n: int) -> list[Block]:
        root = self.pkg.xml(part)
        if root is None:
            self._warn(f"슬라이드 파일 없음: {part}")
            return []
        self._part = part
        self._layout_boxes = self._placeholder_boxes(part)
        tree = root.find(f"{q('p:cSld')}/{q('p:spTree')}")
        items: list[tuple[tuple, list[Block]]] = []  # (정렬 열쇠, 블록)
        if tree is not None:
            elements = list(self._shapes(tree, (0, 0, 1, 1)))
            diagrams, used = self._diagrams(elements, n)
            for el, xf in elements:
                if id(el) not in used:
                    items.extend(self._shape(el, xf, n))
            items.extend(diagrams)
        titles = [it for it in items if it[0][0] == 0]
        rest = [it for it in items if it[0][0] != 0]
        out = [b for _, blocks in titles + xy_order(rest) for b in blocks]
        out.extend(self._notes(part, n))
        return out

    def _shapes(self, tree, xf):
        """도형 나무를 펼친다 (묶음은 좌표 변환을 쌓아 안쪽으로). (요소, 좌표 변환) 차례."""
        for el in tree:
            name = ln(el)
            if name == "grpSp":
                yield from self._shapes(el, self._group_xf(el, xf))
            elif name == "AlternateContent":
                pick = next((c for c in el if ln(c) == "Choice"), None) or next((c for c in el if ln(c) == "Fallback"), None)
                if pick is not None:
                    yield from self._shapes(pick, xf)
            elif name in ("sp", "pic", "graphicFrame", "cxnSp"):
                yield el, xf

    @staticmethod
    def _group_xf(grp, xf):
        x = grp.find(f"{q('p:grpSpPr')}/{q('a:xfrm')}")
        if x is None:
            return xf
        off, ext, choff, chext = (x.find(q(t)) for t in ("a:off", "a:ext", "a:chOff", "a:chExt"))
        if None in (off, ext, choff, chext):
            return xf
        sx = int(ext.get("cx", 0)) / max(int(chext.get("cx", 1)), 1)
        sy = int(ext.get("cy", 0)) / max(int(chext.get("cy", 1)), 1)
        ox, oy, pxs, pys = xf
        # 안쪽 좌표 c → 바깥 좌표: off + (c - chOff)·s, 이를 다시 바깥 변환에 넣는다
        return (ox + (int(off.get("x", 0)) - int(choff.get("x", 0)) * sx) * pxs,
                oy + (int(off.get("y", 0)) - int(choff.get("y", 0)) * sy) * pys, pxs * sx, pys * sy)

    def _box(self, el, xf, ph=None):
        """도형의 슬라이드 위 위치 (pt). 자리 표시자에 위치가 없으면 레이아웃·마스터의 위치."""
        x = el.find(f".//{q('a:xfrm')}") if ln(el) != "graphicFrame" else el.find(q("p:xfrm"))
        if x is None and ph is not None:
            return self._layout_boxes.get(ph) or self._layout_boxes.get((ph[0], None))
        if x is None:
            return None
        off, ext = x.find(q("a:off")), x.find(q("a:ext"))
        if off is None or ext is None:
            return None
        ox, oy, sx, sy = xf
        x0, y0 = (ox + int(off.get("x", 0)) * sx) / EMU, (oy + int(off.get("y", 0)) * sy) / EMU
        return (x0, y0, x0 + int(ext.get("cx", 0)) * sx / EMU, y0 + int(ext.get("cy", 0)) * sy / EMU)

    def _placeholder_boxes(self, slide_part: str) -> dict:
        """레이아웃·마스터의 자리 표시자 위치: (종류, idx) → 상자. 슬라이드 쪽 값이 먼저다(레이아웃이 마스터를 덮는다)."""
        boxes: dict = {}
        layout = self.pkg.rel_of_type(slide_part, "slideLayout")
        master = self.pkg.rel_of_type(layout, "slideMaster") if layout else None
        for part in (master, layout):  # 마스터 먼저, 레이아웃이 덮어쓴다
            root = self.pkg.xml(part) if part else None
            if root is None:
                continue
            for sp in root.iter(q("p:sp")):
                ph = sp.find(f"{q('p:nvSpPr')}/{q('p:nvPr')}/{q('p:ph')}")
                if ph is None:
                    continue
                box = self._box(sp, (0, 0, 1, 1))
                if box:
                    kind = ph.get("type", "body")
                    boxes[(kind, ph.get("idx"))] = box
                    boxes.setdefault((kind, None), box)
        return boxes

    # ------------------------------------------------------------------
    # 도형 한 개
    # ------------------------------------------------------------------

    def _shape(self, el, xf, n: int) -> list[tuple[tuple, list[Block]]]:
        name = ln(el)
        ph_el = el.find(f".//{q('p:nvPr')}/{q('p:ph')}")
        ph = (ph_el.get("type", "body") if ph_el.get("type") or ph_el.get("idx") else "body", ph_el.get("idx")) \
            if ph_el is not None else None
        kind = ph[0] if ph else None
        if kind in _SKIP_PH:
            return []
        box = self._box(el, xf, ph)
        key = (0 if kind in _TITLE else 1, box)
        if name == "pic":
            fig = self._picture(el, n, box)
            return [(key, [fig])] if fig else []
        if name == "graphicFrame":
            return [(key, self._frame(el, n, box))]
        body = el.find(q("p:txBody"))
        has_text = body is not None and any((t.text or "").strip() for t in body.iter(q("a:t")))
        if not has_text:
            if name in ("sp", "cxnSp") and el.find(f".//{q('a:prstGeom')}") is not None and kind is None:
                self._warn("이어지지 않은 글 없는 도형(배경·띠·아이콘 모양 등 장식)은 그리지 않음 (PPTX)")
            return []
        if kind == "ftr":
            if not self.doc.footer:
                self.doc.footer = self._paragraphs(body, n, box, bullets=False)
            return []
        if kind in _TITLE:
            text_blocks = self._paragraphs(body, n, box, bullets=False)
            heading = Heading(id=self._new_id(), prov=self._prov(n, box), level=1,
                              inlines=[i for j, p in enumerate(text_blocks) for i in ([Break()] if j else []) + p.inlines])
            return [(key, [heading])]
        bullets = ph is not None and kind in _BODY_PH
        return [(key, self._paragraphs(body, n, box, bullets=bullets))]

    # ------------------------------------------------------------------
    # 연결선으로 이어진 도형 묶음 → 도식
    # ------------------------------------------------------------------

    def _diagrams(self, elements, n: int) -> tuple[list, set[int]]:
        """연결선·선이 도형 둘 이상을 잇는 묶음마다 Drawing. (정렬 열쇠, [Drawing]) 목록과 쓴 요소의 id() 모음.

        실제 화면설계서의 '메뉴구조도'(연결선 18개로 이은 조직도)가 상자 글만 흩어져 나와 위아래 관계가 사라졌다.
        """
        from ...render.llm import _endpoints  # 끝점 → 닿은 도형 (LLM 출력이 연결 관계를 쓰는 규칙과 같게)

        cands = []  # (요소, Shape)
        for el, xf in elements:
            if ln(el) not in ("sp", "cxnSp") or el.find(f".//{q('p:nvPr')}/{q('p:ph')}") is not None:
                continue
            shape = self._ir_shape(el, xf, n)
            if shape is not None:
                cands.append((el, shape))
        texts = {id(el): xf for el, xf in elements}
        lines = [(el, s) for el, s in cands if s.is_line]
        if not lines:
            return [], set()
        boxes = [s for _, s in cands if not s.is_line]
        parent = {id(s): id(s) for _, s in cands}

        def find(k):
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        joined = set()  # 양 끝이 모두 도형에 닿은 선이 있는 묶음
        for _, line in lines:
            a, b = _endpoints(line, boxes)
            for end in (a, b):
                if end is not None:
                    parent[find(id(end))] = find(id(line))
            if a is not None and b is not None and a is not b:
                joined.add(id(line))
        groups: dict[int, list] = {}
        for el, shp in cands:
            groups.setdefault(find(id(shp)), []).append((el, shp))
        groups = [m for m in groups.values()
                  if any(id(shp) in joined for _, shp in m) and sum(not shp.is_line for _, shp in m) >= 2]
        # 범위가 겹치는 묶음은 하나로 (조직도의 가지가 따로 이어져 있어도 한 도식)
        merged = True
        while merged:
            merged = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    a, b = _extent(groups[i]), _extent(groups[j])
                    if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                        groups[i] += groups.pop(j)
                        merged = True
                        break
                if merged:
                    break
        out, used = [], {id(el) for m in groups for el, _ in m}
        for members in groups:
            # 묶음 범위 안의 글상자(선 위의 이름표 등)와, 묶음의 상자 바로 위·아래에 같은 너비로 쌓인 상자(조직도의
            # 한 열: 연결선 없이 쌓은 '재해분석', '재해현황')를 넣는다. 쌓인 상자를 넣으면 범위가 늘어 다시 본다
            grown = True
            while grown:
                grown = False
                x0, y0, x1, y1 = _extent(members)
                boxes_in = [shp for _, shp in members if not shp.is_line]
                for el, shp in cands:
                    if id(el) in used or not _has_text(el) or shp.is_line:
                        continue
                    within = shp.x >= x0 - 2 and shp.y >= y0 - 2 and shp.x + shp.width <= x1 + 2 \
                        and shp.y + shp.height <= y1 + 2
                    if within or any(_stacked(shp, m) for m in boxes_in):
                        members.append((el, shp))
                        used.add(id(el))
                        grown = True
            x0, y0, x1, y1 = _extent(members)
            shapes = []
            for el, shp in members:
                body = el.find(q("p:txBody"))
                blocks = self._paragraphs(body, n, self._box(el, texts[id(el)]), bullets=False) if _has_text(el) else []
                shapes.append(shp.model_copy(update={"x": shp.x - x0, "y": shp.y - y0, "text_blocks": blocks}))
            box = (x0, y0, x1, y1)
            drawing = Drawing(id=self._new_id(), prov=self._prov(n, box), width_pt=round(x1 - x0, 1),
                              height_pt=round(y1 - y0, 1), shapes=shapes)
            out.append(((1, box), [drawing]))
        return out, used

    def _ir_shape(self, el, xf, n: int) -> Shape | None:
        """p:sp·p:cxnSp → Shape (슬라이드 좌표, pt). 글이 있으면 글상자 문단도."""
        sppr = el.find(q("p:spPr"))
        box = self._box(el, xf)
        if sppr is None or box is None:
            return None
        x, y, w, h = box[0], box[1], box[2] - box[0], box[3] - box[1]
        xfrm = sppr.find(q("a:xfrm"))
        rot = int(xfrm.get("rot", "0")) / 60000 if xfrm is not None else 0
        prst, cust = sppr.find(q("a:prstGeom")), sppr.find(q("a:custGeom"))
        geometry = prst.get("prst", "rect") if prst is not None else "custom" if cust is not None else "rect"
        path = custom_path(cust, x, y, w, h) if cust is not None else None
        if ln(el) == "cxnSp" and geometry not in LINE_GEOMETRIES:
            geometry = "straightConnector1"
        fill, stroke, width, dash, head, tail = read_fill_line(sppr, el.find(q("p:style")), geometry, self.theme)
        nv = el.find(f".//{q('p:cNvPr')}")
        cxn = el.find(f".//{q('p:cNvCxnSpPr')}")
        ends = [cxn.find(q(t)) if cxn is not None else None for t in ("a:stCxn", "a:endCxn")]
        sid = lambda v: f"s{n}:{v}" if v else None  # noqa: E731 (슬라이드마다 id가 1부터 다시 시작한다)
        body = el.find(q("p:txBody"))
        bpr = body.find(q("a:bodyPr")) if body is not None else None
        return Shape(
            id=sid(nv.get("id")) if nv is not None else None, name=nv.get("name") if nv is not None else None,
            geometry=geometry, path=path, x=x, y=y, width=w, height=h, rotation=rot,
            flip_h=xfrm is not None and xfrm.get("flipH") == "1", flip_v=xfrm is not None and xfrm.get("flipV") == "1",
            fill=fill, stroke=stroke, stroke_width_pt=width, stroke_dash=dash, head_arrow=head, tail_arrow=tail,
            connect_from=sid(ends[0].get("id")) if ends[0] is not None else None,
            connect_to=sid(ends[1].get("id")) if ends[1] is not None else None,
            text_v_align=_ANCHOR.get(bpr.get("anchor", "t") if bpr is not None else "t", "top"),
        )

    def _paragraphs(self, body, n: int, box, bullets: bool) -> list[Paragraph]:
        out: list[Paragraph] = []
        counters: dict[int, int] = {}
        for p in body.iter(q("a:p")):
            inlines = []
            for el in p:
                name = ln(el)
                if name in ("r", "fld"):
                    t = el.find(q("a:t"))
                    if t is not None and t.text:
                        inlines.append(Text(text=t.text, style=self._run_style(el.find(q("a:rPr")))))
                elif name == "br":
                    inlines.append(Break())
            if not any(isinstance(i, Text) and i.text.strip() for i in inlines):
                continue
            ppr = p.find(q("a:pPr"))
            level = int(ppr.get("lvl", "0")) if ppr is not None else 0
            list_info = None
            if ppr is not None and ppr.find(q("a:buAutoNum")) is not None:
                auto = ppr.find(q("a:buAutoNum"))
                for deeper in [k for k in counters if k > level]:
                    del counters[deeper]
                counters[level] = counters.get(level, int(auto.get("startAt", "1")) - 1) + 1
                list_info = ListInfo(level=level, marker=_auto_number(auto.get("type", "arabicPeriod"), counters[level]),
                                     ordered=True)
            elif ppr is not None and ppr.find(q("a:buChar")) is not None:
                list_info = ListInfo(level=level, marker=ppr.find(q("a:buChar")).get("char", "•"), ordered=False)
            elif bullets and (ppr is None or ppr.find(q("a:buNone")) is None):
                list_info = ListInfo(level=level, marker="•", ordered=False)
            style = None
            if ppr is not None and ppr.get("algn") in _ALIGN:
                style = ParagraphStyle(align=_ALIGN[ppr.get("algn")])
            out.append(Paragraph(id=self._new_id(), prov=self._prov(n, box), inlines=inlines, style=style,
                                 list_info=list_info))
        return out

    def _run_style(self, rpr) -> TextStyle | None:
        if rpr is None:
            return None
        s: dict = {}
        if rpr.get("sz"):
            s["size_pt"] = int(rpr.get("sz")) / 100
        if rpr.get("b") in ("1", "true"):
            s["bold"] = True
        if rpr.get("i") in ("1", "true"):
            s["italic"] = True
        if rpr.get("u") not in (None, "none"):
            s["underline"] = True
        if rpr.get("strike") not in (None, "noStrike"):
            s["strike"] = True
        color = read_color(rpr.find(q("a:solidFill")), self.theme)
        if color:
            s["color"] = color
        font = rpr.find(q("a:ea")) if rpr.find(q("a:ea")) is not None else rpr.find(q("a:latin"))
        if font is not None and font.get("typeface") and not font.get("typeface").startswith("+"):
            s["font"] = font.get("typeface")
        return TextStyle(**s) if s else None

    # ------------------------------------------------------------------
    # 그림, 표, 차트, SmartArt
    # ------------------------------------------------------------------

    def _image_id(self, rid: str | None) -> str | None:
        target = self.pkg.rels(self._part).get(rid or "", (None,))[0]
        if not target:
            return None
        if target in self._images:
            return self._images[target]
        data = self.pkg.read(target)
        if data is None:
            self._warn(f"그림 파일 없음: {target}")
            return None
        mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.lower().endswith(".emf"):
            mime = "image/x-emf"
        elif target.lower().endswith(".wmf"):
            mime = "image/x-wmf"
        image_id = f"img{len(self.doc.images) + 1}"
        self.doc.images[image_id] = Image(mime=mime, data_base64=base64.b64encode(data).decode("ascii"),
                                          name=posixpath.basename(target))
        self._images[target] = image_id
        return image_id

    def _picture(self, el, n: int, box) -> Figure | None:
        blip = el.find(f".//{q('a:blip')}")
        image_id = self._image_id(blip.get(R_EMBED)) if blip is not None else None
        if not image_id:
            return None
        nv = el.find(f".//{q('p:cNvPr')}")
        alt = (nv.get("descr") or "").strip() or None if nv is not None else None
        if alt and re.search(r"[/\\]", alt) and re.search(r"\.(png|jpe?g|gif|bmp|svg|emf|wmf|tiff?)$", alt, re.I):
            alt = re.split(r"[/\\]", alt)[-1]  # 만든 프로그램이 넣은 파일 경로: 파일 이름만
        w, h = (box[2] - box[0], box[3] - box[1]) if box else (None, None)
        return Figure(id=self._new_id(), prov=self._prov(n, box), image_id=image_id, width_pt=w, height_pt=h, alt=alt)

    def _frame(self, el, n: int, box) -> list[Block]:
        data = el.find(f"{q('a:graphic')}/{q('a:graphicData')}")
        if data is None:
            return []
        uri = data.get("uri", "")
        if uri.endswith("/table"):
            tbl = data.find(q("a:tbl"))
            return [self._table(tbl, n, box)] if tbl is not None else []
        if uri.endswith("/chart"):
            ref = data.find(q("c:chart"))
            part = self.pkg.rels(self._part).get(ref.get(R_ID) if ref is not None else "", (None,))[0]
            root = self.pkg.xml(part) if part else None
            w, h = (box[2] - box[0], box[3] - box[1]) if box else (400.0, 300.0)
            chart = read_chart(root, w, h, self.theme) if root is not None else None
            if chart is None:
                self._warn("차트를 읽지 못함 (PPTX)")
                return []
            chart.id, chart.prov = self._new_id(), self._prov(n, box)
            return [chart]
        if uri.endswith("/diagram"):
            ids = data.find(q("dgm:relIds"))
            rid = ids.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm") if ids is not None else None
            part = self.pkg.rels(self._part).get(rid or "", (None,))[0]
            root = self.pkg.xml(part) if part else None
            if root is None:
                return []
            self._warn("SmartArt는 모양 없이 글만 꺼냄 (PPTX)")
            texts = [t.text.strip() for t in root.iter(q("a:t")) if t.text and t.text.strip()]
            return [Paragraph(id=self._new_id(), prov=self._prov(n, box), inlines=[Text(text=t)],
                              list_info=ListInfo(level=0, marker="•", ordered=False)) for t in texts]
        self._warn("읽지 못한 개체(OLE 등)가 있음 (PPTX)")
        return []

    def _table(self, tbl, n: int, box) -> Table:
        table = Table(id=self._new_id(), prov=self._prov(n, box))
        table.col_widths_pt = [_pt(c.get("w")) for c in tbl.iter(q("a:gridCol"))]
        table.width_pt = sum(table.col_widths_pt) or None
        tpr = tbl.find(q("a:tblPr"))
        first_row = tpr is not None and tpr.get("firstRow") == "1"
        for r_i, tr in enumerate(tbl.findall(q("a:tr"))):
            row = Row(height_pt=_pt(tr.get("h")) or None, header=first_row and r_i == 0)
            for tc in tr.findall(q("a:tc")):
                if tc.get("hMerge") == "1" or tc.get("vMerge") == "1":
                    continue  # 병합에 덮인 칸
                body = tc.find(q("a:txBody"))
                cell = Cell(blocks=self._paragraphs(body, n, box, bullets=False) if body is not None else [],
                            col_span=int(tc.get("gridSpan", "1")), row_span=int(tc.get("rowSpan", "1")))
                tcpr = tc.find(q("a:tcPr"))
                if tcpr is not None:
                    cell.background = read_color(tcpr.find(q("a:solidFill")), self.theme)
                    cell.v_align = _ANCHOR.get(tcpr.get("anchor", "t"))
                    sides = {}
                    for side, tag in (("left", "a:lnL"), ("right", "a:lnR"), ("top", "a:lnT"), ("bottom", "a:lnB")):
                        ln_el = tcpr.find(q(tag))
                        if ln_el is None or ln_el.find(q("a:noFill")) is not None:
                            continue
                        color = read_color(ln_el.find(q("a:solidFill")), self.theme)
                        if color:
                            sides[side] = Border(width_pt=max(_pt(ln_el.get("w", "12700")), 0.25), color=color)
                    cell.borders = Borders(**sides) if sides else None
                row.cells.append(cell)
            table.rows.append(row)
        return table

    def _move_repeated_text(self, n_slides: int) -> None:
        """여러 슬라이드의 같은 자리에 되풀이되는 글(바닥글, 쪽 번호)을 머리말·꼬리말로 옮긴다.

        자리 표시자 없이 글상자로 만든 슬라이드는 바닥글·쪽 번호가 슬라이드마다 본문에 들어 있다. 같은 글이
        같은 자리(5pt 안)에 슬라이드 절반 이상(3장 이상) 나오고 (슬라이드 번호와 같은 숫자만 다를 수 있다), 슬라이드 위·아래 가장자리(높이의 12%) 안에 있으면 꾸밈 글로
        본다 (PDF의 되풀이 머리말·꼬리말 판단과 같다). 가장자리 조건이 없으면 같은 틀의 슬라이드마다 같은 자리에 놓인
        '진행 순서', '방법 1' 같은 내용 이름표까지 옮겨졌다 (실제 PPTX). 쪽 번호(숫자만)는 버리고, 나머지는 위쪽이면 머리말,
        아래쪽이면 꼬리말에 한 번만 둔다.
        """
        if n_slides < 3:
            return

        def key(b):
            # 슬라이드 번호와 같은 숫자만 '#'로 ('02 / 14'는 쪽 번호, '방법 1'·'방법 2'는 내용이라 서로 다른 글)
            x0, y0 = b.prov.bbox[0], b.prov.bbox[1]
            # 첫 번째만: 마지막 슬라이드의 '4 / 4'에서 전체 수까지 바꾸면 다른 슬라이드의 '# / 4'와 달라진다
            text = re.sub(rf"(?<!\d)0*{b.prov.page}(?!\d)", "#", " ".join(_text_of(b).split()), count=1)
            return text, round(x0 / 5), round(y0 / 5)

        height = self.doc.page.height_pt if self.doc.page else 540.0

        def at_edge(b) -> bool:
            return b.prov.bbox[1] < height * 0.12 or b.prov.bbox[3] > height * 0.88

        def movable(b) -> bool:  # 문단, 또는 슬라이드마다 같은 자리에 놓인 도식(범례 등)
            return isinstance(b, (Paragraph, Drawing)) and b.prov and b.prov.bbox and _text_of(b).strip() and at_edge(b)

        cands = [b for b in self.doc.blocks if movable(b)]
        pages: Counter = Counter()
        seen = set()
        for b in cands:
            k = key(b)
            if (k, b.prov.page) not in seen:
                seen.add((k, b.prov.page))
                pages[k] += 1
        repeated = {k for k, c in pages.items() if c >= max(3, n_slides / 2)}
        if not repeated:
            return
        kept: dict = {}
        out = []
        for b in self.doc.blocks:
            if movable(b) and key(b) in repeated:
                k = key(b)
                if k not in kept and not re.fullmatch(r"[#\d\s/|\-·.]*", k[0]):  # 쪽 번호('3', '3 / 14')만이면 버린다
                    kept[k] = b
                    (self.doc.header if b.prov.bbox[1] < height / 2 else self.doc.footer).append(b)
                continue
            out.append(b)
        self.doc.blocks = out

    def _guess_slide_titles(self) -> None:
        """제목 자리 표시자가 없는 슬라이드: 위쪽 40% 안의 가장 큰 글씨 글상자에 제목 추정을 붙인다.

        그 크기가 슬라이드 글자 크기 중앙값(글자 수 가중)의 1.25배 이상이고 60자 이하일 때만. 같은 글상자의 같은 크기 문단
        (두 줄 제목)은 함께 표시한다.
        """
        height = self.doc.page.height_pt if self.doc.page else 540.0
        slides: dict[int, list] = {}
        for b in self.doc.blocks:
            if b.prov and b.prov.page:
                slides.setdefault(b.prov.page, []).append(b)
        for blocks in slides.values():
            if any(isinstance(b, Heading) for b in blocks):
                continue
            paras = [b for b in blocks if isinstance(b, Paragraph) and b.style_name != "slide_notes" and b.prov.bbox]
            sized = [(b, max((i.style.size_pt for i in b.inlines if isinstance(i, Text) and i.style and i.style.size_pt),
                             default=None)) for b in paras]
            weights = [s for b, s in sized if s for _ in range(min(len(b.text), 200))]
            if not weights:
                continue
            body = statistics.median(weights)
            top = [(b, s) for b, s in sized if s and b.prov.bbox[1] < height * 0.4 and len(b.text.strip()) <= 60]
            if not top:
                continue
            biggest = max(s for _, s in top)
            if biggest < body * 1.25:
                continue
            box = next(b.prov.bbox for b, s in top if s == biggest)
            for b, s in top:
                if s == biggest and b.prov.bbox == box:
                    b.heading_guess = HeadingGuess(level=1, reason=f"슬라이드 위쪽 가장 큰 글씨 {biggest:g}pt (본문 {body:g}pt)")

    def _notes(self, slide_part: str, n: int) -> list[Paragraph]:
        part = self.pkg.rel_of_type(slide_part, "notesSlide")
        root = self.pkg.xml(part) if part else None
        if root is None:
            return []
        out = []
        for sp in root.iter(q("p:sp")):
            ph = sp.find(f"{q('p:nvSpPr')}/{q('p:nvPr')}/{q('p:ph')}")
            if ph is None or ph.get("type") != "body":
                continue
            body = sp.find(q("p:txBody"))
            if body is None:
                continue
            for p in self._paragraphs(body, n, None, bullets=False):
                p.style_name = "slide_notes"
                out.append(p)
        return out


def _text_of(b) -> str:
    """문단 글, 또는 도식 안 글상자 글을 이은 것."""
    if isinstance(b, Drawing):
        return " ".join(x.text for s in b.shapes for x in s.text_blocks if hasattr(x, "text"))
    return b.text


def _extent(members) -> tuple[float, float, float, float]:
    """(요소, Shape) 목록의 전체 범위."""
    return (min(s.x for _, s in members), min(s.y for _, s in members),
            max(s.x + s.width for _, s in members), max(s.y + s.height for _, s in members))


def _stacked(a: Shape, b: Shape) -> bool:
    """a가 b 바로 위나 아래에 같은 너비·같은 왼쪽 위치로 쌓인 상자인가 (틈은 b 높이 이하)."""
    if abs(a.x - b.x) > 2 or abs(a.width - b.width) > 2:
        return False
    gap = a.y - (b.y + b.height) if a.y >= b.y else b.y - (a.y + a.height)
    return -1 <= gap <= max(b.height, 4)


def _has_text(el) -> bool:
    body = el.find(q("p:txBody"))
    return body is not None and any((t.text or "").strip() for t in body.iter(q("a:t")))


def xy_order(items: list) -> list:
    """(열쇠, 블록) 목록을 XY 자르기 순서로. 열쇠의 둘째 값이 상자(없으면 맨 뒤, 원래 차례).

    상자들 사이에서 어느 상자도 걸치지 않는 빈 띠(가로·세로)를 찾아 가장 넓은 띠로 나눈다 (같으면 위아래를 먼저).
    위아래로 나누면 위쪽부터, 좌우로 나누면 왼쪽부터 읽고, 나뉜 조각마다 되풀이한다. 두 단 비교 슬라이드는 왼쪽 단을
    다 읽고 오른쪽 단으로, 카드 세 장은 카드마다 읽는다.
    """
    boxed = [it for it in items if it[0][1]]
    loose = [it for it in items if not it[0][1]]

    def gap(group, axis):
        lo, hi = (0, 2) if axis == "x" else (1, 3)
        spans = sorted((it[0][1][lo], it[0][1][hi]) for it in group)
        best, edge = None, spans[0][1]
        for a, b in spans[1:]:
            if a - edge > 2 and (best is None or a - edge > best[0]):
                best = (a - edge, (edge + a) / 2)
            edge = max(edge, b)
        return best

    def order(group):
        if len(group) <= 1:
            return group
        gy, gx = gap(group, "y"), gap(group, "x")
        if gy is None and gx is None:
            return sorted(group, key=lambda it: (round(it[0][1][1] / 4), it[0][1][0]))
        axis, cut = ("y", gy[1]) if gy and (gx is None or gy[0] >= gx[0]) else ("x", gx[1])
        i = 1 if axis == "y" else 0
        first = [it for it in group if it[0][1][i] < cut]
        second = [it for it in group if it[0][1][i] >= cut]
        return order(first) + order(second)

    return order(boxed) + loose
