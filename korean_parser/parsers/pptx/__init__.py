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
- 읽는 순서: 제목 → 나머지는 위에서 아래, 왼쪽에서 오른쪽 (자리 표시자 위치가 슬라이드에 없으면 레이아웃·마스터 것)
- 표(a:tbl): 가로·세로 병합, 칸 배경, 테두리, 세로 정렬. 차트: DOCX 차트 해석을 그대로 쓴다
- 그림 → Figure. SmartArt → 안의 글만 문단으로. 글 없는 도형(장식·선)은 아직 그리지 않는다 (경고)
- 발표자 메모 → 슬라이드 끝에 문단 (style_name='slide_notes')
- 날짜·슬라이드 번호 자리 표시자는 버리고, 바닥글(ftr)은 문서 꼬리말로
"""

from __future__ import annotations

import base64
import mimetypes
import posixpath
from pathlib import Path

from ...ir import (
    Block, Border, Borders, Break, Cell, Document, Figure, Heading, Image, ListInfo, Meta, PageBreak, PageSetup,
    Paragraph, ParagraphStyle, Provenance, Row, Table, Text, TextStyle,
)
from ..docx.drawing import Theme, read_chart, read_color
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
            for el, xf in self._shapes(tree, (0, 0, 1, 1)):
                items.extend(self._shape(el, xf, n))
        items.sort(key=lambda it: it[0])
        out = [b for _, blocks in items for b in blocks]
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
        key = (0 if kind in _TITLE else 1, round(box[1] / 4) if box else 9999, box[0] if box else 0)
        if name == "pic":
            fig = self._picture(el, n, box)
            return [(key, [fig])] if fig else []
        if name == "graphicFrame":
            return [(key, self._frame(el, n, box))]
        body = el.find(q("p:txBody"))
        has_text = body is not None and any((t.text or "").strip() for t in body.iter(q("a:t")))
        if not has_text:
            if name in ("sp", "cxnSp") and el.find(f".//{q('a:prstGeom')}") is not None and kind is None:
                self._warn("글 없는 도형(장식·선·연결선)은 아직 그리지 않음 (PPTX)")
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
