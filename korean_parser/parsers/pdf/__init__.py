"""PDF 파서: Docling(레이아웃·표·OCR) 결과를 통합 문서 모델로 옮기고, PDF 텍스트 레이어로 보정한다.

    Docling            무엇이 제목·문단·목록·표·그림인지, 읽기 순서, 표 구조(병합 포함)
    PDF 텍스트 레이어   글자 크기·굵기·색 → 서식과 제목 단계, 정렬·들여쓰기
    PDF 그래픽 개체     그림 영역 보정: 이미지·도형 묶음 안의 글자는 '도식 글자', 바깥 글자는 다시 문단으로
    한국어 처리 레이어  정규화, 띄어쓰기 복원 (PDF는 띄어쓰기가 빠지는 경우가 많다)

텍스트 레이어가 없는 스캔 PDF는 OCR(RapidOCR 한국어)로 처리한다. 이때는 서식 복원이 제한된다.
Docling은 선택 설치다: pip install "korean-parser[pdf]"
"""

from __future__ import annotations

import base64
import difflib
import os
import re
import statistics
from pathlib import Path

from ...ir import (
    Block, Border, Borders, Cell, Document, Figure, FixedPage, Heading, Image, Meta, PageBreak,
    PageSetup, Paragraph, ParagraphStyle, Provenance, Row, Table, Text, TextRun, TextStyle,
)
from ...normalize import is_hangul_syllable
from .page import AMBIGUOUS_GAP, BBox, PageInfo, PdfPages, cluster, expand, inside_ratio

PARSER_NAME = "pdf-docling"
_CAPTION = re.compile(r"^[\[<(]?\s*(그림|표|도표|사진|Figure|Fig\.?|Table)\s*[\d IVX]+", re.I)
_CONVERTERS: dict[tuple, object] = {}


class PdfEngineMissing(RuntimeError):
    pass


def _converter(ocr: bool, lang: str, threads: int):
    key = (ocr, lang, threads)
    if key in _CONVERTERS:
        return _CONVERTERS[key]
    try:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import OcrMode, PdfPipelineOptions, RapidOcrOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:  # pragma: no cover - 설치 환경에 따라
        raise PdfEngineMissing('PDF를 읽으려면 Docling이 필요합니다: pip install "korean-parser[pdf]"') from exc

    opts = PdfPipelineOptions()
    opts.accelerator_options = AcceleratorOptions(num_threads=threads, device=AcceleratorDevice.CPU)
    opts.table_structure_options.mode = TableFormerMode.FAST
    opts.do_table_structure = True
    opts.do_ocr = ocr
    if ocr:
        opts.ocr_options = RapidOcrOptions(lang=[lang], mode=OcrMode.FULL_PAGE)
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    _CONVERTERS[key] = conv
    return conv


def parse_pdf(path: str | Path, ocr: bool | None = None, ocr_lang: str = "korean",
              fixed_layout: bool = True) -> Document:
    """ocr=None이면 텍스트 레이어 유무로 자동 결정한다.

    fixed_layout=True면 원본 모양 재현용 쪽(바탕 그림 + 원래 위치의 글)도 만든다 (텍스트 레이어가 있을 때).
    """
    return PdfParser(path, ocr=ocr, ocr_lang=ocr_lang, fixed_layout=fixed_layout).parse()


_SPACE_BEFORE_PUNCT = re.compile(r"(?<=[가-힣]) +(?=[.,](?:\s|$))")


