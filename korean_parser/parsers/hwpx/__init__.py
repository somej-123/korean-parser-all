"""HWPX 파서: 한글(OWPML, KS X 6101) 문서를 AI 없이 XML 규칙만으로 IR(Document)로 변환한다.

패키지 구조
    Contents/content.hpf   목록(manifest): 섹션 파일 순서, 이미지(BinData) 경로, 메타데이터
    Contents/header.xml    글꼴, 글자·문단 모양, 테두리, 번호, 스타일
    Contents/section0.xml  본문 (섹션마다 하나)
    BinData/*              이미지 등 바이너리
"""

from __future__ import annotations

import base64
import mimetypes
import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

from ...ir import (
    Anchor, Block, Break, Cell, Document, Drawing, Equation, Heading, ImageRef, Image, ListInfo, Meta,
    Note, NoteRef, PageBreak, PageSetup, Paragraph, Provenance, Row, Tab, Table, Text, TextStyle, walk_blocks,
)
from .header import Counters, Header, color, format_hwp_number, hwp_pt, kid, kids, ln, shape_comment
from ..docx.symbols import map_symbol
from .shapes import DRAWING_TAGS, ShapeReader

PARSER_NAME = "hwpx-native"
MAX_TABLE_DEPTH = 32
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, recover=True)
_UNRENDERABLE = {"image/x-emf", "image/x-wmf", "image/emf", "image/wmf", "image/tiff"}

_WRAP = {"SQUARE": "square", "TIGHT": "tight", "THROUGH": "through", "TOP_AND_BOTTOM": "top_bottom",
         "BEHIND_TEXT": "none", "IN_FRONT_OF_TEXT": "none"}
_REL_H = {"PAPER": "page", "PAGE": "margin", "COLUMN": "column", "PARA": "paragraph"}
_REL_V = {"PAPER": "page", "PAGE": "margin", "PARA": "paragraph", "LINE": "line"}


def parse_hwpx(path: str | Path) -> Document:
    return HwpxParser(path).parse()


class _Slot(list):
    """문단 앞·뒤에 놓을 블록 목록. 넣을 때 문단 글의 어디(인라인 몇 개 뒤)였는지도 기억한다 (`at`)."""

    def __init__(self, inlines: list):
        super().__init__()
        self.inlines, self.at = inlines, []

    def append(self, b) -> None:
        super().append(b)
        self.at.append(len(self.inlines))


def _strip_ends(inlines: list) -> list:
    """맨 앞 글의 앞 공백과 맨 뒤 글의 뒤 공백을 지운다."""
    out = list(inlines)
    if out and isinstance(out[0], Text):
        out[0] = out[0].model_copy(update={"text": out[0].text.lstrip()})
    if out and isinstance(out[-1], Text):
        out[-1] = out[-1].model_copy(update={"text": out[-1].text.rstrip()})
    return [i for i in out if not (isinstance(i, Text) and not i.text)]


def _has_text(inlines) -> bool:
    return any(isinstance(i, Text) and i.text.strip() for i in inlines)


