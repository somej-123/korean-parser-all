"""HWP 5.0 바이너리 파서 (parsers/hwp).

재배포할 수 있는 HWP 시험 파일이 없어, 레코드를 바이트로 직접 만들어 넣는다 (OLE 파일 대신 FakeFile).
실제 문서 시험은 tests/test_real_samples.py (samples_rfp/에 문서가 있을 때만).
"""

import struct
from pathlib import Path

import pytest

from korean_parser.ir import Break, Drawing, Heading, Paragraph, Tab, Table
from korean_parser.parsers.hwp import HwpParser
from korean_parser.parsers.hwp.records import Tag, build_tree, read_records


def rec(tag: int, level: int, data: bytes) -> bytes:
    size = len(data)
    if size >= 0xFFF:
        return struct.pack("<I", tag | level << 10 | 0xFFF << 20) + struct.pack("<I", size) + data
    return struct.pack("<I", tag | level << 10 | size << 20) + data


def wtext(s: str) -> bytes:
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def ctrl(code: int, cid: str = "") -> str:
    """문단 글 안의 8글자짜리 컨트롤: 코드, 컨트롤 id(4바이트, 거꾸로), 나머지, 코드."""
    raw = struct.pack("<H", code) + (cid[::-1].encode("latin-1") if cid else bytes(4)) + bytes(8)
    return (raw + struct.pack("<H", code)).decode("utf-16-le")


def para(level: int, text: str, para_shape=0, shapes=((0, 0),), extra: bytes = b"") -> bytes:
    chars = text + "\r"
    head = struct.pack("<IIHBBHHHI", len(chars), 0, para_shape, 0, 0, len(shapes), 0, 0, 0)
    out = rec(Tag.PARA_HEADER, level, head)
    out += rec(Tag.PARA_TEXT, level + 1, chars.encode("utf-16-le"))
    out += rec(Tag.PARA_CHAR_SHAPE, level + 1, b"".join(struct.pack("<II", a, b) for a, b in shapes))
    return out + extra


def char_shape(size: int, bold: bool = False) -> bytes:
    return (struct.pack("<7H", *[0] * 7) + bytes([100] * 7) + bytes(7) + bytes([100] * 7) + bytes(7)
            + struct.pack("<iI", size, 0x2 if bold else 0) + bytes(2)
            + struct.pack("<IIII", 0, 0, 0xFFFFFFFF, 0) + struct.pack("<H", 1))


def para_shape(align=0, head=0, level=0, left=0, indent=0) -> bytes:
    """left·indent는 pt×100 (HWPUNIT). 파일에는 2배로 저장한다."""
    a1 = align << 2 | head << 23 | level << 25
    return (struct.pack("<I6i", a1, left * 2, 0, indent * 2, 0, 0, 160) + struct.pack("<HHH", 0, 0, 0)
            + bytes(8) + struct.pack("<III", 0, 0, 160))


def border_fill(solid: bool) -> bytes:
    side = struct.pack("<BBI", 1 if solid else 0, 1, 0)
    return struct.pack("<H", 0) + side * 4 + side + struct.pack("<I", 0)


def numbering() -> bytes:
    out = b""
    for fmt, text in [(0, "^1."), (8, "^2.")] + [(0, f"^{i}") for i in range(3, 8)]:
        out += struct.pack("<IhhI", fmt << 5, 0, 0, 0) + wtext(text)
    return out + struct.pack("<H", 1) + struct.pack("<7I", *[1] * 7)


