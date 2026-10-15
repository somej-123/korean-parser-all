"""DOCX 파서: Word 문서를 AI 없이 XML 규칙만으로 IR(Document)로 변환한다."""

from __future__ import annotations

import base64
import mimetypes
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

from ...ir import (
    Anchor, Block, Break, Cell, Document, Drawing, Heading, ImageRef, Image, ListInfo, Meta, Note,
    NoteRef, PageBreak, PageSetup, Paragraph, Provenance, Row, Shape, Tab, Table, Text,
)
from .common import NS, child, emu_to_pt, hex_color, on_off, q, twips_to_pt, wattr
from .drawing import ShapeReader, Theme, read_chart, read_dsp_drawing
from .numbering import Numbering
from .symbols import map_symbol
from .styles import (
    StyleSheet, cell_margins, parse_borders, parse_ppr, parse_rpr, to_borders, to_paragraph_style,
    to_text_style,
)

PARSER_NAME = "docx-native"

_REL_IMAGE = "/image"
_REL_HYPERLINK = "/hyperlink"
_REL_CHART = "/chart"
_REL_DIAGRAM_DRAWING = "/diagramDrawing"
_HYPERLINK_FIELD = re.compile(r'HYPERLINK\s+"([^"]+)"')
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, recover=True)

# 이보다 깊게 중첩된 표는 글자만 꺼낸다 (악의적이거나 손상된 문서의 재귀 폭주 방지)
MAX_TABLE_DEPTH = 32

# 브라우저가 그릴 수 없는 이미지 형식
_UNRENDERABLE = {"image/x-emf", "image/x-wmf", "image/emf", "image/wmf", "image/tiff"}


def parse_docx(path: str | Path) -> Document:
    return DocxParser(path).parse()


