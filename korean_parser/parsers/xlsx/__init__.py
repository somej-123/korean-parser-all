"""XLSX 파서: Excel 통합 문서를 AI 없이 XML 규칙만으로 IR(Document)로 변환한다.

꾸러미 구조
    xl/workbook.xml         시트 목록(이름, 숨김, 관계 id), 날짜 기준(1904)
    xl/sharedStrings.xml    공유 문자열
    xl/styles.xml           표시 형식(numFmt)·글꼴·채우기 (칸의 s 속성 → cellXfs)
    xl/worksheets/sheetN.xml  칸(sheetData), 병합(mergeCells), 열 너비(cols)

변환 규칙
- 시트마다 Heading(1단계, 시트 이름) + 쓰인 범위(값이 있는 칸을 모두 덮는 가장 작은 사각형)의 Table 하나
- 값은 Excel이 화면에 보여 주는 모양을 따른다: 공유·인라인 문자열, 참·거짓, 오류(#DIV/0!), 수식은 저장된 결과 값.
  숫자 표시 형식은 자주 쓰는 것만: 천 단위 쉼표(#,##0), 소수 자릿수(0.00), 백분율(0%), 날짜·시각(yyyy-mm-dd 등 → ISO).
  그 밖의 형식은 일반 숫자(유효 숫자 15자리)로 쓴다
- 병합 칸(mergeCells) → row_span·col_span. 굵은 글씨, 단색 칸 배경, 열 너비
- 숨긴 시트도 넣고 경고를 남긴다. 차트·그림이 있는 시트는 경고만 남긴다 (아직 읽지 않음)
"""

from __future__ import annotations

import datetime
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from ...ir import Cell, Document, Heading, Meta, Paragraph, Provenance, Row, Table, Text, TextStyle
from ..ooxml import R_ID, Package, ln

PARSER_NAME = "xlsx-native"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
# 기본 제공 날짜·시각 표시 형식 번호
_BUILTIN_DATE = set(range(14, 23)) | {45, 46, 47}
_BUILTIN_FMT = {1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
                37: "#,##0", 38: "#,##0", 39: "#,##0.00", 40: "#,##0.00"}
_MAX_CELLS = 200_000  # 시트 하나의 칸 수가 이보다 많으면 앞부분만 넣고 경고 (거대한 데이터 시트)


def parse_xlsx(path: str | Path) -> Document:
    return XlsxParser(path).parse()


def col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def _ref(ref: str) -> tuple[int, int] | None:
    m = _CELL_REF.match(ref or "")
    return (int(m.group(2)) - 1, col_index(m.group(1))) if m else None


def _is_date_format(code: str) -> bool:
    """사용자 정의 표시 형식이 날짜·시각인가 (따옴표·대괄호 안의 글자는 빼고 y m d h s를 찾는다)."""
    bare = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", code or "")
    return bool(re.search(r"[ymdhs]", bare, re.I)) and "0" not in bare.replace("00:", "").split(";")[0][:1]


def _excel_date(value: float, date1904: bool) -> datetime.datetime:
    base = datetime.datetime(1904, 1, 1) if date1904 else datetime.datetime(1899, 12, 30)
    return base + datetime.timedelta(days=value)


def _round(value: float, decimals: int) -> Decimal:
    """Excel처럼 반올림한다 (0.5는 0에서 먼 쪽으로). 파이썬 round·서식은 짝수 쪽으로 보내 1234.5가 1234가 된다."""
    return Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def format_number(value: float, code: str | None, is_date: bool, date1904: bool = False) -> str:
    if is_date:
        d = _excel_date(value, date1904)
        has_time = abs(value - int(value)) > 1e-9
        has_date = int(value) != 0
        if has_date and has_time:
            return d.strftime("%Y-%m-%d %H:%M:%S" if d.second else "%Y-%m-%d %H:%M")
        return d.strftime("%Y-%m-%d") if has_date else d.strftime("%H:%M:%S" if d.second else "%H:%M")
    code = (code or "").split(";")[0]
    if "%" in code:
        decimals = len(code.split(".")[1].split("%")[0]) if "." in code else 0
        return f"{_round(value * 100, decimals):.{decimals}f}%"
    if re.search(r"[0#]", code):
        decimals = len(re.sub(r"[^0#]", "", code.split(".")[1])) if "." in code else 0
        rounded = _round(value, decimals)
        return f"{rounded:,.{decimals}f}" if "," in code else f"{rounded:.{decimals}f}"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.15g}"


