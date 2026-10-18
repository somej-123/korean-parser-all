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
    Block, Border, Borders, Cell, Document, Figure, FixedPage, Heading, Image, Meta, Note, NoteRef, PageBreak,
    PageSetup, Paragraph, ParagraphStyle, Provenance, Row, Table, Text, TextRun, TextStyle, walk_blocks,
)
from ...normalize import is_hangul_syllable, normalize_text
from ...outline import match_marker
from .rules import build_grid, rule_regions
from .open_table import text_grid
from .page import AMBIGUOUS_GAP, BBox, PageInfo, PdfPages, cluster, expand, inside_ratio, intersect, union

PARSER_NAME = "pdf-docling"
_CAPTION = re.compile(r"^[\[<(]?\s*(그림|표|도표|사진|Figure|Fig\.?|Table)\s*[\d IVX]+", re.I)
_CONVERTERS: dict[tuple, object] = {}


class PdfEngineMissing(RuntimeError):
    pass


def _converter(ocr: bool, lang: str, threads: int, tables: bool = True):
    """Docling 변환기 (설정마다 한 번만 만든다). tables=False면 표 구조 인식 모델(TableFormer)을 돌리지 않는다."""
    key = (ocr, lang, threads, tables)
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
    opts.do_table_structure = tables
    opts.do_ocr = ocr
    if ocr:
        # 글 방향 분류기(cls)는 끈다: 한국어 가로줄을 180도 뒤집힌 것으로 잘못 판단해 줄 전체를 엉뚱한 글자로 읽는다
        # (정답 세트로 잰 OCR 글자 재현율 0.60 → 0.93). 스캔 문서는 대개 바로 놓여 있다
        opts.ocr_options = RapidOcrOptions(lang=[lang], mode=OcrMode.FULL_PAGE, use_cls=False)
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    _CONVERTERS[key] = conv
    return conv


def parse_pdf(path: str | Path, ocr: bool | None = None, ocr_lang: str = "korean",
              fixed_layout: bool = True) -> Document:
    """ocr=None이면 텍스트 레이어 유무로 자동 결정한다.

    fixed_layout=True면 원본 모양 재현용 쪽(바탕 그림 + 원래 위치의 글)도 만든다 (텍스트 레이어가 있을 때).
    """
    return PdfParser(path, ocr=ocr, ocr_lang=ocr_lang, fixed_layout=fixed_layout).parse()


_SENTENCE_END = re.compile(r"([.!?。:;]|[다요음함임됨것][.)]?)\s*$")


def _side_by_side(blocks) -> bool:
    """세로 범위가 겹치면서 가로로는 떨어져 있는 블록 쌍이 있는가 (다단, 나란히 놓인 블록)."""
    for i, a in enumerate(blocks):
        ax0, ay0, ax1, ay1 = a.prov.bbox
        for b in blocks[i + 1:]:
            bx0, by0, bx1, by1 = b.prov.bbox
            if min(ay1, by1) - max(ay0, by0) > 2 and (bx0 >= ax1 - 2 or ax0 >= bx1 - 2):
                return True
    return False


def _band_order(blocks, page_height: float) -> list | None:
    """세로로 겹치는 블록끼리 띠로 묶어, 띠는 위에서 아래로, 띠 안에서는 왼쪽 단부터(단 안은 위에서 아래로) 읽는 순서.

    신청서 양식은 왼쪽 띠의 구역 이름('신청고객정보')과 오른쪽 표가 나란히 놓인 줄이 위아래로 이어진다. Docling은
    왼쪽 이름들을 먼저 다 읽고 오른쪽으로 넘어가서, 이름이 자기 표와 떨어지고 표 사이 안내문이 쪽 끝으로 밀렸다.
    진짜 다단 문서는 단끼리 세로로 계속 겹쳐 띠 하나가 쪽 대부분을 차지하므로, 띠가 모두 쪽 높이의 1/4보다 낮을 때만
    이 순서를 쓴다 (아니면 None).
    """
    order = sorted(blocks, key=lambda b: b.prov.bbox[1])
    bands: list[list] = []
    bottom = None
    for b in order:
        y0, y1 = b.prov.bbox[1], b.prov.bbox[3]
        if bands and y0 < bottom - 2:  # 띠의 아래 끝보다 위에서 시작하면 같은 띠
            bands[-1].append(b)
            bottom = max(bottom, y1)
        else:
            bands.append([b])
            bottom = y1
    if any(max(b.prov.bbox[3] for b in band) - min(b.prov.bbox[1] for b in band) > page_height / 4 for band in bands):
        return None
    out = []
    for band in bands:
        cols: list[list] = []  # 가로로 겹치는 블록끼리 한 단
        for b in sorted(band, key=lambda b: b.prov.bbox[0]):
            x0, x1 = b.prov.bbox[0], b.prov.bbox[2]
            col = next((c for c in cols if any(min(x1, o.prov.bbox[2]) - max(x0, o.prov.bbox[0]) > 2 for o in c)), None)
            if col is None:
                cols.append([b])
            else:
                col.append(b)
        for col in cols:
            out.extend(sorted(col, key=lambda b: b.prov.bbox[1]))
    return out


def _row_texts(row) -> list[str]:
    return [" ".join(getattr(x, "text", "") for x in c.blocks).strip() for c in row.cells]


_DATE_ONLY = re.compile(r"^\s*\d{4}\s*[.년]\s*\d{1,2}\s*[.월]\s*\d{1,2}\s*[.일]?\s*(\([^)]{1,3}\))?\s*$")
_SENTENCE = re.compile(r"(니다|습니다|한다|된다|이다|있다|없다|하라|하시오|바랍니다)\s*[.!]?\s*[>〉》\])）」』】]*\s*$")


_BULLET_LINE = re.compile(r"^\s*[□○◦❍ㅇ※]\s")  # 개조식 항목 (공문서의 □ ○ ㅇ). 계층은 OutlineInfo가 맡는다
_BRACKET_LINE = re.compile(r"^\s*[<〈＜《]\s*[^<>〈〉＜＞《》]+[>〉＞》]\s*$")  # '< 측정 결과 >' 모양


def _not_a_title(text: str) -> bool:
    """제목으로 분류되었어도 제목이 아닌 줄: 날짜뿐인 줄, '- 다 음 -', 문장, 개조식 항목(□ ○ ㅇ ※)."""
    t = text.strip()
    return bool(_DATE_ONLY.match(t) or re.sub(r"[\s\-–—]", "", t) == "다음" or _SENTENCE.search(t)
                or _BULLET_LINE.match(t))


def _same_table(a, b, last_page: int | None = None) -> bool:
    """쪽을 넘어 이어지는 같은 표인가: 다음 쪽 표이고 열 수와 열 너비가 같다.

    last_page: a가 이미 여러 쪽을 이은 표면 그 마지막 쪽 (a.prov.page는 첫 쪽이다).
    """
    if not isinstance(b, Table) or not a.prov or not b.prov:
        return False
    if b.prov.page != (last_page or a.prov.page) + 1:
        return False
    wa, wb = a.col_widths_pt, b.col_widths_pt
    if not wa or len(wa) != len(wb):
        return False
    if any(abs(x - y) > 3 for x, y in zip(wa, wb)):
        return False
    if a.prov.bbox and b.prov.bbox and abs(a.prov.bbox[0] - b.prov.bbox[0]) > 6:
        return False
    return True


def _first_style(p):
    return next((x.style for x in p.inlines if isinstance(x, Text) and x.text.strip() and x.style), None)


_FIXER = None
_JOIN_CACHE: dict[tuple[str, str], bool] = {}


def _spacing_fixer():
    global _FIXER
    if _FIXER is None:
        from ...spacing import SpacingFixer

        _FIXER = SpacingFixer()
    return _FIXER


