"""IR → HTML. Markdown을 거치지 않고 문서 모델에서 바로 화면용 HTML을 만든다.

- 서식(글꼴 크기, 굵게, 색), 문단 들여쓰기·간격, 자동 번호, 셀 병합·테두리·배경,
  이미지, 도형(SVG), 각주, 머리말/꼬리말을 원본에 가깝게 표현한다.
- 모든 블록에 data-kp-id를 달아, LLM 답변의 근거 위치를 화면에서 강조할 수 있게 한다.
- 한 파일로 완결되도록 CSS와 이미지(data URI)를 모두 안에 넣는다.
"""

from __future__ import annotations

import re
from html import escape

from ..ir import (
    Block, Equation, Border, Borders, Break, Chart, Document, Drawing, Figure, Heading, ImageRef, NoteRef,
    PageBreak, Paragraph, ParagraphStyle, Tab, Table, Text, TextStyle, walk_blocks,
)
from .chart_svg import render_chart_svg
from .svg import render_drawing, render_slide

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SAFE_FONT = re.compile(r"^[\w\s가-힣ㄱ-ㅎ.\-,']{1,64}$")
_SAFE_LINK = re.compile(r"^(https?://|mailto:|#)", re.IGNORECASE)
_KOREAN_FONTS = '"맑은 고딕", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", "Noto Sans CJK KR", sans-serif'
# 바탕·명조 계열 글꼴이 없는 환경에서는 고딕이 아니라 명조 계열로 대체한다
_KOREAN_SERIF = '"바탕", "Batang", "AppleMyungjo", "Noto Serif KR", "Nanum Myeongjo", "Noto Serif CJK KR", serif'
_SERIF_HINT = re.compile(r"바탕|명조|궁서|Batang|Myeongjo|Myungjo|Gungsuh|Times|Serif|Georgia|Garamond|Cambria", re.I)
_RENDERABLE = {"image/png", "image/jpeg", "image/gif", "image/svg+xml", "image/webp", "image/bmp"}

BASE_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 12px; background: #e9e9ec; color: #000; }
.kp-page { position: relative; background: #fff; margin: 0 auto 24px; max-width: 100%;
  box-shadow: 0 1px 3px rgba(0,0,0,.15), 0 4px 16px rgba(0,0,0,.08); overflow-x: auto; }
.kp-page p, .kp-page h1, .kp-page h2, .kp-page h3, .kp-page h4, .kp-page h5, .kp-page h6 {
  margin: 0; font-size: inherit; font-weight: inherit; white-space: pre-wrap;
  word-break: keep-all; overflow-wrap: anywhere; }