class HwpxParser:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path)
        self.names = set(self.zip.namelist())
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="hwpx"))
        self._seq = 0
        self._hf_seen: dict[str, set[str]] = {}  # 머리말·꼬리말 → 이미 담은 글
        self._manifest: dict[str, tuple[str, str]] = {}  # id → (경로, media-type)
        self._image_by_ref: dict[str, str] = {}
        self._table_depth = 0
        self._outline_ref = "1"
        self._page_set = False
        self.header = Header(self._xml("Contents/header.xml"))
        self.counters = Counters(self.header)
        self.shapes = ShapeReader(self._sublist_blocks, self._image_id)

    # ------------------------------------------------------------------

    def parse(self) -> Document:
        sections = self._read_manifest()
        style0 = self.header.styles.get("0")
        if style0 is not None and style0.char_ref in self.header.char_styles:
            self.doc.default_text_style = self.header.char_styles[style0.char_ref]
        for part in sections:
            root = self._xml(part)
            if root is None:
                self._warn(f"섹션 파일 없음: {part}")
                continue
            self.doc.blocks.extend(self._paragraphs(root))
        # 바탕쪽(Contents/masterpageN.xml): 쪽마다 되풀이되는 글이라 머리말 뒤에 담는다 (같은 글은 한 번)
        pages = [n for n in getattr(self, "names", []) if re.match(r"Contents/masterpage\d+\.xml$", n)]
        for name in sorted(pages, key=lambda n: int(re.sub(r"\D", "", n))):
            sub = kid(self._xml(name), "subList")
            if sub is not None:
                self._master_page(sub)
        self.zip.close()
        return self.doc

    def _master_page(self, sub) -> None:
        """바탕쪽의 글 → 머리말 뒤 (style_name='master_page'). 쪽마다 되풀이되는 글이라 본문에는 넣지 않는다.
        v1.0에서는 읽지 않고 경고만 남겼다. 글이 없는 바탕쪽(쪽 테두리 선, 그림만)은 넣지 않는다."""
        blocks = self._paragraphs(sub)
        text = " ".join(b.text for b in walk_blocks(blocks) if isinstance(b, (Paragraph, Heading))).strip()
        seen = self._hf_seen.setdefault("header", set())
        if not text or text in seen:
            return
        seen.add(text)
        for b in blocks:
            if isinstance(b, Paragraph):
                b.style_name = "master_page"
        self.doc.header.extend(blocks)

    def _xml(self, name: str):
        if name not in self.names:
            return None
        return etree.fromstring(self.zip.read(name), _XML_PARSER)

    def _new_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _prov(self) -> Provenance:
        return Provenance(parser=PARSER_NAME)

    def _warn(self, msg: str) -> None:
        if msg not in self.doc.warnings:
            self.doc.warnings.append(msg)

    def _read_manifest(self) -> list[str]:
        hpf = self._xml("Contents/content.hpf")
        sections: list[str] = []
        if hpf is not None:
            for el in hpf.iter():
                name = ln(el)
                if name == "item":
                    href = el.get("href", "")
                    self._manifest[el.get("id", "")] = (href, el.get("media-type", ""))
                elif name == "itemref":
                    item = self._manifest.get(el.get("idref", ""))
                    if item and item[0].startswith("Contents/section"):
                        sections.append(item[0])
                elif name == "title" and el.text:
                    self.doc.meta.title = el.text.strip() or None
                elif name == "language" and el.text:
                    self.doc.meta.language = el.text.strip() or None
                elif name == "meta":
                    key, val = el.get("name"), (el.text or "").strip() or None
                    if key == "creator":
                        self.doc.meta.author = val
                    elif key == "CreatedDate":
                        self.doc.meta.created = val
                    elif key == "ModifiedDate":
                        self.doc.meta.modified = val
        if not sections:  # 목록이 없거나 손상된 경우
            sections = sorted((n for n in self.names if n.startswith("Contents/section") and n.endswith(".xml")),
                              key=lambda n: int("".join(ch for ch in n if ch.isdigit()) or 0))
        return sections

    # ------------------------------------------------------------------
    # 문단
    # ------------------------------------------------------------------

    def _paragraphs(self, container) -> list[Block]:
        out: list[Block] = []
        for p in container:
            if ln(p) == "p":
                out.extend(self._paragraph(p))
        return out

    def _sublist_blocks(self, sub) -> list[Block]:
        return self._paragraphs(sub)

    def _paragraph(self, p) -> list[Block]:
        pdef = self.header.para_defs.get(p.get("paraPrIDRef", ""))
        style_ref = p.get("styleIDRef", "0")
        style_def = self.header.styles.get(style_ref)

        state = {"link": None, "deleted": False, "highlight": None}
        inlines: list = []
        after = _Slot(inlines)  # 문단 뒤에 놓을 표·도식
        before = _Slot(inlines)  # 문단 앞에 놓을 떠 있는 개체
        for run in p:
            if ln(run) == "run":
                self._run(run, inlines, before, after, state)

        blocks: list[Block] = []
        if p.get("pageBreak") == "1" or (pdef is not None and pdef.page_break_before):
            blocks.append(PageBreak(id=self._new_id()))

        list_info, heading_level = None, None
        if pdef is not None and pdef.heading_type != "NONE":
            list_info, heading_level = self._numbering(pdef)

        # 글 가운데에 놓인 개체(앞뒤 모두 글이 있는 글상자·표)는 그 자리에서 문단을 나눈다. 한컴 미리보기처럼
        # 놓인 자리에서 읽힌다 ('앞 글' 문단, 개체, '뒤 글' 문단). 나누지 않으면 문단 앞뒤로 밀려 읽는 순서가 달라졌다
        # (hwpxlib RectInPara 미리보기 재현율 0.78). 제목 문단은 나누지 않는다
        mid = [] if heading_level is not None else sorted(
            ((at, i, b) for slot in (before, after) for i, (b, at) in enumerate(zip(slot, slot.at))
             if _has_text(inlines[:at]) and _has_text(inlines[at:])), key=lambda x: (x[0], x[1]))
        if mid:
            style = pdef.style if pdef else None
            name = style_def.name if style_def else None
            used = {id(b) for _, _, b in mid}
            blocks.extend(b for b in before if id(b) not in used)
            start = 0
            for k, (at, _, b) in enumerate(mid + [(len(inlines), 0, None)]):
                part = _strip_ends(_merge(inlines[start:at]))  # 나눈 자리 앞뒤의 공백은 문단 경계로 사라진다
                if _has_text(part):
                    blocks.append(Paragraph(id=self._new_id(), prov=self._prov(), inlines=part, style=style,
                                            style_name=name, list_info=list_info if k == 0 else None))
                if b is not None:
                    blocks.append(b)
                start = at
            blocks.extend(b for b in after if id(b) not in used)
            return blocks
        blocks.extend(before)

        inlines = _merge(inlines)
        has_text = any(isinstance(i, Text) and i.text.strip() for i in inlines)
        if inlines or not (before or after):
            style = pdef.style if pdef else None
            name = style_def.name if style_def else None
            # 글자·숫자가 없는 개요 문단('…' 자리표시)은 제목으로 내지 않는다
            if heading_level is not None and has_text and any(
                    isinstance(i, Text) and re.search(r"\w", i.text) for i in inlines):
                blocks.append(Heading(id=self._new_id(), prov=self._prov(), level=heading_level, inlines=inlines,
                                      style=style, style_name=name, list_info=list_info))
            else:
                blocks.append(Paragraph(id=self._new_id(), prov=self._prov(), inlines=inlines, style=style,
                                        style_name=name, list_info=list_info))
        blocks.extend(after)
        return blocks

    def _numbering(self, pdef) -> tuple[ListInfo | None, int | None]:
        kind, level = pdef.heading_type, pdef.heading_level
        if kind == "BULLET":
            char = self.header.bullets.get(pdef.heading_ref, "•") or "•"
            if "\uf000" <= char <= "\uf0ff":
                char = map_symbol("Wingdings", char)  # 기호 글꼴 문자 (● ■ ◆ ➢ ✓ 등)
            elif "\ue000" <= char <= "\uf8ff":
                char = "•"  # 알 수 없는 사용자 정의 문자
            return ListInfo(level=level, marker=char, ordered=False, list_id=f"bullet{pdef.heading_ref}"), None
        ref = self._outline_ref if kind == "OUTLINE" else pdef.heading_ref
        marker = self.counters.next(ref, level) or ""
        info = ListInfo(level=level, marker=marker, ordered=True, list_id=f"num{ref}") if marker else None
        if kind == "OUTLINE":
            return info, min(level + 1, 6)  # 개요 문단 = 문서 구조상의 제목
        return info, None

    def _run(self, run, inlines, before, after, state) -> None:
        style = self._char_style(run.get("charPrIDRef", "0"))
        for el in run:
            name = ln(el)
            if name == "t":
                self._text(el, style, inlines, state)
            elif name == "secPr":
                self._section(el)
            elif name == "ctrl":
                self._ctrl(el, inlines, state, style)
            elif name == "tbl":
                after.append(self._table(el))
                self._caption(el, after)
            elif name == "equation":
                script = kid(el, "script")
                if script is not None and script.text:
                    inlines.append(Equation(script=script.text.strip()))
                self._caption(el, after)
            elif name == "switch":
                self._switch(el, style, inlines, before, after, state)
            elif name in DRAWING_TAGS or name in ("chart", "ole"):
                self._object(el, inlines, before, after)
            elif name in ("btn", "checkBtn", "radioBtn"):
                caption = el.get("caption") or ""
                mark = {"checkBtn": "☐ ", "radioBtn": "○ "}.get(name, "")
                if caption or mark:
                    inlines.append(Text(text=f"{mark}{caption}", style=style))
            elif name in ("comboBox", "edit", "listBox"):
                text = el.get("selectedValue") or "".join(t.text or "" for t in el.iter() if ln(t) == "text")
                inlines.append(Text(text=f"[{text}]" if text else "[ ]", style=style))
            elif name == "dutmal":  # 덧말: 본문(덧말)
                main, sub = kid(el, "mainText"), kid(el, "subText")
                txt = (main.text if main is not None else "") or ""
                if sub is not None and sub.text:
                    txt += f"({sub.text})"
                inlines.append(Text(text=txt, style=style))
            elif name == "compose":
                inlines.append(Text(text=el.get("composeText", ""), style=style))
            elif name in ("video",):
                self._warn("동영상 개체는 표시하지 않음")

    def _switch(self, el, style, inlines, before, after, state) -> None:
        """hp:switch: 최신 개체(차트 등)를 지원하면 case를, 아니면 default(호환용 OLE 등)를 쓴다."""
        case, default = kid(el, "case"), kid(el, "default")
        if case is not None and any(ln(c) in {"chart"} | DRAWING_TAGS for c in case):
            target = case
        else:
            target = default if default is not None else case
        if target is None:
            return
        fake = etree.Element("run")
        fake.set("charPrIDRef", "0")
        for c in target:
            fake.append(c.__copy__())
        self._run(fake, inlines, before, after, state)

    def _char_style(self, ref: str) -> TextStyle | None:
        """글자 모양 중 문서 기본(바탕글)과 다른 값만 남긴다."""
        style = self.header.char_styles.get(ref)
        if style is None:
            return None
        base = self.doc.default_text_style
        diff = {k: v for k, v in style.model_dump().items() if v is not None and getattr(base, k) != v}
        return TextStyle(**diff) if diff else None

    def _text(self, t, style, inlines, state) -> None:
        def add(s: str | None):
            if s and not state["deleted"]:
                st = style
                if state["highlight"]:  # 형광펜
                    st = (style or TextStyle()).model_copy(update={"background": state["highlight"]})
                inlines.append(Text(text=s, style=st, link=state["link"]))

        add(t.text)
        for c in t:
            name = ln(c)
            if name == "tab":
                inlines.append(Tab())
            elif name == "lineBreak":
                inlines.append(Break())
            elif name == "nbSpace":
                add(" ")
            elif name == "fwSpace":
                add("　")
            elif name == "hyphen":
                add("-")
            elif name == "markpenBegin":
                state["highlight"] = color(c.get("color")) or "#FFFF00"
            elif name == "markpenEnd":
                state["highlight"] = None
            elif name == "deleteBegin":
                state["deleted"] = True
            elif name == "deleteEnd":
                state["deleted"] = False
            add(c.tail)

    def _ctrl(self, ctrl, inlines, state, style=None) -> None:
        for c in ctrl:
            name = ln(c)
            if name == "fieldBegin" and c.get("type") == "HYPERLINK":
                cmd = next((p.text for p in c.iter() if ln(p) == "stringParam" and p.get("name") == "Command"), "")
                url = (cmd or "").replace("\\:", ":").replace("\\\\", "\\").split(";")[0].strip()
                state["link"] = url or None
            elif name == "fieldBegin" and c.get("type") == "MEMO":
                # 한글 메모: 필드 안의 subList가 메모 글이다. 본문이 아니므로 Note(kind="comment")로 두고 메모가 붙은
                # 자리에 NoteRef (XLSX 칸 메모와 같은 모양. v1.0에서는 경고만 남겼다)
                sub = kid(c, "subList")
                blocks = self._paragraphs(sub) if sub is not None else []
                if blocks:
                    k = sum(1 for n in self.doc.notes.values() if n.kind == "comment") + 1
                    key, label = f"cm{k}", f"메모{k}"
                    self.doc.notes[key] = Note(kind="comment", label=label, blocks=blocks)
                    inlines.append(NoteRef(note_id=key, label=label))
            elif name == "hiddenComment":
                # 숨은 설명: 인쇄되지 않는 설명 글. 메모처럼 Note(kind="comment")와 붙은 자리의 NoteRef (v1.0에서는 경고만)
                sub = kid(c, "subList")
                blocks = self._paragraphs(sub) if sub is not None else []
                if any(b.text.strip() for b in walk_blocks(blocks) if isinstance(b, (Paragraph, Heading))):
                    k = sum(1 for key in self.doc.notes if key.startswith("hc")) + 1
                    key, label = f"hc{k}", f"숨은 설명{k}"
                    self.doc.notes[key] = Note(kind="comment", label=label, blocks=blocks)
                    inlines.append(NoteRef(note_id=key, label=label))
            elif name == "autoNum" and c.get("numType") in ("PICTURE", "TABLE", "EQUATION"):
                # 캡션의 자동 번호 ('그림 1'의 1). 글이 아니라 컨트롤이라 예전에는 번호가 빠졌다 (hwpxlib SimpleRectangle).
                # 쪽 번호·각주 번호는 넣지 않는다 (쪽마다 다른 값, 각주 번호는 NoteRef 이름표)
                fmt = kid(c, "autoNumFormat")
                kind = fmt.get("type", "DIGIT") if fmt is not None else "DIGIT"
                try:
                    num = int(c.get("num", "0"))
                except ValueError:
                    num = 0
                user = (fmt.get("userChar") or "") if fmt is not None else ""
                body = user if kind == "USER_CHAR" and user else format_hwp_number(num, kind) if num > 0 else ""
                if body:
                    pre, suf = ((fmt.get("prefixChar") or "", fmt.get("suffixChar") or "") if fmt is not None
                                else ("", ""))
                    inlines.append(Text(text=f"{pre}{body}{suf}", style=style, link=state["link"]))
            elif name == "fieldEnd":
                state["link"] = None
            elif name in ("header", "footer"):
                # 첫 양쪽·홀수 쪽 머리말(꼬리말)이 화면용이다. 짝수 쪽이나 다른 구역의 머리말도 글이 다르면 뒤에 함께 담는다
                # (예전에는 버려서 짝수 쪽 머리말 글이 경고 없이 빠졌다: benchmarks/goal/samples의 한글 표본)
                kind = name
                sub = kid(c, "subList")
                if sub is None:
                    continue
                blocks = self._paragraphs(sub)
                text = " ".join(b.text for b in walk_blocks(blocks) if isinstance(b, Paragraph)).strip()
                seen = self._hf_seen.setdefault(kind, set())
                if not getattr(self.doc, kind) and c.get("applyPageType", "BOTH") in ("BOTH", "ODD"):
                    setattr(self.doc, kind, blocks)
                    seen.add(text)
                elif text and text not in seen:
                    getattr(self.doc, kind).extend(blocks)
                    seen.add(text)
            elif name in ("footNote", "endNote"):
                kind = "footnote" if name == "footNote" else "endnote"
                label = str(sum(1 for n in self.doc.notes.values() if n.kind == kind) + 1)
                key = f"{kind[0]}n{label}"
                sub = kid(c, "subList")
                self.doc.notes[key] = Note(kind=kind, label=label, blocks=self._paragraphs(sub) if sub is not None else [])
                inlines.append(NoteRef(note_id=key, label=label))

    def _section(self, sec) -> None:
        self._outline_ref = sec.get("outlineShapeIDRef", self._outline_ref) or self._outline_ref
        for mp in kids(sec, "masterPage"):  # HWP 바이너리에서 옮긴 바탕쪽 (HWPX는 idRef만 있고 글은 따로 된 파일)
            sub = kid(mp, "subList")
            if sub is not None:
                self._master_page(sub)
        if self._page_set:
            return
        page = kid(sec, "pagePr")
        if page is None:
            return
        w, h = hwp_pt(page.get("width", "59528")), hwp_pt(page.get("height", "84188"))
        if page.get("landscape") == "NARROWLY":  # 가로 방향
            w, h = max(w, h), min(w, h)
        margin = kid(page, "margin")
        ps = PageSetup(width_pt=w, height_pt=h)
        if margin is not None:
            # HWP는 머리말·꼬리말 영역이 위·아래 여백과 별도다
            ps.margin_left_pt = hwp_pt(margin.get("left", "8504"))
            ps.margin_right_pt = hwp_pt(margin.get("right", "8504"))
            ps.margin_top_pt = hwp_pt(margin.get("top", "5668")) + hwp_pt(margin.get("header", "0"))
            ps.margin_bottom_pt = hwp_pt(margin.get("bottom", "4252")) + hwp_pt(margin.get("footer", "0"))
        # 쪽 테두리/배경 (양쪽 공통 설정)
        for pbf in kids(sec, "pageBorderFill"):
            if pbf.get("type", "BOTH") != "BOTH":
                continue
            bf = self.header.border_fills.get(pbf.get("borderFillIDRef", ""))
            if bf is not None:
                ps.background = bf.background
                if bf.image_ref:
                    ps.background_image_id = self._image_id(bf.image_ref)
        self.doc.page = ps
        self._page_set = True

    # ------------------------------------------------------------------
    # 개체 (그림, 도형, 차트)
    # ------------------------------------------------------------------

    def _image_id(self, ref: str | None) -> str | None:
        if not ref:
            return None
        if ref in self._image_by_ref:
            return self._image_by_ref[ref]
        href, media = self._manifest.get(ref, ("", ""))
        if not href:
            href = next((n for n in self.names if n.startswith("BinData/") and posixpath.splitext(posixpath.basename(n))[0] == ref), "")
        if not href or href not in self.names:
            self._warn(f"그림 파일 없음: {ref}")
            return None
        mime = media or mimetypes.guess_type(href)[0] or "application/octet-stream"
        if href.lower().endswith(".emf"):
            mime = "image/x-emf"
        elif href.lower().endswith(".wmf"):
            mime = "image/x-wmf"
        elif mime == "image/jpg":
            mime = "image/jpeg"
        if mime in _UNRENDERABLE:
            self._warn(f"브라우저에서 표시할 수 없는 이미지 형식: {posixpath.basename(href)} ({mime})")
        image_id = f"img{len(self.doc.images) + 1}"
        self.doc.images[image_id] = Image(mime=mime, data_base64=base64.b64encode(self.zip.read(href)).decode("ascii"),
                                          name=posixpath.basename(href))
        self._image_by_ref[ref] = image_id
        return image_id

    def _object(self, el, inlines, before, after) -> None:
        name = ln(el)
        sz, pos = kid(el, "sz"), kid(el, "pos")
        w = hwp_pt(sz.get("width")) if sz is not None else 0.0
        h = hwp_pt(sz.get("height")) if sz is not None else 0.0
        inline = pos is not None and pos.get("treatAsChar") == "1"
        alt = shape_comment(el)

        if name == "chart":
            root = self._xml(el.get("chartIDRef", ""))
            from ..docx.drawing import read_chart

            chart = read_chart(root, w, h) if root is not None else None
            if chart is None:
                self._warn("차트를 읽지 못함")
                return
            chart.id, chart.prov = self._new_id(), self._prov()
            after.append(chart)
            self._caption(el, after)
            return
        if name == "ole":
            chart = self._ole_chart(el, w, h)  # HWP 차트 (OLE 안의 데이터 표)
            if chart is not None:
                chart.id, chart.prov = self._new_id(), self._prov()
                after.append(chart)
                self._caption(el, after)
                return
        if name == "ole" and next((c for c in el.iter() if ln(c) == "img"), None) is None:
            self._warn("OLE 개체는 표시하지 않음")
            return  # 미리보기 그림이 있는 OLE는 아래에서 그림으로 표시한다

        if name in ("pic", "ole") and inline:
            img = next((c for c in el.iter() if ln(c) == "img"), None)
            image_id = self._image_id(img.get("binaryItemIDRef")) if img is not None else None
            if image_id:
                inlines.append(ImageRef(image_id=image_id, width_pt=w, height_pt=h, alt=alt))
            self._caption(el, after)  # 글자처럼 놓인 그림의 캡션: 문단 뒤
            return

        shapes = self.shapes.read(el) if name != "ole" else self.shapes.read(_as_pic(el))
        if not shapes:
            return
        drawing = Drawing(id=self._new_id(), prov=self._prov(), width_pt=w, height_pt=h, shapes=shapes, alt=alt)
        if inline:
            after.append(drawing)
            self._caption(el, after)
            return
        drawing.anchor = self._anchor(el, pos)
        slot = after if drawing.anchor.wrap == "top_bottom" else before
        slot.append(drawing)
        self._caption(el, slot)

    def _ole_chart(self, el, w: float, h: float):
        """OLE 개체가 차트면 Chart. HWPX의 차트는 hp:chart(OOXML)로 따로 오므로 여기서는 없다 (HWP 파서가 바꾼다)."""
        return None

    def _caption(self, el, slot) -> None:
        """개체 캡션(hp:caption의 글)을 개체 바로 뒤(위쪽 캡션이면 바로 앞) 문단으로 둔다.

        캡션 글은 개체 안이 아니라 hp:caption/hp:subList에 있다. 읽지 않으면 '그림 1. …' 같은 글이 경고 없이 빠졌다
        (hwpxlib SimpleRectangle). 문단으로 두면 표·그림·도식 어느 개체든 같은 방식으로 남는다.
        """
        cap = kid(el, "caption")
        sub = kid(cap, "subList") if cap is not None else None
        if sub is None:
            return
        blocks = [b for b in self._paragraphs(sub) if not isinstance(b, Paragraph) or b.text.strip()]
        if not blocks:
            return
        if cap.get("side", "BOTTOM") == "TOP":
            obj = slot.pop()
            at = slot.at.pop() if hasattr(slot, "at") else None
            for b in blocks:
                slot.append(b)
            slot.append(obj)
            if at is not None:  # 위치 기억(_Slot.at)은 개체가 놓였던 자리 그대로
                slot.at[-len(blocks) - 1:] = [at] * (len(blocks) + 1)
        else:
            at = slot.at[-1] if hasattr(slot, "at") and slot.at else None
            for b in blocks:
                slot.append(b)
            if at is not None:
                slot.at[-len(blocks):] = [at] * len(blocks)

    def _anchor(self, el, pos) -> Anchor:
        wrap_attr = el.get("textWrap", "SQUARE")
        a = Anchor(wrap=_WRAP.get(wrap_attr, "square"), behind_text=wrap_attr == "BEHIND_TEXT")
        if pos is None:
            return a
        a.relative_h = _REL_H.get(pos.get("horzRelTo", "COLUMN"), "column")
        a.relative_v = _REL_V.get(pos.get("vertRelTo", "PARA"), "paragraph")
        h_align, v_align = pos.get("horzAlign", "LEFT"), pos.get("vertAlign", "TOP")
        if h_align in ("LEFT", "INSIDE"):
            a.x_pt = hwp_pt(pos.get("horzOffset", "0"))
        else:
            a.align_h = {"CENTER": "center", "RIGHT": "right", "OUTSIDE": "right"}.get(h_align, "left")
        if v_align == "TOP":
            a.y_pt = hwp_pt(pos.get("vertOffset", "0"))
        else:
            a.align_v = {"CENTER": "center", "BOTTOM": "bottom"}.get(v_align, "top")
        return a

    # ------------------------------------------------------------------
    # 표
    # ------------------------------------------------------------------

    def _table(self, tbl) -> Block:
        if self._table_depth >= MAX_TABLE_DEPTH:
            self._warn(f"표 중첩이 {MAX_TABLE_DEPTH}단계를 넘어 안쪽 표는 글자만 추출함")
            text = " ".join(t.text for t in tbl.iter() if ln(t) == "t" and t.text)
            return Paragraph(id=self._new_id(), prov=self._prov(), inlines=[Text(text=text)] if text else [])
        self._table_depth += 1
        try:
            return self._table_body(tbl)
        finally:
            self._table_depth -= 1

    def _table_body(self, tbl) -> Table:
        table = Table(id=self._new_id(), prov=self._prov())
        in_margin = kid(tbl, "inMargin")
        pad = _margins(in_margin) if in_margin is not None else (1.41, 5.1, 1.41, 5.1)
        table.cell_padding_pt = pad
        sz = kid(tbl, "sz")
        if sz is not None:
            table.width_pt = hwp_pt(sz.get("width"))
        pos = kid(tbl, "pos")
        if pos is not None and pos.get("horzAlign") in ("CENTER", "RIGHT"):
            table.align = "center" if pos.get("horzAlign") == "CENTER" else "right"
        if pos is not None and pos.get("treatAsChar") != "1":
            anchor = self._anchor(tbl, pos)
            # 위아래 배치이면서 왼쪽 정렬·0 위치인 표는 일반 흐름과 같으므로 배치 정보를 남기지 않는다
            if not (anchor.wrap == "top_bottom" and not anchor.x_pt and anchor.align_h is None):
                table.anchor = anchor
        ncols = int(tbl.get("colCnt", "0") or 0)

        placed = []  # (col, colspan, width)
        for tr in kids(tbl, "tr"):
            row = Row()
            heights = []
            for tc in kids(tr, "tc"):
                addr, span, csz = kid(tc, "cellAddr"), kid(tc, "cellSpan"), kid(tc, "cellSz")
                col = int(addr.get("colAddr", "0")) if addr is not None else 0
                cs = int(span.get("colSpan", "1")) if span is not None else 1
                rs = int(span.get("rowSpan", "1")) if span is not None else 1
                sub = kid(tc, "subList")
                cell = Cell(blocks=self._paragraphs(sub) if sub is not None else [], col_span=cs, row_span=rs)
                bf = self.header.border_fills.get(tc.get("borderFillIDRef", ""))
                if bf is not None:
                    cell.borders, cell.background = bf.borders, bf.background
                if sub is not None:
                    cell.v_align = {"TOP": "top", "CENTER": "middle", "BOTTOM": "bottom"}.get(sub.get("vertAlign", "CENTER"))
                if csz is not None:
                    placed.append((col, cs, hwp_pt(csz.get("width", "0"))))
                    if rs == 1:
                        heights.append(hwp_pt(csz.get("height", "0")))
                if tc.get("header") == "1":
                    row.header = True
                ncols = max(ncols, col + cs)
                row.cells.append(cell)
            if heights:
                row.height_pt = max(heights)
            table.rows.append(row)
        table.col_widths_pt = _solve_widths(ncols, placed)
        return table