def _ocr_clusters(result) -> dict[int, list]:
    """Docling 변환 결과의 OCR 상자: 쪽 번호 → [(레이아웃 영역 좌표, [OcrCell …]) …]. 좌표는 왼쪽 위 원점, pt."""
    from .ocr_spacing import OcrCell

    out: dict[int, list] = {}
    for page in getattr(result, "pages", None) or []:
        layout = page.predictions.layout if page.predictions else None
        if layout is None:
            continue
        for cl in layout.clusters:
            b = cl.bbox if cl.bbox.coord_origin.value == "TOPLEFT" else cl.bbox.to_top_left_origin(page.size.height)
            cells = []
            for c in cl.cells:
                if not c.text.strip():
                    continue
                r = c.rect.to_bounding_box()
                if r.coord_origin.value != "TOPLEFT":
                    r = r.to_top_left_origin(page.size.height)
                cells.append(OcrCell(c.text, (r.l, r.t, r.r, r.b)))
            out.setdefault(page.page_no, []).append(((b.l, b.t, b.r, b.b), cells))
    return out


def _is_toc(item) -> bool:
    """Docling이 목차(document_index)로 본 표인가. 목차는 줄마다 '제목 ···· 쪽'인 글이라 행·열 표가 아니다."""
    from docling_core.types.doc import DocItemLabel

    return getattr(item, "label", None) == DocItemLabel.DOCUMENT_INDEX


def _table_of(item, ddoc):
    """Docling 항목이 들어 있는 표 (부모를 따라 올라가 본다). 없으면 None."""
    from docling_core.types.doc import TableItem

    node = item
    for _ in range(8):
        parent = getattr(node, "parent", None)
        if parent is None:
            return None
        node = parent.resolve(ddoc)
        if isinstance(node, TableItem):
            return node
    return None


def _rows_without_lines(grid, page: PageInfo) -> bool:
    """선 없이 여러 행을 둔 표(머리 행 아래에만 선)인가: 마지막 행에서 두 칸 이상이 같은 높이에 여러 줄을 두었다."""
    last = [c for c in grid.cells if c.r1 == grid.n_rows - 1]
    baselines = []
    for c in last:
        ys = sorted({round(ch.origin[1]) for ch in page.chars_in(c.box, 0) if ch.text.strip()})
        if len(ys) >= 2:
            baselines.append(ys)
    if len(baselines) < 2:
        return False
    shared = [y for y in baselines[0] if any(abs(y - z) <= 2 for z in baselines[1])]
    return len(shared) >= 2


