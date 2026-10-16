"""HWP 5.0 바이너리 파서 (parsers/hwp).

재배포할 수 있는 HWP 시험 파일이 없어, 레코드를 바이트로 직접 만들어 넣는다 (OLE 파일 대신 FakeFile).
실제 문서로는 samples_rfp/*.hwp가 있을 때만 돌린다 (재배포 조건이 불분명해 저장소에 넣지 않는다).
"""

import struct
from pathlib import Path

import pytest

from korean_parser.ir import Break, Heading, Paragraph, Tab, Table
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

    def __init__(self):
        self._doc = read_records(docinfo())
        self._body = build_tree(read_records(body()))

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


SAMPLES = sorted(Path(__file__).resolve().parents[1].glob("samples_rfp/*.hwp"))


@pytest.mark.skipif(not SAMPLES, reason="samples_rfp/*.hwp 없음 (재배포할 수 없는 실제 문서)")
@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.name[:20])
def test_real_documents_preview_text(path):
    """실제 문서: 한컴오피스 미리보기 글(PrvText)의 앞부분이 결과에 그대로 나온다."""
    import olefile

    from korean_parser.parsers import parse

    with olefile.OleFileIO(str(path)) as ole:
        preview = ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
    want = "".join(ch for ch in preview if ch.isalnum())[:100]
    doc = parse(path)
    got = "".join(ch for ch in " ".join(b.text for b in doc.iter_blocks() if isinstance(b, (Heading, Paragraph)))
                  if ch.isalnum())
    assert want in got