.kp-page p:empty::before, .kp-page .kp-empty::before { content: "\\200b"; }
.kp-marker { display: inline-block; text-indent: 0; white-space: pre; padding-right: .35em; }
.kp-tab { display: inline-block; width: 36pt; }
.kp-equation { font-family: "Cambria Math", "STIX Two Math", serif; font-style: italic;
  background: #f4f6fa; border-radius: 3px; padding: 0 3px; white-space: pre-wrap; }
.kp-table { border-collapse: collapse; table-layout: fixed; }
.kp-table td { overflow-wrap: anywhere; }
.kp-drawing { margin: 4pt 0; max-width: 100%; overflow: visible; }
.kp-body::after { content: ""; display: block; clear: both; }
.kp-svg { max-width: 100%; height: auto; overflow: visible; }
.kp-shape-text { display: flex; flex-direction: column; width: 100%; height: 100%;
  padding: 3.6pt 7.2pt; overflow: hidden; word-break: keep-all; overflow-wrap: anywhere; }
.kp-shape-text p { white-space: pre-wrap; }
.kp-shape-text > :first-child { padding-top: 0 !important; }
.kp-shape-text > :last-child { padding-bottom: 0 !important; }
.kp-missing-image { display: inline-flex; align-items: center; justify-content: center;
  border: 1px dashed #999; color: #666; font-size: 9pt; background: #f6f6f6; vertical-align: middle; }
.kp-page-break { border: 0; border-top: 1px dashed #b5b5b5; margin: 12pt 0; height: 0; }
.kp-header { margin-bottom: 12pt; }
.kp-footer { margin-top: 12pt; }
.kp-notes { border-top: 1px solid #999; margin-top: 18pt; padding-top: 6pt; font-size: 0.9em; }
.kp-note { display: flex; gap: 4pt; }
.kp-chart { margin: 4pt 0; }
.kp-figure { margin: 4pt 0; }
.kp-chart figcaption { font-weight: bold; margin-bottom: 4pt; }
.kp-chart summary { font-size: 9pt; color: #555; cursor: pointer; margin-top: 2pt; }
.kp-chart table { border-collapse: collapse; }
.kp-chart td, .kp-chart th { border: 1px solid #999; padding: 2pt 6pt; text-align: right; }
.kp-chart th { background: #f2f2f2; }
/* 한글(HWP)은 표 칸·글상자 안에서 마지막 줄 뒤 줄 간격을 넣지 않는다 */
.kp-src-hwpx td p, .kp-src-hwpx td h1, .kp-src-hwpx td h2, .kp-src-hwpx td h3,
.kp-src-hwpx .kp-shape-text p { margin-block: calc((1 - var(--kp-lh, 1.17)) * 0.5em); }
.kp-anchor-host { position: relative; height: 0; }
.kp-anchor-host > * { position: absolute; margin: 0; }
.kp-fixed-page { margin: 0 auto 24px; max-width: 100%; background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,.15), 0 4px 16px rgba(0,0,0,.08); }
.kp-fixed-svg { display: block; width: 100%; height: auto; }
.kp-fixed-svg text { white-space: pre; }
/* PPTX 슬라이드: 도형 안 글은 HTML로 흐르게 두되 PowerPoint처럼 상자 밖으로 넘친 글도 보인다 */
.kp-fixed-slide { font-size: 18pt; line-height: 1.2; }
.kp-fixed-slide foreignObject, .kp-fixed-slide .kp-shape-text { overflow: visible; }
.kp-fixed-slide p, .kp-fixed-slide h1, .kp-fixed-slide h2, .kp-fixed-slide h3, .kp-fixed-slide h4,
.kp-fixed-slide h5, .kp-fixed-slide h6 { margin: 0; font-size: inherit; font-weight: inherit; white-space: pre-wrap;
  word-break: keep-all; overflow-wrap: anywhere; }
.kp-fixed-slide .kp-nowrap p, .kp-fixed-slide .kp-nowrap h1 { white-space: pre; }
.kp-fixed-slide .kp-figure, .kp-fixed-slide .kp-drawing, .kp-fixed-slide .kp-chart { margin: 0; }
.kp-fixed-slide .kp-drawing .kp-svg { max-width: none; }
g.kp-highlight text { fill: #b45309; }
[data-kp-id].kp-highlight { outline: 2px solid #f5a623; outline-offset: 2px; background: #fff7e0; }
"""


_WEIGHT_HINTS = [
    (re.compile(r"black|heavy|extra\s*bold|ultra\s*bold", re.I), 800),
    (re.compile(r"semi\s*bold|demi\s*bold", re.I), 600),
    (re.compile(r"bold|견고딕|헤드라인|굵은|태고딕", re.I), 700),
    (re.compile(r"medium", re.I), 500),
    (re.compile(r"extra\s*light|ultra\s*light|thin", re.I), 200),
    (re.compile(r"light", re.I), 300),
]


def font_weight(name: str | None) -> int | None:
    """글꼴 이름에 굵기가 들어 있는 경우 (예: Pretendard ExtraBold). 글꼴이 없는 환경에서도 굵기를 살린다."""
    if not name:
        return None
    for pattern, weight in _WEIGHT_HINTS:
        if pattern.search(name):
            return weight
    return None


def font_stack(name: str | None) -> str:
    fallback = _KOREAN_SERIF if name and _SERIF_HINT.search(name) else _KOREAN_FONTS
    if name and _SAFE_FONT.match(name):
        return f"'{name}', {fallback}"
    return fallback


def _color(c: str | None) -> str | None:
    return c if c and _HEX.match(c) else None


def _pt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") + "pt"


def text_style_css(s: TextStyle | None) -> str:
    if s is None:
        return ""
    css = []
    weight = font_weight(s.font)
    if s.bold:
        css.append("font-weight:bold")
    elif weight:
        css.append(f"font-weight:{weight}")
    elif s.bold is not None:
        css.append("font-weight:normal")
    if s.italic is not None:
        css.append("font-style:" + ("italic" if s.italic else "normal"))
    deco = []
    if s.underline:
        deco.append("underline")
    if s.strike:
        deco.append("line-through")
    if deco:
        css.append("text-decoration:" + " ".join(deco))
    elif s.underline is False or s.strike is False:
        css.append("text-decoration:none")
    if _color(s.color):
        css.append(f"color:{s.color}")
    if _color(s.background):
        css.append(f"background-color:{s.background}")
    if s.size_pt:
        size = s.size_pt * (0.75 if s.vertical else 1)
        css.append(f"font-size:{_pt(size)}")
    if s.font and _SAFE_FONT.match(s.font):
        css.append(f"font-family:{font_stack(s.font)}")
    if s.vertical:
        css.append(f"vertical-align:{s.vertical}")
    if s.letter_spacing_em:
        css.append(f"letter-spacing:{s.letter_spacing_em:.3f}em")
    if s.caps == "all":
        css.append("text-transform:uppercase")
    elif s.caps == "small":
        css.append("font-variant:small-caps")
    return ";".join(css)


def paragraph_css(s: ParagraphStyle | None) -> str:
    if s is None:
        return ""
    css = []
    if s.align:
        css.append(f"text-align:{s.align}")
    if s.indent_left_pt:
        css.append(f"padding-left:{_pt(max(s.indent_left_pt, 0))}")
    if s.indent_right_pt:
        css.append(f"padding-right:{_pt(max(s.indent_right_pt, 0))}")
    if s.indent_first_pt:
        css.append(f"text-indent:{_pt(s.indent_first_pt)}")
    # Word는 앞 문단의 '뒤 간격'과 다음 문단의 '앞 간격'을 더한다. CSS margin은 겹치므로 padding을 쓴다.
    # 단, 문단 테두리가 있으면 간격은 테두리 바깥이므로 margin을 쓴다.
    space = "margin" if s.borders else "padding"
    if s.space_before_pt:
        css.append(f"{space}-top:{_pt(s.space_before_pt)}")
    if s.space_after_pt:
        css.append(f"{space}-bottom:{_pt(s.space_after_pt)}")
    if s.line_spacing:
        css.append(f"line-height:{s.line_spacing * 1.17:.3f}")
        css.append(f"--kp-lh:{s.line_spacing * 1.17:.3f}")
    elif s.line_spacing_pt:
        css.append(f"line-height:{_pt(s.line_spacing_pt)}")
    if _color(s.background):
        css.append(f"background-color:{s.background}")
    css += _borders_css(s.borders)
    return ";".join(css)


def _border_css(side: str, b: Border | None) -> str:
    if b is None or b.style == "none" or b.width_pt <= 0:
        return ""
    width = max(b.width_pt, 0.5) if b.style != "double" else max(b.width_pt * 3, 2.25)
    return f"border-{side}:{_pt(width)} {b.style} {_color(b.color) or '#000'}"


def _borders_css(bs: Borders | None) -> list[str]:
    if bs is None:
        return []
    return [c for c in (_border_css(s, getattr(bs, s)) for s in ("top", "right", "bottom", "left")) if c]


def _attr(name: str, css: str) -> str:
    return f' {name}="{escape(css)}"' if css else ""


class HtmlRenderer:
    def __init__(self, doc: Document, standalone: bool = True, layout: str = "auto"):
        self.doc = doc
        self.standalone = standalone
        self._by_id: dict | None = None  # 블록 id → 블록 (고정 쪽 슬라이드가 본문 블록을 가리킬 때)
        self._slide = False  # 슬라이드 도형 안을 그리는 중
        # auto: 고정 쪽(PDF, PPTX)이 있으면 원본 모양 그대로(fixed), 없으면 흐름형(flow)
        self.layout = ("fixed" if doc.fixed_pages else "flow") if layout == "auto" else layout
        if self.layout == "fixed" and not doc.fixed_pages:
            self.layout = "flow"

    # ------------------------------------------------------------------

    def render(self) -> str:
        doc = self.doc
        if self.layout == "fixed":
            return self._wrap(self._fixed_pages())
        p = doc.page
        base = doc.default_text_style
        font = font_stack(base.font)
        page_css = (
            f"width:{_pt(p.width_pt)};min-height:{_pt(p.height_pt)};"
            f"padding:{_pt(p.margin_top_pt)} {_pt(p.margin_right_pt)} {_pt(p.margin_bottom_pt)} {_pt(p.margin_left_pt)};"
            f"font-family:{font};font-size:{_pt(base.size_pt or 10)};line-height:1.17"
        )
        if _color(base.color):
            page_css += f";color:{base.color}"
        if _color(p.background):
            page_css += f";background-color:{p.background}"
        bg = self._image_src(p.background_image_id) if p.background_image_id else None
        if bg:
            # 쪽마다 같은 배경이 반복되도록 쪽 크기로 늘려 세로로 반복한다
            page_css += (f";background-image:url('{bg}');background-size:{_pt(p.width_pt)} {_pt(p.height_pt)};"
                         "background-repeat:repeat-y")

        src = doc.meta.source_format or ""
        parts = [f'<article class="kp-page kp-src-{escape(src)}"{_attr("style", page_css)}>']
        if doc.header:
            parts.append(f'<header class="kp-header">{self.blocks(doc.header)}</header>')
        parts.append(f'<div class="kp-body">{self.blocks(doc.blocks)}</div>')
        if doc.notes:
            parts.append(self._notes())
        if doc.footer:
            parts.append(f'<footer class="kp-footer">{self.blocks(doc.footer)}</footer>')
        parts.append("</article>")
        return self._wrap("".join(parts))

    def _wrap(self, body: str) -> str:
        doc = self.doc
        if not self.standalone:
            return f"<style>{BASE_CSS}</style>{body}"

        title = escape(doc.meta.title or doc.meta.source_name or "문서")
        lang = doc.meta.language or "ko"
        return (
            f'<!doctype html><html lang="{escape(lang[:10])}"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="generator" content="korean-parser"><title>{title}</title>'
            f"<style>{BASE_CSS}</style></head><body>{body}</body></html>"
        )

    # ------------------------------------------------------------------
    # 고정 레이아웃 (PDF): 글자를 뺀 바탕 그림 위에 원래 위치·크기로 글을 올린다
    # ------------------------------------------------------------------

    def _fixed_pages(self) -> str:
        pages = []
        for fp in self.doc.fixed_pages:
            if fp.shapes and not fp.runs:
                pages.append(self._fixed_slide(fp))
                continue
            w, h = fp.width_pt, fp.height_pt
            parts = [f'<svg class="kp-fixed-svg" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.2f} {h:.2f}" '
                     f'width="{w:.2f}pt" height="{h:.2f}pt" role="document" aria-label="{fp.page}쪽">']
            src = self._image_src(fp.background_image_id) if fp.background_image_id else None
            if src:
                parts.append(f'<image href="{src}" x="0" y="0" width="{w:.2f}" height="{h:.2f}" preserveAspectRatio="none"/>')
            open_group, current = False, None
            for r in fp.runs:
                # 같은 블록의 글 조각은 하나의 <g data-kp-id>로 묶는다 (화면 강조용)
                if not open_group or r.block_id != current:
                    if open_group:
                        parts.append("</g>")
                    attr = f' data-kp-id="{escape(r.block_id)}"' if r.block_id else ""
                    parts.append(f"<g{attr}>")
                    open_group, current = True, r.block_id
                attrs = [f'x="{r.x:.2f}"', f'y="{r.baseline:.2f}"', f'font-size="{r.size_pt:.2f}"',
                         f'textLength="{r.width:.2f}"', 'lengthAdjust="spacingAndGlyphs"',
                         f'fill="{_color(r.color) or "#000"}"']
                style = [f"font-family:{font_stack(r.font)}"]
                weight = "bold" if r.bold else font_weight(r.font)
                if weight:
                    style.append(f"font-weight:{weight}")
                if r.italic:
                    style.append("font-style:italic")
                parts.append(f'<text {" ".join(attrs)} style="{escape(";".join(style))}" xml:space="preserve">'
                             f"{escape(r.text)}</text>")
            if open_group:
                parts.append("</g>")
            parts.append("</svg>")
            pages.append(f'<article class="kp-fixed-page" data-page="{fp.page}" style="width:{w:.2f}pt">{"".join(parts)}</article>')
        return "".join(pages)

    def _fixed_slide(self, fp) -> str:
        """PPTX 슬라이드: 도형을 그리는 차례대로 원래 위치에 그린다. 글이 있는 도형은 가리키는 본문 블록을 그 상자 안에
        HTML로 그리므로, 화면 강조(data-kp-id)가 흐름 배치와 같은 블록 id로 이어진다."""
        if self._by_id is None:
            doc = self.doc
            self._by_id = {b.id: b for b in walk_blocks(doc.blocks + doc.header + doc.footer) if getattr(b, "id", "")}
        shapes = []
        for s in fp.shapes:
            if s.image_id:  # 그림: 도형이 직접 그리고, 강조용으로 그 Figure 블록 id를 단다
                shapes.append((s, s.block_ids[0] if s.block_ids else None))
                continue
            blocks = [self._by_id[i] for i in s.block_ids if i in self._by_id] + list(s.text_blocks)
            shapes.append((s.model_copy(update={"text_blocks": blocks}) if s.block_ids else s, None))
        bg = self._image_src(fp.background_image_id) if fp.background_image_id else None
        self._slide = True
        try:
            svg = render_slide(shapes, fp.width_pt, fp.height_pt, self.blocks, self._image_src, fp.background, bg,
                               label=f"{fp.page}쪽")
        finally:
            self._slide = False
        return (f'<article class="kp-fixed-page kp-fixed-slide" data-page="{fp.page}" '
                f'style="width:{fp.width_pt:.2f}pt">{svg}</article>')

    # ------------------------------------------------------------------
    # 블록
    # ------------------------------------------------------------------

    def blocks(self, blocks: list[Block]) -> str:
        return "".join(self.block(b) for b in blocks)

    def block(self, b: Block) -> str:
        if isinstance(b, Heading):
            level = min(max(b.level, 1), 6)
            return self._para(f"h{level}", b, b.inlines, b.style, b.list_info)
        if isinstance(b, Paragraph):
            return self._para("p", b, b.inlines, b.style, b.list_info)
        if isinstance(b, Table):
            return self._place(b, self._table(b))
        if isinstance(b, Drawing):
            svg = render_drawing(b, self.blocks, self._image_src)
            return self._place(b, f'<div class="kp-drawing"{self._id(b)}>{svg}</div>')
        if isinstance(b, Chart):
            return self._chart(b)
        if isinstance(b, Figure):
            img = self._image(b.image_id, b.width_pt, b.height_pt, b.alt or b.inner_text)
            cap = f"<figcaption>{escape(b.caption)}</figcaption>" if b.caption else ""
            return f'<figure class="kp-figure"{self._id(b)}{_attr("style", paragraph_css(b.style))}>{img}{cap}</figure>'
        if isinstance(b, PageBreak):
            return f'<hr class="kp-page-break"{self._id(b)} aria-label="페이지 나눔">'
        return ""

    def _place(self, b, html: str) -> str:
        """떠 있는 표·도식을 배치 방식에 맞게 감싼다."""
        a = b.anchor
        if a is None:
            return html
        css = self._anchor_css(b)
        if a.wrap == "none" and a.relative_v not in ("page", "margin"):
            # 글 앞/뒤로 겹치는 개체: 자리를 차지하지 않고 문단 위치 기준으로 겹쳐 그린다
            return f'<div class="kp-anchor-host"><div style="{escape(css)}">{html}</div></div>'
        return f'<div style="{escape(css)}">{html}</div>' if css else html

    def _anchor_css(self, d) -> str:
        """떠 있는 개체: 글이 옆으로 흐르면 float, 겹치면 절대 위치, 위아래 배치는 들여쓰기만."""
        a = d.anchor
        if a is None:
            return ""
        width = getattr(d, "width_pt", None) or sum(getattr(d, "col_widths_pt", []) or [0])
        p = self.doc.page
        content_w = p.width_pt - p.margin_left_pt - p.margin_right_pt

        # 본문 영역 왼쪽 기준 x
        if a.align_h in ("right", "outside"):
            x = content_w - width
        elif a.align_h == "center":
            x = (content_w - width) / 2
        else:
            x = a.x_pt or 0.0
            if a.relative_h == "page":
                x -= p.margin_left_pt
        if a.relative_h == "page" and a.align_h in ("right", "outside"):
            x += p.margin_right_pt

        if a.wrap == "top_bottom":
            return f"margin-left:{_pt(x)}" if x > 0 else ""
        height = getattr(d, "height_pt", None) or 0.0
        if a.wrap == "none" and a.relative_v in ("page", "margin"):
            y = a.y_pt or 0.0
            if a.align_v == "center":
                y = (p.height_pt - height) / 2 - (0 if a.relative_v == "page" else -p.margin_top_pt)
            if a.relative_v == "margin":
                y += p.margin_top_pt
            css = f"position:absolute;left:{_pt(x + p.margin_left_pt)};top:{_pt(y)};margin:0"
            return css + (";pointer-events:none" if a.behind_text else "")
        if a.wrap == "none":
            # 문단 기준으로 겹치는 개체: _place가 높이 0인 자리에 두고 여기서는 위치만 준다
            return f"left:{_pt(x)};top:{_pt(a.y_pt or 0.0)}" + (";pointer-events:none" if a.behind_text else "")

        gap = "9pt"
        if x + width / 2 > content_w / 2:
            right = max(content_w - (x + width), -p.margin_right_pt)
            return f"float:right;margin:0 {_pt(right)} 4pt {gap}"
        left = max(x, -p.margin_left_pt)
        return f"float:left;margin:0 {gap} 4pt {_pt(left)}"

    def _id(self, b) -> str:
        return f' data-kp-id="{escape(b.id)}"' if getattr(b, "id", "") else ""

    def _para(self, tag, b, inlines, style, list_info) -> str:
        marker = ""
        if list_info and list_info.marker:
            # 내어쓰기가 있으면 번호가 그 폭을 차지하고 본문은 들여쓴 위치에서 시작한다 (Word 목록)
            hanging = style is not None and (style.indent_first_pt or 0) < 0
            width = f' style="min-width:{_pt(-style.indent_first_pt)}"' if hanging else ""
            marker = f'<span class="kp-marker"{width}>{escape(list_info.marker)}</span>'
        css = paragraph_css(style)
        if self._slide:
            # 줄 높이(배수)는 문단 자신의 글자 크기로 계산된다. 상자 기본 크기(18pt)가 남아 있으면 작은 글자 줄이
            # 벌어지므로, PowerPoint처럼 그 문단의 가장 큰 글자 크기를 문단 크기로 둔다
            size = max((i.style.size_pt for i in inlines if isinstance(i, Text) and i.style and i.style.size_pt),
                       default=None)
            if size:
                css = f"font-size:{_pt(size)}" + (";" + css if css else "")
        return f"<{tag}{self._id(b)}{_attr('style', css)}>{marker}{self.inlines(inlines)}</{tag}>"

    def inlines(self, inlines) -> str:
        out = []
        for i in inlines:
            if isinstance(i, Text):
                html = escape(i.text)
                css = text_style_css(i.style)
                if css:
                    html = f'<span style="{escape(css)}">{html}</span>'
                if i.link and _SAFE_LINK.match(i.link):
                    html = f'<a href="{escape(i.link)}">{html}</a>'
                out.append(html)
            elif isinstance(i, Tab):
                out.append('<span class="kp-tab"></span>')
            elif isinstance(i, Break):
                out.append('<br><span class="kp-page-break" role="separator"></span>' if i.kind == "page" else "<br>")
            elif isinstance(i, ImageRef):
                out.append(self._image(i.image_id, i.width_pt, i.height_pt, i.alt))
            elif isinstance(i, Equation):
                out.append(f'<span class="kp-equation" title="수식">{escape(i.script)}</span>')
            elif isinstance(i, NoteRef):
                key = escape(i.note_id)
                out.append(f'<sup><a href="#kp-note-{key}" id="kp-noteref-{key}">{escape(i.label)}</a></sup>')
        return "".join(out)

    # ------------------------------------------------------------------
    # 이미지
    # ------------------------------------------------------------------

    def _image_src(self, image_id: str) -> str | None:
        img = self.doc.images.get(image_id)
        if img is None or img.mime not in _RENDERABLE:
            return None
        return f"data:{img.mime};base64,{img.data_base64}"

    def _image(self, image_id, w, h, alt) -> str:
        size = (f"width:{_pt(w)};" if w else "") + (f"height:{_pt(h)};" if h else "")
        src = self._image_src(image_id)
        img = self.doc.images.get(image_id)
        if src is None:
            label = escape(f"이미지: {img.name if img else image_id}")
            return f'<span class="kp-missing-image" role="img" aria-label="{label}" style="{size}max-width:100%">{label}</span>'
        return f'<img src="{src}" alt="{escape(alt or "")}" style="{size}max-width:100%;height:auto;vertical-align:bottom">'

    # ------------------------------------------------------------------
    # 표
    # ------------------------------------------------------------------

    def _table(self, t: Table) -> str:
        total = sum(t.col_widths_pt)
        css = []
        if total > 0:
            css.append(f"width:{_pt(t.width_pt or total)}")
        if t.align == "center":
            css.append("margin-left:auto;margin-right:auto")
        elif t.align == "right":
            css.append("margin-left:auto")
        cols = ""
        if total > 0:
            cols = "<colgroup>" + "".join(f'<col style="width:{_pt(w)}">' for w in t.col_widths_pt) + "</colgroup>"
        pad = t.cell_padding_pt or (0, 5.4, 0, 5.4)
        pad_css = "padding:" + " ".join(_pt(v) for v in pad)

        rows = []
        for row in t.rows:
            cells = []
            for c in row.cells:
                ccss = [pad_css] + _borders_css(c.borders)
                if _color(c.background):
                    ccss.append(f"background-color:{c.background}")
                ccss.append(f"vertical-align:{c.v_align or 'top'}")
                span = (f' rowspan="{c.row_span}"' if c.row_span > 1 else "") + (
                    f' colspan="{c.col_span}"' if c.col_span > 1 else ""
                )
                cells.append(f'<td{span} style="{escape(";".join(ccss))}">{self.blocks(c.blocks)}</td>')
            rcss = f' style="height:{_pt(row.height_pt)}"' if row.height_pt else ""
            rows.append(f"<tr{rcss}>{''.join(cells)}</tr>")
        return f'<table class="kp-table"{self._id(t)}{_attr("style", ";".join(css))}>{cols}<tbody>{"".join(rows)}</tbody></table>'

    def _chart(self, c: Chart) -> str:
        title = escape(c.title or "차트")
        head = "<tr><th></th>" + "".join(f"<th>{escape(s.name or '')}</th>" for s in c.series) + "</tr>"
        rows = []
        n = max([len(c.categories)] + [len(s.values) for s in c.series] or [0])
        for i in range(n):
            cat = escape(c.categories[i]) if i < len(c.categories) else ""
            vals = "".join(
                f"<td>{'' if i >= len(s.values) or s.values[i] is None else f'{s.values[i]:g}'}</td>" for s in c.series
            )
            rows.append(f"<tr><th>{cat}</th>{vals}</tr>")
        table = f"<table>{head}{''.join(rows)}</table>"
        svg = render_chart_svg(c)
        if svg is None:
            return (
                f'<figure class="kp-chart"{self._id(c)}><figcaption>{title} ({escape(c.chart_type)} 차트)</figcaption>'
                f"{table}</figure>"
            )
        return (
            f'<figure class="kp-chart"{self._id(c)}>{svg}'
            f"<details><summary>차트 데이터</summary>{table}</details></figure>"
        )

    def _notes(self) -> str:
        items = []
        for key, note in self.doc.notes.items():
            k = escape(key)
            items.append(
                f'<div class="kp-note" id="kp-note-{k}"><a href="#kp-noteref-{k}">{escape(note.label)}</a>'
                f"<div>{self.blocks(note.blocks)}</div></div>"
            )
        return f'<section class="kp-notes" aria-label="각주">{"".join(items)}</section>'


def render_html(doc: Document, standalone: bool = True, layout: str = "auto") -> str:
    """layout: auto(기본), flow(문서 흐름형), fixed(원본 쪽 모양 그대로, PDF·PPTX)."""
    return HtmlRenderer(doc, standalone=standalone, layout=layout).render()
