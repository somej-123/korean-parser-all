"""HWP 5.0 파서: 한글 바이너리 문서(.hwp)를 IR(Document)로 변환한다.

HWP 5.0과 HWPX는 같은 문서 모델을 바이너리와 XML로 저장한 것이다. 그래서 바이너리 레코드를 HWPX와 같은 모양의
XML(header.xml, section.xml에서 HWPX 파서가 읽는 요소·속성)로 옮기고, 그 뒤의 변환(글자·문단 모양, 개요 번호,
표 병합, 머리말·각주 …)은 HWPX 파서를 그대로 쓴다.

    DocInfo           → header.xml  (글꼴, 테두리/배경, 글자 모양, 문단 모양, 번호, 글머리표, 스타일)
    BodyText/SectionN → section.xml (문단, 글자 모양 구간, 표, 그림, 수식, 머리말·꼬리말, 각주, 하이퍼링크)
    BinData/BINxxxx.* → 그림

HWPX와 다른 점
- 문단 여백·간격 값은 실제의 2배로 저장된다 (HWPX의 hp:default와 같은 구버전 호환 값). 2로 나눈다.
- 테두리/배경 id는 1부터, 글자·문단 모양과 스타일 id는 0부터 센다 (HWPX와 같다).
- 그리기 개체(사각형, 타원, 선, 다각형, 곡선, 연결선, 묶음, 그림)는 개체 요소 레코드를 HWPX의 hp:rect 등과
  같은 모양(renderingInfo 행렬, 꼭짓점, lineShape, fillBrush, drawText)으로 옮겨 HWPX의 ShapeReader가 그린다.

참고: 한글과컴퓨터 「한글 문서 파일 형식 5.0」 공개 문서.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import struct
from pathlib import Path

from lxml import etree

from ...ir import Chart, ChartSeries, Document, Image, Meta, Provenance
from ..hwpx import HwpxParser
from ..hwpx.header import Counters, Header
from ..hwpx.shapes import ShapeReader
from .records import Reader, Record, Tag, build_tree, ctrl_id, inflate, read_records

PARSER_NAME = "hwp-native"

_LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
_ALIGN = {0: "JUSTIFY", 1: "LEFT", 2: "RIGHT", 3: "CENTER", 4: "DISTRIBUTE", 5: "DISTRIBUTE_SPACE"}
_HEAD = {0: "NONE", 1: "OUTLINE", 2: "NUMBER", 3: "BULLET"}
_LINE_SPACING = {0: "PERCENT", 1: "FIXED", 2: "BETWEEN_LINES", 3: "AT_LEAST"}
_NUM_FORMAT = ["DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL", "LATIN_SMALL",
               "CIRCLED_LATIN_CAPITAL", "CIRCLED_LATIN_SMALL", "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE",
               "HANGUL_JAMO", "CIRCLED_HANGUL_JAMO", "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH"]
# 테두리선 종류 (0은 '없음')와 굵기(mm)
_STROKE = ["NONE", "SOLID", "DASH", "DOT", "DASH_DOT", "DASH_DOT_DOT", "LONG_DASH", "CIRCLE", "DOUBLE_SLIM",
           "SLIM_THICK", "THICK_SLIM", "SLIM_THICK_SLIM", "WAVE", "DOUBLE_WAVE", "THICK_3D", "THICK_3D_REVERSAL",
           "3D", "3D_REVERSAL"]
_STROKE_MM = [0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]
_V_ALIGN = {0: "TOP", 1: "CENTER", 2: "BOTTOM"}
_WRAP = {0: "SQUARE", 1: "TOP_AND_BOTTOM", 2: "BEHIND_TEXT", 3: "IN_FRONT_OF_TEXT"}
_VERT_REL = {0: "PAPER", 1: "PAGE", 2: "PARA"}
_HORZ_REL = {0: "PAPER", 1: "PAGE", 2: "COLUMN", 3: "PARA"}
_VERT_ALIGN = {0: "TOP", 1: "CENTER", 2: "BOTTOM", 3: "INSIDE", 4: "OUTSIDE"}
_HORZ_ALIGN = {0: "LEFT", 1: "CENTER", 2: "RIGHT", 3: "INSIDE", 4: "OUTSIDE"}
_PAGE_TYPE = {0: "BOTH", 1: "EVEN", 2: "ODD"}

# 문단 글(PARA_TEXT)의 제어 문자. 문자 하나짜리와, 뒤에 7글자(14바이트)가 더 붙는 것(인라인·확장 컨트롤)이 있다
_CHAR_CTRL = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}
# 글자 겹침 테두리 (HWP 0~ 순서 = HWPX circleType)
_COMPOSE_CIRCLE = dict(enumerate([
    "CHAR", "SHAPE_CIRCLE", "SHAPE_REVERSAL_CIRCLE", "SHAPE_RECTANGLE", "SHAPE_REVERSAL_RECTANGLE", "SHAPE_TRIANGLE",
    "SHAPE_REVERSAL_TIRANGLE", "SHAPE_LIGHT", "SHAPE_RHOMBUS", "SHAPE_REVERSAL_RHOMBUS", "SHAPE_ROUNDED_RECTANGLE",
    "SHAPE_EMPTY_CIRCULATE_TRIANGLE", "SHAPE_THIN_CIRCULATE_TRIANGLE", "SHAPE_THICK_CIRCULATE_TRIANGLE",
]))
_EXT_CTRL = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}  # CTRL_HEADER 레코드와 차례로 짝이 된다


class HwpError(ValueError):
    """열 수 없는 HWP (암호, 배포용 문서, 손상). 메시지는 사용자에게 그대로 보여 줄 수 있다."""


def parse_hwp(path: str | Path) -> Document:
    return HwpParser(path).parse()


def is_hwp(path: str | Path) -> bool:
    """OLE 복합 파일 안에 HWP 서명이 있는가."""
    import olefile

    try:
        if not olefile.isOleFile(str(path)):
            return False
        with olefile.OleFileIO(str(path)) as ole:
            return ole.exists("FileHeader") and ole.openstream("FileHeader").read(17) == b"HWP Document File"
    except Exception:  # noqa: BLE001 - 손상된 OLE 파일: HWP로 보지 않고 '열 수 없음'으로 안내한다
        return False


# ---------------------------------------------------------------------------
# 파일 열기
# ---------------------------------------------------------------------------


class HwpFile:
    """OLE 복합 파일에서 HWP 스트림을 꺼낸다 (압축이면 푼다)."""

    def __init__(self, path: Path):
        import olefile

        self.ole = olefile.OleFileIO(str(path))
        head = self.ole.openstream("FileHeader").read()
        if head[:17] != b"HWP Document File":
            raise HwpError(f"{path.name}: HWP 문서가 아닙니다")
        self.version = tuple(head[32:36][::-1])
        flags = int.from_bytes(head[36:40], "little")
        self.compressed = bool(flags & 0x1)
        if flags & 0x2:
            raise HwpError(f"{path.name}: 암호가 걸린 문서입니다. 한글에서 암호를 풀고 저장한 뒤 다시 넣어 주세요")
        if flags & 0x4:
            raise HwpError(f"{path.name}: 배포용 문서(읽기 전용)라 본문이 암호화되어 있습니다. "
                           "한글에서 일반 문서로 저장한 뒤 다시 넣어 주세요")

    def stream(self, name: str) -> bytes:
        data = self.ole.openstream(name).read()
        return inflate(data) if self.compressed else data

    def records(self, name: str) -> list[Record]:
        """레코드 나무 (본문: 문단 → 컨트롤 → 칸 문단 …)."""
        return build_tree(read_records(self.stream(name)))

    def flat_records(self, name: str) -> list[Record]:
        """레코드를 나온 차례대로 (DocInfo: 글꼴·모양 레코드가 ID_MAPPINGS 아래에 매달려 있어 평평하게 읽는다)."""
        return read_records(self.stream(name))

    def sections(self) -> list[str]:
        names = ["/".join(e) for e in self.ole.listdir() if e[0] == "BodyText" and len(e) == 2]
        return sorted(names, key=lambda n: int(re.sub(r"\D", "", n) or 0))

    def bin_data(self, name: str, compressed: bool) -> bytes | None:
        path = f"BinData/{name}"
        if not self.ole.exists(path):
            return None
        data = self.ole.openstream(path).read()
        if compressed:
            try:
                return inflate(data)
            except Exception:  # noqa: BLE001 - 압축 표시와 실제가 다른 파일이 있다
                return data
        return data

    def summary(self) -> dict:
        """문서 요약 정보 (OLE 속성 집합): 속성 번호 → 값. 2 제목, 4 지은이, 12 만든 날짜, 13 고친 날짜.

        olefile.getproperties는 HWP가 쓰는 UTF-16 문자열(VT_LPWSTR)의 길이를 바이트로 읽어 뒤 속성까지 섞어 버려서
        직접 읽는다.
        """
        import datetime

        if not self.ole.exists("\x05HwpSummaryInformation"):
            return {}
        data = self.ole.openstream("\x05HwpSummaryInformation").read()
        out: dict = {}
        try:
            (sec,) = struct.unpack_from("<I", data, 44)  # 첫 속성 묶음의 위치 (머리 28 + FMTID 16 뒤)
            _, count = struct.unpack_from("<II", data, sec)
            for k in range(count):
                pid, off = struct.unpack_from("<II", data, sec + 8 + 8 * k)
                pos = sec + off
                (vt,) = struct.unpack_from("<I", data, pos)
                if vt == 31:  # VT_LPWSTR: 글자 수(끝 0 포함) + UTF-16
                    (n,) = struct.unpack_from("<I", data, pos + 4)
                    out[pid] = data[pos + 8:pos + 8 + 2 * n].decode("utf-16-le", "replace").rstrip("\x00")
                elif vt == 64:  # VT_FILETIME: 1601-01-01부터 100ns 단위
                    (ft,) = struct.unpack_from("<Q", data, pos + 4)
                    if ft:
                        out[pid] = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)
        except struct.error:
            pass
        return out

    def close(self) -> None:
        self.ole.close()


# ---------------------------------------------------------------------------
# DocInfo → header.xml
# ---------------------------------------------------------------------------


def _sub(parent, tag: str, **attrs):
    el = etree.SubElement(parent, tag)
    for k, v in attrs.items():
        if v is not None:
            el.set(k, str(v))
    return el


def _has_text(paras: list[Record]) -> bool:
    """문단 레코드들에 보이는 글자가 있는가 (8글자짜리 컨트롤은 건너뛴다)."""
    for p in paras:
        for t in p.kids(Tag.PARA_TEXT):
            chars, i = t.data.decode("utf-16-le", "replace"), 0
            while i < len(chars):
                c = ord(chars[i])
                if c >= 32:
                    if not chars[i].isspace():
                        return True
                    i += 1
                else:
                    i += 1 if c in _CHAR_CTRL else 8
        if _has_text(p.kids(Tag.PARA_HEADER)):
            return True
    return False


class _DocInfo:
    """DocInfo 레코드를 HWPX header.xml 모양의 XML로 옮긴다. 그림 목록(id → 스트림 이름, 압축 여부)도 모은다."""

    def __init__(self, records: list[Record], compressed: bool):
        self.root = etree.Element("head")
        self.bins: dict[int, tuple[str, bool]] = {}
        self.compressed = compressed
        ref = _sub(self.root, "refList")
        by: dict[int, list[Record]] = {}
        for r in records:
            by.setdefault(r.tag, []).append(r)
        mapping = by.get(Tag.ID_MAPPINGS)
        counts = []
        if mapping:
            rd = Reader(mapping[0].data)
            counts = [rd.i32() for _ in range(len(mapping[0].data) // 4)]
        self._fonts(_sub(ref, "fontfaces"), by.get(Tag.FACE_NAME, []), counts[1:8])
        for i, r in enumerate(by.get(Tag.BIN_DATA, [])):
            self._bin(r)
        bfs = _sub(ref, "borderFills")
        for i, r in enumerate(by.get(Tag.BORDER_FILL, [])):
            self._border_fill(bfs, i + 1, r.data)
        cps = _sub(ref, "charProperties")
        for i, r in enumerate(by.get(Tag.CHAR_SHAPE, [])):
            self._char(cps, i, r.data)
        pps = _sub(ref, "paraProperties")
        for i, r in enumerate(by.get(Tag.PARA_SHAPE, [])):
            self._para(pps, i, r.data)
        nbs = _sub(ref, "numberings")
        for i, r in enumerate(by.get(Tag.NUMBERING, [])):
            self._numbering(nbs, i + 1, r.data)
        bls = _sub(ref, "bullets")
        for i, r in enumerate(by.get(Tag.BULLET, [])):
            rd = Reader(r.data)
            rd.skip(12)  # 문단 머리 정보 (정렬, 너비, 거리, 글자 모양)
            _sub(bls, "bullet", id=i + 1, char=chr(rd.u16() or 0x2022))
        sts = _sub(ref, "styles")
        for i, r in enumerate(by.get(Tag.STYLE, [])):
            rd = Reader(r.data)
            name, eng = rd.text(), rd.text()
            rd.skip(4)  # 속성, 다음 스타일, 언어
            para, char = rd.u16(), rd.u16()
            _sub(sts, "style", id=i, name=name, engName=eng, paraPrIDRef=para, charPrIDRef=char)

    def _fonts(self, parent, faces: list[Record], counts: list[int]) -> None:
        pos = 0
        for lang, n in zip(_LANGS, counts or [len(faces)]):
            ff = _sub(parent, "fontface", lang=lang)
            for i in range(max(n, 0)):
                if pos >= len(faces):
                    break
                rd = Reader(faces[pos].data)
                rd.u8()
                _sub(ff, "font", id=i, face=rd.text())
                pos += 1

    def _bin(self, r: Record) -> None:
        rd = Reader(r.data)
        attr = rd.u16()
        kind, comp = attr & 0xF, (attr >> 4) & 0x3
        if kind == 0:  # 바깥 파일에 연결된 그림: 문서 안에 없다
            return
        bid = rd.u16()
        # 1 EMBEDDING: 레코드에 확장자가 있다. 2 STORAGE(OLE 개체, 차트 등): 확장자 없이 스트림 이름이 'BIN0001.OLE'
        ext = rd.text() if kind == 1 else "OLE" if kind == 2 else ""
        name = f"BIN{bid:04X}" + (f".{ext}" if ext else "")
        compressed = self.compressed if comp == 0 else comp == 1
        self.bins[bid] = (name, compressed)

    def _border_fill(self, parent, bid: int, data: bytes) -> None:
        rd = Reader(data)
        rd.u16()
        bf = _sub(parent, "borderFill", id=bid)
        for side in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
            kind, width, col = rd.u8(), rd.u8(), rd.color()
            mm = _STROKE_MM[width] if width < len(_STROKE_MM) else 0.12
            _sub(bf, side, type=_STROKE[kind] if kind < len(_STROKE) else "SOLID", width=f"{mm} mm",
                 color=col or "#000000")
        rd.skip(6)  # 대각선
        fill = rd.u32()
        if fill & 0x1:  # 단색 채우기
            face = rd.color()
            rd.u32()  # 무늬 색
            rd.i32()  # 무늬 종류
            if face:
                brush = _sub(bf, "fillBrush")
                _sub(brush, "winBrush", faceColor=face)

    def _char(self, parent, cid: int, data: bytes) -> None:
        rd = Reader(data)
        fonts = [rd.u16() for _ in range(7)]
        ratios = [rd.u8() for _ in range(7)]
        spacings = [rd.i8() for _ in range(7)]
        rd.skip(14)  # 상대 크기, 글자 위치
        height, attr = rd.i32(), rd.u32()
        rd.skip(2)  # 그림자 간격
        text_color, _underline_color, shade = rd.color(), rd.color(), rd.color()
        cp = _sub(parent, "charPr", id=cid, height=height, textColor=text_color or "#000000",
                  shadeColor=shade or "none")
        _sub(cp, "fontRef", hangul=fonts[0], latin=fonts[1])
        _sub(cp, "ratio", hangul=ratios[0])
        _sub(cp, "spacing", hangul=spacings[0])
        if attr & 0x1:
            _sub(cp, "italic")
        if attr & 0x2:
            _sub(cp, "bold")
        if (attr >> 2) & 0x3:
            _sub(cp, "underline", type="BOTTOM")
        if (attr >> 18) & 0x7:
            _sub(cp, "strikeout", shape="SOLID")
        if attr & (1 << 15):
            _sub(cp, "supscript")
        elif attr & (1 << 16):
            _sub(cp, "subscript")

    def _para(self, parent, pid: int, data: bytes) -> None:
        rd = Reader(data)
        a1 = rd.u32()
        left, right, indent, prev, nxt, old_spacing = (rd.i32() for _ in range(6))
        rd.u16()  # 탭 정의
        head_ref, border = rd.u16(), rd.u16()
        rd.skip(8)  # 테두리 간격
        rd.u32()
        a3, spacing = rd.u32(), rd.u32()
        if len(data) < 54:  # 5.0.2.5보다 오래된 문서: 줄 간격은 속성1과 옛 값
            a3, spacing = a1 & 0x3, old_spacing
        pp = _sub(parent, "paraPr", id=pid)
        _sub(pp, "align", horizontal=_ALIGN.get((a1 >> 2) & 0x7, "JUSTIFY"))
        head = (a1 >> 23) & 0x3
        _sub(pp, "heading", type=_HEAD[head], idRef=head_ref, level=(a1 >> 25) & 0x7)
        if a1 & (1 << 19):
            _sub(pp, "breakSetting", pageBreakBefore=1)
        margin = _sub(pp, "margin")
        # 여백·간격은 2배로 저장되어 있다
        for tag, v in (("intent", indent), ("left", left), ("right", right), ("prev", prev), ("next", nxt)):
            _sub(margin, tag, value=v // 2)
        kind = _LINE_SPACING.get(a3 & 0x1F, "PERCENT")
        _sub(pp, "lineSpacing", type=kind, value=spacing if kind == "PERCENT" else spacing // 2)
        if border:
            _sub(pp, "border", borderFillIDRef=border)

    def _numbering(self, parent, nid: int, data: bytes) -> None:
        rd = Reader(data)
        heads = []
        for _ in range(7):
            attr = rd.u32()
            rd.skip(8)  # 너비 보정, 본문과의 거리, 글자 모양
            heads.append((attr, rd.text()))
        start = rd.u16()
        starts = [rd.u32() for _ in range(7)]
        nb = _sub(parent, "numbering", id=nid, start=start or 1)
        for level, (attr, fmt_text) in enumerate(heads, 1):
            fmt = (attr >> 5) & 0xF
            ph = _sub(nb, "paraHead", level=level, start=starts[level - 1] or 1,
                      numFormat=_NUM_FORMAT[fmt] if fmt < len(_NUM_FORMAT) else "DIGIT")
            ph.text = fmt_text


# ---------------------------------------------------------------------------
# BodyText → section.xml
# ---------------------------------------------------------------------------


class _Body:
    """본문 레코드(문단 나무)를 HWPX section.xml 모양의 XML로 옮긴다."""

    def __init__(self, warn):
        self.warn = warn

    def section(self, records: list[Record]):
        sec = etree.Element("sec")
        self._paragraphs(sec, [r for r in records if r.tag == Tag.PARA_HEADER])
        _link_connectors(sec)
        return sec

    def _paragraphs(self, parent, paras: list[Record]) -> None:
        for p in paras:
            parent.append(self._paragraph(p))

    def _paragraph(self, p: Record):
        if _has_text(p.kids(Tag.PARA_HEADER)):
            # 문단 바로 아래(컨트롤 없이) 달린 LIST_HEADER + 문단: 메모·변경 기록으로 보이나 확인하지 못했다 (hwplib etc.hwp)
            self.warn("본문 밖 글 목록(메모 등)의 글은 넣지 않음")
        rd = Reader(p.data)
        rd.u32()
        rd.u32()  # 컨트롤 마스크
        para_shape, style, brk = rd.u16(), rd.u8(), rd.u8()
        el = etree.Element("p", paraPrIDRef=str(para_shape), styleIDRef=str(style))
        if brk & 0x04:  # 쪽 나누기
            el.set("pageBreak", "1")
        text = p.kid(Tag.PARA_TEXT)
        chars = text.data.decode("utf-16-le", "replace") if text else ""
        shapes = []
        cs = p.kid(Tag.PARA_CHAR_SHAPE)
        if cs is not None:
            r = Reader(cs.data)
            shapes = [(r.u32(), r.u32()) for _ in range(len(cs.data) // 8)]
        if not shapes:
            shapes = [(0, 0)]
        ctrls = iter(p.kids(Tag.CTRL_HEADER))

        run = t = None
        bounds = [s[0] for s in shapes[1:]]
        shape_iter = iter(shapes)
        next_shape = next(shape_iter)

        def new_run(shape_id: int):
            nonlocal run, t
            run = _sub(el, "run", charPrIDRef=shape_id)
            t = None

        def add_text(s: str):
            nonlocal t
            if t is None:
                t = _sub(run, "t")
                t.text = ""
            if len(t):
                t[-1].tail = (t[-1].tail or "") + s
            else:
                t.text += s

        def add_mark(tag: str):
            nonlocal t
            if t is None:
                t = _sub(run, "t")
                t.text = ""
            _sub(t, tag)

        new_run(next_shape[1])
        upcoming = iter(bounds)
        nb = next(upcoming, None)
        i = 0
        while i < len(chars):
            while nb is not None and i >= nb:
                next_shape = next(shape_iter, next_shape)
                new_run(next_shape[1])
                nb = next(upcoming, None)
            c = ord(chars[i])
            if c >= 32:
                add_text(chars[i])
                i += 1
                continue
            if c in _CHAR_CTRL:
                if c == 10:
                    add_mark("lineBreak")
                elif c == 24:
                    add_mark("hyphen")
                elif c == 30:
                    add_mark("nbSpace")
                elif c == 31:
                    add_mark("fwSpace")
                i += 1
                continue
            # 인라인·확장 컨트롤: 8글자
            if c == 9:
                add_mark("tab")
            elif c == 4:  # 필드 끝
                _sub(_sub(run, "ctrl"), "fieldEnd")
                t = None
            elif c in _EXT_CTRL:
                ctrl = next(ctrls, None)
                if ctrl is not None:
                    self._control(run, ctrl)
                    t = None
            i += 8
        return el

    # ------------------------------------------------------------------

    def _control(self, run, ctrl: Record) -> None:
        cid = ctrl_id(ctrl.data)
        if cid == "secd":
            self._section_def(run, ctrl)
        elif cid == "tbl ":
            self._table(run, ctrl)
        elif cid == "gso ":
            self._shape(run, ctrl)
        elif cid == "eqed":
            eq = ctrl.kid(Tag.EQEDIT)
            if eq is not None:
                rd = Reader(eq.data)
                rd.u32()
                eq_el = _sub(run, "equation")
                _sub(eq_el, "script").text = rd.text()
                self._caption(eq_el, ctrl)
        elif cid == "tcps":
            # 글자 겹침: 겹칠 글자는 문단 글이 아니라 컨트롤에 있다 (WORD 길이 + WCHAR[]).
            # 실제 제안요청서의 '□'+'√'(체크한 네모) 6개가 이것이었다: 읽지 않으면 어느 칸에 체크했는지가 사라진다
            rd = Reader(ctrl.data, 4)
            text = rd.text()
            circle, size, kind, count = rd.u8(), rd.i8(), rd.u8(), rd.u8()
            _sub(run, "compose", composeText=text, circleType=_COMPOSE_CIRCLE.get(circle, "CHAR"), charSz=size,
                 composeType="SPREAD" if kind else "OVERLAP", charPrCnt=count)
        elif cid == "tdut":
            # 덧말: 본말과 덧말 모두 컨트롤에 있다 (각각 WORD 길이 + WCHAR[]), 그 뒤 위치·크기 비율·정렬
            rd = Reader(ctrl.data, 4)
            main, sub = rd.text(), rd.text()
            pos, ratio, option, style, align = (rd.u32() for _ in range(5))
            el = _sub(run, "dutmal", posType=("TOP", "BOTTOM", "CENTER")[pos] if pos < 3 else "TOP", szRatio=ratio,
                      option=option, styleIDRef=style, align=_ALIGN.get(align, "JUSTIFY"))
            _sub(el, "mainText").text = main
            _sub(el, "subText").text = sub
        elif cid == "tcmt":
            # 숨은 설명: 인쇄되지 않는 설명 글 → HWPX의 hiddenComment (메모처럼 읽는다. v1.0에서는 경고만)
            self._sublist(_sub(_sub(run, "ctrl"), "hiddenComment"), ctrl)
        elif cid == "atno":
            # 자동 번호 (캡션의 '그림 1'의 1 등): 속성(번호 종류 0~3비트, 번호 모양 4~11비트), 번호, 사용자 기호,
            # 앞·뒤 장식 문자 (각 WCHAR) → HWPX의 autoNum
            rd = Reader(ctrl.data, 4)
            attr, num = rd.u32(), rd.u16()
            user, prefix, suffix = (chr(rd.u16()).strip("\0") for _ in range(3))
            kinds = ("PAGE", "FOOTNOTE", "ENDNOTE", "PICTURE", "TABLE", "EQUATION")
            fmt = (attr >> 4) & 0xFF
            el = _sub(_sub(run, "ctrl"), "autoNum", num=num, numType=kinds[attr & 0xF] if attr & 0xF < 6 else "PAGE")
            _sub(el, "autoNumFormat", type=_NUM_FORMAT[fmt] if fmt < len(_NUM_FORMAT) else "DIGIT", userChar=user,
                 prefixChar=prefix, suffixChar=suffix)
        elif cid in ("head", "foot"):
            rd = Reader(ctrl.data, 4)
            kind = "header" if cid == "head" else "footer"
            el = _sub(_sub(run, "ctrl"), kind, applyPageType=_PAGE_TYPE.get(rd.u32() & 0x3, "BOTH"))
            self._sublist(el, ctrl)
        elif cid in ("fn  ", "en  "):
            el = _sub(_sub(run, "ctrl"), "footNote" if cid == "fn  " else "endNote")
            self._sublist(el, ctrl)
        elif cid.startswith("%"):
            rd = Reader(ctrl.data, 4)
            rd.u32()
            rd.u8()
            command = rd.text()
            begin = _sub(_sub(run, "ctrl"), "fieldBegin", type="HYPERLINK" if cid == "%hlk" else cid.strip("%"))
            if cid == "%hlk":
                _sub(_sub(begin, "parameters"), "stringParam", name="Command").text = command

    def _sublist(self, parent, ctrl: Record, vert: str | None = None):
        """컨트롤 아래의 글 목록(LIST_HEADER 뒤 문단들) → subList."""
        sub = _sub(parent, "subList", vertAlign=vert)
        self._paragraphs(sub, ctrl.kids(Tag.PARA_HEADER))
        return sub

    def _section_def(self, run, ctrl: Record) -> None:
        rd = Reader(ctrl.data, 4)
        rd.u32()
        rd.skip(6)  # 단 간격, 세로·가로 줄맞춤
        rd.u32()  # 기본 탭 간격
        outline = rd.u16()
        sec = _sub(run, "secPr", outlineShapeIDRef=outline or None)
        page = ctrl.kid(Tag.PAGE_DEF)
        if page is not None:
            pr = Reader(page.data)
            w, h, left, right, top, bottom, header, footer, _gutter = (pr.u32() for _ in range(9))
            attr = pr.u32()
            pp = _sub(sec, "pagePr", width=w, height=h, landscape="NARROWLY" if attr & 0x1 else "WIDELY")
            _sub(pp, "margin", left=left, right=right, top=top, bottom=bottom, header=header, footer=footer)
        # 구역 정의 아래 LIST_HEADER + 문단 = 바탕쪽 (양쪽·홀수·짝수 쪽마다 LIST_HEADER 하나). HWPX의 masterpage처럼
        # secPr 안 masterPage/subList로 옮긴다 (v1.0에서는 경고만 남겼다)
        groups: list[list[Record]] = []
        for r in ctrl.children:
            if r.tag == Tag.LIST_HEADER:
                groups.append([])
            elif r.tag == Tag.PARA_HEADER and groups:
                groups[-1].append(r)
        for g in groups:
            if _has_text(g):
                self._paragraphs(_sub(_sub(sec, "masterPage"), "subList"), g)
        for i, pbf in enumerate(ctrl.kids(Tag.PAGE_BORDER_FILL)[:3]):
            br = Reader(pbf.data)
            br.u32()
            br.skip(8)
            _sub(sec, "pageBorderFill", type=("BOTH", "EVEN", "ODD")[i], borderFillIDRef=br.u16())

    def _common(self, ctrl: Record) -> dict:
        """표·그림·도형 공통 속성 (글자처럼 취급, 위치 기준·정렬·간격, 크기, 본문 배치, 설명)."""
        rd = Reader(ctrl.data, 4)
        attr = rd.u32()
        v_off, h_off, w, h = rd.i32(), rd.i32(), rd.u32(), rd.u32()
        rd.i32()  # z 순서
        rd.skip(8)  # 바깥 여백
        obj_id = rd.u32()
        rd.i32()  # 쪽 나눔 방지
        desc = rd.text()
        return {
            "treat": attr & 0x1, "vert_rel": _VERT_REL.get((attr >> 3) & 0x3, "PARA"),
            "vert_align": _VERT_ALIGN.get((attr >> 5) & 0x7, "TOP"), "horz_rel": _HORZ_REL.get((attr >> 8) & 0x3, "COLUMN"),
            "horz_align": _HORZ_ALIGN.get((attr >> 10) & 0x7, "LEFT"), "wrap": _WRAP.get((attr >> 21) & 0x7, "SQUARE"),
            "v_off": v_off, "h_off": h_off, "w": w, "h": h, "desc": desc, "id": obj_id,
        }

    def _place(self, el, common: dict) -> None:
        el.set("textWrap", common["wrap"])
        _sub(el, "sz", width=common["w"], height=common["h"])
        _sub(el, "pos", treatAsChar=common["treat"], vertRelTo=common["vert_rel"], horzRelTo=common["horz_rel"],
             vertAlign=common["vert_align"], horzAlign=common["horz_align"],
             vertOffset=common["v_off"] & 0xFFFFFFFF, horzOffset=common["h_off"] & 0xFFFFFFFF)
        if common["desc"]:
            _sub(el, "shapeComment").text = common["desc"]

    def _table(self, run, ctrl: Record) -> None:
        common = self._common(ctrl)
        tbl = _sub(run, "tbl")
        info = ctrl.kid(Tag.TABLE)
        if info is not None:
            rd = Reader(info.data)
            rd.u32()
            rows, cols = rd.u16(), rd.u16()
            rd.i16()  # 칸 간격
            left, right, top, bottom = (rd.u16() for _ in range(4))
            tbl.set("rowCnt", str(rows))
            tbl.set("colCnt", str(cols))
        self._place(tbl, common)
        if info is not None:
            _sub(tbl, "inMargin", left=left, right=right, top=top, bottom=bottom)
        self._caption(tbl, ctrl)
        # 칸: LIST_HEADER 뒤에 그 칸의 문단이 '형제'로 이어진다. TABLE 레코드 앞의 LIST_HEADER는 캡션이다
        cells: list[tuple[Record, list[Record]]] = []
        seen_table = info is None
        for r in ctrl.children:
            if r.tag == Tag.TABLE:
                seen_table = True
            elif r.tag == Tag.LIST_HEADER and seen_table:
                cells.append((r, []))
            elif r.tag == Tag.PARA_HEADER and cells:
                cells[-1][1].append(r)
        rows_el: dict[int, object] = {}
        for head, paras in cells:
            rd = Reader(head.data)
            rd.i16()
            rd.u16()
            attr = rd.u32()
            col, row, cspan, rspan = rd.u16(), rd.u16(), rd.u16(), rd.u16()
            cw, ch = rd.u32(), rd.u32()
            rd.skip(8)  # 칸 안 여백
            bf = rd.u16()
            if row not in rows_el:
                rows_el[row] = _sub(tbl, "tr")
            tc = _sub(rows_el[row], "tc", borderFillIDRef=bf)
            sub = _sub(tc, "subList", vertAlign=_V_ALIGN.get((attr >> 5) & 0x3, "CENTER"))
            self._paragraphs(sub, paras)
            _sub(tc, "cellAddr", colAddr=col, rowAddr=row)
            _sub(tc, "cellSpan", colSpan=cspan or 1, rowSpan=rspan or 1)
            _sub(tc, "cellSz", width=cw, height=ch)

    def _shape(self, run, ctrl: Record) -> None:
        """그리기 개체(gso): 개체 요소(SHAPE_COMPONENT)를 HWPX의 hp:rect, hp:pic … 으로 옮긴다."""
        common = self._common(ctrl)
        comp = ctrl.kid(Tag.SHAPE_COMPONENT)
        if comp is None:
            return
        el = self._component(run, comp, top=True)
        if el is None:
            return
        el.set("id", str(common["id"]))
        self._place(el, common)
        self._caption(el, ctrl)

    def _caption(self, el, ctrl: Record) -> None:
        """개체 캡션 → hp:caption/hp:subList (HWPX 파서가 개체 뒤 문단으로 읽는다).

        컨트롤 바로 아래, 개체 레코드(SHAPE_COMPONENT·EQEDIT·TABLE)보다 앞의 LIST_HEADER와 그 뒤 형제 문단이 캡션이다.
        LIST_HEADER: 문단 수 2, 2, 목록 속성 4, 캡션 속성 4(0~1비트 방향: 왼쪽·오른쪽·위·아래) … (hwplib 글상자·수식 시험 파일)
        """
        kids = ctrl.children
        for i, r in enumerate(kids):
            if r.tag in (Tag.TABLE, Tag.SHAPE_COMPONENT, Tag.EQEDIT):
                return
            if r.tag != Tag.LIST_HEADER:
                continue
            rd = Reader(r.data)
            rd.i16()
            rd.u16()
            rd.u32()
            attr = rd.u32()
            paras = []
            for q in kids[i + 1:]:
                if q.tag != Tag.PARA_HEADER:
                    break
                paras.append(q)
            cap = _sub(el, "caption", side=("LEFT", "RIGHT", "TOP", "BOTTOM")[attr & 0x3])
            self._paragraphs(_sub(cap, "subList"), paras)
            return

    def _component(self, parent, sc: Record, top: bool):
        """개체 요소 하나 → HWPX 개체 요소. 레코드 배치 (실제 파일과 hwplib으로 확인):

            SHAPE_COMPONENT  컨트롤 id (맨 위 개체는 두 번), 묶음 안 위치, 원래·현재 크기, 회전,
                             renderingInfo(이동 행렬 + 확대·회전 행렬 쌍), [선, 채우기, 그림자, 개체 id]
              LIST_HEADER + PARA_HEADER …   글상자 (사각형·타원·다각형·곡선·호)
              SHAPE_COMPONENT_RECTANGLE 등  모양 (꼭짓점)
              SHAPE_COMPONENT …             묶음 안 개체
        """
        cid = ctrl_id(sc.data)
        kind = _GSO.get(cid)
        if kind is None:
            self.warn(f"{_GSO_NAME.get(cid, '알 수 없는 그리기 개체')}는 아직 그리지 않음 (HWP)")
            return None
        el = _sub(parent, kind)
        rd = Reader(sc.data, 8 if top else 4)
        rd.skip(8 + 4)  # 묶음 안 위치, 묶음 단계, 버전
        org_w, org_h, cur_w, cur_h = (rd.u32() for _ in range(4))
        rd.skip(4 + 2 + 8)  # 뒤집기 속성, 회전각, 회전 중심 (행렬에 이미 들어 있다)
        _sub(el, "orgSz", width=org_w, height=org_h)
        _sub(el, "curSz", width=cur_w, height=cur_h)
        info = _sub(el, "renderingInfo")
        pairs = rd.u16()
        _matrix(info, "transMatrix", rd)
        for _ in range(pairs):
            _matrix(info, "scaMatrix", rd)
            _matrix(info, "rotMatrix", rd)
        if kind == "container":
            rd.skip(4 * rd.u16())  # 묶음 안 개체의 컨트롤 id
            if rd.left() >= 4:
                el.set("instid", str(rd.u32()))
            for c in sc.children:
                for child in ([c] if c.tag == Tag.SHAPE_COMPONENT else c.kids(Tag.SHAPE_COMPONENT)):
                    self._component(el, child, top=False)
            return el
        if rd.left():
            _line_info(el, rd)
        if rd.left():
            _fill_info(el, rd)
        rd.skip(16)  # 그림자: 종류, 색, 가로·세로 거리
        if rd.left() >= 4:
            el.set("instid", str(rd.u32()))
        self._draw_text(el, sc)
        getattr(self, f"_{kind}_geometry")(el, sc)
        return el

    def _draw_text(self, el, sc: Record) -> None:
        """글상자: LIST_HEADER(문단 수, 속성, 안쪽 여백, 최대 너비) 뒤에 그 글상자의 문단이 형제로 이어진다."""
        head = sc.kid(Tag.LIST_HEADER)
        if head is None:
            return
        rd = Reader(head.data)
        rd.i16()
        rd.u16()
        attr = rd.u32()
        left, right, top, bottom = (rd.u16() for _ in range(4))
        dt = _sub(el, "drawText", lastWidth=rd.u32())
        _sub(dt, "textMargin", left=left, right=right, top=top, bottom=bottom)
        sub = _sub(dt, "subList", vertAlign=_V_ALIGN.get((attr >> 5) & 0x3, "TOP"))
        start = sc.children.index(head) + 1
        paras = []
        for r in sc.children[start:]:
            if r.tag != Tag.PARA_HEADER:
                break
            paras.append(r)
        self._paragraphs(sub, paras)

    def _rect_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_RECTANGLE)
        if r is None:
            return
        rd = Reader(r.data)
        el.set("ratio", str(rd.u8()))  # 모서리 곡률(%)
        for i in range(4):
            _point(el, f"pt{i}", rd)

    def _ellipse_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_ELLIPSE)
        if r is None:
            return
        rd = Reader(r.data)
        rd.u32()  # 속성 (호로 바뀐 타원 등)
        for name in ("center", "ax1", "ax2"):
            _point(el, name, rd)

    def _arc_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_ARC)
        if r is None:
            return
        rd = Reader(r.data)
        el.set("type", _ARC_TYPE.get(rd.u8(), "NORMAL"))
        for name in ("center", "ax1", "ax2"):
            _point(el, name, rd)

    def _line_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_LINE)
        if r is None:
            return
        rd = Reader(r.data)
        _point(el, "startPt", rd)
        _point(el, "endPt", rd)

    def _connectLine_geometry(self, el, sc: Record) -> None:  # noqa: N802 - HWPX 요소 이름
        """연결선: 선 레코드에 종류와 양 끝이 붙은 개체(id, 연결점 번호)가 더 붙는다."""
        r = sc.kid(Tag.SHAPE_COMPONENT_LINE)
        if r is None:
            return
        rd = Reader(r.data)
        sx, sy, ex, ey = (rd.i32() for _ in range(4))
        kind = rd.u32()
        el.set("type", _LINK_TYPE[kind] if kind < len(_LINK_TYPE) else "STRAIGHT_NOARROW")
        for name, x, y in (("startPt", sx, sy), ("endPt", ex, ey)):
            subject, idx = rd.u32(), rd.u32()
            _sub(el, name, x=x, y=y, subjectIDRef=subject, subjectIdx=idx)

    def _polygon_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_POLYGON)
        if r is None:
            return
        rd = Reader(r.data)
        for _ in range(max(min(rd.i32(), rd.left() // 8), 0)):
            _point(el, "pt", rd)

    def _curve_geometry(self, el, sc: Record) -> None:
        """곡선: 점 n개 뒤에 구간 n-1개의 종류(0 직선, 1 곡선)."""
        r = sc.kid(Tag.SHAPE_COMPONENT_CURVE)
        if r is None:
            return
        rd = Reader(r.data)
        n = max(min(rd.i32(), rd.left() // 8), 0)
        pts = [(rd.i32(), rd.i32()) for _ in range(n)]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            _sub(el, "seg", type="CURVE" if rd.u8() == 1 else "LINE", x1=x1, y1=y1, x2=x2, y2=y2)

    def _pic_geometry(self, el, sc: Record) -> None:
        r = sc.kid(Tag.SHAPE_COMPONENT_PICTURE)
        if r is None:
            return
        rd = Reader(r.data)
        rd.skip(4 + 4 + 4)  # 테두리 색, 굵기, 속성
        rect = _sub(el, "imgRect")
        for i in range(4):
            _point(rect, f"pt{i}", rd)
        rd.skip(16 + 8 + 3)  # 자르기, 안쪽 여백, 밝기·명암·효과
        _sub(el, "img", binaryItemIDRef=f"bin{rd.u16()}")

    def _ole_geometry(self, el, sc: Record) -> None:
        # SHAPE_COMPONENT_OLE: 속성 4, 크기 x·y 8, BinData 번호 2 … (차트면 HWPX 파서가 데이터 표를 읽고, 아니면 '표시하지 않음')
        r = sc.kid(Tag.SHAPE_COMPONENT_OLE)
        if r is not None and len(r.data) >= 14:
            rd = Reader(r.data)
            rd.skip(12)
            el.set("binaryItemIDRef", f"bin{rd.u16()}")


# 그리기 개체의 컨트롤 id → HWPX 요소 이름
_GSO = {"$rec": "rect", "$ell": "ellipse", "$arc": "arc", "$lin": "line", "$pol": "polygon", "$cur": "curve",
        "$col": "connectLine", "$con": "container", "$pic": "pic", "$ole": "ole"}
_GSO_NAME = {"$tat": "글맵시"}
_ARC_TYPE = {0: "NORMAL", 1: "PIE", 2: "CHORD"}
_LINK_TYPE = ["STRAIGHT_NOARROW", "STRAIGHT_ONEWAY", "STRAIGHT_BOTH", "STROKE_NOARROW", "STROKE_ONEWAY", "STROKE_BOTH",
              "ARC_NOARROW", "ARC_ONEWAY", "ARC_BOTH"]
_ARROW = ["NORMAL", "ARROW", "SPEAR", "CONCAVE_ARROW", "DIAMOND", "CIRCLE", "BOX"]


def _link_connectors(sec) -> None:
    """연결선 끝의 개체 id가 컨트롤 머리의 개체 id를 가리키면, ShapeReader가 도형 id로 쓰는 instid로 바꾼다.
    (실제 HWP 연결선 표본이 없어, 둘 중 어느 쪽을 가리키든 이어지게 한다.)"""
    inst = {el.get("id"): el.get("instid") for el in sec.iter() if el.get("id") and el.get("instid")}
    for line in sec.iter("connectLine"):
        for end in (kid for kid in line if kid.tag in ("startPt", "endPt")):
            ref = end.get("subjectIDRef")
            if ref in inst:
                end.set("subjectIDRef", inst[ref])


def _point(parent, name: str, rd: Reader):
    return _sub(parent, name, x=rd.i32(), y=rd.i32())


def _matrix(parent, name: str, rd: Reader) -> None:
    """행렬: double 6개 (e1 e2 e3 / e4 e5 e6, x' = e1·x + e2·y + e3)."""
    values = [struct.unpack("<d", rd.data[rd.pos + 8 * k:rd.pos + 8 * k + 8])[0] if rd.left() >= 8 * (k + 1) else 0.0
              for k in range(6)]
    rd.skip(48)
    _sub(parent, name, **{f"e{k + 1}": repr(v) for k, v in enumerate(values)})


def _line_info(el, rd: Reader) -> None:
    """선: 색, 굵기(HWPUNIT), 속성(종류·끝 모양·화살표), 바깥선 방식."""
    col, width, attr = rd.color(), rd.i32(), rd.u32()
    rd.u8()
    kind = attr & 0x3F

    def arrow(shape: int, filled: bool) -> str:
        name = _ARROW[shape] if shape < len(_ARROW) else "NORMAL"
        if name in ("DIAMOND", "CIRCLE", "BOX"):
            name = ("FILLED_" if filled else "EMPTY_") + name
        return name

    _sub(el, "lineShape", color=col or "#000000", width=width,
         style=_STROKE[kind] if kind < len(_STROKE) else "SOLID",
         headStyle=arrow((attr >> 10) & 0x3F, bool(attr >> 30 & 1)),
         tailStyle=arrow((attr >> 16) & 0x3F, bool(attr >> 31 & 1)))


def _fill_info(el, rd: Reader) -> None:
    """채우기: 종류(1 단색, 2 그림, 4 그러데이션) 뒤에 종류마다의 값, 추가 속성.
    그러데이션의 값 크기는 공개 문서(2바이트)와 달리 1·4바이트다 (hwplib의 확인)."""
    kind = rd.u32()
    if kind == 0:
        rd.skip(4)
        return
    brush = _sub(el, "fillBrush")
    if kind & 0x1:
        face, hatch = rd.color(), rd.color()
        rd.i32()  # 무늬 종류
        _sub(brush, "winBrush", faceColor=face or "none", hatchColor=hatch or "#000000")
    if kind & 0x4:
        rd.i8()  # 종류 (줄무늬, 원형 …)
        rd.skip(16)  # 각도, 중심 x·y, 번짐 정도
        n = rd.u32()
        if n > 2:
            rd.skip(4 * n)  # 색이 바뀌는 곳
        grad = _sub(brush, "gradation")
        for _ in range(min(n, rd.left() // 4)):
            _sub(grad, "color", value=rd.color() or "#FFFFFF")
    if kind & 0x2:
        mode = rd.u8()
        rd.skip(3)  # 명암, 밝기, 효과
        img_brush = _sub(brush, "imgBrush", mode=mode)
        _sub(img_brush, "img", binaryItemIDRef=f"bin{rd.u16()}")
    rd.skip(rd.u32())  # 추가 속성
    rd.skip(bin(kind & 0x7).count("1"))  # 종류마다 1바이트 (내용 모름)


# ---------------------------------------------------------------------------
# 파서
# ---------------------------------------------------------------------------


class HwpParser(HwpxParser):
    """HWPX 파서에 바이너리에서 옮긴 XML을 넣는다."""

    def __init__(self, path: str | Path, file=None):  # noqa: D107 - 부모의 zip 열기를 쓰지 않는다
        """file: HwpFile과 같은 메서드를 가진 대신할 개체 (시험에서 레코드를 직접 넣을 때). 없으면 path를 연다."""
        self.path = Path(path)
        self.file = file or HwpFile(self.path)
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="hwp"))
        self._seq = 0
        self._hf_seen: dict[str, set[str]] = {}  # 머리말·꼬리말 → 이미 담은 글
        self._manifest = {}
        self._image_by_ref = {}
        self._table_depth = 0
        self._outline_ref = "1"
        self._page_set = False
        info = _DocInfo(self.file.flat_records("DocInfo"), self.file.compressed)
        self._bins = info.bins
        self.header = Header(info.root)
        self.counters = Counters(self.header)
        self.shapes = ShapeReader(self._sublist_blocks, self._image_id)
        self.names: set[str] = set()

    def parse(self) -> Document:
        self._read_summary()
        style0 = self.header.styles.get("0")
        if style0 is not None and style0.char_ref in self.header.char_styles:
            self.doc.default_text_style = self.header.char_styles[style0.char_ref]
        body = _Body(self._warn)
        for name in self.file.sections():
            sec = body.section(self.file.records(name))
            self.doc.blocks.extend(self._paragraphs(sec))
        self.file.close()
        return self.doc

    def _prov(self) -> Provenance:
        return Provenance(parser=PARSER_NAME)

    def _xml(self, name: str):
        return None  # HWP에는 HWPX의 차트 XML 같은 부속 파일이 없다

    def _read_summary(self) -> None:
        props = self.file.summary()
        m = self.doc.meta
        m.title = (props.get(2) or "").strip() or None
        m.author = (props.get(4) or "").strip() or None
        for key, attr in ((12, "created"), (13, "modified")):
            v = props.get(key)
            if v is not None and hasattr(v, "isoformat"):
                setattr(m, attr, v.isoformat())

    def _ole_chart(self, el, w: float, h: float):
        """OLE 개체가 한글 차트(VtChart)면 데이터 표를 Chart로. 차트 종류는 정하지 않는다 (hwp/chart.py)."""
        from .chart import read_chart_grid

        try:
            bid = int((el.get("binaryItemIDRef") or "").removeprefix("bin"))
        except ValueError:
            return None
        name, compressed = self._bins.get(bid, (None, False))
        data = self.file.bin_data(name, compressed) if name else None
        grid = read_chart_grid(data) if data else None
        if grid is None:
            return None
        cols, rows, values = grid
        return Chart(id="", prov=self._prov(), chart_type="unknown", categories=rows,
                     series=[ChartSeries(name=c, values=[row[j] for row in values]) for j, c in enumerate(cols)],
                     width_pt=w or None, height_pt=h or None)

    def _image_id(self, ref: str | None) -> str | None:
        if not ref:
            return None
        if ref in self._image_by_ref:
            return self._image_by_ref[ref]
        try:
            bid = int(ref.removeprefix("bin"))
        except ValueError:
            bid = -1
        name, compressed = self._bins.get(bid, (None, False))
        data = self.file.bin_data(name, compressed) if name else None
        if data is None:
            self._warn(f"그림 파일 없음: {ref}")
            return None
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        lower = name.lower()
        if lower.endswith(".emf"):
            mime = "image/x-emf"
        elif lower.endswith(".wmf"):
            mime = "image/x-wmf"
        elif lower.endswith(".bmp"):
            mime = "image/bmp"
        if mime in ("image/x-emf", "image/x-wmf", "image/tiff"):
            self._warn(f"브라우저에서 표시할 수 없는 이미지 형식: {name} ({mime})")
        image_id = f"img{len(self.doc.images) + 1}"
        self.doc.images[image_id] = Image(mime=mime, data_base64=base64.b64encode(data).decode("ascii"), name=name)
        self._image_by_ref[ref] = image_id
        return image_id