class PdfParser:
    def __init__(self, path, ocr: bool | None = None, ocr_lang: str = "korean", fixed_layout: bool = True):
        self.fixed_layout = fixed_layout
        self.path = Path(path)
        self.pages = PdfPages(self.path)
        self.ocr = (not self.pages.has_text_layer()) if ocr is None else ocr
        self.ocr_lang = ocr_lang
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="pdf"))
        self._seq = 0
        self._content: dict[int, BBox] = {}  # 쪽별 본문 영역

    def _new_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _prov(self, page: int, box: BBox | None, confidence: float | None = None) -> Provenance:
        return Provenance(parser=PARSER_NAME + ("+ocr" if self.ocr else ""), page=page,
                          bbox=tuple(round(v, 1) for v in box) if box else None, confidence=confidence)

    # ------------------------------------------------------------------

    def parse(self) -> Document:
        from ...docling_adapter import postprocess_docling
        from ...spacing import SpacingFixer

        threads = int(os.environ.get("KP_THREADS", os.cpu_count() or 4))
        result = _converter(self.ocr, self.ocr_lang, threads).convert(self.path)
        ddoc = result.document

        self.fixer = SpacingFixer()
        # 텍스트 레이어가 있으면 글은 그 레이어에서 다시 읽는다 (Docling 글은 공백이 자주 빠진다).
        # 다시 읽지 못한 글(OCR, 영역이 어긋난 글)만 형태소 분석으로 띄어쓰기를 복원한다.
        keep = set() if self.ocr else self._restore_text_layer(ddoc)
        report = postprocess_docling(ddoc, fixer=self.fixer, keep_spacing=keep)
        self.spacing_reset = report.spacing_reset
        if self.ocr:
            self.doc.warnings.append("텍스트 레이어가 없어 OCR로 읽었습니다. 글자 서식은 복원되지 않습니다.")
        if report.garbled:
            self.doc.warnings.append("글자 깨짐이 의심됩니다 (글꼴 정보가 없는 PDF일 수 있습니다).")

        self._read_meta()
        self._page_setup(ddoc)
        self._convert(ddoc)
        self._assign_heading_levels()
        if self.fixed_layout and not self.ocr:
            self._build_fixed_pages()
        self.pages.close()
        return self.doc

    def _build_fixed_pages(self) -> None:
        """쪽마다 '글자를 지운 바탕 그림 + 원래 위치의 글 조각'을 만들고, 글 조각을 블록 id에 연결한다."""
        located: dict[int, list[tuple[BBox, str]]] = {}
        for b in self.doc.iter_blocks():
            p = getattr(b, "prov", None)
            if p and p.bbox and getattr(b, "id", ""):
                located.setdefault(p.page, []).append((p.bbox, b.id))
        for number in range(1, len(self.pages) + 1):
            page = self.pages.page(number)
            png = self.pages.background_png(number)
            bg_id = None
            if png:
                bg_id = f"img{len(self.doc.images) + 1}"
                self.doc.images[bg_id] = Image(mime="image/png", data_base64=base64.b64encode(png).decode("ascii"),
                                               name=f"page{number}_background.png")
            fp = FixedPage(page=number, width_pt=page.width, height_pt=page.height, background_image_id=bg_id)
            boxes = sorted(located.get(number, []), key=lambda x: (x[0][2] - x[0][0]) * (x[0][3] - x[0][1]))
            for r in page.runs():
                cx, cy = (r.box[0] + r.box[2]) / 2, (r.box[1] + r.box[3]) / 2
                # 가장 작은(가장 구체적인) 블록에 연결
                owner = next((bid for bb, bid in boxes if bb[0] - 2 <= cx <= bb[2] + 2 and bb[1] - 2 <= cy <= bb[3] + 2), None)
                fp.runs.append(TextRun(text=r.text, x=round(r.x, 2), baseline=round(r.baseline, 2),
                                       width=round(r.width, 2), size_pt=round(r.size, 2),
                                       color=r.color if r.color != "#000000" else None, bold=r.bold,
                                       italic=r.italic, font=r.font or None, block_id=owner))
            self.doc.fixed_pages.append(fp)

    def _read_meta(self) -> None:
        try:
            meta = self.pages.pdf.get_metadata_dict()
        except Exception:  # noqa: BLE001
            meta = {}
        m = self.doc.meta
        m.title = (meta.get("Title") or "").strip() or None
        m.author = (meta.get("Author") or "").strip() or None
        m.created = (meta.get("CreationDate") or "").strip() or None
        m.modified = (meta.get("ModDate") or "").strip() or None

    def _page_setup(self, ddoc) -> None:
        """쪽 크기와, 본문 글이 놓인 범위로 여백을 추정한다."""
        from docling_core.types.doc import ContentLayer

        boxes: dict[int, list[BBox]] = {}
        for item, _ in ddoc.iterate_items(included_content_layers={ContentLayer.BODY}):
            for prov in getattr(item, "prov", []) or []:
                boxes.setdefault(prov.page_no, []).append(self._box(prov, ddoc))
        for page_no, bs in boxes.items():
            self._content[page_no] = (min(b[0] for b in bs), min(b[1] for b in bs),
                                      max(b[2] for b in bs), max(b[3] for b in bs))
        if len(self.pages) == 0:
            return
        first = self.pages.page(1)
        ps = PageSetup(width_pt=first.width, height_pt=first.height)
        if self._content:
            lefts = [c[0] for c in self._content.values()]
            rights = [first.width - c[2] for c in self._content.values()]
            ps.margin_left_pt = max(min(lefts) - 2, 18)
            ps.margin_right_pt = max(min(rights) - 2, 18)
            ps.margin_top_pt = max(min(c[1] for c in self._content.values()) - 6, 24)
            ps.margin_bottom_pt = 36
        self.doc.page = ps

    def _restore_text_layer(self, ddoc) -> set[int]:
        """Docling 글 항목과 표 칸의 글을 PDF 텍스트 레이어에서 다시 읽는다. 다시 읽은 항목의 id() 모음."""
        keep: set[int] = set()
        for item in ddoc.texts:
            if len(item.prov) != 1:
                continue
            prov = item.prov[0]
            text = self._layer_text(self.pages.page(prov.page_no), self._box(prov, ddoc), item.text)
            if text is not None:
                item.text = text
                keep.add(id(item))
        for table in ddoc.tables:
            if len(table.prov) != 1:
                continue
            page = self.pages.page(table.prov[0].page_no)
            for cell in table.data.table_cells:
                if cell.bbox is None:
                    continue
                b = cell.bbox if cell.bbox.coord_origin.value == "TOPLEFT" else cell.bbox.to_top_left_origin(
                    page_height=page.height)
                text = self._layer_text(page, (b.l, b.t, b.r, b.b), cell.text)
                if text is not None:
                    cell.text = text
                    keep.add(id(cell))
        return keep

    def _layer_text(self, page: PageInfo, box: BBox, original: str) -> str | None:
        """영역 안 텍스트 레이어 글. Docling 글과 (공백을 빼고) 같은 내용일 때만 쓴다."""
        lines = page.text_lines(box)
        if not lines:
            return None
        text = self._resolve_gaps(self._join_lines(lines))
        # 한글 뒤 마침표·쉼표 앞의 공백은 조판 과정에서 생긴 것이다 ("돌아갑니다 ."). ':' 앞은 일부러 띄우므로 둔다
        text = _SPACE_BEFORE_PUNCT.sub("", text)
        a, b = "".join(original.split()), "".join(text.split())
        if not a or not b:
            return None
        if a == b or a in b or difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.9:
            return text
        return None

    def _join_lines(self, lines: list[str]) -> str:
        """한 문단의 줄을 합친다. 줄 끝에 공백 글자가 있으면 어절 경계, 한글-한글로 끊긴 자리는 형태소 분석으로 정한다."""
        out = lines[0]
        for ln in lines[1:]:
            if out.endswith(" "):
                out += ln
            elif is_hangul_syllable(out[-1]) and is_hangul_syllable(ln[0]):
                out += (" " if self._space_at_join(out, ln) else "") + ln
            elif out.endswith("-") and out[-2:-1].isascii() and out[-2:-1].isalpha() and ln[:1].isascii():
                out = out[:-1] + ln  # 영어 하이픈 줄바꿈
            else:
                out += " " + ln
        return re.sub(r" {2,}", " ", out).strip()

    def _resolve_gaps(self, text: str) -> str:
        """한글-영문 사이의 애매한 간격을 형태소 분석으로 정한다 ('2026년', 'OCR이'는 붙이고 'PDF 기준'은 띄운다)."""
        if AMBIGUOUS_GAP not in text:
            return text
        parts = text.split(AMBIGUOUS_GAP)
        out = parts[0]
        for part in parts[1:]:
            out += (" " if out and part and self._space_at_join(out, part) else "") + part
        return out

    def _space_at_join(self, left: str, right: str) -> bool:
        """left와 right를 이을 때 그 자리에 공백이 필요한지 (형태소 분석)."""
        lw, rw = left.split(), right.split()
        if not lw or not rw:
            return False
        if is_hangul_syllable(lw[-1][-1]) and is_hangul_syllable(rw[0][0]):
            a, b = " ".join(lw[-2:]), " ".join(rw[:2])  # 줄바꿈 자리: 앞뒤 두 어절을 보고 그 자리만 정한다
            joined = self.fixer.join_lines([a, b])
        else:
            a = lw[-1]  # 한글-영문 사이: 맞닿은 두 어절만 다시 띄워 본다
            joined = self.fixer.respace(a + rw[0], reset=True)
        n, seen = len(a.replace(" ", "")), 0
        for i, ch in enumerate(joined):
            if ch != " ":
                seen += 1
                if seen == n:
                    return joined[i + 1: i + 2] == " "
        return False

    @staticmethod
    def _box(prov, ddoc) -> BBox:
        page = ddoc.pages.get(prov.page_no)
        h = page.size.height if page else 842
        b = prov.bbox.to_top_left_origin(page_height=h)
        return (b.l, b.t, b.r, b.b)

    # ------------------------------------------------------------------
    # 변환
    # ------------------------------------------------------------------

    def _convert(self, ddoc) -> None:
        from docling_core.types.doc import ContentLayer, DocItemLabel, ListItem, PictureItem, TableItem, TextItem

        skip: set[str] = set()
        last_page = None
        for item, _level in ddoc.iterate_items(included_content_layers={ContentLayer.BODY}):
            if item.self_ref in skip or not getattr(item, "prov", None):
                continue
            prov = item.prov[0]
            page_no, box = prov.page_no, self._box(prov, ddoc)
            page = self.pages.page(page_no)
            if last_page is not None and page_no != last_page:
                self.doc.blocks.append(PageBreak(id=self._new_id(), prov=self._prov(page_no, None)))
            last_page = page_no

            if isinstance(item, TableItem):
                self.doc.blocks.append(self._table(item, page, box, ddoc))
                for c in item.captions:
                    skip.add(c.cref)
                    cap = c.resolve(ddoc)
                    if cap.prov:
                        self.doc.blocks.append(self._paragraph(cap, page, self._box(cap.prov[0], ddoc)))
                continue
            if isinstance(item, PictureItem):
                blocks, used = self._picture(item, page, box, ddoc)
                self.doc.blocks.extend(blocks)
                skip.update(used)
                continue
            if isinstance(item, TextItem):
                if item.label in (DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER):
                    self.doc.blocks.append(self._heading(item, page, box))
                elif isinstance(item, ListItem):
                    self.doc.blocks.append(self._list_item(item, page, box))
                else:
                    self.doc.blocks.append(self._paragraph(item, page, box))

        # 머리말·꼬리말(furniture): 쪽 위·아래 끝에 있는 것만 인정한다.
        # 본문 중간에서 머리말로 잘못 분류된 글(예: 표 위의 '(단위: 억원)')은 위치에 맞게 본문에 끼워 넣는다.
        misplaced: list[Block] = []
        furniture = [it for it, _ in ddoc.iterate_items(included_content_layers={ContentLayer.FURNITURE})
                     if isinstance(it, TextItem) and it.prov and it.text.strip()]
        # 진짜 머리말·꼬리말은 여러 쪽에 반복된다 (쪽 번호는 숫자만 다르다). 한 번만 나오면 본문으로 본다.
        pages_of: dict[str, set[int]] = {}
        for it in furniture:
            pages_of.setdefault(_strip_digits(it.text), set()).add(it.prov[0].page_no)
        multi_page = len(self.pages) > 1
        for item in furniture:
            prov = item.prov[0]
            page_no, box = prov.page_no, self._box(prov, ddoc)
            page = self.pages.page(page_no)
            near_top, near_bottom = box[3] < page.height * 0.12, box[1] > page.height * 0.88
            repeated = not multi_page or len(pages_of[_strip_digits(item.text)]) > 1
            if not repeated:
                misplaced.append(self._paragraph(item, page, box))
                continue
            if item.label == DocItemLabel.PAGE_HEADER and near_top or item.label == DocItemLabel.PAGE_FOOTER and near_bottom:
                target = self.doc.header if item.label == DocItemLabel.PAGE_HEADER else self.doc.footer
                if page_no == 1 and not any(getattr(b, "text", None) == item.text for b in target):
                    target.append(self._paragraph(item, page, box))
                continue
            if near_top or near_bottom:
                continue  # 쪽 번호 등
            misplaced.append(self._paragraph(item, page, box))
        for blk in misplaced:
            self._insert_by_position(blk)
        self._attach_captions()
        self._vertical_spacing()

    def _vertical_spacing(self) -> None:
        """같은 쪽에서 앞 블록과의 세로 간격을 '앞 간격'으로 옮겨 원본의 여백 느낌을 살린다."""
        prev = None
        for b in self.doc.blocks:
            p = getattr(b, "prov", None)
            if p is None or p.bbox is None:
                prev = None
                continue
            if prev is not None and prev.page == p.page:
                gap = p.bbox[1] - prev.bbox[3]
                size = 10.0
                inl = getattr(b, "inlines", None)
                if inl and getattr(inl[0], "style", None) and inl[0].style.size_pt:
                    size = inl[0].style.size_pt
                space = round(min(max(gap - size * 0.35, 0), 72), 1)
                if space >= 1 and hasattr(b, "style"):
                    base = b.style or ParagraphStyle()
                    b.style = base.model_copy(update={"space_before_pt": space})
            prev = p

    def _insert_by_position(self, blk: Block) -> None:
        page, y = blk.prov.page, blk.prov.bbox[1]
        for i, b in enumerate(self.doc.blocks):
            p = getattr(b, "prov", None)
            if p and p.bbox and (p.page > page or (p.page == page and p.bbox[1] > y)):
                self.doc.blocks.insert(i, blk)
                return
        self.doc.blocks.append(blk)

    def _attach_captions(self) -> None:
        """'그림 1. …', '[표 2] …' 같은 문단이 그림 바로 앞뒤에 있으면 그림의 캡션으로 붙인다."""
        blocks = self.doc.blocks
        i = 0
        while i < len(blocks):
            b = blocks[i]
            if isinstance(b, Figure) and not b.caption:
                for j in (i + 1, i - 1):
                    if 0 <= j < len(blocks) and isinstance(blocks[j], Paragraph) and _CAPTION.match(blocks[j].text.strip()):
                        b.caption = blocks[j].text.strip()
                        del blocks[j]
                        if j < i:
                            i -= 1
                        break
            i += 1

    # ------------------------------------------------------------------

    def _text_style(self, page: PageInfo, box: BBox) -> tuple[TextStyle | None, float | None]:
        g = page.style_in(box)
        if g.size is None:
            if self.ocr:  # OCR: 글 줄 높이로 크기 추정
                h = box[3] - box[1]
                return None, round(h * 0.75 * 2) / 2 if h < 40 else None
            return None, None
        style = TextStyle(size_pt=g.size, bold=g.bold or None, italic=g.italic or None,
                          color=g.color if g.color not in ("#000000",) else None, font=g.font)
        return style, g.size

    def _layout(self, page: PageInfo, box: BBox) -> ParagraphStyle | None:
        """본문 영역 대비 위치로 정렬과 들여쓰기를 추정한다."""
        content = self._content.get(page.number)
        if not content:
            return None
        left, right = content[0], content[2]
        width = right - left
        x0, x1 = box[0], box[2]
        center_off = abs((x0 + x1) / 2 - (left + right) / 2)
        s: dict = {}
        if x0 - left > 20 and center_off < 6 and (x1 - x0) < width * 0.85:
            s["align"] = "center"
        elif abs(x1 - right) < 4 and x0 - left > width * 0.4:
            s["align"] = "right"
        elif x0 - left > 4:
            s["indent_left_pt"] = round(x0 - left, 1)
        return ParagraphStyle(**s) if s else None

    def _inlines(self, text: str, style: TextStyle | None) -> list:
        text = _undouble(text)
        return [Text(text=text, style=style)] if text else []

    def _paragraph(self, item, page: PageInfo, box: BBox) -> Paragraph:
        style, _ = self._text_style(page, box)
        return Paragraph(id=self._new_id(), prov=self._prov(page.number, box), inlines=self._inlines(item.text, style),
                         style=self._layout(page, box))

    def _heading(self, item, page: PageInfo, box: BBox) -> Heading:
        style, size = self._text_style(page, box)
        h = Heading(id=self._new_id(), prov=self._prov(page.number, box), level=1,
                    inlines=self._inlines(item.text, style), style=self._layout(page, box))
        h.style_name = "title" if item.label.value == "title" else None
        self._heading_sizes = getattr(self, "_heading_sizes", [])
        self._heading_sizes.append((h, size))
        return h

    def _list_item(self, item, page: PageInfo, box: BBox) -> Paragraph:
        """목록 항목. Docling이 떼어 낸 기호(○, 1., 가.)를 본문 앞에 되돌려 놓는다.

        한국 문서의 목록 단계는 들여쓰기보다 기호 종류(□ → ○ → -, 1. → 가. → 1))로 정해지는 경우가 많아,
        단계는 한국어 처리 레이어의 개조식 계층 인식에 맡긴다. 화면용 들여쓰기는 위치 그대로 유지한다.
        """
        style, _ = self._text_style(page, box)
        marker = (item.marker or "").strip()
        text = item.text
        if marker and not text.lstrip().startswith(marker):
            text = f"{marker} {text}"
        return Paragraph(id=self._new_id(), prov=self._prov(page.number, box), inlines=self._inlines(text, style),
                         style=self._layout(page, box))

    def _assign_heading_levels(self) -> None:
        """Docling은 제목 단계를 거의 1로 준다. 글자 크기 순서로 단계를 다시 매긴다."""
        entries = getattr(self, "_heading_sizes", [])
        sizes = sorted({s for _, s in entries if s}, reverse=True)
        has_title = any(h.style_name == "title" for h, _ in entries)
        for h, s in entries:
            if h.style_name == "title":
                h.level = 1
                continue
            rank = sizes.index(s) if s in sizes else 0
            h.level = min(rank + 1 + (1 if has_title else 0), 6)
            h.style_name = None
        for h, _ in entries:
            if h.style_name == "title":
                h.style_name = None

    # ------------------------------------------------------------------
    # 표
    # ------------------------------------------------------------------

    def _table(self, item, page: PageInfo, box: BBox, ddoc) -> Table:
        data = item.data
        table = Table(id=self._new_id(), prov=self._prov(page.number, box), width_pt=round(box[2] - box[0], 1))
        # 표 영역 안에 선이 있으면 칸마다 테두리를 그린다
        lines = [g for g in page.graphics if g.kind == "path" and inside_ratio(g.box, expand(box, 2)) > 0.9]
        bordered = len(lines) >= 2
        border = Border(style="solid", width_pt=0.5, color="#000000")

        origins: dict[int, list] = {}
        col_edges: dict[int, list[tuple[float, float]]] = {}
        for c in data.table_cells:
            if c.start_row_offset_idx is None:
                continue
            origins.setdefault(c.start_row_offset_idx, []).append(c)
        for r in range(data.num_rows):
            row = Row()
            for c in sorted(origins.get(r, []), key=lambda c: c.start_col_offset_idx):
                cb = None
                if c.bbox is not None:
                    b = c.bbox.to_top_left_origin(page_height=page.height) if c.bbox.coord_origin.value != "TOPLEFT" else c.bbox
                    cb = (b.l, b.t, b.r, b.b)
                    if c.col_span == 1:
                        col_edges.setdefault(c.start_col_offset_idx, []).append((b.l, b.r))
                cell = Cell(row_span=c.row_span or 1, col_span=c.col_span or 1)
                text = c.text.strip()
                if text:
                    style = None
                    if cb:
                        style, _ = self._text_style(page, cb)
                    # 칸마다 id와 좌표를 달아 LLM 답변의 근거를 표의 특정 칸까지 짚을 수 있게 한다
                    cell.blocks = [Paragraph(id=self._new_id(), prov=self._prov(page.number, cb),
                                             inlines=[Text(text=_undouble(text), style=style)])]
                if cb:
                    # 칸 글자 중심을 덮는 '채운 사각형'의 색 = 칸 배경
                    bg = page.fill_at((cb[0] + cb[2]) / 2, (cb[1] + cb[3]) / 2)
                    if bg and _luma(bg) > 90:
                        cell.background = bg
                if bordered:
                    cell.borders = Borders(top=border, bottom=border, left=border, right=border)
                if c.column_header:
                    row.header = True
                row.cells.append(cell)
            table.rows.append(row)

        # 열 너비: 한 칸짜리 칸들의 좌우 경계 평균, 모르면 남은 너비를 나눈다
        widths = []
        for i in range(data.num_cols):
            edges = col_edges.get(i)
            widths.append(statistics.mean(r - l for l, r in edges) if edges else None)
        known = sum(w for w in widths if w)
        unknown = [i for i, w in enumerate(widths) if not w]
        rest = max((box[2] - box[0]) - known, 0)
        for i in unknown:
            widths[i] = rest / len(unknown) if unknown else 0
        table.col_widths_pt = [round(w or 0, 1) for w in widths]
        table.cell_padding_pt = (1.5, 4, 1.5, 4)
        return table

    # ------------------------------------------------------------------
    # 그림 (영역 보정)
    # ------------------------------------------------------------------

    def _picture(self, item, page: PageInfo, box: BBox, ddoc) -> tuple[list[Block], set[str]]:
        """Docling이 찾은 그림 영역을 실제 그래픽 묶음 단위로 나눈다.

        그래픽(이미지·선·도형) 묶음 안의 글자 → 그 그림의 '도식 글자' (LLM용)
        묶음 밖의 글자 (캡션, 잘못 묶인 제목 등) → 원래 블록으로 되돌림
        """
        from docling_core.types.doc import DocItemLabel, TextItem

        used: set[str] = {c.cref for c in item.children} | {c.cref for c in item.captions}
        graphics = [g.box for g in page.graphics if inside_ratio(g.box, expand(box, 2)) > 0.6]
        groups = cluster(graphics, gap=10) if graphics else []
        groups = [g for g in groups if (g[2] - g[0]) > 8 and (g[3] - g[1]) > 8]
        if not groups or self.ocr:
            groups = [box]  # 그래픽 정보가 없으면 (스캔본 등) Docling 영역 그대로

        texts = []
        seen: set[str] = set()
        for ref in list(item.children) + list(item.captions):
            if ref.cref in seen:
                continue
            seen.add(ref.cref)
            t = ref.resolve(ddoc)
            if isinstance(t, TextItem) and t.prov and t.text.strip():
                texts.append((t, self._box(t.prov[0], ddoc)))
        caption_refs = {c.cref for c in item.captions}

        figures: list[tuple[BBox, list[str]]] = [(g, []) for g in groups]
        loose: list[tuple[object, BBox]] = []
        for t, tb in texts:
            home = next((f for f in figures if inside_ratio(tb, expand(f[0], 3)) > 0.7), None)
            if home is not None and t.self_ref not in caption_refs:
                home[1].append(t.text)
            else:
                loose.append((t, tb))

        out: list[tuple[float, Block]] = []
        for g, inner in figures:
            png = page.crop_png(g)
            if not png:
                continue
            image_id = f"img{len(self.doc.images) + 1}"
            self.doc.images[image_id] = Image(mime="image/png", data_base64=base64.b64encode(png).decode("ascii"),
                                              name=f"page{page.number}_{image_id}.png")
            fig = Figure(id=self._new_id(), prov=self._prov(page.number, g), image_id=image_id,
                         width_pt=round(g[2] - g[0], 1), height_pt=round(g[3] - g[1], 1),
                         inner_text=" / ".join(inner) or None)
            fig.style = self._layout(page, g)
            out.append((g[1], fig))
        for t, tb in loose:
            if t.label in (DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE):
                blk = self._heading(t, page, tb)
            else:
                blk = self._paragraph(t, page, tb)
            out.append((tb[1], blk))
        out.sort(key=lambda x: x[0])
        return [b for _, b in out], used


def _luma(hex_color: str) -> float:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _strip_digits(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split()))


_DOUBLED = re.compile(r"^(.{2,}?)\s+\1$")


def _undouble(text: str) -> str:
    """그림자·외곽선 효과로 같은 글이 두 번 겹쳐 그려진 경우 ('제목 제목' → '제목')."""
    m = _DOUBLED.match(text.strip())
    return m.group(1) if m else text