def docinfo() -> bytes:
    # 그림, 글꼴 7종(한글 1개), 테두리 2, 글자 모양 2, 탭, 번호 1, 글머리표, 문단 모양 3, 스타일 1 …
    counts = [0, 1, 0, 0, 0, 0, 0, 0, 2, 2, 0, 1, 0, 3, 1, 0, 0, 0]
    out = rec(Tag.DOCUMENT_PROPERTIES, 0, bytes(26))
    out += rec(Tag.ID_MAPPINGS, 0, struct.pack(f"<{len(counts)}i", *counts))
    out += rec(Tag.FACE_NAME, 1, b"\0" + wtext("바탕"))
    out += rec(Tag.BORDER_FILL, 1, border_fill(False)) + rec(Tag.BORDER_FILL, 1, border_fill(True))
    out += rec(Tag.CHAR_SHAPE, 1, char_shape(1000)) + rec(Tag.CHAR_SHAPE, 1, char_shape(1400, bold=True))
    out += rec(Tag.NUMBERING, 1, numbering())
    out += rec(Tag.PARA_SHAPE, 1, para_shape())
    out += rec(Tag.PARA_SHAPE, 1, para_shape(head=1, level=0))  # 개요 1수준
    out += rec(Tag.PARA_SHAPE, 1, para_shape(left=2000, indent=-1000))  # 왼쪽 20pt, 내어쓰기 10pt
    out += rec(Tag.STYLE, 1, wtext("바탕글") + wtext("Normal") + bytes(4) + struct.pack("<HH", 0, 0))
    return out


def table(level: int) -> bytes:
    """2×2 표: 첫 행은 두 칸을 합친 칸, 둘째 행은 두 칸. 칸은 가운데 세로 정렬, 테두리 2번(실선)."""
    common = struct.pack("<IiiIIi", 0x1, 0, 0, 20000, 4000, 0) + bytes(8) + struct.pack("<Ii", 0, 0) + wtext("")
    out = rec(Tag.CTRL_HEADER, level, b" lbt" + common)
    out += rec(Tag.TABLE, level + 1, struct.pack("<IHHh4H", 0, 2, 2, 0, 141, 141, 141, 141) + struct.pack("<3H", 1, 2, 2))

    def cell(col, row, cs, rs, text):
        head = struct.pack("<hHI4H2I4HH", 1, 0, 1 << 5, col, row, cs, rs, 10000 * cs, 2000, 141, 141, 141, 141, 2)
        return rec(Tag.LIST_HEADER, level + 1, head) + para(level + 1, text)

    return out + cell(0, 0, 2, 1, "머리") + cell(0, 1, 1, 1, "왼쪽") + cell(1, 1, 1, 1, "오른쪽")


def body() -> bytes:
    secd = rec(Tag.CTRL_HEADER, 1, b"dces" + struct.pack("<IHHHIH", 0, 0, 0, 0, 8000, 1) + bytes(20))
    secd += rec(Tag.PAGE_DEF, 2, struct.pack("<10I", 59528, 84188, 5669, 5669, 5669, 4252, 4252, 4252, 0, 0))
    out = para(0, ctrl(2, "secd") + "제목입니다", para_shape=1, extra=secd)
    out += para(0, "보통 글자굵은 글자", shapes=((0, 0), (5, 1)))
    out += para(0, "가" + ctrl(9) + "나" + "\n" + "다", para_shape=2)
    hlk = rec(Tag.CTRL_HEADER, 1, b"klh%" + struct.pack("<IB", 0, 0) + wtext("https\\://example.com;1;0;0;"))
    out += para(0, "링크: " + ctrl(3, "%hlk") + "누리집" + ctrl(4) + " 끝", extra=hlk)
    out += para(0, ctrl(11, "tbl "), extra=table(1))
    return out


class FakeFile:
    """HwpFile 대신 만든 레코드를 돌려준다."""

    compressed = False

    def __init__(self, body_data: bytes | None = None):
        self._doc = read_records(docinfo())
        self._body = build_tree(read_records(body() if body_data is None else body_data))

    def flat_records(self, name):
        return self._doc

    def records(self, name):
        return self._body

    def sections(self):
        return ["BodyText/Section0"]

    def bin_data(self, name, compressed):
        return None

    def summary(self):
        return {2: "시험 문서", 4: "우정호"}

    def close(self):
        pass


@pytest.fixture(scope="module")
def doc():
    return HwpParser("synthetic.hwp", file=FakeFile()).parse()