class DocxParser:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path)
        self.names = set(self.zip.namelist())
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="docx"))
        self._block_seq = 0
        self._image_by_part: dict[str, str] = {}
        self._note_labels: dict[tuple[str, str], str] = {}
        self._part = "word/document.xml"
        self._rels: dict[str, tuple[str, str, bool]] = {}
        self._table_text: list[tuple[dict, dict]] = []  # 중첩 표마다 표 스타일의 (pPr, rPr)
        self._para_ctx: dict[str, tuple[str | None, bool]] = {}  # 블록 id → (스타일, 같은 스타일 간격 무시)

        self.styles = StyleSheet(self._xml("word/styles.xml"))
        self.numbering = Numbering(self._xml("word/numbering.xml"))
        self.theme = Theme(self._xml("word/theme/theme1.xml"))
        self.shapes = ShapeReader(self.theme, self._read_text_box, self._image_id)

    # ------------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------------

    def parse(self) -> Document:
        doc = self.doc
        self._read_meta()
        _, rpr = self.styles.paragraph_props(None)
        doc.default_text_style = to_text_style(rpr) or doc.default_text_style
        self._default_rpr = rpr

        root = self._xml("word/document.xml")
        if root is None:
            raise ValueError(f"{self.path.name}: word/document.xml이 없습니다")
        self._enter_part("word/document.xml")
        body = child(root, "w:body")
        doc.blocks = self._blocks(body)

        sect = child(body, "w:sectPr")
        self._read_section(sect)
        self._read_notes()
        self.zip.close()
        return doc

    # ------------------------------------------------------------------
    # 패키지 (zip, rels)
    # ------------------------------------------------------------------

    def _xml(self, name: str):
        if name not in self.names:
            return None
        return etree.fromstring(self.zip.read(name), _XML_PARSER)

    def _enter_part(self, part: str) -> None:
        self._part = part
        self._rels = self._load_rels(part)

    def _load_rels(self, part: str) -> dict[str, tuple[str, str, bool]]:
        rels_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        root = self._xml(rels_name)
        out = {}
        if root is None:
            return out
        for rel in root.findall(q("rel:Relationship")):
            target, mode = rel.get("Target", ""), rel.get("TargetMode")
            external = mode == "External"
            if not external:
                if target.startswith("/"):
                    target = target.lstrip("/")
                else:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
            out[rel.get("Id")] = (rel.get("Type", ""), target, external)
        return out

    def _new_id(self) -> str:
        self._block_seq += 1
        return f"b{self._block_seq}"

    def _prov(self) -> Provenance:
        return Provenance(parser=PARSER_NAME)

    def _warn(self, msg: str) -> None:
        if msg not in self.doc.warnings:
            self.doc.warnings.append(msg)

    # ------------------------------------------------------------------
    # 메타데이터, 구역, 각주
    # ------------------------------------------------------------------

    def _read_meta(self) -> None:
        core = self._xml("docProps/core.xml")
        if core is None:
            return
        m = self.doc.meta

        def text(tag):
            el = core.find(q(tag))
            return el.text.strip() if el is not None and el.text and el.text.strip() else None

        m.title = text("dc:title")
        m.author = text("dc:creator")
        m.created = text("dcterms:created")
        m.modified = text("dcterms:modified")
        m.language = text("dc:language")

    def _read_section(self, sect) -> None:
        if sect is None:
            return
        page = PageSetup()
        sz, mar = child(sect, "w:pgSz"), child(sect, "w:pgMar")
        if sz is not None:
            page.width_pt = twips_to_pt(wattr(sz, "w")) or page.width_pt
            page.height_pt = twips_to_pt(wattr(sz, "h")) or page.height_pt
        if mar is not None:
            for side in ("top", "bottom", "left", "right"):
                v = twips_to_pt(wattr(mar, side))
                if v is not None:
                    setattr(page, f"margin_{side}_pt", abs(v))
        self.doc.page = page

        # 연속된 한 화면으로 보여주므로 머리말은 첫 페이지 것을 쓴다 (첫 페이지 다르게 설정 시 first)
        wanted = "first" if on_off(child(sect, "w:titlePg")) else "default"
        for kind in ("header", "footer"):
            refs = {wattr(r, "type", "default"): r for r in sect.findall(q(f"w:{kind}Reference"))}
            ref = refs.get(wanted)
            if ref is None:
                ref = refs.get("default")
            if ref is not None:
                rel = self._rels.get(ref.get(f"{{{NS['r']}}}id"))
                root = self._xml(rel[1]) if rel else None
                if root is not None:
                    saved = (self._part, self._rels)
                    self._enter_part(rel[1])
                    setattr(self.doc, kind, self._blocks(root))
                    self._part, self._rels = saved

    def _read_notes(self) -> None:
        for kind, part in (("footnote", "word/footnotes.xml"), ("endnote", "word/endnotes.xml")):
            root = self._xml(part)
            if root is None:
                continue
            self._enter_part(part)
            for note in root.findall(q(f"w:{kind}")):
                nid = wattr(note, "id")
                label = self._note_labels.get((kind, nid))
                if label is None:
                    continue  # 본문에서 참조되지 않은 각주 (구분선 등)
                key = f"{kind[0]}n{nid}"
                self.doc.notes[key] = Note(kind=kind, label=label, blocks=self._blocks(note))

    # ------------------------------------------------------------------
    # 블록
    # ------------------------------------------------------------------

    def _blocks(self, container) -> list[Block]:
        out = self._blocks_raw(container)
        _apply_contextual_spacing(out, self._para_ctx)
        return out

    def _blocks_raw(self, container) -> list[Block]:
        out: list[Block] = []
        if container is None:
            return out
        for el in container:
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag == q("w:p"):
                out.extend(self._paragraph(el))
            elif tag == q("w:tbl"):
                if len(self._table_text) >= MAX_TABLE_DEPTH:
                    self._warn(f"표 중첩이 {MAX_TABLE_DEPTH}단계를 넘어 안쪽 표는 글자만 추출함")
                    text = " ".join(t.text for t in el.iter(q("w:t")) if t.text)
                    out.append(Paragraph(id=self._new_id(), prov=self._prov(), inlines=[Text(text=text)] if text else []))
                else:
                    out.append(self._table(el))
            elif tag in (q("w:sdt"),):
                out.extend(self._blocks_raw(child(el, "w:sdtContent")))
            elif tag in (q("w:customXml"), q("w:ins"), q("w:smartTag")):
                out.extend(self._blocks_raw(el))
            elif tag == q("mc:AlternateContent"):
                choice = el.find(q("mc:Choice"))
                out.extend(self._blocks_raw(choice if choice is not None else el.find(q("mc:Fallback"))))
        return out

    def _read_text_box(self, txbx) -> list[Block]:
        return self._blocks(txbx)

    def _paragraph(self, p) -> list[Block]:
        ppr_el = child(p, "w:pPr")
        direct = parse_ppr(ppr_el)
        style_id = wattr(child(ppr_el, "w:pStyle"), "val")
        style_ppr, style_rpr = self.styles.paragraph_props(style_id)
        if self._table_text:
            # 표 스타일 서식은 기본(바탕글) 스타일보다 우선하고, 직접 지정한 문단 스타일보다는 약하다.
            t_ppr, t_rpr = self._table_text[-1]
            if self.styles.is_default_paragraph_style(style_id):
                style_ppr, style_rpr = {**style_ppr, **t_ppr}, {**style_rpr, **t_rpr}
            else:
                style_ppr, style_rpr = {**t_ppr, **style_ppr}, {**t_rpr, **style_rpr}

        num = direct.get("_num") or style_ppr.get("_num")
        list_info, num_ppr = None, {}
        if num:
            res = self.numbering.next_marker(*num)
            if res:
                marker, ordered, num_ppr = res
                list_info = ListInfo(level=num[1], marker=marker, ordered=ordered, list_id=num[0])
        ppr = {**style_ppr, **num_ppr, **direct}

        before: list[Block] = []
        if on_off(child(ppr_el, "w:pageBreakBefore")):
            before.append(PageBreak(id=self._new_id()))

        inlines, floats = self._inlines(p, style_rpr)
        pstyle = to_paragraph_style(ppr)
        level = self.styles.heading_level(style_id, ppr)
        name = self.styles.name(style_id)

        blocks: list[Block] = list(before)
        has_content = bool(inlines) or not floats
        if has_content:
            block_id = self._new_id()
            self._para_ctx[block_id] = (style_id or self.styles.default_para_style, bool(ppr.get("_contextual")))
            if level is not None and _has_text(inlines):
                blocks.append(Heading(id=block_id, prov=self._prov(), level=level, inlines=inlines,
                                      style=pstyle, style_name=name, list_info=list_info))
            else:
                blocks.append(Paragraph(id=block_id, prov=self._prov(), inlines=inlines,
                                        style=pstyle, style_name=name, list_info=list_info))
        if floats:
            canvas = self._float_canvas(floats)
            if isinstance(canvas, Drawing) and canvas.anchor is not None and canvas.anchor.wrap != "top_bottom":
                # 옆으로 글이 흐르거나 겹치는 개체는 HTML float/절대 위치로 그리므로 문단보다 앞에 둔다
                blocks.insert(len(before), canvas)
            else:
                blocks.append(canvas)
        return blocks

    # ------------------------------------------------------------------
    # 인라인
    # ------------------------------------------------------------------

    def _inlines(self, p, para_rpr: dict):
        out: list = []
        floats: list = []
        state = {"field": 0, "field_link": None, "in_instr": False}
        self._walk_inlines(p, para_rpr, None, out, floats, state)
        return _merge_text(out), floats

    def _walk_inlines(self, el, para_rpr, link, out, floats, state) -> None:
        for kid in el:
            tag = kid.tag if isinstance(kid.tag, str) else ""
            if tag == q("w:r"):
                self._run(kid, para_rpr, link or state["field_link"], out, floats, state)
            elif tag == q("w:hyperlink"):
                rid = kid.get(f"{{{NS['r']}}}id")
                target = None
                if rid and rid in self._rels:
                    target = self._rels[rid][1]
                elif wattr(kid, "anchor"):
                    target = "#" + wattr(kid, "anchor")
                self._walk_inlines(kid, para_rpr, target, out, floats, state)
            elif tag == q("w:sdt"):
                content = child(kid, "w:sdtContent")
                self._walk_inlines(kid if content is None else content, para_rpr, link, out, floats, state)
            elif tag in (q("w:ins"), q("w:smartTag"), q("w:customXml"), q("w:fldSimple"), q("w:bdo"), q("w:dir")):
                self._walk_inlines(kid, para_rpr, link, out, floats, state)
            elif tag == q("mc:AlternateContent"):
                choice = kid.find(q("mc:Choice"))
                self._walk_inlines(choice if choice is not None else kid, para_rpr, link, out, floats, state)
            # w:del(삭제된 변경 내용), w:pPr, 책갈피 등은 무시

    def _run(self, r, para_rpr, link, out, floats, state) -> None:
        rpr_el = child(r, "w:rPr")
        rstyle = wattr(child(rpr_el, "w:rStyle"), "val")
        props = {**para_rpr, **self.styles.run_style_props(rstyle), **parse_rpr(rpr_el)}
        if props.get("_hidden"):
            return
        diff = {k: v for k, v in props.items() if self._default_rpr.get(k) != v}
        style = to_text_style(diff)

        for kid in r:
            tag = kid.tag if isinstance(kid.tag, str) else ""
            if tag == q("w:fldChar"):
                kind = wattr(kid, "fldCharType")
                if kind == "begin":
                    state["in_instr"] = True
                    box = kid.find(f"{q('w:ffData')}/{q('w:checkBox')}")
                    if box is not None:
                        checked = child(box, "w:checked")
                        if checked is None:
                            checked = child(box, "w:default")
                        on = checked is not None and wattr(checked, "val", "1") not in ("0", "false")
                        out.append(Text(text="☒" if on else "☐", style=style, link=link))
                elif kind == "separate":
                    state["in_instr"] = False
                elif kind == "end":
                    state["in_instr"] = False
                    state["field_link"] = None
                continue
            if tag == q("w:instrText"):
                m = _HYPERLINK_FIELD.search(kid.text or "")
                if m:
                    state["field_link"] = m.group(1)
                continue
            if state["in_instr"]:
                continue
            if tag == q("w:t"):
                if kid.text:
                    text = kid.text
                    if props.get("font") and any("\uf000" <= ch <= "\uf0ff" for ch in text):
                        text = "".join(map_symbol(props["font"], ch) for ch in text)
                    out.append(Text(text=text, style=style, link=link))
            elif tag in (q("w:tab"), q("w:ptab")):
                out.append(Tab())
            elif tag == q("w:br"):
                kind = wattr(kid, "type", "textWrapping")
                out.append(Break(kind="page" if kind == "page" else "column" if kind == "column" else "line"))
            elif tag == q("w:cr"):
                out.append(Break())
            elif tag == q("w:noBreakHyphen"):
                out.append(Text(text="-", style=style, link=link))
            elif tag == q("w:sym"):
                out.append(Text(text=_symbol(wattr(kid, "font"), wattr(kid, "char")), style=style, link=link))
            elif tag in (q("w:footnoteReference"), q("w:endnoteReference")):
                kind = "footnote" if "footnote" in tag else "endnote"
                nid = wattr(kid, "id")
                label = self._note_labels.setdefault(
                    (kind, nid), str(sum(1 for k in self._note_labels if k[0] == kind) + 1)
                )
                out.append(NoteRef(note_id=f"{kind[0]}n{nid}", label=label))
            elif tag in (q("w:footnoteRef"), q("w:endnoteRef")):
                continue  # 각주 본문의 번호 자리. 번호는 렌더러가 Note.label로 표시한다.
            elif tag == q("w:drawing"):
                self._drawing(kid, out, floats)
            elif tag in (q("w:pict"), q("w:object")):
                self._vml(kid, out, floats)
            elif tag == q("mc:AlternateContent"):
                choice = kid.find(q("mc:Choice"))
                target = choice if choice is not None else kid.find(q("mc:Fallback"))
                if target is not None:
                    fake_run = etree.Element(q("w:r"))
                    if rpr_el is not None:
                        fake_run.append(rpr_el.__copy__())
                    for c in target:
                        fake_run.append(c.__copy__())
                    self._run(fake_run, para_rpr, link, out, floats, state)

    # ------------------------------------------------------------------
    # 이미지, 도형, 차트
    # ------------------------------------------------------------------

    def _image_id(self, rid: str | None) -> str | None:
        rel = self._rels.get(rid or "")
        if not rel or rel[2]:
            return None
        part = rel[1]
        if part in self._image_by_part:
            return self._image_by_part[part]
        if part not in self.names:
            self._warn(f"이미지 파일 없음: {part}")
            return None
        data = self.zip.read(part)
        mime = mimetypes.guess_type(part)[0] or "application/octet-stream"
        if part.lower().endswith(".emf"):
            mime = "image/x-emf"
        elif part.lower().endswith(".wmf"):
            mime = "image/x-wmf"
        if mime in _UNRENDERABLE:
            self._warn(f"브라우저에서 표시할 수 없는 이미지 형식: {posixpath.basename(part)} ({mime})")
        image_id = f"img{len(self.doc.images) + 1}"
        self.doc.images[image_id] = Image(
            mime=mime, data_base64=base64.b64encode(data).decode("ascii"), name=posixpath.basename(part)
        )
        self._image_by_part[part] = image_id
        return image_id

    def _drawing(self, dr, out, floats) -> None:
        container = child(dr, "wp:inline")
        anchored = container is None
        if anchored:
            container = child(dr, "wp:anchor")
        if container is None:
            return
        ext = child(container, "wp:extent")
        w = emu_to_pt(ext.get("cx")) if ext is not None else None
        h = emu_to_pt(ext.get("cy")) if ext is not None else None
        doc_pr = child(container, "wp:docPr")
        alt = None
        if doc_pr is not None:
            alt = doc_pr.get("descr") or doc_pr.get("title") or None

        data = container.find(f"{q('a:graphic')}/{q('a:graphicData')}")
        if data is None:
            return
        uri = data.get("uri", "")
        item = None
        if uri.endswith("/picture"):
            blip = data.find(f".//{q('a:blip')}")
            image_id = self._image_id(blip.get(f"{{{NS['r']}}}embed")) if blip is not None else None
            if image_id:
                item = ImageRef(image_id=image_id, width_pt=w, height_pt=h, alt=alt)
        elif uri.endswith(("wordprocessingShape", "wordprocessingGroup", "wordprocessingCanvas")):
            el = next(iter(data), None)
            shapes = self.shapes.read(el) if el is not None else []
            if shapes:
                item = Drawing(id=self._new_id(), prov=self._prov(), width_pt=w or 0, height_pt=h or 0,
                               shapes=shapes, alt=alt)
        elif uri.endswith("/chart"):
            ref = data.find(q("c:chart"))
            rel = self._rels.get(ref.get(f"{{{NS['r']}}}id")) if ref is not None else None
            root = self._xml(rel[1]) if rel else None
            if root is not None:
                item = read_chart(root, w, h, self.theme)
                if item is not None:
                    item.id, item.prov = self._new_id(), self._prov()
        elif uri.endswith("/diagram"):
            item = self._smartart(data, w, h, alt)

        if item is None:
            self._warn(f"지원하지 않는 그래픽 요소: {uri.rsplit('/', 1)[-1]}")
            return

        if isinstance(item, ImageRef) and not anchored:
            out.append(item)
            return
        if isinstance(item, ImageRef):
            item = Drawing(id=self._new_id(), prov=self._prov(), width_pt=w or 0, height_pt=h or 0, alt=alt,
                           shapes=[Shape(x=0, y=0, width=w or 0, height=h or 0, image_id=item.image_id)])
        x, y = (_anchor_offset(container, "H"), _anchor_offset(container, "V")) if anchored else (0.0, 0.0)
        if anchored and isinstance(item, Drawing):
            item.anchor = _read_anchor(container)
        floats.append((item, x, y))

    def _smartart(self, data, w, h, alt):
        """SmartArt: Word가 저장해 둔 그리기 결과(diagrams/drawingN.xml)를 도형으로 읽는다."""
        rel_ids = data.find(f"{{http://schemas.openxmlformats.org/drawingml/2006/diagram}}relIds")
        if rel_ids is None:
            return None
        dm = self._rels.get(rel_ids.get(f"{{{NS['r']}}}dm"))
        drawing_part = None
        if dm:
            data_root = self._xml(dm[1])
            if data_root is not None:
                ext = data_root.find(".//{http://schemas.microsoft.com/office/drawing/2008/diagram}dataModelExt")
                if ext is not None and ext.get("relId") in self._rels:
                    drawing_part = self._rels[ext.get("relId")][1]
        if drawing_part is None:
            drawing_part = next((t for (typ, t, _) in self._rels.values() if typ.endswith(_REL_DIAGRAM_DRAWING)), None)
        root = self._xml(drawing_part) if drawing_part else None
        if root is None:
            return None
        shapes = read_dsp_drawing(root, self.theme)
        if not shapes:
            return None
        return Drawing(id=self._new_id(), prov=self._prov(), width_pt=w or 0, height_pt=h or 0, shapes=shapes, alt=alt)

    def _vml(self, pict, out, floats) -> None:
        """구형 VML 그림/글상자 (w:pict, w:object)."""
        for el in pict.iter():
            if not isinstance(el.tag, str):
                continue
            if el.tag == f"{{{NS['v']}}}imagedata":
                image_id = self._image_id(el.get(f"{{{NS['r']}}}id"))
                if image_id:
                    parent = el.getparent()
                    w, h = _vml_size(parent.get("style", "") if parent is not None else "")
                    out.append(ImageRef(image_id=image_id, width_pt=w, height_pt=h))
                return
        txbx = pict.find(f".//{q('w:txbxContent')}")
        if txbx is not None:
            shape_el = txbx.getparent().getparent() if txbx.getparent() is not None else None
            w, h = _vml_size(shape_el.get("style", "") if shape_el is not None else "")
            fill = hex_color((shape_el.get("fillcolor") or "").split()[0]) if shape_el is not None and shape_el.get("fillcolor") else None
            shape = Shape(x=0, y=0, width=w or 200, height=h or 40, fill=fill, stroke="#000000",
                          text_blocks=self._blocks(txbx), text_v_align="top")
            floats.append((Drawing(id=self._new_id(), prov=self._prov(), width_pt=shape.width,
                                   height_pt=shape.height, shapes=[shape]), 0.0, 0.0))

    def _float_canvas(self, floats) -> Block:
        """한 문단에 떠 있는 개체들을 위치를 살려 하나의 캔버스로 합친다."""
        if len(floats) == 1 and not isinstance(floats[0][0], Drawing):
            return floats[0][0]
        drawings = [(d, x, y) for d, x, y in floats if isinstance(d, Drawing)]
        others = [d for d, _, _ in floats if not isinstance(d, Drawing)]
        if others and not drawings:
            return others[0]
        if len(drawings) == 1 and not others:
            return drawings[0][0]
        min_x = min(x for _, x, _ in drawings)
        min_y = min(y for _, _, y in drawings)
        shapes: list[Shape] = []
        width = height = 0.0
        for d, x, y in drawings:
            dx, dy = x - min_x, y - min_y
            for s in d.shapes:
                shapes.append(s.model_copy(update={"x": s.x + dx, "y": s.y + dy}))
            width = max(width, dx + d.width_pt)
            height = max(height, dy + d.height_pt)
        for o in others:
            self._warn(f"떠 있는 개체 일부를 캔버스에 합치지 못함: {o.type}")
        return Drawing(id=self._new_id(), prov=self._prov(), width_pt=width, height_pt=height, shapes=shapes)

    # ------------------------------------------------------------------
    # 표
    # ------------------------------------------------------------------

    def _table(self, tbl) -> Table:
        tblpr = child(tbl, "w:tblPr")
        style_id = wattr(child(tblpr, "w:tblStyle"), "val")
        style_borders, style_margins = self.styles.table_props(style_id)
        borders = {**style_borders, **parse_borders(child(tblpr, "w:tblBorders"))}
        margins = {"top": 0.0, "bottom": 0.0, "left": 5.4, "right": 5.4, **style_margins}
        mar_el = child(tblpr, "w:tblCellMar")
        if mar_el is not None:
            margins.update(cell_margins(mar_el))

        grid = [twips_to_pt(wattr(g, "w")) or 0.0 for g in tbl.findall(f"{q('w:tblGrid')}/{q('w:gridCol')}")]
        table = Table(id=self._new_id(), prov=self._prov(), col_widths_pt=grid,
                      cell_padding_pt=(margins["top"], margins["right"], margins["bottom"], margins["left"]))
        tbl_w = child(tblpr, "w:tblW")
        if tbl_w is not None and wattr(tbl_w, "type") == "dxa":
            table.width_pt = twips_to_pt(wattr(tbl_w, "w"))
        jc = wattr(child(tblpr, "w:jc"), "val")
        table.align = {"center": "center", "right": "right", "end": "right"}.get(jc or "", None)

        self._table_text.append(self.styles.table_text_props(style_id))
        try:
            return self._table_body(tbl, table, borders, grid)
        finally:
            self._table_text.pop()

    def _table_body(self, tbl, table: Table, borders: dict, grid: list[float]) -> Table:
        rows_raw = []  # [(row, [(col, colspan, vmerge, tc)])]
        row_borders: list[dict] = []  # 행마다 적용할 표 테두리 (tblPrEx로 행별 재정의 가능)
        for tr in _iter_rows(tbl):
            trpr = child(tr, "w:trPr")
            row = Row()
            h = child(trpr, "w:trHeight")
            if h is not None:
                row.height_pt = twips_to_pt(wattr(h, "val"))
            row.header = bool(on_off(child(trpr, "w:tblHeader")))
            ex = child(tr, "w:tblPrEx")
            row_borders.append({**borders, **parse_borders(child(ex, "w:tblBorders"))})
            col = int(wattr(child(trpr, "w:gridBefore"), "val", "0") or 0)
            cells = []
            for tc in _iter_cells(tr):
                tcpr = child(tc, "w:tcPr")
                span = int(wattr(child(tcpr, "w:gridSpan"), "val", "1") or 1)
                vm_el = child(tcpr, "w:vMerge")
                vmerge = None if vm_el is None else (wattr(vm_el, "val") or "continue")
                cells.append((col, span, vmerge, tc))
                col += span
            rows_raw.append((row, cells))

        ncols = max([len(grid)] + [c[0] + c[1] for _, cells in rows_raw for c in cells] or [0])
        if len(table.col_widths_pt) < ncols:
            table.col_widths_pt += [0.0] * (ncols - len(table.col_widths_pt))

        origin_at: dict[int, Cell] = {}  # 열 → 세로 병합 시작 셀
        origin_pos: dict[int, tuple[int, int, int]] = {}
        placed: list[tuple[Cell, int, int, int]] = []  # (cell, row, col, span)
        for r_idx, (row, cells) in enumerate(rows_raw):
            for col, span, vmerge, tc in cells:
                if vmerge == "continue" and col in origin_at:
                    origin_at[col].row_span += 1
                    continue
                cell = self._cell(tc, span)
                row.cells.append(cell)
                placed.append((cell, r_idx, col, span))
                for c in range(col, col + span):
                    origin_at.pop(c, None)
                if vmerge == "restart":
                    origin_at[col] = cell
            table.rows.append(row)

        nrows = len(table.rows)
        for cell, r_idx, col, span in placed:
            if cell.borders is not None and all(
                getattr(cell.borders, s) is not None for s in ("top", "bottom", "left", "right")
            ):
                continue
            rb = row_borders[r_idx]
            resolved = {
                "top": rb.get("top") if r_idx == 0 else rb.get("insideH"),
                "bottom": rb.get("bottom") if r_idx + cell.row_span >= nrows else rb.get("insideH"),
                "left": rb.get("left") if col == 0 else rb.get("insideV"),
                "right": rb.get("right") if col + span >= ncols else rb.get("insideV"),
            }
            if cell.borders is not None:
                for side in ("top", "bottom", "left", "right"):
                    if getattr(cell.borders, side) is not None:
                        resolved[side] = getattr(cell.borders, side)
            cell.borders = to_borders(resolved)
        return table

    def _cell(self, tc, span: int) -> Cell:
        tcpr = child(tc, "w:tcPr")
        cell = Cell(col_span=span, blocks=self._blocks(tc))
        shd = child(tcpr, "w:shd")
        if shd is not None:
            cell.background = hex_color(wattr(shd, "fill"))
        va = wattr(child(tcpr, "w:vAlign"), "val")
        cell.v_align = {"top": "top", "center": "middle", "bottom": "bottom"}.get(va or "", None)
        direct = parse_borders(child(tcpr, "w:tcBorders"), names=("top", "bottom", "left", "right"))
        cell.borders = to_borders(direct)
        return cell


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------


