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
- 그림·글상자 밖의 도형(사각형, 선 등)은 아직 그리지 않고, 도형 안 글만 문단으로 꺼낸다.

참고: 한글과컴퓨터 「한글 문서 파일 형식 5.0」 공개 문서.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import struct
from pathlib import Path

from lxml import etree

from ...ir import Document, Image, Meta, Provenance
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
        ext = rd.text() if kind == 1 else ""
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
        return sec

    def _paragraphs(self, parent, paras: list[Record]) -> None:
        for p in paras:
            parent.append(self._paragraph(p))

    def _paragraph(self, p: Record):
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
                _sub(_sub(run, "equation"), "script").text = rd.text()
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
        rd.u32()  # 개체 id
        rd.i32()  # 쪽 나눔 방지
        desc = rd.text()
        return {
            "treat": attr & 0x1, "vert_rel": _VERT_REL.get((attr >> 3) & 0x3, "PARA"),
            "vert_align": _VERT_ALIGN.get((attr >> 5) & 0x7, "TOP"), "horz_rel": _HORZ_REL.get((attr >> 8) & 0x3, "COLUMN"),
            "horz_align": _HORZ_ALIGN.get((attr >> 10) & 0x7, "LEFT"), "wrap": _WRAP.get((attr >> 21) & 0x7, "SQUARE"),
            "v_off": v_off, "h_off": h_off, "w": w, "h": h, "desc": desc,
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
        # 칸: LIST_HEADER 뒤에 그 칸의 문단이 '형제'로 이어진다
        cells: list[tuple[Record, list[Record]]] = []
        for r in ctrl.children:
            if r.tag == Tag.LIST_HEADER:
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
        """그림은 그림으로, 그 밖의 도형은 도형 안 글만 꺼낸다."""
        common = self._common(ctrl)
        comp = ctrl.kid(Tag.SHAPE_COMPONENT)
        pic = _find(comp, Tag.SHAPE_COMPONENT_PICTURE) if comp is not None else None
        if pic is not None:
            el = _sub(run, "pic")
            self._place(el, common)
            rd = Reader(pic.data)
            rd.skip(4 + 4 + 4 + 32 + 16 + 8 + 3)  # 테두리, 꼭짓점, 자르기, 안쪽 여백, 밝기·명암·효과
            _sub(el, "img", binaryItemIDRef=f"bin{rd.u16()}")
            return
        texts = _texts_in(comp) if comp is not None else []
        if texts:
            # 글상자의 글: 도형은 그리지 않고 글만 본문으로 (글자처럼 취급한 개체는 그 자리에)
            self.warn("도형(글상자 등)은 아직 그리지 않고 안의 글만 꺼냄 (HWP)")
            box = _sub(run, "rect")
            self._place(box, common)
            dt = _sub(box, "drawText")
            sub = _sub(dt, "subList")
            self._paragraphs(sub, texts)
        else:
            self.warn("도형은 아직 그리지 않음 (HWP)")


def _find(rec: Record | None, tag: int) -> Record | None:
    if rec is None:
        return None
    if rec.tag == tag:
        return rec
    for c in rec.children:
        found = _find(c, tag)
        if found is not None:
            return found
    return None


def _texts_in(rec: Record) -> list[Record]:
    """도형 안 글상자의 문단들 (LIST_HEADER 뒤 PARA_HEADER)."""
    out: list[Record] = []
    seen_list = False
    for c in rec.children:
        if c.tag == Tag.LIST_HEADER:
            seen_list = True
        elif c.tag == Tag.PARA_HEADER and seen_list:
            out.append(c)
        else:
            out += _texts_in(c)
    return out


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