def test_records_roundtrip():
    # 크기가 0xFFF 이상이면 머리 뒤 4바이트에 크기를 둔다
    recs = read_records(rec(Tag.PARA_HEADER, 0, b"x" * 5000) + rec(Tag.PARA_TEXT, 1, b"ab"))
    assert [(r.tag, r.level, len(r.data)) for r in recs] == [(Tag.PARA_HEADER, 0, 5000), (Tag.PARA_TEXT, 1, 2)]
    tree = build_tree(recs)
    assert len(tree) == 1 and tree[0].children[0].data == b"ab"


def test_meta_and_page(doc):
    assert doc.meta.source_format == "hwp" and doc.meta.title == "시험 문서" and doc.meta.author == "우정호"
    assert doc.page.width_pt == pytest.approx(595.28) and doc.page.margin_left_pt == pytest.approx(56.69)


def test_outline_heading(doc):
    # 개요 문단은 제목. 번호는 구역의 개요 번호 정의(^1.)로 매긴다
    h = next(b for b in doc.blocks if isinstance(b, Heading))
    assert h.text == "제목입니다" and h.level == 1 and h.list_info.marker == "1."


def test_char_shapes_split_runs(doc):
    p = next(b for b in doc.blocks if isinstance(b, Paragraph) and "굵은" in b.text)
    bold = [i for i in p.inlines if i.style and i.style.bold]
    assert [i.text for i in bold] == ["굵은 글자"] and bold[0].style.size_pt == 14.0


def test_tab_line_break_and_halved_margins(doc):
    p = next(b for b in doc.blocks if isinstance(b, Paragraph) and b.text.startswith("가"))
    assert any(isinstance(i, Tab) for i in p.inlines) and any(isinstance(i, Break) for i in p.inlines)
    # 파일의 2배 값을 반으로: 왼쪽 20pt, 내어쓰기 -10pt → 첫 줄은 20pt, 나머지 줄은 30pt
    assert p.style.indent_left_pt == pytest.approx(30.0) and p.style.indent_first_pt == pytest.approx(-10.0)


def test_hyperlink(doc):
    p = next(b for b in doc.blocks if isinstance(b, Paragraph) and "누리집" in b.text)
    link = [i for i in p.inlines if getattr(i, "link", None)]
    assert [(i.text, i.link) for i in link] == [("누리집", "https://example.com")]


def test_table_merge_and_borders(doc):
    t = next(b for b in doc.blocks if isinstance(b, Table))
    assert [[(c.blocks[0].text, c.col_span) for c in r.cells] for r in t.rows] == [[("머리", 2)],
                                                                                 [("왼쪽", 1), ("오른쪽", 1)]]
    assert t.rows[0].cells[0].borders.top.style == "solid"
    assert t.rows[0].cells[0].v_align == "middle"
    assert t.col_widths_pt == [100.0, 100.0]


def test_not_hwp_ole_is_refused(tmp_path):
    from korean_parser.parsers import DocumentError, parse

    fake = tmp_path / "old.doc"
    fake.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504))
    with pytest.raises(DocumentError):
        parse(fake)



# ---------------------------------------------------------------------------
# 그리기 개체 (gso): 실제 문서(제안요청서의 글상자)에서 확인한 레코드 배치를 따른다
# ---------------------------------------------------------------------------


def matrix(a=1.0, b=0.0, c=0.0, d=0.0, e=1.0, f=0.0) -> bytes:
    return struct.pack("<6d", a, b, c, d, e, f)


def colorref(rgb: str) -> int:
    """#RRGGBB → COLORREF(0x00BBGGRR)."""
    r, g, b = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
    return b << 16 | g << 8 | r


