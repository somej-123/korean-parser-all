"""통합 문서 모델 (IR, Intermediate Representation).

모든 파서는 이 모델로 결과를 내고, 모든 출력(HTML, LLM 텍스트, JSON)은 이 모델에서 만든다.
원본을 다시 그릴 수 있을 만큼의 정보(서식, 병합, 좌표, 도형)를 담는 것이 목표다.

단위
- 길이는 pt(포인트, 1/72인치)로 통일한다. HTML로 그릴 때 그대로 `pt` 단위를 쓴다.
- 색은 "#RRGGBB" 문자열이다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------


class Provenance(_Model):
    """이 요소를 어떤 파서가, 원본의 어디에서 만들었는지.

    같은 문서를 여러 파서로 처리해 요소별로 결과를 고를 때 근거가 된다.
    """

    parser: str
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None  # (x0, y0, x1, y1) pt
    confidence: float | None = None


class TextStyle(_Model):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    color: str | None = None
    background: str | None = None
    size_pt: float | None = None
    font: str | None = None
    vertical: Literal["super", "sub"] | None = None
    caps: Literal["all", "small"] | None = None  # 모두 대문자 / 작은 대문자로 표시 (원문 글자는 그대로)
    letter_spacing_em: float | None = None  # 자간 (글자 크기 대비, -0.05 = -5%)
    width_ratio: float | None = None  # 장평 (1.0 = 100%)


class ParagraphStyle(_Model):
    align: Literal["left", "center", "right", "justify"] | None = None
    indent_left_pt: float | None = None
    indent_right_pt: float | None = None
    indent_first_pt: float | None = None  # 음수면 내어쓰기
    space_before_pt: float | None = None
    space_after_pt: float | None = None
    line_spacing: float | None = None  # 배수 (1.0 = 한 줄)
    line_spacing_pt: float | None = None  # 고정/최소 줄 간격
    background: str | None = None
    borders: Borders | None = None


class Border(_Model):
    style: Literal["solid", "double", "dashed", "dotted", "none"] = "solid"
    width_pt: float = 0.5
    color: str | None = None


class Borders(_Model):
    top: Border | None = None
    bottom: Border | None = None
    left: Border | None = None
    right: Border | None = None


# ---------------------------------------------------------------------------
# 인라인 (문단 안의 요소)
# ---------------------------------------------------------------------------


class Text(_Model):
    type: Literal["text"] = "text"
    text: str
    style: TextStyle | None = None
    link: str | None = None


class Break(_Model):
    type: Literal["break"] = "break"
    kind: Literal["line", "page", "column"] = "line"


class Tab(_Model):
    type: Literal["tab"] = "tab"


class ImageRef(_Model):
    type: Literal["image"] = "image"
    image_id: str
    width_pt: float | None = None
    height_pt: float | None = None
    alt: str | None = None


class NoteRef(_Model):
    type: Literal["note_ref"] = "note_ref"
    note_id: str
    label: str


class Equation(_Model):
    """수식. script는 원본 형식의 수식 문자열 (HWP 수식 편집기 문법 또는 LaTeX)."""

    type: Literal["equation"] = "equation"
    script: str
    syntax: Literal["hwp", "latex", "mathml"] = "hwp"


Inline = Annotated[Union[Text, Break, Tab, ImageRef, NoteRef, Equation], Field(discriminator="type")]


# ---------------------------------------------------------------------------
# 블록
# ---------------------------------------------------------------------------


class ListInfo(_Model):
    """원본 문서의 자동 번호/글머리표. marker는 화면에 보이는 그대로 계산한 값."""

    level: int
    marker: str
    ordered: bool
    list_id: str | None = None


class OutlineInfo(_Model):
    """개조식(□ ○ -)이나 직접 입력한 번호(1. 가.)로 표현된 계층 (한국어 처리 레이어가 채움)."""

    level: int
    marker: str
    kind: str


class _Block(_Model):
    id: str = ""
    prov: Provenance | None = None


class Paragraph(_Block):
    type: Literal["paragraph"] = "paragraph"
    inlines: list[Inline] = Field(default_factory=list)
    style: ParagraphStyle | None = None
    style_name: str | None = None
    list_info: ListInfo | None = None
    outline: OutlineInfo | None = None

    @property
    def text(self) -> str:
        return inline_text(self.inlines)


class Heading(_Block):
    type: Literal["heading"] = "heading"
    level: int  # 1~6
    inlines: list[Inline] = Field(default_factory=list)
    style: ParagraphStyle | None = None
    style_name: str | None = None
    list_info: ListInfo | None = None

    @property
    def text(self) -> str:
        return inline_text(self.inlines)


class Cell(_Model):
    blocks: list[Block] = Field(default_factory=list)
    row_span: int = 1
    col_span: int = 1
    background: str | None = None
    borders: Borders | None = None
    v_align: Literal["top", "middle", "bottom"] | None = None


class Row(_Model):
    cells: list[Cell] = Field(default_factory=list)
    height_pt: float | None = None
    header: bool = False


class Table(_Block):
    type: Literal["table"] = "table"
    rows: list[Row] = Field(default_factory=list)
    col_widths_pt: list[float] = Field(default_factory=list)
    width_pt: float | None = None
    align: Literal["left", "center", "right"] | None = None
    cell_padding_pt: tuple[float, float, float, float] | None = None  # top, right, bottom, left
    anchor: Anchor | None = None  # 본문 흐름 밖에 떠 있는 표 (HWP에서 흔함)

    @property
    def has_merged_cells(self) -> bool:
        return any(c.row_span > 1 or c.col_span > 1 for r in self.rows for c in r.cells)


class Shape(_Model):
    """도형 하나. 좌표는 Drawing 캔버스 기준(pt)."""

    id: str | None = None
    name: str | None = None
    geometry: str = "rect"  # rect, ellipse, line, rightArrow, ... (OOXML preset 이름)
    path: str | None = None  # 사용자 정의 도형: SVG path (캔버스 좌표)
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0
    rotation: float = 0
    flip_h: bool = False
    flip_v: bool = False
    fill: str | None = None
    stroke: str | None = None
    stroke_width_pt: float = 0.75
    stroke_dash: Literal["solid", "dashed", "dotted"] = "solid"
    head_arrow: bool = False  # 선 시작점 화살표
    tail_arrow: bool = False  # 선 끝점 화살표
    connect_from: str | None = None  # 연결선이 이어진 도형 id
    connect_to: str | None = None
    text_blocks: list[Block] = Field(default_factory=list)
    text_v_align: Literal["top", "middle", "bottom"] = "middle"
    image_id: str | None = None  # 그림 도형
    approximate: bool = False  # 원본 도형을 정확히 그리지 못하고 근사했는지

    @property
    def is_line(self) -> bool:
        return self.geometry in LINE_GEOMETRIES


LINE_GEOMETRIES = {
    "line",
    "straightConnector1",
    "bentConnector2",
    "bentConnector3",
    "bentConnector4",
    "bentConnector5",
    "curvedConnector2",
    "curvedConnector3",
    "curvedConnector4",
    "curvedConnector5",
}


class Anchor(_Model):
    """본문 흐름 밖에 떠 있는 개체의 배치 (Word의 '텍스트 배치' 설정).

    wrap: square/tight/through = 글이 옆으로 흐름, top_bottom = 위아래로만, none = 글 위/뒤에 겹침
    relative_h/v: 기준 (page, margin, column, paragraph 등). x/y는 기준에서의 거리(pt), 정렬(align)이면 None
    """

    wrap: Literal["square", "tight", "through", "top_bottom", "none"] = "square"
    relative_h: str = "column"
    relative_v: str = "paragraph"
    x_pt: float | None = None
    y_pt: float | None = None
    align_h: str | None = None
    align_v: str | None = None
    behind_text: bool = False


class Drawing(_Block):
    """도식: 도형, 연결선, 글상자의 묶음. HTML에서는 SVG로 그린다."""

    type: Literal["drawing"] = "drawing"
    width_pt: float
    height_pt: float
    shapes: list[Shape] = Field(default_factory=list)
    alt: str | None = None
    anchor: Anchor | None = None  # None이면 본문 흐름 안의 블록


class ChartSeries(_Model):
    name: str | None = None
    values: list[float | None] = Field(default_factory=list)
    color: str | None = None
    point_colors: list[str | None] = Field(default_factory=list)  # 원형 차트 조각별 색


class Chart(_Block):
    type: Literal["chart"] = "chart"
    chart_type: str  # bar, line, pie, doughnut, area, scatter, ...
    bar_dir: Literal["col", "bar"] | None = None  # 세로 막대(col) / 가로 막대(bar)
    grouping: str | None = None  # clustered, stacked, percentStacked, standard
    title: str | None = None
    categories: list[str] = Field(default_factory=list)
    series: list[ChartSeries] = Field(default_factory=list)
    width_pt: float | None = None
    height_pt: float | None = None


class Figure(_Block):
    """독립된 그림 블록 (PDF처럼 그림이 문단 밖에 있는 형식용)."""

    type: Literal["figure"] = "figure"
    image_id: str
    width_pt: float | None = None
    height_pt: float | None = None
    alt: str | None = None
    caption: str | None = None
    inner_text: str | None = None  # 그림·도식 안에 있는 글자 (PDF 텍스트 레이어나 OCR에서). LLM용
    style: ParagraphStyle | None = None  # 정렬·들여쓰기


class PageBreak(_Block):
    type: Literal["page_break"] = "page_break"


Block = Annotated[
    Union[Paragraph, Heading, Table, Drawing, Chart, Figure, PageBreak],
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# 문서
# ---------------------------------------------------------------------------


class Image(_Model):
    mime: str
    data_base64: str
    name: str | None = None


class Note(_Model):
    kind: Literal["footnote", "endnote"]
    label: str
    blocks: list[Block] = Field(default_factory=list)


class PageSetup(_Model):
    width_pt: float = 595.3  # A4
    height_pt: float = 841.9
    margin_top_pt: float = 72
    margin_bottom_pt: float = 72
    margin_left_pt: float = 72
    margin_right_pt: float = 72
    background: str | None = None  # 쪽 배경색
    background_image_id: str | None = None  # 쪽 배경 그림 (쪽 전체 크기로 늘림)


class Meta(_Model):
    title: str | None = None
    author: str | None = None
    created: str | None = None
    modified: str | None = None
    source_name: str | None = None
    source_format: str | None = None
    language: str | None = None


class TextRun(_Model):
    """고정 레이아웃용 글 조각: 원본 위치(기준선)와 너비 그대로 그린다."""

    text: str
    x: float
    baseline: float
    width: float
    size_pt: float
    color: str | None = None
    bold: bool = False
    italic: bool = False
    font: str | None = None
    block_id: str | None = None  # 이 글이 속한 블록 (LLM 답변 ↔ 화면 위치 연결)


class FixedPage(_Model):
    """원본과 같은 모양으로 보여 주기 위한 쪽: 글자를 뺀 바탕 그림 + 원래 위치의 글 조각.

    PDF처럼 쪽 배치가 정해진 문서에서 쓴다. 의미 구조(blocks)와 별개로 화면 재현용이다.
    """

    page: int
    width_pt: float
    height_pt: float
    background_image_id: str | None = None
    runs: list[TextRun] = Field(default_factory=list)


class Document(_Model):
    meta: Meta = Field(default_factory=Meta)
    page: PageSetup = Field(default_factory=PageSetup)
    default_text_style: TextStyle = Field(default_factory=TextStyle)
    blocks: list[Block] = Field(default_factory=list)
    header: list[Block] = Field(default_factory=list)
    footer: list[Block] = Field(default_factory=list)
    notes: dict[str, Note] = Field(default_factory=dict)
    images: dict[str, Image] = Field(default_factory=dict)
    fixed_pages: list[FixedPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def iter_blocks(self):
        """본문의 모든 블록을 표 셀과 글상자 안까지 깊이 우선으로 순회한다."""
        yield from _walk(self.blocks)


def _walk(blocks):
    for b in blocks:
        yield b
        if isinstance(b, Table):
            for row in b.rows:
                for cell in row.cells:
                    yield from _walk(cell.blocks)
        elif isinstance(b, Drawing):
            for shape in b.shapes:
                yield from _walk(shape.text_blocks)


def inline_text(inlines) -> str:
    out = []
    for i in inlines:
        if isinstance(i, Text):
            out.append(i.text)
        elif isinstance(i, Tab):
            out.append("\t")
        elif isinstance(i, Break):
            out.append("\n")
    return "".join(out)


ParagraphStyle.model_rebuild()
Cell.model_rebuild()
Shape.model_rebuild()
Table.model_rebuild()
Drawing.model_rebuild()
Document.model_rebuild()