def _iou(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


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
        self._ocr_cells: dict[int, list] = {}
        self._page_images: dict = {}  # 쪽 번호 → 스캔 쪽 그림 (ocr_spacing.PageImage), 한 번만 그린다
        self._ocr_gap: float | None = None  # 문서의 OCR 공백 기준 (em)
        self._done: set[str] = set()  # 블록으로 만든 Docling 항목 (그림이 뒤늦게 가져가지 않도록)

    def _new_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _prov(self, page: int, box: BBox | None, confidence: float | None = None) -> Provenance:
        return Provenance(parser=PARSER_NAME + ("+ocr" if self.ocr else ""), page=page,
                          bbox=tuple(round(v, 1) for v in box) if box else None, confidence=confidence)

    # ------------------------------------------------------------------

    def parse(self) -> Document:
        from ...docling_adapter import postprocess_docling

        self.threads = int(os.environ.get("KP_THREADS", os.cpu_count() or 4))
        # 텍스트 레이어가 있으면 표 구조 인식 모델 없이 먼저 돌린다. 표 대부분은 괘선으로 읽고,
        # 괘선으로 읽지 못한 표가 있는 쪽만 모델을 다시 돌린다 (모델이 처리 시간의 절반 이상을 차지한다)
        result = _converter(self.ocr, self.ocr_lang, self.threads, tables=self.ocr).convert(self.path)
        ddoc = result.document

        self.fixer = _spacing_fixer()  # 형태소 분석기 로딩(약 2초)은 한 번만
        clusters = _ocr_clusters(result) if self.ocr else {}
        self._ocr_cells = {n: [c for _, cells in cl for c in cells] for n, cl in clusters.items()}  # 쪽 → OCR 글자 상자
        self._find_ruled_tables(ddoc)
        self._model_tables = {} if self.ocr else self._run_table_model(ddoc)
        # 텍스트 레이어가 있으면 글은 그 레이어에서 다시 읽는다 (Docling 글은 공백이 자주 빠진다).
        # OCR 글은 쪽 그림의 글자 사이 빈칸으로 공백을 되살린다. 둘 다 못 한 글만 형태소 분석으로 띄어쓰기를 복원한다.
        keep = self._restore_ocr_spacing(clusters, ddoc) if self.ocr else self._restore_text_layer(ddoc)
        report = postprocess_docling(ddoc, fixer=self.fixer, keep_spacing=keep, ocr=self.ocr)
        self.spacing_reset = report.spacing_reset
        for table, tdoc in self._model_tables.values():
            restored = self._restore_table_cells(table, tdoc)
            for cell in table.data.table_cells:
                if id(cell) not in restored:
                    cell.text = self.fixer.respace(normalize_text(cell.text), reset=report.spacing_reset)
        if self.ocr:
            self.doc.warnings.append("텍스트 레이어가 없어 OCR로 읽었습니다. 글자 서식은 복원되지 않습니다.")
        if report.garbled:
            self.doc.warnings.append("글자 깨짐이 의심됩니다 (글꼴 정보가 없는 PDF일 수 있습니다).")

        self._read_meta()
        self._page_setup(ddoc)
        self._convert(ddoc)
        self._demote_non_titles()
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
        m.page_count = len(self.pages)
        m.page_count_source = "layout"

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
            original = item.text
            marker = (getattr(item, "marker", None) or "").strip()
            if marker and not original.lstrip().startswith(marker):
                # 목록 항목: Docling은 기호('7.')를 떼어 두지만 영역에는 기호 글자도 있다. 붙여서 비교해야
                # 레이어 글을 쓴다 (예전에는 기호만큼 달라 Docling 글이 남고, 그 글의 ‘ ’가 '로 바뀌어 있었다)
                original = f"{marker} {original}"
            text = self._layer_text(self.pages.page(prov.page_no), self._box(prov, ddoc), original)
            if text is not None:
                item.text = text
                keep.add(id(item))
        for table in ddoc.tables:
            keep |= self._restore_table_cells(table, ddoc)
        return keep

    def _restore_ocr_spacing(self, clusters: dict[int, list], ddoc) -> set[int]:
        """OCR 글의 공백과 개조식 기호를 쪽 그림에서 되살린다 (`ocr_spacing`). 되살린 항목의 id() 모음.

        Docling 글 항목은 레이아웃 영역(cluster) 하나의 OCR 상자들을 이은 것이다. 영역마다 줄을 묶어 줄 그림의 빈칸으로
        공백을 정하고, 줄과 줄은 텍스트 레이어 글처럼 이어 붙인다 (한글-한글로 끊긴 자리는 형태소 분석).
        그림과 맞지 않는 줄은 OCR용 형태소 분석 띄어쓰기를 쓴다.
        """
        from .ocr_spacing import cells_gaps, space_threshold

        # 공백으로 보는 빈칸 폭은 글꼴마다 다르다: 문서 전체 OCR 줄의 빈칸 폭 분포로 정한다
        self._ocr_gap = space_threshold([g for n, cl in clusters.items() for _, cells in cl
                                         for g in cells_gaps(self._page_image(n), cells)])

        def respaced(n: int, cells) -> str:
            lines = self._ocr_lines(n, cells)
            return self._join_lines(lines) if lines else ""

        keep: set[int] = set()
        for item in ddoc.texts:
            if len(item.prov) != 1:
                continue
            n, box = item.prov[0].page_no, self._box(item.prov[0], ddoc)
            cells = next((cs for cb, cs in clusters.get(n, []) if max(abs(a - b) for a, b in zip(cb, box)) < 1.0), None)
            if not cells:
                continue
            text = respaced(n, cells)
            if text and "".join(text.split()).lstrip("□○■●-") == "".join(item.text.split()).lstrip("□○■●-Oo0ㅇㅁ"):
                item.text = text
                keep.add(id(item))
        for table in ddoc.tables:
            if len(table.prov) != 1:
                continue
            n = table.prov[0].page_no
            ocr_cells = [c for cb, cs in clusters.get(n, []) for c in cs
                         if inside_ratio(c.box, expand(self._box(table.prov[0], ddoc), 2)) > 0.6]
            page_h = self.pages.page(n).height
            for cell in table.data.table_cells:
                if cell.bbox is None or not cell.text.strip():
                    continue
                b = cell.bbox if cell.bbox.coord_origin.value == "TOPLEFT" else cell.bbox.to_top_left_origin(page_height=page_h)
                inner = [c for c in ocr_cells if b.l - 1 <= (c.box[0] + c.box[2]) / 2 <= b.r + 1
                         and b.t - 1 <= (c.box[1] + c.box[3]) / 2 <= b.b + 1]
                if not inner:
                    continue
                text = respaced(n, inner)
                if text and "".join(text.split()) == "".join(cell.text.split()):
                    cell.text = text
                    keep.add(id(cell))
        return keep

    def _page_image(self, n: int):
        from .ocr_spacing import PageImage

        if n not in self._page_images:
            self._page_images[n] = PageImage(self.pages.pdf[n - 1])
        return self._page_images[n]

    def _ocr_lines(self, n: int, cells) -> list[str]:
        """OCR 상자들의 줄마다 글 (쪽 그림의 빈칸으로 공백을 되살림, 맞출 수 없는 줄은 형태소 분석)."""
        from ...spacing import fix_ocr_marker
        from .ocr_spacing import SPACE_GAP, respace_cells

        lines = [fix_ocr_marker(fixed) if fixed is not None else self.fixer.respace_ocr(normalize_text(raw))
                 for fixed, raw in respace_cells(self._page_image(n), cells, self._ocr_gap or SPACE_GAP)]
        return [ln for ln in lines if ln.strip()]

    def _restore_table_cells(self, table, ddoc) -> set[int]:
        keep: set[int] = set()
        if len(table.prov) != 1:
            return keep
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

    def _run_table_model(self, ddoc) -> dict:
        """괘선으로 읽지 못한 표가 있는 쪽만 표 구조 인식 모델로 다시 변환한다. 표 self_ref → (모델 결과 표, 그 문서)."""
        from docling_core.types.doc import TableItem

        pending = [it for it, _ in ddoc.iterate_items()
                   if isinstance(it, TableItem) and it.prov and it.self_ref not in self._ruled and not _is_toc(it)]
        if not pending:
            return {}
        pages = sorted({it.prov[0].page_no for it in pending})
        ranges: list[list[int]] = []
        for p in pages:  # 이어진 쪽은 한 번에
            if ranges and p == ranges[-1][1] + 1:
                ranges[-1][1] = p
            else:
                ranges.append([p, p])
        conv = _converter(False, self.ocr_lang, self.threads, tables=True)
        found: list[tuple[int, BBox, object, object]] = []
        for a, b in ranges:
            tdoc = conv.convert(self.path, page_range=(a, b)).document
            for it, _ in tdoc.iterate_items():
                if isinstance(it, TableItem) and it.prov:
                    found.append((it.prov[0].page_no, self._box(it.prov[0], tdoc), it, tdoc))
        out = {}
        for it in pending:
            page_no, box = it.prov[0].page_no, self._box(it.prov[0], ddoc)
            best = max(((_iou(box, fb), ft, fd) for fp, fb, ft, fd in found if fp == page_no),
                       key=lambda x: x[0], default=None)
            if best and best[0] > 0.3:
                out[it.self_ref] = (best[1], best[2])
        return out

    def _layer_text(self, page: PageInfo, box: BBox, original: str) -> str | None:
        """영역 안 텍스트 레이어 글. Docling 글과 (공백을 빼고) 같은 내용일 때만 쓴다."""
        lines = page.text_lines(box)
        if not lines:
            return None
        text = self._resolve_gaps(self._join_lines(lines))
        # 한글 뒤 마침표·쉼표 앞의 공백은 조판 과정에서 생긴 것이다 ("돌아갑니다 ."). ':' 앞은 일부러 띄우므로 둔다
        text = _SPACE_BEFORE_PUNCT.sub("", text)
        a, b = "".join(original.split()), "".join(text.split())
        if not b:
            return None
        if not a:
            return text  # Docling이 글을 비워 둔 영역: 텍스트 레이어에 있는 글이 원문이다
        if a == b or a in b:
            return text
        if len(b) < len(a) and b in a:
            # 레이어 글이 Docling 글보다 끝 글자 몇 개가 모자라다. 폭 정보가 깨진 글꼴은 글자 상자가 글자 크기만큼 넓어서
            # 마지막 글자(')')의 가운데가 Docling 상자 밖에 놓인다. 옆으로 조금 넓혀 다시 읽고 같아지면 그것을 쓴다
            size = page.style_in(box).size or 10.0
            wide = page.text_lines((box[0] - size * 0.6, box[1], box[2] + size * 0.6, box[3]))
            if wide:
                text2 = _SPACE_BEFORE_PUNCT.sub("", self._resolve_gaps(self._join_lines(wide)))
                if "".join(text2.split()) == a:
                    return text2
        # 같은 글자들이면(순서가 조금 다르거나 몇 글자만 달라도) 텍스트 레이어를 믿는다. 짧은 제목은 한 글자만 달라도
        # 비율이 크게 떨어지므로 비율 대신 다른 글자 수로 본다
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        same = sum(m.size for m in sm.get_matching_blocks())
        if max(len(a), len(b)) - same <= max(2, 0.1 * max(len(a), len(b))):
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
        """애매한 간격을 형태소 분석으로 정한다. 한글-영문 사이('2026년', 'OCR이'는 붙이고 'PDF 기준'은 띄운다)와
        좁은 글꼴의 한글-한글 사이('해지정산 내역 및 동의내역')."""
        if AMBIGUOUS_GAP not in text:
            return text
        parts = text.split(AMBIGUOUS_GAP)
        out = parts[0]
        for part in parts[1:]:
            if not out or not part:
                out += part
                continue
            lw = out.split()[-1] if out.split() else ""
            if is_hangul_syllable(out[-1:]) and is_hangul_syllable(part[:1]):
                out += (" " if self._space_at_join(out, part) else "") + part  # 좁은 글꼴의 낱말 사이 (`_narrow_spaces`)
            elif is_hangul_syllable(out[-1:]) and lw != "제":
                # 한글 뒤에 오는 숫자·영문은 거의 언제나 새 어절이다 ('시 30분', '산출물은 HWPX와'). '제3조'만 예외
                out += " " + part
            else:  # 숫자·영문 뒤의 한글: 단위·조사면 붙이고('30분', 'OCR이'), 낱말이면 띄운다('PDF 기준')
                out += (" " if self._space_at_join(out, part) else "") + part
        return out

    def _space_at_join(self, left: str, right: str) -> bool:
        """left와 right를 이을 때 그 자리에 공백이 필요한지 (형태소 분석)."""
        lw, rw = left.split(), right.split()
        if not lw or not rw:
            return False
        key = (" ".join(lw[-2:]), " ".join(rw[:2]))
        if key not in _JOIN_CACHE:
            _JOIN_CACHE[key] = self._decide_space(lw, rw)
        return _JOIN_CACHE[key]

    def _decide_space(self, lw: list[str], rw: list[str]) -> bool:
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
            self._done.add(item.self_ref)
            prov = item.prov[0]
            page_no, box = prov.page_no, self._box(prov, ddoc)
            page = self.pages.page(page_no)
            if isinstance(item, TextItem) and self._covered_by_table(item, page_no, box, ddoc):
                continue  # 표가 이미 담고 있는 글 (표 구조 모델을 끄면 표 안 글이 따로 나온다)
            if last_page is not None and page_no != last_page:
                self.doc.blocks.append(PageBreak(id=self._new_id(), prov=self._prov(page_no, None)))
            last_page = page_no

            if isinstance(item, TableItem) and _is_toc(item) and item.self_ref not in self._ruled:
                continue  # 목차: 표 대신 안의 줄(글 항목)이 차례로 문단이 된다
            if isinstance(item, TableItem):
                self.doc.blocks.extend(self._table(item, page, box, ddoc))
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
                if page_no == 1 and box[1] < page.height * 0.12 and self._looks_like_title(page, box):
                    title = self._heading(item, page, box)
                    title.style_name = "title"
                    misplaced.append(title)
                else:
                    misplaced.append(self._paragraph(item, page, box))
                continue
            if item.label == DocItemLabel.PAGE_HEADER and near_top or item.label == DocItemLabel.PAGE_FOOTER and near_bottom:
                # 머리말·꼬리말은 글(숫자 빼고)마다 한 번 담는다. 장마다 바뀌는 머리말('Ⅱ. 승인기준 19')도 있어서
                # 1쪽 것만 담으면 나머지 장의 머리말 글이 경고 없이 빠진다 (DOCX·HWPX와 같은 원칙)
                target = self.doc.header if item.label == DocItemLabel.PAGE_HEADER else self.doc.footer
                key = _strip_digits(item.text)
                if not any(_strip_digits(getattr(b, "text", "") or "") == key for b in target):
                    target.append(self._paragraph(item, page, box))
                continue
            if near_top or near_bottom:
                continue  # 쪽 번호 등
            misplaced.append(self._paragraph(item, page, box))
        for blk in misplaced:
            self._insert_by_position(blk)
        for n, grid in self._extra_tables:  # Docling이 놓친 괘선 표
            self._insert_by_position(self._ruled_table(grid, self.pages.page(n)))
        self._fix_page_order()
        self._merge_split_tables()
        self._merge_split_paragraphs()
        self._attach_captions()
        self._apply_links()
        self._link_footnotes()
        self._vertical_spacing()

    def _looks_like_title(self, page: PageInfo, box: BBox) -> bool:
        """쪽 맨 위 한 줄이 문서의 다른 제목보다 확실히 큰 글씨인가 (Docling이 표지 제목을 머리말로 분류하는 경우).

        스캔 '2026 사업계획서'(상자 높이 29.6pt, 절 제목 '1. 개요' 16.8pt)가 머리말로 분류되어 본문 문단이 되었다.
        """
        _, size = self._text_style(page, box)
        others = [s for _, s in getattr(self, "_heading_sizes", []) if s]
        return bool(size and others and size >= max(others) * 1.3)

    def _apply_links(self) -> None:
        """PDF 링크 주석(바깥 주소)을 그 영역에 놓인 글에 붙인다 (Text.link).

        링크 영역의 텍스트 레이어 글을 읽어, 영역과 겹치는 문단에서 같은 글(글자·숫자만 비교)을 찾아 그 부분만 링크로 나눈다.
        """
        if self.ocr:
            return
        targets = [b for blocks in (self.doc.header, self.doc.blocks, self.doc.footer)
                   for b in walk_blocks(blocks) if isinstance(b, (Paragraph, Heading)) and b.prov and b.prov.bbox]
        for n in range(1, len(self.pages) + 1):
            page = self.pages.page(n)
            for rect, uri in self.pages.links(n):
                # 글자·숫자만으로 맞춘다: 마침표 같은 작은 글자는 상자가 링크 영역 밖으로 벗어나기도 한다 ('ricx@kumoh ac kr')
                label = "".join(ch for ch in "".join(page.text_lines(rect, pad=0.5)) if ch.isalnum())
                if not label:
                    continue
                for b in targets:
                    if b.prov.page == n and intersect(b.prov.bbox, expand(rect, 1)) and _link_span(b, label, uri):
                        break

    def _link_footnotes(self) -> None:
        """쪽 아래 각주를 문서 각주(notes)로 옮기고, 본문의 위 첨자 번호 자리에 각주 참조(NoteRef)를 넣는다.

        각주로 보는 문단: 쪽 아래쪽(높이의 55% 아래)에 있고, '번호 + 공백'으로 시작하며, 그 번호가 같은 쪽 본문에
        위 첨자로 있고, 본문보다 큰 글씨가 아니다 (각주 글씨가 본문과 같은 문서도 있다: LibreOffice 기본값).
        Docling은 각주를 각주(footnote)로도 일반 글로도 분류해서 레이블을 쓰지 않는다.
        """
        if self.ocr:
            return
        sizes = [s.size_pt for b in self.doc.blocks if isinstance(b, Paragraph) and (s := _first_style(b)) and s.size_pt
                 for _ in range(min(len(b.text), 200))]
        body = statistics.median(sizes) if sizes else None
        for n in range(1, len(self.pages) + 1):
            page = self.pages.page(n)
            marks = [m for m in page.superscripts() if re.fullmatch(r"\d{1,3}|[*†‡]", m[1])]
            if not marks:
                continue
            for b in [b for b in self.doc.blocks if isinstance(b, Paragraph) and b.prov and b.prov.page == n
                      and b.prov.bbox and b.prov.bbox[1] > page.height * 0.55]:
                m = re.match(r"^(\d{1,3}|[*†‡])\s+\S", b.text)
                st = _first_style(b)
                if not m or (body and st and st.size_pt and st.size_pt > body + 0.1):
                    continue
                mark = next((x for x in marks if x[1] == m.group(1)), None)
                if mark is None:
                    continue
                key = f"fn{len(self.doc.notes) + 1}"
                label = m.group(1)
                note = b.model_copy(deep=True)
                first = note.inlines[0]
                if isinstance(first, Text):
                    note.inlines[0] = first.model_copy(update={"text": first.text.lstrip()[len(label):].lstrip()})
                self.doc.notes[key] = Note(kind="footnote", label=label, blocks=[note])
                self.doc.blocks.remove(b)
                marks.remove(mark)
                self._place_note_ref(n, mark, key, label)

    def _place_note_ref(self, n: int, mark, key: str, label: str) -> None:
        """위 첨자 번호가 있는 본문 문단에서 그 번호(앞뒤 글자로 자리를 찾는다)를 NoteRef로 바꾼다."""
        box, text, before, after = mark
        for b in walk_blocks(self.doc.blocks):
            if not isinstance(b, (Paragraph, Heading)) or not b.prov or b.prov.page != n or not b.prov.bbox \
                    or not intersect(b.prov.bbox, expand(box, 1)):
                continue
            for k, inl in enumerate(b.inlines):
                if not isinstance(inl, Text):
                    continue
                pos = [i for i, ch in enumerate(inl.text) if not ch.isspace()]
                flat = "".join(inl.text[i] for i in pos)
                for ctx, shift in ((before + text + after, len(before)), (before + text, len(before)),
                                   (text + after, 0)):
                    at = flat.find(ctx) if ctx != text else -1
                    if at < 0:
                        continue
                    s = pos[at + shift]
                    e = pos[at + shift + len(text) - 1] + 1
                    parts = [inl.model_copy(update={"text": inl.text[:s].rstrip()}), NoteRef(note_id=key, label=label),
                             inl.model_copy(update={"text": inl.text[e:]})]
                    b.inlines[k:k + 1] = [p for p in parts if not isinstance(p, Text) or p.text]
                    return

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

    # ------------------------------------------------------------------
    # 끊긴 문단 잇기
    # ------------------------------------------------------------------

    def _fix_page_order(self) -> None:
        """한 단(段)으로 된 쪽은 블록을 위에서 아래 순서로 놓는다.

        Docling 읽기 순서가 가끔 쪽 위의 표·주석을 아래 문단 뒤에 둔다. 쪽 안에서 나란히 놓인 블록(다단, 옆으로
        놓인 그림과 글)이 하나도 없으면 위치 순서가 곧 읽기 순서이므로 세로 위치로 다시 정렬한다. 나란히 놓인 블록이
        낮은 띠로만 있는 쪽(왼쪽 구역 이름 + 오른쪽 표가 줄줄이 놓인 신청서 양식)은 띠 순서로 읽는다 (`_band_order`).
        그 밖의 다단 쪽은 그대로 둔다.
        쪽 순서도 바로잡는다: Docling이 쪽 맨 아래 표를 다음 쪽 앞부분 뒤에 두는 경우가 있었다 (정답 세트 synth_11).
        """
        pages: dict[int, list[Block]] = {}
        breaks: dict[int, PageBreak] = {}
        cur = None
        for b in self.doc.blocks:
            if isinstance(b, PageBreak):
                breaks.setdefault(b.prov.page, b)  # 쪽 나눔 블록은 그 id를 그대로 다시 쓴다
                continue
            cur = b.prov.page if b.prov else cur
            pages.setdefault(cur, []).append(b)
        out: list[Block] = []
        for i, n in enumerate(sorted(pages, key=lambda n: (n is None, n or 0))):
            page = pages[n]
            located = [b for b in page if b.prov and b.prov.bbox]
            if len(located) == len(page) and not _side_by_side(located):
                page.sort(key=lambda b: b.prov.bbox[1])
            elif len(located) == len(page) and n is not None:
                page[:] = _band_order(page, self.pages.page(n).height) or page
            if i:
                out.append(breaks.get(n) or PageBreak(id=self._new_id(), prov=self._prov(n, None)))
            out.extend(page)
        self.doc.blocks = out

    def _merge_split_tables(self) -> None:
        """쪽을 넘어가며 둘로 나뉜 표를 하나로 잇는다.

        앞 쪽의 마지막 블록이 표이고 다음 쪽의 첫 블록이 열 수·열 너비가 같은 표면 같은 표다. 여러 쪽에 걸친 표는
        끝까지 잇는다 (예전에는 이은 표의 첫 쪽 번호로 다음 쪽을 판단해서 두 쪽씩만 이어졌다: 12쪽 표가 표 6개가 됨).
        다음 쪽 표가 머리 행을 되풀이했으면 앞 표의 첫 행부터 글이 같은 행을 모두 뺀다 (두 줄 머리 행).
        """
        blocks = self.doc.blocks
        out: list[Block] = []
        i = 0
        while i < len(blocks):
            b = blocks[i]
            out.append(b)
            i += 1
            last_page = b.prov.page if isinstance(b, Table) and b.prov else None
            while isinstance(b, Table):
                j = i
                while j < len(blocks) and isinstance(blocks[j], PageBreak):
                    j += 1
                if j == i or j >= len(blocks) or not _same_table(b, blocks[j], last_page):
                    break
                nxt = blocks[j]
                rows = nxt.rows
                k = 0
                while k < len(rows) and k < len(b.rows) and _row_texts(rows[k]) == _row_texts(b.rows[k]):
                    k += 1  # 되풀이한 머리 행 (한 줄 이상)
                if k == len(rows):
                    k = 0  # 다음 쪽 표 전체가 앞 표의 머리와 같으면 머리 행이 아니라 내용이다
                b.rows.extend(rows[k:])
                last_page = nxt.prov.page
                out.extend(blocks[i:j])  # 쪽 나눔 표시는 이은 표 뒤로
                i = j + 1
        self.doc.blocks = out

    def _merge_split_paragraphs(self) -> None:
        """한 문단이 둘로 나뉜 곳을 잇는다.

        Docling은 내어쓰기한 문단(둘째 줄부터 들여 쓴 개조식 문단)의 이어지는 줄을 다른 문단으로 떼거나,
        쪽이 넘어가는 문단을 쪽마다 따로 둔다. 다음을 모두 만족하면 같은 문단으로 본다.
        - 앞 문단의 마지막 줄이 본문 오른쪽 끝까지 찼다 (줄이 넘어간 것)
        - 앞 문단이 문장으로 끝나지 않았다
        - 뒤 문단이 개조식 기호·번호로 시작하지 않고, 글자 크기·굵기가 같다
        - 같은 쪽이면 바로 아래 줄(줄 간격 이내), 다른 쪽이면 앞 쪽의 마지막 문단과 다음 쪽의 첫 문단
        """
        blocks = self.doc.blocks
        out: list[Block] = []
        i = 0
        while i < len(blocks):
            b = blocks[i]
            out.append(b)
            i += 1
            while isinstance(b, Paragraph):
                j = i
                while j < len(blocks) and isinstance(blocks[j], PageBreak):
                    j += 1
                if j >= len(blocks) or not self._continues(b, blocks[j], crossed_page=j > i):
                    break
                self._absorb(b, blocks[j])
                if j > i:
                    out.extend(blocks[i:j])  # 쪽 나눔 표시는 남긴다 (이은 문단 뒤로)
                i = j + 1
        self.doc.blocks = out

    def _continues(self, a: Paragraph, b: Block, crossed_page: bool) -> bool:
        if not isinstance(b, Paragraph) or not a.prov or not b.prov or not a.prov.bbox or not b.prov.bbox:
            return False
        ta, tb = a.text.strip(), b.text.strip()
        if not ta or not tb or _SENTENCE_END.search(ta) or match_marker(tb) or _CAPTION.match(tb):
            return False
        sa, sb = _first_style(a), _first_style(b)
        if sa and sb and (abs((sa.size_pt or 0) - (sb.size_pt or 0)) > 0.6 or bool(sa.bold) != bool(sb.bold)):
            return False
        size = (sa.size_pt if sa and sa.size_pt else 10.0)
        ab, bb = a.prov.bbox, b.prov.bbox
        if crossed_page:
            if b.prov.page != a.prov.page + 1:
                return False
        elif b.prov.page != a.prov.page or not (0 <= bb[1] - ab[3] <= size * 1.0) or bb[0] > ab[2] or bb[2] < ab[0]:
            return False
        # 앞 문단의 마지막 줄이 본문 오른쪽 끝까지 찼는가
        content = self._content.get(a.prov.page)
        if self.ocr:  # 스캔: OCR 글자 상자의 마지막 줄
            cells = [c for c in self._ocr_cells.get(a.prov.page, []) if inside_ratio(c.box, expand(ab, 2)) > 0.6]
            if not cells or not content:
                return False
            last = max((c.box[1] + c.box[3]) / 2 for c in cells)
            line = [c for c in cells if abs((c.box[1] + c.box[3]) / 2 - last) < (c.box[3] - c.box[1]) * 0.5]
        else:
            page = self.pages.page(a.prov.page)
            chars = [c for c in page.chars_in(ab) if c.text.strip()]
            if not chars or not content:
                return False
            last = max(c.origin[1] for c in chars)
            line = [c for c in chars if abs(c.origin[1] - last) < size * 0.5]
        right, left = max(c.box[2] for c in line), min(c.box[0] for c in line)
        # 오른쪽 정렬한 짧은 줄('2023년 2월 28일 제정')은 끝까지 찬 줄이 아니다: 줄 길이도 본문 폭의 절반 이상이어야 한다
        return right >= content[2] - max(size * 1.5, 10) and right - left >= (content[2] - content[0]) * 0.5

    def _absorb(self, a: Paragraph, b: Paragraph) -> None:
        """b를 a 뒤에 잇는다. 이은 자리의 공백은 줄 합치기와 같은 규칙으로 정한다."""
        ta, tb = a.text.rstrip(), b.text.lstrip()
        if is_hangul_syllable(ta[-1:]) and is_hangul_syllable(tb[:1]):
            sep = " " if self._space_at_join(ta, tb) else ""
        elif ta.endswith("-") and ta[-2:-1].isascii() and ta[-2:-1].isalpha():
            sep = ""
        else:
            sep = " "
        if a.inlines and isinstance(a.inlines[-1], Text):
            a.inlines[-1] = a.inlines[-1].model_copy(update={"text": a.inlines[-1].text.rstrip() + sep})
        rest = [x.model_copy() for x in b.inlines]
        if rest and isinstance(rest[0], Text):
            rest[0] = rest[0].model_copy(update={"text": rest[0].text.lstrip()})
        a.inlines.extend(rest)
        if b.prov.page == a.prov.page:
            a.prov = a.prov.model_copy(update={"bbox": tuple(round(v, 1) for v in union(a.prov.bbox, b.prov.bbox))})

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

    def _demote_non_titles(self) -> None:
        """Docling이 절 제목으로 분류했지만 제목이 아닌 줄을 문단으로 되돌린다.

        날짜뿐인 줄('2026년 9월 14일'), 공문의 '- 다 음 -' 구분 줄, 문장으로 끝나는 줄('< 본 계약은 청렴계약(서약)제가
        적용됩니다 >', '□ … 다음과 같습니다.'). 큰 글씨라서 제목으로 분류되지만 제목은 문장이 아니다 (실제 문서 구조 정답:
        한남대 공고서 제목 정밀도 0.625).
        """
        demoted = set()
        blocks = self.doc.blocks
        for k, b in enumerate(blocks):
            # 표·그림 바로 앞의 '< … >' 줄은 그 표·그림의 제목(캡션)이다. 절 제목이 아니다 (2026-10-18 정한 기준:
            # 보도자료 '< 생활제품 측정 결과 >'). 표 뒤의 줄은 다음 절 제목일 수 있어 그대로 둔다
            caption = (isinstance(b, Heading) and _BRACKET_LINE.match(b.text) is not None and k + 1 < len(blocks)
                       and isinstance(blocks[k + 1], (Table, Figure)))
            if isinstance(b, Heading) and (_not_a_title(b.text) or caption):
                self.doc.blocks[k] = Paragraph(id=b.id, prov=b.prov, inlines=b.inlines, style=b.style)
                demoted.add(id(b))
        if demoted:
            self._heading_sizes = [(h, sz) for h, sz in getattr(self, "_heading_sizes", []) if id(h) not in demoted]

    def _assign_heading_levels(self) -> None:
        """Docling은 제목 단계를 거의 1로 준다. 글자 크기 순서로 단계를 다시 매긴다.

        스캔(OCR)은 글자 크기를 글 상자 높이로 짐작해서 같은 단계 제목도 11~14pt로 흩어진다. 크기 순서로 매기면 크기마다
        새 단계가 생겨 6단계까지 벌어졌다 (정답 세트 스캔 제목 단계 0.572). 스캔은 번호 종류가 처음 나온 순서(Ⅰ. → 1,
        1. → 2 …)로 단계를 정하고, 번호 없는 제목(표지 제목 등)은 1단계로 둔다.
        """
        entries = getattr(self, "_heading_sizes", [])
        if self.ocr:
            order: dict[str, int] = {}
            for h, _ in entries:
                m = match_marker(h.text.strip())
                h.level = 1 if m is None else min(order.setdefault(m[0], len(order)) + 1, 6)
                h.style_name = None
            return
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

    # ------------------------------------------------------------------
    # 괘선 표: PDF에 그어진 선으로 행·열·병합 칸을 읽는다
    # ------------------------------------------------------------------

    def _find_ruled_tables(self, ddoc) -> None:
        """Docling이 찾은 표마다 괘선 격자를 찾는다. 같은 격자를 가리키는 표(한 표를 둘로 나눈 경우)는 하나로."""
        from docling_core.types.doc import TableItem

        self._ruled: dict[str, object] = {}  # 표 self_ref → Grid 또는 "dup"
        self._ruled_boxes: dict[int, list[BBox]] = {}
        self._extra_tables = []
        if self.ocr:
            self._find_scan_grids(ddoc)
            return
        for item, _ in ddoc.iterate_items():
            if not isinstance(item, TableItem) or not item.prov:
                continue
            prov = item.prov[0]
            page, box = self.pages.page(prov.page_no), self._box(prov, ddoc)
            grid = build_grid(page.rules, box)
            rows = item.data.num_rows if self.ocr else None  # 표 구조 모델을 돌리지 않았으면 행 수를 모른다
            if grid is None or not self._grid_fits(grid, page, box, rows):
                if _is_toc(item):
                    continue  # 선이 없는 목차는 표로 만들지 않는다 (줄마다 문단으로)
                # 세로선 없이 위아래(와 머리 행 아래)에만 가로선이 있는 '열린 표': 글 위치로 행·열을 정한다
                grid = text_grid(page.chars, page.rules, box)
                if grid is None or not self._covers_text(grid, page, box):
                    continue
            gbox = (grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1])
            seen = self._ruled_boxes.setdefault(prov.page_no, [])
            if any(inside_ratio(gbox, expand(b, 2)) > 0.9 for b in seen):
                self._ruled[item.self_ref] = "dup"
                continue
            seen.append(gbox)
            self._ruled[item.self_ref] = grid
        self._find_missed_grids(ddoc)
        self._find_open_continuations(ddoc)

    def _find_scan_grids(self, ddoc) -> None:
        """스캔본의 표: 쪽 그림에서 찾은 괘선(`scan_rules`)으로 격자를 만든다.

        표 구조 인식 모델은 스캔에서 병합 칸을 자주 놓쳤다 (실제 스캔 '구분' 두 줄 병합 칸, 합성 스캔 표 TEDS 0.766).
        표 영역 OCR 글자의 90% 이상이 격자 안에 있고, 격자가 2행 이하인데 모델이 4행 이상을 찾은 경우(행 구분선 없는 표)가
        아닐 때만 쓴다. 쓰지 못하면 모델 결과 그대로.
        """
        from docling_core.types.doc import TableItem

        from .ocr_spacing import SCALE
        from .scan_rules import image_rules

        rules: dict[int, list] = {}
        for item, _ in ddoc.iterate_items():
            if not isinstance(item, TableItem) or not item.prov:
                continue
            n, box = item.prov[0].page_no, self._box(item.prov[0], ddoc)
            if n not in rules:
                rules[n] = image_rules(self._page_image(n).ink, SCALE)
            grid = build_grid(rules[n], box)
            if grid is None or (grid.n_rows <= 2 and item.data.num_rows > 3):
                continue
            gbox = expand((grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1]), 1)
            cells = [c for c in self._ocr_cells.get(n, []) if inside_ratio(c.box, expand(box, 2)) > 0.6]
            inside = [c for c in cells if gbox[0] <= (c.box[0] + c.box[2]) / 2 <= gbox[2]
                      and gbox[1] <= (c.box[1] + c.box[3]) / 2 <= gbox[3]]
            if not cells or len(inside) < 0.9 * len(cells):
                continue
            seen = self._ruled_boxes.setdefault(n, [])
            if any(inside_ratio(gbox, expand(b, 2)) > 0.9 for b in seen):
                self._ruled[item.self_ref] = "dup"
                continue
            seen.append((grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1]))
            self._ruled[item.self_ref] = grid

    def _find_open_continuations(self, ddoc) -> None:
        """쪽을 넘어간 열린 표의 나머지를 찾는다.

        열린 표는 다음 쪽 나머지에 세로선도 머리 행도 없어 Docling이 표로 찾지 못한다. 앞 쪽의 마지막 본문 블록이 글 위치로
        읽은 표이고, 다음 쪽의 첫 본문 블록 자리에 같은 가로 범위의 가로선 두 개가 있으면, 앞 표의 열 경계로 그 사이를 읽는다.
        쪽을 넘어간 표 잇기(`_merge_split_tables`)가 열 너비가 같은 두 표를 하나로 합친다.
        """
        from docling_core.types.doc import ContentLayer, PictureItem, TableItem

        body: dict[int, list[BBox]] = {}
        taken: dict[int, list[BBox]] = {}
        for item, _ in ddoc.iterate_items(included_content_layers={ContentLayer.BODY}):
            if item.prov:
                body.setdefault(item.prov[0].page_no, []).append(self._box(item.prov[0], ddoc))
                if isinstance(item, (TableItem, PictureItem)):
                    taken.setdefault(item.prov[0].page_no, []).append(self._box(item.prov[0], ddoc))
        grids = []
        for item, _ in ddoc.iterate_items():
            g = self._ruled.get(getattr(item, "self_ref", None))
            if isinstance(item, TableItem) and item.prov and not isinstance(g, str) and g is not None and g.source == "text":
                grids.append((item.prov[0].page_no, g))
        for n, grid in grids:
            if n >= len(self.pages):
                continue
            if any(b[1] > grid.ys[-1] + 2 for b in body.get(n, [])):
                continue  # 표 아래에 다른 본문이 있다: 이 쪽에서 끝난 표
            page = self.pages.page(n + 1)
            left, right = grid.xs[0], grid.xs[-1]
            hs = sorted(r.pos for r in page.rules
                        if r.axis == "h" and abs(r.a - left) <= 3 and abs(r.b - right) <= 3)
            hs = [y for i, y in enumerate(hs) if i == 0 or y - hs[i - 1] > 2]
            if len(hs) < 2 or any(b[3] < hs[0] - 2 for b in body.get(n + 1, [])):
                continue  # 다음 쪽 맨 위가 표가 아니다
            region = (left, hs[0], right, hs[1])
            if any(intersect(region, expand(b, 2)) for b in taken.get(n + 1, []) + self._ruled_boxes.get(n + 1, [])):
                continue
            rest = text_grid(page.chars, page.rules, region, columns=grid.xs)
            if rest is None:
                continue
            self._ruled_boxes.setdefault(n + 1, []).append((left, hs[0], right, hs[1]))
            self._extra_tables.append((n + 1, rest))

    def _find_missed_grids(self, ddoc) -> None:
        """Docling이 표로 찾지 못한 괘선 표를 선만으로 찾는다.

        쪽을 넘어간 표의 짧은 나머지(두세 행)나 서식의 칸 표를 Docling이 표가 아닌 글로 보는 경우가 있다. 이어진 선
        묶음마다 격자를 만들어 2열 이상이고 글이 든 칸이 둘 이상이면 표로 본다. Docling 표·그림 영역과 겹치는 묶음은
        건너뛴다 (그림 속 눈금선을 표로 보지 않도록).
        """
        from docling_core.types.doc import PictureItem, TableItem

        self._extra_tables: list[tuple[int, object]] = []
        taken: dict[int, list[BBox]] = {}
        for item, _ in ddoc.iterate_items():
            if isinstance(item, (TableItem, PictureItem)) and item.prov:
                taken.setdefault(item.prov[0].page_no, []).append(self._box(item.prov[0], ddoc))
        for n in range(1, len(self.pages) + 1):
            page = self.pages.page(n)
            blocked = taken.get(n, []) + self._ruled_boxes.get(n, [])
            for region in rule_regions(page.rules):
                if any(intersect(region, expand(b, 2)) for b in blocked):
                    continue
                grid = build_grid(page.rules, region, pad=1)
                if grid is None:
                    continue
                gbox = (grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1])
                filled = sum(1 for c in grid.cells if any(ch.text.strip() for ch in page.chars_in(c.box, 0)))
                if filled < 2 or (grid.n_rows <= 2 and _rows_without_lines(grid, page)):
                    continue
                self._ruled_boxes.setdefault(n, []).append(gbox)
                self._extra_tables.append((n, grid))

    @staticmethod
    def _grid_fits(grid, page: PageInfo, box: BBox, docling_rows: int | None) -> bool:
        """괘선 격자를 믿을 수 있는가.

        - 행 구분선이 없는 표(머리 행 위아래에만 선)는 본문 행이 하나로 뭉친다 → 격자가 2행 이하인데
          Docling은 여러 행을 찾았으면 쓰지 않는다. 선이 있는 표에서는 Docling이 행을 더 잘게 나눠도 선을 믿는다
        - Docling 표 영역의 글자 대부분이 격자 안에 있어야 한다 (격자가 표의 일부만 덮으면 글이 빠진다)
        """
        if grid.n_rows <= 2:
            if docling_rows is not None:
                if docling_rows > 3:
                    return False
            elif _rows_without_lines(grid, page):
                return False
        return PdfParser._covers_text(grid, page, box)

    @staticmethod
    def _covers_text(grid, page: PageInfo, box: BBox) -> bool:
        """Docling 표 영역 글자의 90% 이상이 격자 안에 있는가 (격자가 표의 일부만 덮으면 글이 빠진다)."""
        gbox = expand((grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1]), 1)
        chars = [c for c in page.chars_in(box) if c.text.strip()]
        if not chars:
            return False
        inside = sum(1 for c in chars if gbox[0] <= (c.box[0] + c.box[2]) / 2 <= gbox[2]
                     and gbox[1] <= (c.box[1] + c.box[3]) / 2 <= gbox[3])
        return inside >= 0.9 * len(chars)

    def _covered_by_table(self, item, page_no: int, box: BBox, ddoc) -> bool:
        """이 글이 표 블록 안에 이미 들어가는가.

        괘선 표는 격자 안, 모델로 읽은 표는 모델이 찾은 표 영역 안의 글만 표가 담는다. Docling이 표 근처의 주석
        ('※ …')을 표에 묶어 두는 경우가 있어, 그 밖의 글은 본문으로 남긴다.
        """
        if self._in_ruled_table(page_no, box):
            return True
        table = _table_of(item, ddoc)
        if table is None or self.ocr:
            return False
        if _is_toc(table) and table.self_ref not in self._ruled:
            return False  # 선 없는 목차: 줄(글 항목)을 문단으로 둔다
        ref = table.self_ref
        if ref in self._ruled:
            return False  # 괘선 격자 밖의 글
        model = self._model_tables.get(ref)
        if model is None:
            return True  # 표 영역을 문단으로 풀어 쓴 경우: 그 문단에 이미 들어 있다
        mt, mdoc = model
        return inside_ratio(box, expand(self._box(mt.prov[0], mdoc), 2)) > 0.6

    def _in_ruled_table(self, page_no: int, box: BBox) -> bool:
        return any(inside_ratio(box, expand(g, 1)) > 0.8 for g in self._ruled_boxes.get(page_no, []))

    def _ruled_table(self, grid, page: PageInfo) -> Table:
        gbox = (grid.xs[0], grid.ys[0], grid.xs[-1], grid.ys[-1])
        # 글 위치로 정한 격자(열린 표)는 선으로 읽은 격자보다 덜 확실하다
        table = Table(id=self._new_id(), prov=self._prov(page.number, gbox, confidence=1.0 if grid.source == "rules" else 0.8),
                      width_pt=round(gbox[2] - gbox[0], 1))
        rows = [Row(height_pt=round(grid.ys[i + 1] - grid.ys[i], 1)) for i in range(grid.n_rows)]
        for gc in grid.cells:
            cell = Cell(row_span=gc.r1 - gc.r0 + 1, col_span=gc.c1 - gc.c0 + 1, v_align="middle")
            inner = (gc.box[0] + 0.5, gc.box[1] + 0.5, gc.box[2] - 0.5, gc.box[3] - 0.5)
            if self.ocr:  # 스캔: 가운데가 칸 안에 있는 OCR 상자
                ocr = [c for c in self._ocr_cells.get(page.number, [])
                       if inner[0] <= (c.box[0] + c.box[2]) / 2 <= inner[2] and inner[1] <= (c.box[1] + c.box[3]) / 2 <= inner[3]]
                paragraphs = self._cell_paragraphs(self._ocr_lines(page.number, ocr)) if ocr else []
            else:
                paragraphs = self._cell_paragraphs(page.text_lines(inner, pad=0))
            if paragraphs:
                style, _ = self._text_style(page, inner) if not self.ocr else (None, None)
                layout = self._cell_align(page, inner)
                cell.blocks = [Paragraph(id=self._new_id(), prov=self._prov(page.number, gc.box),
                                         inlines=[Text(text=t, style=style)], style=layout) for t in paragraphs]
            bg = page.fill_at((gc.box[0] + gc.box[2]) / 2, (gc.box[1] + gc.box[3]) / 2)
            if bg and _luma(bg) > 90:
                cell.background = bg
            # 원래 선의 모양(실선·파선·점선)과 굵기. 글 위치로 정한 격자처럼 모양 정보가 없으면 0.5pt 실선
            cell.borders = Borders(**{side: Border(style=gc.line_styles.get(side, ("solid", 0.5))[0],
                                                   width_pt=max(gc.line_styles.get(side, ("solid", 0.5))[1], 0.25),
                                                   color="#000000")
                                      for side, has in gc.borders.items() if has})
            rows[gc.r0].cells.append(cell)
        table.rows = rows
        table.col_widths_pt = [round(grid.xs[j + 1] - grid.xs[j], 1) for j in range(grid.n_cols)]
        table.cell_padding_pt = (1.5, 4, 1.5, 4)
        return table

    @staticmethod
    def _cell_align(page: PageInfo, inner: BBox) -> ParagraphStyle | None:
        """칸 안 글의 좌우 여백으로 정렬을 정한다 (가운데·오른쪽 정렬은 표에서 흔하다)."""
        chars = [c for c in page.chars_in(inner, 0) if c.text.strip()]
        if not chars:
            return None
        lm = min(c.box[0] for c in chars) - inner[0]
        rm = inner[2] - max(c.box[2] for c in chars)
        if lm > 8 and abs(lm - rm) < max(3.0, 0.2 * (lm + rm)):  # 칸을 거의 채운 글은 왼쪽 정렬로 둔다
            return ParagraphStyle(align="center")
        if rm < 4 and lm > 8:
            return ParagraphStyle(align="right")
        return None

    def _cell_paragraphs(self, lines: list[str]) -> list[str]:
        """칸 안의 줄을 문단으로 묶는다. 개조식 기호·번호로 시작하는 줄은 새 문단이다."""
        groups: list[list[str]] = []
        for ln in lines:
            if not groups or match_marker(ln.strip()):
                groups.append([ln])
            else:
                groups[-1].append(ln)
        out = []
        for g in groups:
            text = _SPACE_BEFORE_PUNCT.sub("", self._resolve_gaps(self._join_lines(g)))
            text = text.replace(AMBIGUOUS_GAP, "").strip()
            if text:
                out.append(text)
        return out

    def _table(self, item, page: PageInfo, box: BBox, ddoc) -> list[Block]:
        grid = self._ruled.get(item.self_ref)
        if grid == "dup":
            return []  # 앞의 표와 같은 괘선 표 (Docling이 한 표를 둘로 나눈 경우)
        if grid is not None:
            return [self._ruled_table(grid, page)]
        if not self.ocr:
            model = self._model_tables.get(item.self_ref)
            if model is None:
                # 괘선도 없고 모델도 표를 찾지 못한 영역: 글을 잃지 않도록 문단으로 둔다
                return [Paragraph(id=self._new_id(), prov=self._prov(page.number, box),
                                  inlines=[Text(text=t, style=self._text_style(page, box)[0])])
                        for t in self._cell_paragraphs(page.text_lines(box))]
            item = model[0]
        return [self._model_table(item, page, box)]

    def _model_table(self, item, page: PageInfo, box: BBox) -> Table:
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

        # 그림 아래 글은 목록 묶음(ListGroup) 같은 묶음 아래에 한 단계 더 들어 있기도 하다. 끝까지 따라 내려간다
        # (예전에는 바로 아래 자식만 읽어 묶음 안 글이 경고 없이 빠졌다)
        refs: list = []

        def descend(node, depth=0):
            for c in node.children:
                refs.append(c)
                if depth < 8:
                    descend(c.resolve(ddoc), depth + 1)

        descend(item)
        used: set[str] = {c.cref for c in refs} | {c.cref for c in item.captions}
        graphics = [g.box for g in page.graphics if inside_ratio(g.box, expand(box, 2)) > 0.6]
        groups = cluster(graphics, gap=10) if graphics else []
        groups = [g for g in groups if (g[2] - g[0]) > 8 and (g[3] - g[1]) > 8]
        if not groups or self.ocr:
            groups = [box]  # 그래픽 정보가 없으면 (스캔본 등) Docling 영역 그대로

        texts = []
        seen: set[str] = set()
        for ref in refs + list(item.captions):
            if ref.cref in seen:
                continue
            seen.add(ref.cref)
            t = ref.resolve(ddoc)
            if isinstance(t, TextItem) and t.prov and t.text.strip():
                texts.append((t, self._box(t.prov[0], ddoc)))
        # Docling이 캡션을 captions가 아니라 그림의 자식(label=caption)으로 넣는 경우도 있다 (스캔 '그림 1. 분기별 매출 추이')
        caption_refs = {c.cref for c in item.captions} | {t.self_ref for t, _ in texts if t.label == DocItemLabel.CAPTION}
        if self.ocr:
            # 스캔: 그림 영역 위쪽에 걸친 번호 없는 제목·글(차트 제목 '분기별 매출(억원)')은 그림 안 글자다.
            # Docling은 이것을 그림 밖 절 제목으로 분류한다
            for t in ddoc.texts:
                if (t.self_ref in self._done or t.self_ref in used or len(t.prov) != 1 or t.prov[0].page_no != page.number
                        or t.label not in (DocItemLabel.SECTION_HEADER, DocItemLabel.TEXT) or not t.text.strip()):
                    continue
                tb = self._box(t.prov[0], ddoc)
                if (inside_ratio(tb, expand(box, 3)) >= 0.3 and tb[0] >= box[0] - 3 and tb[2] <= box[2] + 3
                        and match_marker(t.text.strip()) is None and not _CAPTION.match(t.text.strip())):
                    used.add(t.self_ref)
                    texts.insert(0, (t, expand(box, -1)))

        figures: list[tuple[BBox, list[str]]] = [(g, []) for g in groups]
        loose: list[tuple[object, BBox]] = []
        for t, tb in texts:
            home = next((f for f in figures if inside_ratio(tb, expand(f[0], 3)) > 0.7), None)
            if home is not None and t.self_ref not in caption_refs:
                home[1].append(t.text)
            else:
                loose.append((t, tb))

        if not self.ocr:
            # Docling은 그림 안 글자의 일부만 그림의 자식으로 넣는다 (상자 모양 '개요' 그림에서 '○ 제목: …' 같은 줄이
            # 자식에 없어 경고 없이 빠졌다). 텍스트 레이어가 있으면 그림 영역 안 글을 레이어에서 직접 읽고,
            # 다른 블록(그림 밖으로 나간 캡션, 그림과 겹친 문단)이 담는 글자는 뺀다.
            others = [self._box(t.prov[0], ddoc) for t in ddoc.texts
                      if t.prov and t.prov[0].page_no == page.number and t.self_ref not in used]
            others += [tb for _, tb in loose]
            for k, (g, inner) in enumerate(figures):
                layer = [ln.replace(AMBIGUOUS_GAP, " ").strip() for ln in page.text_lines(g, exclude=others)]
                layer = [ln for ln in layer if ln]
                if sum(len("".join(ln.split())) for ln in layer) > sum(len("".join(t.split())) for t in inner):
                    figures[k] = (g, layer)

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


def _link_span(b, label: str, uri: str) -> bool:
    """문단 b의 글에서 label(글자·숫자만)과 같은 부분을 찾아 그 부분만 링크 달린 Text로 나눈다. 찾으면 True."""
    for k, inl in enumerate(b.inlines):
        if not isinstance(inl, Text) or inl.link:
            continue
        pos = [i for i, ch in enumerate(inl.text) if ch.isalnum()]  # 글자·숫자만 → 원래 위치
        flat = "".join(inl.text[i] for i in pos)
        at = flat.find(label)
        if at < 0:
            continue
        s, e = pos[at], pos[at + len(label) - 1] + 1
        parts = [inl.model_copy(update={"text": inl.text[:s]}),
                 inl.model_copy(update={"text": inl.text[s:e], "link": uri}),
                 inl.model_copy(update={"text": inl.text[e:]})]
        b.inlines[k:k + 1] = [p for p in parts if p.text]
        return True
    return False


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