class XlsxParser:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.pkg = Package(self.path)
        self.doc = Document(meta=Meta(source_name=self.path.name, source_format="xlsx"))
        self._seq = 0
        self.strings: list[str] = []
        self.xf: list[tuple[int, int, int]] = []  # 칸 서식 → (표시 형식 번호, 글꼴 번호, 채우기 번호)
        self.formats: dict[int, str] = dict(_BUILTIN_FMT)
        self.bold_fonts: set[int] = set()
        self.fills: dict[int, str] = {}
        self.date1904 = False

    def _new_id(self) -> str:
        self._seq += 1
        return f"b{self._seq}"

    def _warn(self, msg: str) -> None:
        if msg not in self.doc.warnings:
            self.doc.warnings.append(msg)

    def parse(self) -> Document:
        wb_part = "xl/workbook.xml"
        wb = self.pkg.xml(wb_part)
        if wb is None:
            raise ValueError("xl/workbook.xml이 없습니다")
        pr = wb.find(f"{S}workbookPr")
        self.date1904 = pr is not None and pr.get("date1904") in ("1", "true")
        self._read_strings()
        self._read_styles()
        rels = self.pkg.rels(wb_part)
        for n, sheet in enumerate(wb.iter(f"{S}sheet"), 1):
            name = sheet.get("name", f"Sheet{n}")
            part = rels.get(sheet.get(R_ID), (None,))[0]
            if sheet.get("state") in ("hidden", "veryHidden"):
                self._warn(f"숨긴 시트도 넣음: {name}")
            root = self.pkg.xml(part) if part else None
            if root is None:
                continue
            self.doc.blocks.append(Heading(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n), level=1,
                                           inlines=[Text(text=name)]))
            table = self._sheet(root, n, name)
            if table is not None:
                self.doc.blocks.append(table)
            if root.find(f"{S}drawing") is not None:
                self._warn(f"시트의 차트·그림은 아직 읽지 않음: {name}")
        core = self.pkg.core()
        m = self.doc.meta
        m.title, m.author = core.get("title"), core.get("author")
        m.created, m.modified, m.language = core.get("created"), core.get("modified"), core.get("language")
        self.pkg.close()
        return self.doc

    # ------------------------------------------------------------------

    def _read_strings(self) -> None:
        root = self.pkg.xml("xl/sharedStrings.xml")
        if root is None:
            return
        for si in root.iter(f"{S}si"):
            # 서식 있는 글(r/t)과 일반 글(t). 윗주(rPh)는 뺀다
            self.strings.append("".join(t.text or "" for t in si.iter(f"{S}t") if ln(t.getparent()) != "rPh"))

    def _read_styles(self) -> None:
        root = self.pkg.xml("xl/styles.xml")
        if root is None:
            return
        for nf in root.iter(f"{S}numFmt"):
            self.formats[int(nf.get("numFmtId", "0"))] = nf.get("formatCode", "")
        fonts = root.find(f"{S}fonts")
        for i, font in enumerate(fonts if fonts is not None else []):
            b = font.find(f"{S}b")
            if b is not None and b.get("val", "1") not in ("0", "false"):
                self.bold_fonts.add(i)
        fills = root.find(f"{S}fills")
        for i, fill in enumerate(fills if fills is not None else []):
            pf = fill.find(f"{S}patternFill")
            fg = pf.find(f"{S}fgColor") if pf is not None else None
            if pf is not None and pf.get("patternType") == "solid" and fg is not None and fg.get("rgb"):
                rgb = fg.get("rgb")[-6:].upper()
                if rgb != "FFFFFF":
                    self.fills[i] = f"#{rgb}"
        xfs = root.find(f"{S}cellXfs")
        for xf in xfs if xfs is not None else []:
            self.xf.append((int(xf.get("numFmtId", "0")), int(xf.get("fontId", "0")), int(xf.get("fillId", "0"))))

    def _value(self, c) -> str:
        t = c.get("t", "n")
        v = c.find(f"{S}v")
        raw = v.text if v is not None and v.text is not None else None
        if t == "s":
            try:
                return self.strings[int(raw)]
            except (TypeError, ValueError, IndexError):
                return ""
        if t == "inlineStr":
            return "".join(x.text or "" for x in c.iter(f"{S}t"))
        if raw is None:
            # 계산 결과가 저장되지 않은 수식(Excel이 아닌 프로그램이 만든 파일): 수식 자체를 보여 준다
            f = c.find(f"{S}f")
            return f"={f.text}" if f is not None and f.text else ""
        if t == "b":
            return "TRUE" if raw == "1" else "FALSE"
        if t in ("str", "e"):
            return raw
        try:
            value = float(raw)
        except ValueError:
            return raw
        fmt_id = self.xf[int(c.get("s", "0"))][0] if c.get("s") and int(c.get("s")) < len(self.xf) else 0
        code = self.formats.get(fmt_id)
        is_date = fmt_id in _BUILTIN_DATE or (fmt_id >= 164 and _is_date_format(code or ""))
        return format_number(value, code, is_date, self.date1904)

    def _sheet(self, root, n: int, name: str) -> Table | None:
        cells: dict[tuple[int, int], tuple[str, int]] = {}
        for c in root.iter(f"{S}c"):
            pos = _ref(c.get("r", ""))
            if pos is None:
                continue
            text = self._value(c)
            style = int(c.get("s", "0") or 0)
            if text or (style < len(self.xf) and self.xf[style][2] in self.fills):
                cells[pos] = (text, style)
            if len(cells) >= _MAX_CELLS:
                self._warn(f"칸이 너무 많아 앞부분 {_MAX_CELLS:,}칸만 넣음: {name}")
                break
        merges: dict[tuple[int, int], tuple[int, int]] = {}
        covered: set[tuple[int, int]] = set()
        for mc in root.iter(f"{S}mergeCell"):
            a, _, b = mc.get("ref", "").partition(":")
            p, q2 = _ref(a), _ref(b)
            if p is None or q2 is None:
                continue
            merges[p] = (q2[0] - p[0] + 1, q2[1] - p[1] + 1)
            for r in range(p[0], q2[0] + 1):
                for cc in range(p[1], q2[1] + 1):
                    if (r, cc) != p:
                        covered.add((r, cc))
        filled = [pos for pos, (text, _) in cells.items() if text]
        if not filled:
            return None
        r0, r1 = min(p[0] for p in filled), max(p[0] for p in filled)
        c0, c1 = min(p[1] for p in filled), max(p[1] for p in filled)
        for (r, c), (rs, cs) in merges.items():  # 병합 칸이 쓰인 범위를 넘으면 범위를 넓힌다
            if (r, c) in cells and cells[(r, c)][0]:
                r1, c1 = max(r1, r + rs - 1), max(c1, c + cs - 1)
        widths = self._col_widths(root, c0, c1)
        table = Table(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n), col_widths_pt=widths,
                      width_pt=sum(widths))
        for r in range(r0, r1 + 1):
            row = Row()
            for c in range(c0, c1 + 1):
                if (r, c) in covered:
                    continue
                text, style = cells.get((r, c), ("", 0))
                rs, cs = merges.get((r, c), (1, 1))
                cell = Cell(row_span=min(rs, r1 - r + 1), col_span=min(cs, c1 - c + 1))
                fmt, font, fill = self.xf[style] if style < len(self.xf) else (0, 0, 0)
                if text:
                    tstyle = TextStyle(bold=True) if font in self.bold_fonts else None
                    cell.blocks = [Paragraph(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n),
                                             inlines=[Text(text=text, style=tstyle)])]
                cell.background = self.fills.get(fill)
                row.cells.append(cell)
            table.rows.append(row)
        return table

    @staticmethod
    def _col_widths(root, c0: int, c1: int) -> list[float]:
        """열 너비(pt). Excel 너비 단위(기본 글꼴 '0' 글자 수)를 약 5.25pt로 본다. 적지 않은 열은 8.43자."""
        widths = {}
        for col in root.iter(f"{S}col"):
            try:
                lo, hi, w = int(col.get("min")) - 1, int(col.get("max")) - 1, float(col.get("width", "8.43"))
            except (TypeError, ValueError):
                continue
            for c in range(max(lo, c0), min(hi, c1) + 1):
                widths[c] = 0.0 if col.get("hidden") in ("1", "true") else w
        return [round(widths.get(c, 8.43) * 5.25, 1) for c in range(c0, c1 + 1)]
