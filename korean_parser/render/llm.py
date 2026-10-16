"""IR → LLM 입력용 텍스트.

원칙
- 일반 텍스트와 단순한 표는 Markdown (토큰 절약)
- 병합 셀이 있거나 셀 안에 표·여러 문단이 있는 표는 HTML <table> (구조 손실 방지)
- 자동 번호와 개조식 기호는 원문 그대로 남기고, 계층은 들여쓰기로 표현
- 도식은 도형 안 글자와 연결 관계("신청 → 검토")로 풀어 쓴다
- 블록마다 id를 남겨 LLM 답변의 근거를 원문 위치로 되짚을 수 있게 한다 (with_ids)
"""

from __future__ import annotations

from html import escape

from ..ir import (
    Block, Equation, Chart, Document, Drawing, Figure, Heading, ImageRef, NoteRef, PageBreak, Paragraph, Shape,
    Table, Text, _walk, inline_text,
)


def render_llm(doc: Document, with_ids: bool = True) -> str:
    return LlmRenderer(doc, with_ids).render()


class LlmRenderer:
    def __init__(self, doc: Document, with_ids: bool = True, _all_shapes: list[Shape] | None = None):
        self.doc = doc
        self.with_ids = with_ids
        # HWP는 도형과 연결선이 서로 다른 개체이므로, 연결 대상은 문서 전체에서 찾는다 (한 번만 모은다)
        if _all_shapes is None:
            _all_shapes = [s for b in doc.iter_blocks() if isinstance(b, Drawing) for s in b.shapes]
        self._all_shapes = _all_shapes
        self._inner: LlmRenderer | None = None

    def render(self) -> str:
        m = self.doc.meta
        head = []
        for key, val in (("title", m.title), ("author", m.author), ("created", m.created), ("source", m.source_name)):
            if val:
                head.append(f"{key}: {val}")
        out = ["---\n" + "\n".join(head) + "\n---"] if head else []
        body = self.blocks(self.doc.blocks)
        out.append(body)
        if self.doc.notes:
            out.append("\n".join(
                f"[^{n.label}]: {self._plain(n.blocks)}" for n in self.doc.notes.values()
            ))
        return "\n\n".join(x for x in out if x.strip()) + "\n"

    def _tag(self, b) -> str:
        return f"  <!-- {b.id} -->" if self.with_ids and getattr(b, "id", "") else ""

    def blocks(self, blocks: list[Block], depth: int = 0) -> str:
        parts = [self.block(b, depth) for b in blocks]
        return "\n\n".join(p for p in parts if p)

    def block(self, b: Block, depth: int = 0) -> str:
        if isinstance(b, Heading):
            marker = f"{b.list_info.marker} " if b.list_info else ""
            return f"{'#' * min(b.level, 6)} {marker}{self.inlines(b.inlines).strip()}{self._tag(b)}"
        if isinstance(b, Paragraph):
            text = self.inlines(b.inlines).strip()
            if not text:
                return ""
            if b.list_info:
                indent = "  " * b.list_info.level
                return f"{indent}{b.list_info.marker} {text}{self._tag(b)}"
            if b.outline:
                # 표지(□, 1.)는 이미 본문에 있으므로 들여쓰기만 더한다
                return f"{'  ' * b.outline.level}{text}{self._tag(b)}"
            return text + self._tag(b)
        if isinstance(b, Table):
            return self._table(b)
        if isinstance(b, Drawing):
            return self._drawing(b)
        if isinstance(b, Chart):
            return self._chart(b)
        if isinstance(b, Figure):
            head = f"[그림{': ' + b.caption if b.caption else ''}]{self._tag(b)}"
            return head + (f"\n그림 안 글자: {b.inner_text}" if b.inner_text else "")
        if isinstance(b, PageBreak):
            return ""
        return ""

    def inlines(self, inlines) -> str:
        out = []
        for i in inlines:
            if isinstance(i, Text):
                out.append(i.text)
            elif isinstance(i, ImageRef):
                img = self.doc.images.get(i.image_id)
                out.append(f"[이미지{': ' + i.alt if i.alt else ''} ({i.image_id})]" if img else "")
            elif isinstance(i, NoteRef):
                out.append(f"[^{i.label}]")
            elif isinstance(i, Equation):
                out.append(f"[수식: {i.script}]")
            else:
                out.append(inline_text([i]))
        return "".join(out)

    def _plain(self, blocks) -> str:
        return " ".join(self._nested(blocks).split())

    def _nested(self, blocks) -> str:
        """표 셀, 도형, 각주 안의 블록. 바깥 블록에 id가 있으므로 안쪽에는 달지 않는다."""
        if not self.with_ids:
            return self.blocks(blocks)
        if self._inner is None:
            self._inner = LlmRenderer(self.doc, with_ids=False, _all_shapes=self._all_shapes)
        return self._inner.blocks(blocks)

    # ------------------------------------------------------------------

    def _table(self, t: Table) -> str:
        flat = self.flattened_cells(t)
        if flat is not None:
            # 안쪽 문단이 각자 id를 달고 있으므로 표 id는 따로 달지 않는다
            parts = (self.blocks(c.blocks) for c in flat)
            return "\n\n".join(p for p in parts if p.strip())

        complex_cells = any(
            len(c.blocks) > 1 or any(isinstance(x, (Table, Drawing)) for x in c.blocks)
            for r in t.rows for c in r.cells
        )
        if t.has_merged_cells or complex_cells:
            return self._html_table(t) + self._tag(t)

        rows = [[" ".join(self._plain(c.blocks).split()).replace("|", "\\|") for c in r.cells] for r in t.rows]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines) + self._tag(t)

    def flattened_cells(self, t: Table):
        """표가 아닌 글로 풀어 쓸 표면 내용 있는 칸 목록, 아니면 None.

        - 한 열짜리 표: 제목 상자·강조 상자로 쓰인 경우가 대부분이다 (한글 문서에서 흔함)
        - 칸 대부분이 빈 표: 표지·로고 배치용 틀
        """
        if t.rows and all(len(r.cells) == 1 for r in t.rows):
            return [c for r in t.rows for c in r.cells]
        cells = [c for r in t.rows for c in r.cells]
        filled = [c for c in cells if self._plain(c.blocks).strip()]
        # 일정표(간트 차트)처럼 칸이 비는 게 정상인 표는 머리 행에 여러 칸이 차 있으므로 제외한다
        one_per_row = all(sum(1 for c in r.cells if c in filled) <= 1 for r in t.rows)
        if len(cells) >= 4 and (len(filled) <= 2 or (len(filled) <= len(cells) * 0.25 and one_per_row)):
            return filled
        return None

    def _html_table(self, t: Table) -> str:
        """병합 셀이나 셀 안 표가 있는 표. 셀 안의 표는 HTML 표로 중첩한다."""
        rows = []
        for r in t.rows:
            cells = []
            for c in r.cells:
                span = (f' rowspan="{c.row_span}"' if c.row_span > 1 else "") + (
                    f' colspan="{c.col_span}"' if c.col_span > 1 else ""
                )
                parts = []
                for b in c.blocks:
                    if isinstance(b, Table):
                        parts.append(self._html_table(b))
                    else:
                        text = self._nested([b])
                        if text.strip():
                            parts.append(escape(text).replace("\n", "<br>"))
                cells.append(f"<td{span}>{'<br>'.join(parts)}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        return f"<table>{''.join(rows)}</table>"

    def text_frame_blocks(self, d: Drawing):
        """글 틀(글상자)로 쓰인 그리기 개체면 그 안의 블록(읽는 순서대로), 아니면 None.

        ODT에서 변환한 DOCX나 한글 문서는 표·본문을 글상자 안에 넣는 경우가 많다. 도식으로 보고 한 줄로
        뭉치면 표 구조가 사라지므로, 글상자 안 블록을 본문처럼 풀어 쓴다.
        - 연결선이 없고 글상자가 하나뿐인 개체
        - 연결선이 없고 글상자 중 하나라도 표나 문단 세 개 이상을 담은 개체 (도식의 짧은 이름표와 구별)
        """
        if any(s.is_line for s in d.shapes):
            return None
        boxes = [s for s in d.shapes if s.text_blocks]
        if not boxes or any(s.image_id and s not in boxes for s in d.shapes):
            return None
        frame = len(boxes) == 1 or any(
            len(s.text_blocks) >= 3 or any(isinstance(b, Table) for b in s.text_blocks) for s in boxes
        )
        if not frame:
            return None
        return [b for s in sorted(boxes, key=lambda s: (round(s.y), s.x)) for b in s.text_blocks]

    def _drawing(self, d: Drawing) -> str:
        frame = self.text_frame_blocks(d)
        if frame is not None:
            body = self.blocks(frame)
            return f"{body}{self._tag(d)}" if body and "\n" not in body else body
        boxes = [s for s in d.shapes if not s.is_line and (s.text_blocks or s.image_id)]
        lines = [s for s in d.shapes if s.is_line]
        names = {}
        for s in self._all_shapes:
            if not s.is_line and (s.text_blocks or s.image_id):
                names[id(s)] = _shape_label(s)

        edges = []
        for ln in lines:
            a, b = _endpoints(ln, d.shapes, self._all_shapes)
            if a is not None and b is not None and names.get(id(a)) and names.get(id(b)):
                if ln.head_arrow and not ln.tail_arrow:
                    a, b = b, a
                arrow = "↔" if ln.head_arrow and ln.tail_arrow else "→" if (ln.head_arrow or ln.tail_arrow) else "—"
                edges.append((a, arrow, b))

        out = [f"[도식{': ' + d.alt if d.alt else ''}]{self._tag(d)}"]
        for s in _flow_order(boxes, edges):
            if names[id(s)]:
                out.append(f"- {names[id(s)]}")
        if edges:
            out.append("연결:")
            out.extend(f"- {names[id(a)]} {arrow} {names[id(b)]}" for a, arrow, b in edges)
        return "\n".join(out)

    def _chart(self, c: Chart) -> str:
        head = "| 항목 | " + " | ".join(s.name or f"계열{i + 1}" for i, s in enumerate(c.series)) + " |"
        sep = "|---|" + "---|" * len(c.series)
        rows = []
        for i, cat in enumerate(c.categories):
            vals = [("" if i >= len(s.values) or s.values[i] is None else f"{s.values[i]:g}") for s in c.series]
            rows.append(f"| {cat} | " + " | ".join(vals) + " |")
        title = f"[차트({c.chart_type}){': ' + c.title if c.title else ''}]{self._tag(c)}"
        return "\n".join([title, head, sep, *rows])


def _endpoints(line: Shape, shapes: list[Shape], all_shapes: list[Shape] | None = None):
    """연결선 양 끝이 닿은 도형. 연결 정보(stCxn/endCxn)가 있으면 그것을, 없으면 좌표로 찾는다.

    좌표 비교는 같은 캔버스(shapes) 안에서만 의미가 있고, id 연결은 문서 전체(all_shapes)에서 찾는다.
    """
    by_id = {s.id: s for s in (all_shapes or shapes) if s.id}
    a = by_id.get(line.connect_from) if line.connect_from else None
    b = by_id.get(line.connect_to) if line.connect_to else None
    if a is not None and b is not None:
        return a, b

    x1, y1, x2, y2 = line.x, line.y, line.x + line.width, line.y + line.height
    if line.flip_h:
        x1, x2 = x2, x1
    if line.flip_v:
        y1, y2 = y2, y1
    if line.rotation:
        import math

        cx, cy = line.x + line.width / 2, line.y + line.height / 2
        t = math.radians(line.rotation)

        def rot(px, py):
            dx, dy = px - cx, py - cy
            return cx + dx * math.cos(t) - dy * math.sin(t), cy + dx * math.sin(t) + dy * math.cos(t)

        (x1, y1), (x2, y2) = rot(x1, y1), rot(x2, y2)

    def hit(px, py):
        best, best_d = None, 8.0  # 8pt 이내
        for s in shapes:
            if s.is_line or s is line:
                continue
            dx = max(s.x - px, 0, px - (s.x + s.width))
            dy = max(s.y - py, 0, py - (s.y + s.height))
            d = (dx * dx + dy * dy) ** 0.5
            if d <= best_d:
                best, best_d = s, d
        return best

    return a or hit(x1, y1), b or hit(x2, y2)


def _flow_order(boxes: list[Shape], edges) -> list[Shape]:
    """연결선이 있으면 흐름 순서(들어오는 선이 없는 도형부터)로, 없으면 위→아래, 왼쪽→오른쪽으로."""
    by_pos = sorted(boxes, key=lambda s: (round(s.y / 10), s.x))
    if not edges:
        return by_pos
    nexts: dict[int, list[Shape]] = {}
    indeg = {id(s): 0 for s in boxes}
    for a, _, b in edges:
        nexts.setdefault(id(a), []).append(b)
        indeg[id(b)] = indeg.get(id(b), 0) + 1
    order: list[Shape] = []
    seen: set[int] = set()
    queue = [s for s in by_pos if indeg[id(s)] == 0]
    while queue or len(order) < len(by_pos):
        if not queue:
            queue = [next(s for s in by_pos if id(s) not in seen)]  # 순환이나 연결 안 된 도형
        s = queue.pop(0)
        if id(s) in seen:
            continue
        seen.add(id(s))
        order.append(s)
        queue.extend(sorted((n for n in nexts.get(id(s), []) if id(n) not in seen), key=lambda n: (n.y, n.x)))
    return order


def _shape_label(s: Shape) -> str:
    """도형 이름 = 안에 든 글자. 글상자 안에 도형이 또 있어도 글자만 모은다 (렌더링하지 않으므로 재귀 없음)."""
    texts = [b.text for b in _walk(s.text_blocks) if isinstance(b, (Paragraph, Heading)) and b.text.strip()]
    label = " ".join(" ".join(texts).split())
    return label or (f"이미지 {s.image_id}" if s.image_id else "")