def _iter_rows(tbl):
    for el in tbl:
        if el.tag == q("w:tr"):
            yield el
        elif el.tag in (q("w:sdt"), q("w:customXml")):
            content = child(el, "w:sdtContent") if el.tag == q("w:sdt") else el
            if content is not None:
                yield from (r for r in content if r.tag == q("w:tr"))


def _iter_cells(tr):
    for el in tr:
        if el.tag == q("w:tc"):
            yield el
        elif el.tag in (q("w:sdt"), q("w:customXml")):
            content = child(el, "w:sdtContent") if el.tag == q("w:sdt") else el
            if content is not None:
                yield from (c for c in content if c.tag == q("w:tc"))


def _position(anchor, axis: str):
    """wp:positionH/V. mc:AlternateContent 안에 들어 있는 경우도 찾는다 (posOffset이 있는 쪽 우선)."""
    found = None
    for pos in anchor.iter(q(f"wp:position{axis}")):
        if child(pos, "wp:posOffset") is not None or child(pos, "wp:align") is not None:
            return pos
        found = found if found is not None else pos
    return found


def _anchor_offset(anchor, axis: str) -> float:
    off = child(_position(anchor, axis), "wp:posOffset")
    if off is not None and off.text:
        return emu_to_pt(off.text.strip()) or 0.0
    return 0.0


