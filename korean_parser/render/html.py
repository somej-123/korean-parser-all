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
    Block, Border, Borders, Break, Chart, Document, Drawing, Figure, Heading, ImageRef, NoteRef,
    PageBreak, Paragraph, ParagraphStyle, Tab, Table, Text, TextStyle,
)
from .chart_svg import render_chart_svg
from .svg import render_drawing

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_SAFE_FONT = re.compile(r"^[\w\s가-힣ㄱ-ㅎ.\-,']{1,64}$")
_SAFE_LINK = re.compile(r"^(https?://|mailto:|#)", re.IGNORECASE)
_KOREAN_FONTS = '"맑은 고딕", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans KR", "Noto Sans CJK KR", sans-serif'
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
.kp-chart figcaption { font-weight: bold; margin-bottom: 4pt; }
.kp-chart summary { font-size: 9pt; color: #555; cursor: pointer; margin-top: 2pt; }
.kp-chart table { border-collapse: collapse; }
.kp-chart td, .kp-chart th { border: 1px solid #999; padding: 2pt 6pt; text-align: right; }
.kp-chart th { background: #f2f2f2; }
[data-kp-id].kp-highlight { outline: 2px solid #f5a623; outline-offset: 2px; background: #fff7e0; }
"""


def _color(c: str | None) -> str | None:
    return c if c and _HEX.match(c) else None


def _pt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") + "pt"


def text_style_css(s: TextStyle | None) -> str:
    if s is None:
        return ""
    css = []
    if s.bold is not None:
        css.append("font-weight:" + ("bold" if s.bold else "normal"))
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
        css.append(f"font-family:'{s.font}', {_KOREAN_FONTS}")
    if s.vertical:
        css.append(f"vertical-align:{s.vertical}")
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
    def __init__(self, doc: Document, standalone: bool = True):
        self.doc = doc
        self.standalone = standalone

    # ------------------------------------------------------------------

    def render(self) -> str:
        doc = self.doc
        p = doc.page
        base = doc.default_text_style
        font = f"'{base.font}', {_KOREAN_FONTS}" if base.font and _SAFE_FONT.match(base.font) else _KOREAN_FONTS
        page_css = (
            f"width:{_pt(p.width_pt)};min-height:{_pt(min(p.height_pt, 300))};"
            f"padding:{_pt(p.margin_top_pt)} {_pt(p.margin_right_pt)} {_pt(p.margin_bottom_pt)} {_pt(p.margin_left_pt)};"
            f"font-family:{font};font-size:{_pt(base.size_pt or 10)};line-height:1.17"
        )
        if _color(base.color):
            page_css += f";color:{base.color}"

        parts = [f'<article class="kp-page"{_attr("style", page_css)}>']
        if doc.header:
            parts.append(f'<header class="kp-header">{self.blocks(doc.header)}</header>')
        parts.append(f'<div class="kp-body">{self.blocks(doc.blocks)}</div>')
        if doc.notes:
            parts.append(self._notes())
        if doc.footer:
            parts.append(f'<footer class="kp-footer">{self.blocks(doc.footer)}</footer>')
        parts.append("</article>")
        body = "".join(parts)
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
            return self._table(b)
        if isinstance(b, Drawing):
            svg = render_drawing(b, self.blocks, self._image_src)
            return f'<div class="kp-drawing"{self._id(b)}{_attr("style", self._anchor_css(b))}>{svg}</div>'
        if isinstance(b, Chart):
            return self._chart(b)
        if isinstance(b, Figure):
            img = self._image(b.image_id, b.width_pt, b.height_pt, b.alt)
            cap = f"<figcaption>{escape(b.caption)}</figcaption>" if b.caption else ""
            return f"<figure{self._id(b)}>{img}{cap}</figure>"
        if isinstance(b, PageBreak):
            return f'<hr class="kp-page-break"{self._id(b)} aria-label="페이지 나눔">'
        return ""

    def _anchor_css(self, d: Drawing) -> str:
        """떠 있는 개체: 글이 옆으로 흐르면 float, 겹치면 페이지 기준 절대 위치."""
        a = d.anchor
        if a is None or a.wrap == "top_bottom":
            return ""
        p = self.doc.page
        content_w = p.width_pt - p.margin_left_pt - p.margin_right_pt

        # 본문 영역 왼쪽 기준 x
        if a.align_h in ("right", "outside"):
            x = content_w - d.width_pt
        elif a.align_h == "center":
            x = (content_w - d.width_pt) / 2
        else:
            x = a.x_pt or 0.0
            if a.relative_h == "page":
                x -= p.margin_left_pt
        if a.relative_h == "page" and a.align_h in ("right", "outside"):
            x += p.margin_right_pt

        if a.wrap == "none" and a.relative_h in ("page", "margin") and a.relative_v in ("page", "margin"):
            y = a.y_pt or 0.0
            if a.align_v == "center":
                y = (p.height_pt - d.height_pt) / 2 - (0 if a.relative_v == "page" else -p.margin_top_pt)
            if a.relative_v == "margin":
                y += p.margin_top_pt
            css = f"position:absolute;left:{_pt(x + p.margin_left_pt)};top:{_pt(y)};margin:0"
            return css + (";pointer-events:none" if a.behind_text else "")
        if a.wrap == "none":
            return ""  # 문단 기준으로 겹치는 개체는 흐름 안에 그대로 둔다

        gap = "9pt"
        if x + d.width_pt / 2 > content_w / 2:
            right = max(content_w - (x + d.width_pt), -p.margin_right_pt)
            return f"float:right;margin:0 {_pt(right)} 4pt {gap}"
        left = max(x, -p.margin_left_pt)
        return f"float:left;margin:0 {gap} 4pt {_pt(left)}"

    def _id(self, b) -> str:
        return f' data-kp-id="{escape(b.id)}"' if getattr(b, "id", "") else ""

    def _para(self, tag, b, inlines, style, list_info) -> str:
        marker = ""
        if list_info and list_info.marker:
            hang = -(style.indent_first_pt or 0) if style and style.indent_first_pt and style.indent_first_pt < 0 else 18
            marker = f'<span class="kp-marker" style="min-width:{_pt(hang)}">{escape(list_info.marker)}</span>'
        return f"<{tag}{self._id(b)}{_attr('style', paragraph_css(style))}>{marker}{self.inlines(inlines)}</{tag}>"

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


def render_html(doc: Document, standalone: bool = True) -> str:
    return HtmlRenderer(doc, standalone=standalone).render()
