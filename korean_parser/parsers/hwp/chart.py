"""HWP 차트 (OLE 개체 안의 VtChart 직렬화)에서 데이터 표를 읽는다.

한글 5.0의 차트는 BinData의 OLE 개체(BINxxxx.OLE)다. 앞 4바이트(크기) 뒤에 OLE 복합 파일이 있고, 그 안의 `Contents`
스트림에 차트 개체들이 이어서 저장되어 있다 (MS Chart의 Vt 개체 구조: VtChart, VtDataGrid, VtSeries …).

여기서는 차트 모양(종류, 색, 축)은 읽지 않고 **데이터 표**(열 이름, 행 이름, 값)만 읽는다. hwplib 시험 파일
`차트.hwp`의 바이트로 확인한 규칙:

- 데이터 표(VtDataGrid)의 칸은 '순번(u32) + 종류 번호(u32) + 값'으로 차례로 놓인다. 순번은 1씩 는다.
  종류가 처음 나올 때는 종류 번호 뒤에 선언(u16 길이 + 'VtString\\0' 같은 이름 + u16 판)이 붙는다.
- 글(VtString): u16 바이트 수 + CP949 글 + NUL + … + UTF-16 글. 수(VtInteger): i32, (VtDouble): f64, (VtSingle): f32
- 열 이름이 먼저 모두 나오고, 그 뒤 행마다 행 이름과 열 수만큼의 값이 나온다

모든 행이 열 수만큼 값을 가질 때만 데이터 표로 인정한다. 아니면 None (부르는 쪽이 'OLE 개체는 표시하지 않음'으로 알린다).
차트 종류는 확인할 표본이 없어 정하지 않는다 (chart_type='unknown': 화면에는 데이터 표로 보인다).
"""

from __future__ import annotations

import io
import struct

_NUMBER = {"VtInteger": ("<i", 4), "VtDouble": ("<d", 8), "VtSingle": ("<f", 4)}


def read_chart_grid(ole: bytes) -> tuple[list[str], list[str], list[list[float]]] | None:
    """BinData OLE 개체 → (열 이름, 행 이름, 행마다 값). 읽을 수 없으면 None."""
    import olefile

    start = ole.find(b"\xd0\xcf\x11\xe0")
    if start < 0:
        return None
    try:
        with olefile.OleFileIO(io.BytesIO(ole[start:])) as f:
            if not f.exists("Contents"):
                return None
            data = f.openstream("Contents").read()
    except Exception:  # noqa: BLE001 - 손상된 OLE: 차트로 읽지 않는다
        return None
    return parse_grid(data)


def parse_grid(data: bytes) -> tuple[list[str], list[str], list[list[float]]] | None:
    a = data.find(b"VtDataGrid\x00")
    if a < 0:
        return None
    b = data.find(b"VtBackdrop\x00", a)
    end = b if b > 0 else len(data)
    names: dict[int, str] = {}  # 종류 번호 → 이름 (선언에서 배운다)
    tokens: list[tuple[str, object]] = []
    expect: int | None = None
    pos = a
    while pos + 8 <= end:
        tok = _token(data, pos, end, names)
        if tok is not None and (expect is None or tok[0] == expect):
            idx, kind, value, pos = tok
            tokens.append((kind, value))
            expect = idx + 1
        else:
            pos += 1
    # 맨 앞에 이어지는 글이 열 이름이다. 단 그 마지막 글 바로 뒤에 값이 오면 그것은 첫 행 이름이다
    lead = 0
    while lead < len(tokens) and tokens[lead][0] == "str":
        lead += 1
    if lead < 2 or lead >= len(tokens):
        return None
    cols = [v for _, v in tokens[:lead - 1]]
    tokens = tokens[lead - 1:]
    rows: list[str] = []
    values: list[list[float]] = []
    for kind, value in tokens:
        if kind == "str":
            rows.append(value)
            values.append([])
        elif values:
            values[-1].append(value)
        else:
            return None  # 행 이름 없이 값이 먼저 나왔다: 모르는 배치
    if not cols or not rows or any(len(v) != len(cols) for v in values):
        return None
    return cols, rows, values


def _token(data: bytes, pos: int, end: int, names: dict[int, str]):
    """pos에서 '순번 + 종류 번호 [+ 선언] + 값'을 읽는다. (순번, 'str'|'num', 값, 다음 위치) 또는 None."""
    idx, cls = struct.unpack_from("<II", data, pos)
    if idx > 100000:
        return None
    p = pos + 8
    if p + 2 <= end:
        n = struct.unpack_from("<H", data, p)[0]
        name = data[p + 2:p + 2 + n]
        if 3 <= n <= 40 and name.startswith(b"Vt") and name.endswith(b"\x00") and name[:-1].isalnum():
            names[cls] = name[:-1].decode("ascii")
            p += 2 + n + 2  # 이름 + 판 번호
    kind = names.get(cls)
    if kind == "VtString":
        if p + 2 > end:
            return None
        n = struct.unpack_from("<H", data, p)[0]
        raw = data[p + 2:p + 2 + n]
        if not (1 <= n <= 2000) or len(raw) < n or b"\x00" not in raw:
            return None
        text = raw.split(b"\x00", 1)[0].decode("cp949", "replace")
        # 같은 글의 UTF-16 사본이 뒤에 있어야 한다 (엉뚱한 바이트를 글로 읽지 않도록)
        if text and (text + "\x00").encode("utf-16-le") not in raw:
            return None
        return idx, "str", text, p + 2 + n
    if kind in _NUMBER:
        fmt, size = _NUMBER[kind]
        if p + size > end:
            return None
        return idx, "num", float(struct.unpack_from(fmt, data, p)[0]), p + size
    return None