_WRAPS = {"wrapSquare": "square", "wrapTight": "tight", "wrapThrough": "through",
          "wrapTopAndBottom": "top_bottom", "wrapNone": "none"}


def _read_anchor(anchor) -> Anchor:
    wrap = "square"
    for el in anchor:
        name = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
        if name in _WRAPS:
            wrap = _WRAPS[name]
            break
    out = Anchor(wrap=wrap, behind_text=anchor.get("behindDoc") in ("1", "true"))
    for axis in ("H", "V"):
        pos = _position(anchor, axis)
        if pos is None:
            continue
        rel = pos.get("relativeFrom", "column" if axis == "H" else "paragraph")
        off, align = child(pos, "wp:posOffset"), child(pos, "wp:align")
        value = emu_to_pt(off.text.strip()) if off is not None and off.text else None
        al = align.text.strip() if align is not None and align.text else None
        if axis == "H":
            out.relative_h, out.x_pt, out.align_h = rel, value, al
        else:
            out.relative_v, out.y_pt, out.align_v = rel, value, al
    return out


def _vml_size(style: str) -> tuple[float | None, float | None]:
    props = dict(
        (k.strip().lower(), v.strip()) for k, _, v in (p.partition(":") for p in style.split(";")) if k.strip()
    )
    return _css_len(props.get("width")), _css_len(props.get("height"))