def component(cid: str, w: int, h: int, *, top=True, scale=(1.0, 1.0), move=(0, 0), line=("#000000", 33, 1),
              fill: str | None = None, instid=0, children: list[int] | None = None) -> bytes:
    """개체 요소(SHAPE_COMPONENT): 컨트롤 id(맨 위 개체는 두 번), 위치·크기, 행렬, 선, 채우기, 그림자, 개체 id."""
    ids = cid[::-1].encode("latin-1") * (2 if top else 1)
    cur = (round(w * scale[0]), round(h * scale[1]))
    out = ids + struct.pack("<iiHHIIIIIhii", 0, 0, 0, 1, w, h, *cur, 0, 0, cur[0] // 2, cur[1] // 2)
    out += struct.pack("<H", 1) + matrix(c=move[0], f=move[1]) + matrix(a=scale[0], e=scale[1]) + matrix()
    if children is not None:  # 묶음: 안 개체 수와 컨트롤 id
        return out + struct.pack("<H", len(children)) + b"".join(struct.pack("<I", c) for c in children)             + struct.pack("<I", instid)
    color, width, attr = line
    out += struct.pack("<IiIB", colorref(color), width, attr, 0)
    if fill:
        out += struct.pack("<IIIi", 1, colorref(fill), 0, -1) + struct.pack("<I", 0) + bytes(1)
    else:
        out += struct.pack("<II", 0, 0)
    return out + bytes(16) + struct.pack("<I", instid) + bytes(2)


def gso(level: int, obj_id: int, w: int, h: int) -> bytes:
    """그리기 개체의 컨트롤 머리: 글자처럼 취급, 크기, 개체 id."""
    common = struct.pack("<IiiIIi", 0x1, 0, 0, w, h, 0) + bytes(8) + struct.pack("<Ii", obj_id, 0) + wtext("")
    return rec(Tag.CTRL_HEADER, level, b" osg" + common)


def rect_points(level: int, w: int, h: int, ratio=0) -> bytes:
    return rec(Tag.SHAPE_COMPONENT_RECTANGLE, level, struct.pack("<B8i", ratio, 0, 0, w, 0, w, h, 0, h))


def text_box(level: int, text: str, v_align=1) -> bytes:
    """글상자: LIST_HEADER(문단 수, 속성의 세로 정렬, 안쪽 여백 1mm, 너비) 뒤 문단."""
    head = struct.pack("<hHI4HI", 1, 0, v_align << 5, 283, 283, 283, 283, 10000)
    return rec(Tag.LIST_HEADER, level, head) + para(level, text)


def shape_doc(*objects: bytes):
    """그리기 개체를 하나씩 문단에 넣은 문서."""
    data = b"".join(para(0, ctrl(11, "gso "), extra=o) for o in objects)
    doc = HwpParser("shapes.hwp", file=FakeFile(data)).parse()
    return doc, [b for b in doc.blocks if isinstance(b, Drawing)]


def test_rect_text_box():
    # 100×50pt로 만든 둥근 사각형을 가로로 2배 늘림. 노란 채우기, 빨간 파선 0.5pt, 가운데 정렬 글상자
    obj = gso(1, 7, 20000, 5000)
    obj += rec(Tag.SHAPE_COMPONENT, 2, component("$rec", 10000, 5000, scale=(2.0, 1.0),
                                                   line=("#FF0000", 50, 2), fill="#FFFF00", instid=8))
    obj += text_box(3, "상자 안 글") + rect_points(3, 10000, 5000, ratio=20)
    doc, (d,) = shape_doc(obj)
    s = d.shapes[0]
    assert (d.width_pt, d.height_pt) == (200.0, 50.0)
    assert (s.geometry, s.x, s.y, s.width, s.height) == ("roundRect", 0, 0, 200.0, 50.0)
    assert s.path.startswith("M10,0 H190 A10,10")  # 곡률 20% → 반지름은 짧은 변(50pt)의 20%
    assert (s.fill, s.stroke, s.stroke_width_pt, s.stroke_dash) == ("#FFFF00", "#FF0000", 0.5, "dashed")
    assert [b.text for b in s.text_blocks] == ["상자 안 글"] and s.text_v_align == "middle"
    assert s.id == "8" and not doc.warnings


def test_group_with_connector():
    # 묶음 안에 사각형 두 개와 꺾인 한쪽 화살표 연결선. 안 개체의 행렬은 묶음 기준 좌표로 옮긴다
    left = rec(Tag.SHAPE_COMPONENT, 3, component("$rec", 4000, 2000, top=False, instid=11)) + rect_points(4, 4000, 2000)
    right = rec(Tag.SHAPE_COMPONENT, 3, component("$rec", 4000, 2000, top=False, move=(10000, 3000), instid=12))
    right += rect_points(4, 4000, 2000)
    link = struct.pack("<4iI4II", 4000, 1000, 10000, 4000, 4, 11, 1, 12, 3, 0)  # 끝: (개체 id, 연결점), 조절점 0개
    conn = rec(Tag.SHAPE_COMPONENT, 3, component("$col", 6000, 3000, top=False, move=(4000, 1000), instid=13))
    conn += rec(Tag.SHAPE_COMPONENT_LINE, 4, struct.pack("<4i", 0, 0, 6000, 3000) + link[16:])
    obj = gso(1, 20, 14000, 5000)
    obj += rec(Tag.SHAPE_COMPONENT, 2, component("$con", 14000, 5000, instid=21, children=[1, 2, 3]))
    obj += left + right + conn
    doc, (d,) = shape_doc(obj)
    a, b, c = d.shapes
    assert (a.x, a.y, b.x, b.y) == (0, 0, 100.0, 30.0)
    assert c.geometry == "bentConnector3" and c.tail_arrow and not c.head_arrow
    assert (c.connect_from, c.connect_to) == ("11", "12") and (c.x, c.y, c.width, c.height) == (40.0, 10.0, 60.0, 30.0)
    assert not doc.warnings


def test_connector_to_top_level_ids():
    # 연결선 끝이 컨트롤 머리의 개체 id를 가리켜도 그 개체의 도형 id(instid)로 잇는다
    box = gso(1, 100, 4000, 2000) + rec(Tag.SHAPE_COMPONENT, 2, component("$rec", 4000, 2000, instid=101))
    box += rect_points(3, 4000, 2000)
    line = gso(1, 200, 4000, 1) + rec(Tag.SHAPE_COMPONENT, 2, component("$col", 4000, 1, instid=201))
    line += rec(Tag.SHAPE_COMPONENT_LINE, 3, struct.pack("<4iI4II", 0, 0, 4000, 0, 0, 100, 1, 0, 0, 0))
    doc, (_, d) = shape_doc(box, line)
    assert d.shapes[0].geometry == "straightConnector1" and d.shapes[0].connect_from == "101"


def test_ellipse_line_polygon_curve():
    ell = gso(1, 1, 4000, 2000) + rec(Tag.SHAPE_COMPONENT, 2, component("$ell", 4000, 2000, fill="#00FF00"))
    ell += rec(Tag.SHAPE_COMPONENT_ELLIPSE, 3, struct.pack("<I6i", 0, 2000, 1000, 4000, 1000, 2000, 0) + bytes(24))
    # 끝 화살표(모양 1) 실선
    lin = gso(1, 2, 4000, 0) + rec(Tag.SHAPE_COMPONENT, 2, component("$lin", 4000, 0, line=("#0000FF", 100, 1 | 1 << 16)))
    lin += rec(Tag.SHAPE_COMPONENT_LINE, 3, struct.pack("<4ii", 0, 0, 4000, 0, 0))
    pol = gso(1, 3, 2000, 2000) + rec(Tag.SHAPE_COMPONENT, 2, component("$pol", 2000, 2000))
    pol += rec(Tag.SHAPE_COMPONENT_POLYGON, 3, struct.pack("<i6i", 3, 0, 2000, 1000, 0, 2000, 2000))
    cur = gso(1, 4, 2000, 1000) + rec(Tag.SHAPE_COMPONENT, 2, component("$cur", 2000, 1000))
    cur += rec(Tag.SHAPE_COMPONENT_CURVE, 3, struct.pack("<i6i2B", 3, 0, 1000, 1000, 0, 2000, 1000, 1, 1) + bytes(4))
    doc, (e, ln, pg, cv) = shape_doc(ell, lin, pol, cur)
    s = e.shapes[0]
    assert (s.geometry, s.width, s.height, s.fill) == ("ellipse", 40.0, 20.0, "#00FF00")
    s = ln.shapes[0]
    assert (s.geometry, s.width, s.stroke, s.stroke_width_pt, s.tail_arrow, s.head_arrow) ==         ("line", 40.0, "#0000FF", 1.0, True, False)
    assert pg.shapes[0].path == "M0,20 L10,0 L20,20 Z"
    assert cv.shapes[0].path.startswith("M0,10 C") and not doc.warnings


def test_compose_and_dutmal_text_comes_from_controls():
    # 글자 겹침(tcps)·덧말(tdut)의 글은 문단 글(PARA_TEXT)이 아니라 컨트롤에 있다. 실제 제안요청서의 체크 표시
    # '□'+'√' 겹침이 사라져 '□ 있음 ☑ 없음'에서 어느 칸에 체크했는지 알 수 없었다
    tcps = rec(Tag.CTRL_HEADER, 1, b"spct" + wtext("□√") + struct.pack("<BbBB", 3, -3, 0, 2) + struct.pack("<2I", 7, 0xFFFFFFFF))
    tdut = rec(Tag.CTRL_HEADER, 1, b"tudt" + wtext("大韓民國") + wtext("대한민국") + struct.pack("<5I", 0, 50, 0, 0, 3))
    data = para(0, "□ 있음 " + ctrl(23, "tcps") + "없음", extra=tcps)
    data += para(0, ctrl(23, "tdut") + " 만세", extra=tdut)
    doc = HwpParser("compose.hwp", file=FakeFile(data)).parse()
    texts = [b.text for b in doc.blocks if isinstance(b, Paragraph)]
    assert "□ 있음 □√없음" in texts and "大韓民國(대한민국) 만세" in texts


def test_object_between_text_splits_paragraph():
    # 글 가운데 놓인 글상자는 그 자리에서 문단을 나눈다 ('앞 글' 문단, 도식, '뒤 글' 문단). 한컴 미리보기처럼 놓인
    # 자리에서 읽힌다. 문단 앞으로 밀면 hwpxlib RectInPara의 미리보기 재현율이 0.78이었다. 한쪽에만 글이 있으면 그대로
    obj = gso(1, 7, 10000, 5000) + rec(Tag.SHAPE_COMPONENT, 2, component("$rec", 10000, 5000, instid=8))
    obj += text_box(3, "상자 안 글") + rect_points(3, 10000, 5000)
    data = para(0, "앞 글 " + ctrl(11, "gso ") + "뒤 글", extra=obj)
    data += para(0, ctrl(11, "gso ") + "글상자 뒤에만 글", extra=obj)
    doc = HwpParser("mid.hwp", file=FakeFile(data)).parse()
    kinds = [(type(b).__name__, getattr(b, "text", "")) for b in doc.blocks]
    assert kinds == [("Paragraph", "앞 글"), ("Drawing", ""), ("Paragraph", "뒤 글"),
                     ("Paragraph", "글상자 뒤에만 글"), ("Drawing", "")]


def _vt(idx: int, cls: int, name: str | None, payload: bytes) -> bytes:
    """VtChart 직렬화 칸: 순번 + 종류 번호 [+ 처음 나온 종류의 선언] + 값."""
    decl = b""
    if name:
        n = (name + "\0").encode("ascii")
        decl = struct.pack("<H", len(n)) + n + struct.pack("<H", 1)
    return struct.pack("<II", idx, cls) + decl + payload


def _vt_str(text: str) -> bytes:
    raw = text.encode("cp949") + b"\0" + b"\0" + (text + "\0").encode("utf-16-le")
    return struct.pack("<H", len(raw)) + raw


def test_hwp_chart_data_grid():
    # 한글 차트(OLE 안 VtChart)의 데이터 표: 열 이름 → 행마다 행 이름 + 값. hwplib '차트.hwp'의 배치 그대로
    from korean_parser.parsers.hwp.chart import parse_grid

    data = b"VtChart\0..VtDataGrid\0" + b"\x00" * 6
    data += _vt(2, 5, "VtString", _vt_str("1분기")) + _vt(3, 5, None, _vt_str("2분기"))
    data += _vt(4, 5, None, _vt_str("매출")) + _vt(5, 7, "VtInteger", struct.pack("<i", 92))
    data += _vt(6, 7, None, struct.pack("<i", 82))
    data += _vt(7, 5, None, _vt_str("비용")) + _vt(8, 9, "VtDouble", struct.pack("<d", 12.5))
    data += _vt(9, 9, None, struct.pack("<d", 30.0)) + b"\x00\x08\x00VtBackdrop\0"
    assert parse_grid(data) == (["1분기", "2분기"], ["매출", "비용"], [[92.0, 82.0], [12.5, 30.0]])
    # 행마다 값 수가 열 수와 다르면 차트로 보지 않는다 ('OLE 개체는 표시하지 않음'으로 남는다)
    broken = data.replace(_vt(9, 9, None, struct.pack("<d", 30.0)), b"")
    assert parse_grid(broken) is None


def test_hwp_caption_hidden_comment_and_master_page():
    # 개체 캡션(컨트롤 아래, 개체 레코드 앞의 LIST_HEADER + 문단)은 개체 뒤 문단으로 읽는다.
    # 숨은 설명(tcmt)·바탕쪽(구역 정의 아래 문단)의 글은 v1.0에서 경고만 남겼고, v1.4.1부터 숨은 설명은 메모 목록,
    # 바탕쪽은 머리말 뒤로 읽는다 (본문에는 넣지 않는다)
    caption = rec(Tag.LIST_HEADER, 2, struct.pack("<hHII", 1, 0, 0, 3) + bytes(14)) + para(2, "그림 1. 흐름")
    obj = gso(1, 7, 10000, 5000) + caption + rec(Tag.SHAPE_COMPONENT, 2, component("$rec", 10000, 5000, instid=8))
    obj += rect_points(3, 10000, 5000)
    tcmt = rec(Tag.CTRL_HEADER, 1, b"tmct") + rec(Tag.LIST_HEADER, 2, struct.pack("<hHI", 1, 0, 0)) + para(2, "숨은 글")
    secd = rec(Tag.CTRL_HEADER, 1, b"dces" + struct.pack("<IHHHIH", 0, 0, 0, 0, 8000, 1) + bytes(20))
    secd += rec(Tag.LIST_HEADER, 2, struct.pack("<hHI", 1, 0, 0)) + para(2, "바탕쪽 글")
    data = para(0, ctrl(2, "secd") + "본문", extra=secd)
    data += para(0, ctrl(11, "gso "), extra=obj)
    data += para(0, ctrl(15, "tcmt") + "끝", extra=tcmt)
    doc = HwpParser("extras.hwp", file=FakeFile(data)).parse()
    kinds = [(type(b).__name__, getattr(b, "text", "")) for b in doc.blocks]
    assert ("Paragraph", "그림 1. 흐름") in kinds and kinds.index(("Paragraph", "그림 1. 흐름")) == \
        [k for k, _ in kinds].index("Drawing") + 1
    assert not doc.warnings
    assert [n.blocks[0].text for n in doc.notes.values()] == ["숨은 글"]
    assert [(b.style_name, b.text) for b in doc.header] == [("master_page", "바탕쪽 글")]
    assert not any("숨은 글" in t or "바탕쪽 글" in t for _, t in kinds)
