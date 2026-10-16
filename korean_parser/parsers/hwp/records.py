"""HWP 5.0 바이너리의 저장 구조: OLE 복합 파일 → 스트림 → 레코드.

    FileHeader        서명, 버전, 속성(압축·암호·배포용)
    DocInfo           글꼴, 글자·문단 모양, 테두리, 번호, 스타일 (HWPX의 header.xml)
    BodyText/SectionN 본문 (HWPX의 section0.xml)
    BinData/BINxxxx.* 그림 등

스트림은 압축 속성이면 zlib(raw deflate)으로 풀고, 안은 레코드의 나열이다. 레코드 머리 4바이트:
    태그 10비트 | 단계(level) 10비트 | 크기 12비트 (0xFFF면 다음 4바이트가 크기)
단계가 앞 레코드보다 깊으면 그 레코드의 자식이다 (문단 → 글자 → 표 → 칸 → 칸 안 문단 …).

참고: 한글과컴퓨터 「한글 문서 파일 형식 5.0」 공개 문서.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

TAG_BEGIN = 0x10


class Tag:
    # DocInfo
    DOCUMENT_PROPERTIES = TAG_BEGIN
    ID_MAPPINGS = TAG_BEGIN + 1
    BIN_DATA = TAG_BEGIN + 2
    FACE_NAME = TAG_BEGIN + 3
    BORDER_FILL = TAG_BEGIN + 4
    CHAR_SHAPE = TAG_BEGIN + 5
    TAB_DEF = TAG_BEGIN + 6
    NUMBERING = TAG_BEGIN + 7
    BULLET = TAG_BEGIN + 8
    PARA_SHAPE = TAG_BEGIN + 9
    STYLE = TAG_BEGIN + 10
    # BodyText
    PARA_HEADER = TAG_BEGIN + 50
    PARA_TEXT = TAG_BEGIN + 51
    PARA_CHAR_SHAPE = TAG_BEGIN + 52
    PARA_LINE_SEG = TAG_BEGIN + 53
    PARA_RANGE_TAG = TAG_BEGIN + 54
    CTRL_HEADER = TAG_BEGIN + 55
    LIST_HEADER = TAG_BEGIN + 56
    PAGE_DEF = TAG_BEGIN + 57
    FOOTNOTE_SHAPE = TAG_BEGIN + 58
    PAGE_BORDER_FILL = TAG_BEGIN + 59
    SHAPE_COMPONENT = TAG_BEGIN + 60
    TABLE = TAG_BEGIN + 61
    SHAPE_COMPONENT_LINE = TAG_BEGIN + 62
    SHAPE_COMPONENT_RECTANGLE = TAG_BEGIN + 63
    SHAPE_COMPONENT_ELLIPSE = TAG_BEGIN + 64
    SHAPE_COMPONENT_ARC = TAG_BEGIN + 65
    SHAPE_COMPONENT_POLYGON = TAG_BEGIN + 66
    SHAPE_COMPONENT_CURVE = TAG_BEGIN + 67
    SHAPE_COMPONENT_OLE = TAG_BEGIN + 68
    SHAPE_COMPONENT_PICTURE = TAG_BEGIN + 69
    SHAPE_COMPONENT_CONTAINER = TAG_BEGIN + 70
    CTRL_DATA = TAG_BEGIN + 71
    EQEDIT = TAG_BEGIN + 72


@dataclass
class Record:
    tag: int
    level: int
    data: bytes
    children: list["Record"] = field(default_factory=list)

    def kids(self, tag: int) -> list["Record"]:
        return [c for c in self.children if c.tag == tag]

    def kid(self, tag: int) -> "Record | None":
        return next((c for c in self.children if c.tag == tag), None)


def read_records(data: bytes) -> list[Record]:
    """레코드를 차례로 읽는다 (평평한 목록)."""
    out: list[Record] = []
    pos, n = 0, len(data)
    while pos + 4 <= n:
        (h,) = struct.unpack_from("<I", data, pos)
        pos += 4
        tag, level, size = h & 0x3FF, (h >> 10) & 0x3FF, (h >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
        out.append(Record(tag, level, data[pos:pos + size]))
        pos += size
    return out


def build_tree(records: list[Record]) -> list[Record]:
    """단계(level)로 부모·자식을 잇는다. 맨 위 레코드 목록을 돌려준다."""
    roots: list[Record] = []
    stack: list[Record] = []
    for r in records:
        while stack and stack[-1].level >= r.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(r)
        stack.append(r)
    return roots


def inflate(data: bytes) -> bytes:
    """HWP 스트림 압축 풀기 (zlib 머리 없는 deflate)."""
    return zlib.decompress(data, -15)


class Reader:
    """레코드 데이터를 앞에서부터 읽는다. 끝을 넘으면 기본값(0, 빈 글)을 돌려준다 (구버전은 뒤 필드가 없다)."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data, self.pos = data, pos

    def left(self) -> int:
        return len(self.data) - self.pos

    def _get(self, fmt: str, default=0):
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            self.pos = len(self.data)
            return default
        (v,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return v

    def u8(self) -> int:
        return self._get("<B")

    def i8(self) -> int:
        return self._get("<b")

    def u16(self) -> int:
        return self._get("<H")

    def i16(self) -> int:
        return self._get("<h")

    def u32(self) -> int:
        return self._get("<I")

    def i32(self) -> int:
        return self._get("<i")

    def skip(self, n: int) -> None:
        self.pos = min(self.pos + n, len(self.data))

    def text(self) -> str:
        """WORD 길이 + WCHAR[길이] (UTF-16LE)."""
        n = self.u16()
        raw = self.data[self.pos:self.pos + 2 * n]
        self.pos += 2 * n
        return raw.decode("utf-16-le", "replace")

    def color(self) -> str | None:
        """COLORREF (0x00BBGGRR) → #RRGGBB. 최상위 바이트가 있으면 '없음'으로 본다."""
        v = self.u32()
        if v >> 24:
            return None
        return f"#{v & 0xFF:02X}{(v >> 8) & 0xFF:02X}{(v >> 16) & 0xFF:02X}"


def ctrl_id(data: bytes) -> str:
    """컨트롤 ID (4글자, 거꾸로 저장된 UINT32): b' lbt' → 'tbl '."""
    if len(data) < 4:
        return ""
    return data[:4][::-1].decode("latin-1")