def _css_len(v: str | None) -> float | None:
    if not v:
        return None
    m = re.match(r"([\d.]+)\s*(pt|px|in|cm|mm)?", v)
    if not m:
        return None
    n = float(m.group(1))
    return n * {"pt": 1, "px": 0.75, "in": 72, "cm": 28.3465, "mm": 2.83465, None: 1 / 12700}[m.group(2)]


def _symbol(font: str | None, char: str | None) -> str:
    if not char:
        return ""
    try:
        cp = int(char, 16)
    except ValueError:
        return ""
    if cp < 0xF000:
        cp += 0xF000
    return map_symbol(font or "Symbol", chr(cp))


def _has_text(inlines) -> bool:
    return any(isinstance(i, Text) and i.text.strip() for i in inlines)


def _merge_text(inlines: list) -> list:
    """서식과 링크가 같은 이웃 텍스트 조각을 합친다 (Word는 글자 단위로 쪼개 저장하는 경우가 많다)."""
    out: list = []
    for i in inlines:
        if out and isinstance(i, Text) and isinstance(out[-1], Text) and i.style == out[-1].style and i.link == out[-1].link:
            out[-1] = Text(text=out[-1].text + i.text, style=i.style, link=i.link)
        else:
            out.append(i)
    return out


def _apply_contextual_spacing(blocks: list, ctx: dict) -> None:
    """'같은 스타일의 문단 사이에 공백 추가 안 함'(contextualSpacing): 이웃 문단 스타일이 같으면 간격을 없앤다."""
    for a, b in zip(blocks, blocks[1:]):
        ca, cb = ctx.get(getattr(a, "id", "")), ctx.get(getattr(b, "id", ""))
        if not ca or not cb or ca[0] != cb[0]:
            continue
        if ca[1] and a.style is not None and a.style.space_after_pt:
            a.style = a.style.model_copy(update={"space_after_pt": 0.0})
        if cb[1] and b.style is not None and b.style.space_before_pt:
            b.style = b.style.model_copy(update={"space_before_pt": 0.0})