# ---------------------------------------------------------------------------


def _margins(el) -> tuple[float, float, float, float]:
    return (hwp_pt(el.get("top", "0")), hwp_pt(el.get("right", "0")),
            hwp_pt(el.get("bottom", "0")), hwp_pt(el.get("left", "0")))


def _solve_widths(ncols: int, placed: list[tuple[int, int, float]]) -> list[float]:
    """셀 너비(병합 포함)에서 열 너비를 구한다. 한 칸짜리 셀로 먼저 정하고, 병합 셀로 나머지를 채운다."""
    widths: list[float | None] = [None] * ncols
    for col, span, w in placed:
        if span == 1 and col < ncols and widths[col] is None:
            widths[col] = w
    changed = True
    while changed:
        changed = False
        for col, span, w in placed:
            cols = range(col, min(col + span, ncols))
            unknown = [c for c in cols if widths[c] is None]
            if unknown:
                known = sum(widths[c] or 0 for c in cols)
                share = max(w - known, 0) / len(unknown)
                for c in unknown:
                    widths[c] = share
                changed = True
    return [w or 0.0 for w in widths]


def _as_pic(ole):
    """OLE 개체를 미리보기 그림 개체처럼 다룬다."""
    pic = etree.Element("pic")
    for c in ole:
        pic.append(c.__copy__())
    return pic


def _merge(inlines: list) -> list:
    out: list = []
    for i in inlines:
        if out and isinstance(i, Text) and isinstance(out[-1], Text) and i.style == out[-1].style and i.link == out[-1].link:
            out[-1] = Text(text=out[-1].text + i.text, style=i.style, link=i.link)
        else:
            out.append(i)
    return out
