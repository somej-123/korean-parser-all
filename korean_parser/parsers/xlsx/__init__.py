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
- 숨긴 시트도 넣고 경고를 남긴다
- 칸 메모(xl/commentsN.xml, 새 스레드 메모 xl/threadedComments)는 Note(kind="comment")로, 칸 글 뒤에 NoteRef.
  스레드 메모가 있으면 그것을 쓴다 (Excel은 옛 메모 자리에 '[스레드 메모]…' 안내 글을 함께 저장한다)
- 칸 하이퍼링크(hyperlinks: 외부 주소 또는 '시트!칸' 위치)는 Text.link
- 인쇄용 머리글·바닥글(headerFooter: &L &C &R 구역, &P 쪽 번호 같은 코드는 뺀다)은 doc.header·footer (글이 다른 것만)
- 칸 수에 상한을 두지 않는다 (82만 칸 시트: 16초, 메모리 2.4GB). 메모리가 모자란 환경은 KP_XLSX_MAX_CELLS로 상한을 두고,
  그때는 앞부분만 넣고 경고한다
- 시트의 글상자·도형 글(xdr:sp)은 Drawing(도형 + 글), 그룹 안 도형도. 시트에 넣은 OLE 개체(다른 파일)는 경고만
- 시트의 차트(xl/drawings → xl/charts)는 표 뒤에 Chart로. 차트 XML에 값 캐시가 없으면(Excel이 아닌 프로그램이 만든 파일)
  계열·범주가 가리키는 칸('일정 관리'!$E$2:$E$12)의 값으로 채운 뒤 DOCX 차트 해석을 그대로 쓴다. 그림은 Figure로
"""

from __future__ import annotations

import base64
import datetime
import mimetypes
import os
import posixpath
import re
from fractions import Fraction
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from lxml import etree

from ...ir import (
    Block, Cell, Document, Drawing, Figure, Heading, Image, Meta, Note, NoteRef, Paragraph, Provenance, Row, Shape, Table,
    Text, TextStyle,
)
from ..docx.drawing import chart_ref, read_chart
from ..ooxml import R_EMBED, R_ID, Package, ln

PARSER_NAME = "xlsx-native"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")
# 기본 제공 날짜·시각 표시 형식 번호
_BUILTIN_DATE = set(range(14, 23)) | {45, 46, 47}
_ACCOUNTING = '_-* #,##0{d}_-;-* #,##0{d}_-;_-* "-"_-;_-@_-'
# 회계 형식(41~44)의 통화 기호는 만든 컴퓨터의 지역 설정을 따라 파일에 없다 → 기호 없이 (0은 '-')
_BUILTIN_FMT = {1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00", 12: "# ?/?",
                13: "# ??/??", 37: "#,##0", 38: "#,##0", 39: "#,##0.00", 40: "#,##0.00",
                41: _ACCOUNTING.format(d=""), 42: _ACCOUNTING.format(d=""), 43: _ACCOUNTING.format(d=".00"),
                44: _ACCOUNTING.format(d=".00"), 48: "##0.0E+0"}
C = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_RANGE = re.compile(r"^(?:'?(.+?)'?!)?\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?$")
TC = "{http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments}"
_HF_CODE = re.compile(r'&(?:"[^"]*"|K[0-9A-Fa-f]{6}|K\d{2}[+-]\d{3}|\d+|[PNDTZFGHBIUSEXYO])')


def _max_cells() -> int | None:
    """시트 하나에 넣을 칸 수 상한. 기본은 없다. 메모리가 모자란 환경은 KP_XLSX_MAX_CELLS로 정한다."""
    try:
        n = int(os.environ.get("KP_XLSX_MAX_CELLS", "0"))
    except ValueError:
        return None
    return n if n > 0 else None


def header_footer_text(code: str, sheet: str) -> str:
    """머리글·바닥글 코드('&L&"맑은 고딕,굵게"&12보고서&R&P쪽')에서 보이는 글만. &A는 시트 이름, &&는 &."""
    code = code.replace("&&", "\0").replace("&A", sheet)
    parts = [p for p in re.split(r"&[LCR]", code)]
    text = "  ".join(" ".join(_HF_CODE.sub(" ", p).split()) for p in parts)
    return " ".join(text.split()).replace("\0", "&")


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
    sections = _sections(code or "")
    if value < 0 and len(sections) > 1:
        section, value = sections[1], -value  # 음수 부분이 있으면 부호는 그 부분이 정한다 ('(#,##0)', '-#,##0')
    elif value == 0 and len(sections) > 2:
        section = sections[2]  # 0 부분 (회계 형식의 '-')
    else:
        section = sections[0]
    prefix, pattern, suffix = _split_section(section)
    if not pattern:  # 숫자 자리가 없는 부분: 글자만 ('-', '없음'). 글 자리('@')만 있으면 숫자를 그대로
        if (prefix or suffix or section.strip()) and "@" not in section:
            return (prefix + suffix).strip()
        return _general(value)
    if "%" in section:
        value *= 100
    return (prefix + _format_pattern(value, pattern) + suffix).strip()


def _general(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.15g}"


def _sections(code: str) -> list[str]:
    """';'로 나눈 부분들 (따옴표·역슬래시 안의 ';'는 글자)."""
    out, cur, quoted, i = [], "", False, 0
    while i < len(code):
        ch = code[i]
        if ch == '"':
            quoted = not quoted
        elif ch == "\\" and i + 1 < len(code):
            cur += code[i:i + 2]
            i += 2
            continue
        elif ch == ";" and not quoted:
            out.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    return out + [cur]


_PATTERN_CHARS = set("0#?,.%/E+-")


def _split_section(section: str) -> tuple[str, str, str]:
    """한 부분 → (앞 글자, 숫자 자리 모양, 뒤 글자).

    따옴표 글('"원"'), 역슬래시 글자('\\원'), 통화 표시('[$₩-412]' → '₩'), 그 밖의 글자(₩ $ 원 ( ))는 글자로,
    색·조건('[Red]', '[>=100]'), 채우기('*-'), 맞춤 빈칸('_)')은 뺀다.
    """
    before, pattern, after = "", "", ""
    i = 0
    while i < len(section):
        ch = section[i]
        lit = None
        if ch == '"':
            j = section.find('"', i + 1)
            j = len(section) if j < 0 else j
            lit, i = section[i + 1:j], j + 1
        elif ch == "\\" and i + 1 < len(section):
            lit, i = section[i + 1], i + 2
        elif ch == "[":
            j = section.find("]", i)
            j = len(section) if j < 0 else j
            body = section[i + 1:j]
            lit, i = (body[1:].split("-")[0] if body.startswith("$") else ""), j + 1
        elif ch in "_*":
            lit, i = "", i + 2
        elif ch in "0#?" or (pattern and not after and (ch in _PATTERN_CHARS or ch.isdigit() and "/" in pattern)):
            pattern += ch
            i += 1
            continue
        elif ch == "@":
            lit, i = "", i + 1
        elif ch == " " and pattern and not after and "/" in section[i:]:
            pattern += ch  # 분수의 '정수 부분 빈칸 분자/분모' ('# ?/?')
            i += 1
            continue
        else:
            lit, i = ch, i + 1
        if pattern:
            after += lit
        else:
            before += lit
    # 'E+' 뒤의 숫자 자리, '%'는 모양에 남기고 앞뒤 글자에서 '%'는 뺀다 (값에 100을 곱하고 뒤에 붙인다)
    pct = "%" if "%" in before + pattern + after else ""
    before, after = before.replace("%", ""), after.replace("%", "")
    pattern = pattern.replace("%", "")
    return before, pattern, pct + after if pattern else after


def _format_pattern(value: float, pattern: str) -> str:
    """숫자 자리 모양(0 # ? , . E+ /)으로 숫자를 쓴다."""
    if "E" in pattern.upper():
        mant, _, exp = pattern.upper().partition("E")
        decimals = len(re.sub(r"[^0#?]", "", mant.split(".")[1])) if "." in mant else 0
        digits = max(len(re.sub(r"[^0]", "", exp)), 1)
        m, e = f"{value:.{decimals}E}".split("E")
        return f"{m}E{'-' if int(e) < 0 else '+'}{abs(int(e)):0{digits}d}"
    if "/" in pattern:  # 분수 '# ?/?', '# ??/??', '?/8'
        whole_part, _, den = pattern.rpartition("/")
        num_part = whole_part.split()[-1] if whole_part.split() else whole_part
        has_whole = len(whole_part.split()) > 1
        sign, v = ("-" if value < 0 else ""), abs(value)
        whole = int(v) if has_whole else 0
        if den.isdigit():  # 고정 분모('?/8'): 분자를 반올림하고 약분하지 않는다 (0.3 → 2/8)
            frac = Fraction(int(_round((v - whole) * int(den), 0)), 1)
            if frac.numerator == int(den):
                return sign + (str(whole + 1) if has_whole else f"{den}/{den}")
            text = f"{frac.numerator}/{den}" if frac.numerator else ""
            return sign + ((f"{whole} {text}".strip() if whole else (text or "0")) if has_whole else (text or "0"))
        frac = Fraction(v - whole).limit_denominator(10 ** max(len(den), 1) - 1)  # 분모 자리 수('??' → 99까지)
        if frac.numerator == frac.denominator:
            whole, frac = whole + 1, Fraction(0)
        text = f"{frac.numerator}/{frac.denominator}" if frac else ""
        if has_whole:
            return sign + (f"{whole} {text}".strip() if whole else (text or "0"))
        return sign + (text or "0") if num_part else sign + text
    scale = 1000 ** (len(pattern) - len(pattern.rstrip(",")))  # 끝의 ','마다 천으로 나눈다
    body = pattern.rstrip(",")
    decimals = len(re.sub(r"[^0#?]", "", body.split(".")[1])) if "." in body else 0
    rounded = _round(value / scale, decimals)
    return f"{rounded:,.{decimals}f}" if "," in body else f"{rounded:.{decimals}f}"


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
        self.values: dict[str, dict[tuple[int, int], tuple[str, float | None]]] = {}  # 시트 → 칸 → (보이는 글, 숫자)
        self._hf_seen: set[tuple[str, str]] = set()

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
        sheets = []
        for n, sheet in enumerate(wb.iter(f"{S}sheet"), 1):
            name = sheet.get("name", f"Sheet{n}")
            part = rels.get(sheet.get(R_ID), (None,))[0]
            if sheet.get("state") in ("hidden", "veryHidden"):
                self._warn(f"숨긴 시트도 넣음: {name}")
            root = self.pkg.xml(part) if part else None
            if root is None:
                continue
            heading = Heading(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n), level=1,
                              inlines=[Text(text=name)])
            links = self._hyperlinks(root, part)
            comments = self._comments(part, n)
            self._header_footer(root, name, n)
            if root.find(f"{S}oleObjects") is not None:
                self._warn(f"시트에 넣은 OLE 개체(다른 파일)는 넣지 않음: {name}")
            sheets.append((n, name, part, root, heading, self._sheet(root, n, name, links, comments)))
        # 차트는 다른 시트의 칸을 가리킬 수 있으므로 모든 시트를 읽은 뒤에
        for n, name, part, root, heading, table in sheets:
            self.doc.blocks.append(heading)
            if table is not None:
                self.doc.blocks.append(table)
            if root.find(f"{S}drawing") is not None:
                self.doc.blocks.extend(self._drawing(part, n, name))
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

    def _hyperlinks(self, root, part: str) -> dict[tuple[int, int], str]:
        """칸 → 링크. 외부 주소는 관계(r:id)에서, 문서 안 위치('시트2!A1')는 location으로."""
        external = self.pkg.external(part)
        out = {}
        for h in root.iter(f"{S}hyperlink"):
            pos = _ref(h.get("ref", "").split(":")[0])
            url = external.get(h.get(R_ID) or "") or (f"#{h.get('location')}" if h.get("location") else None)
            if pos is not None and url:
                out[pos] = url
        return out

    def _comments(self, part: str, n: int) -> dict[tuple[int, int], list[tuple[str, str]]]:
        """칸 → [(메모 id, 표시)]. 메모 글은 doc.notes에 Note(kind="comment")로 넣는다."""
        found: dict[tuple[int, int], list[str]] = {}
        threaded = self.pkg.rel_of_type(part, "threadedComment")
        root = self.pkg.xml(threaded) if threaded else None
        if root is not None:
            # 답글은 첫 메모와 같은 칸에 차례로 붙인다
            for tc in root.iter(f"{TC}threadedComment"):
                pos = _ref(tc.get("ref", ""))
                text = "".join(t.text or "" for t in tc.iter(f"{TC}text")).strip()
                if pos is not None and text:
                    found.setdefault(pos, []).append(text)
        else:
            legacy = self.pkg.rel_of_type(part, "comments")
            root = self.pkg.xml(legacy) if legacy else None
            for cm in root.iter(f"{S}comment") if root is not None else []:
                pos = _ref(cm.get("ref", ""))
                text = "".join(t.text or "" for t in cm.iter(f"{S}t") if ln(t.getparent()) != "rPh").strip()
                if pos is not None and text:
                    found.setdefault(pos, []).append(text)
        out: dict[tuple[int, int], list[tuple[str, str]]] = {}
        for pos in sorted(found):
            k = sum(1 for nt in self.doc.notes.values() if nt.kind == "comment") + 1
            key = f"cm{k}"
            paras = [Paragraph(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n), inlines=[Text(text=t)])
                     for t in found[pos]]
            self.doc.notes[key] = Note(kind="comment", label=f"메모{k}", blocks=paras)
            out[pos] = [(key, f"메모{k}")]
        return out

    def _header_footer(self, root, name: str, n: int) -> None:
        """인쇄용 머리글·바닥글. 글이 같은 것은 한 번만 (시트마다 같은 머리글이 흔하다)."""
        hf = root.find(f"{S}headerFooter")
        if hf is None:
            return
        for tag, target in (("oddHeader", self.doc.header), ("evenHeader", self.doc.header),
                            ("firstHeader", self.doc.header), ("oddFooter", self.doc.footer),
                            ("evenFooter", self.doc.footer), ("firstFooter", self.doc.footer)):
            el = hf.find(f"{S}{tag}")
            text = header_footer_text(el.text or "", name) if el is not None else ""
            key = ("header" if "Header" in tag else "footer", text)
            if text and key not in self._hf_seen:
                self._hf_seen.add(key)
                target.append(Paragraph(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n),
                                        inlines=[Text(text=text)]))

    def _sheet(self, root, n: int, name: str, links=None, comments=None) -> Table | None:
        links, comments = links or {}, comments or {}
        limit = _max_cells()
        cells: dict[tuple[int, int], tuple[str, int]] = {}
        for c in root.iter(f"{S}c"):
            pos = _ref(c.get("r", ""))
            if pos is None:
                continue
            text = self._value(c)
            style = int(c.get("s", "0") or 0)
            v = c.find(f"{S}v")
            try:
                number = float(v.text) if c.get("t", "n") == "n" and v is not None and v.text else None
            except ValueError:
                number = None
            self.values.setdefault(name, {})[pos] = (text, number)
            if text or (style < len(self.xf) and self.xf[style][2] in self.fills):
                cells[pos] = (text, style)
            if limit and len(cells) >= limit:
                self._warn(f"칸이 너무 많아 앞부분 {limit:,}칸만 넣음: {name} (KP_XLSX_MAX_CELLS)")
                break
        for pos in comments:  # 메모만 있고 글이 없는 칸도 표에 남긴다
            cells.setdefault(pos, ("", 0))
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
        filled = [pos for pos, (text, _) in cells.items() if text or pos in comments]
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
                if text or (r, c) in comments:
                    tstyle = TextStyle(bold=True) if font in self.bold_fonts else None
                    inlines = [Text(text=text, style=tstyle, link=links.get((r, c)))] if text else []
                    inlines += [NoteRef(note_id=key, label=label) for key, label in comments.get((r, c), [])]
                    cell.blocks = [Paragraph(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n),
                                             inlines=inlines)]
                cell.background = self.fills.get(fill)
                row.cells.append(cell)
            table.rows.append(row)
        return table

    # ------------------------------------------------------------------
    # 차트·그림 (시트의 그리기 부분)
    # ------------------------------------------------------------------

    def _drawing(self, sheet_part: str, n: int, sheet_name: str) -> list[Block]:
        part = self.pkg.rel_of_type(sheet_part, "drawing")
        root = self.pkg.xml(part) if part else None
        if root is None:
            return []
        out: list[Block] = []
        rels = self.pkg.rels(part)
        for anchor in root:
            if ln(anchor) not in ("oneCellAnchor", "twoCellAnchor", "absoluteAnchor"):
                continue
            ext = anchor.find(f"{XDR}ext")
            w = int(ext.get("cx", "0")) / 12700 if ext is not None else 400.0
            h = int(ext.get("cy", "0")) / 12700 if ext is not None else 300.0
            for frame in anchor.iter(f"{XDR}graphicFrame"):
                ref = chart_ref(frame)  # c:chart, 신형 차트는 cx:chart
                chart_part = rels.get(ref.get(R_ID) if ref is not None else "", (None,))[0]
                chart_root = self.pkg.xml(chart_part) if chart_part else None
                if chart_root is None:
                    self._warn(f"시트의 차트가 아닌 개체는 아직 읽지 않음: {sheet_name}")
                    continue
                self._fill_caches(chart_root, sheet_name)
                chart = read_chart(chart_root, w or 400.0, h or 300.0)
                if chart is None:
                    self._warn(f"차트를 읽지 못함: {sheet_name}")
                    continue
                chart.id, chart.prov = self._new_id(), Provenance(parser=PARSER_NAME, page=n)
                out.append(chart)
            drawing = self._shapes(anchor, n)
            if drawing is not None:
                out.append(drawing)
            for pic in anchor.iter(f"{XDR}pic"):
                blip = pic.find(f".//{A}blip")
                target = rels.get(blip.get(R_EMBED) if blip is not None else "", (None,))[0]
                data = self.pkg.read(target) if target else None
                if data is None:
                    continue
                image_id = f"img{len(self.doc.images) + 1}"
                mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
                self.doc.images[image_id] = Image(mime=mime, data_base64=base64.b64encode(data).decode("ascii"),
                                                  name=posixpath.basename(target))
                nv = pic.find(f".//{XDR}cNvPr")
                out.append(Figure(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n), image_id=image_id,
                                  width_pt=w or None, height_pt=h or None,
                                  alt=(nv.get("descr") or "").strip() or None if nv is not None else None))
        return out

    def _shapes(self, anchor, n: int) -> Drawing | None:
        """앵커 안의 글상자·도형(그룹 안 포함)을 하나의 Drawing으로. 글이 있는 도형만 (장식 도형은 뺀다).

        위치·크기는 도형의 a:xfrm(EMU). 그룹 안 도형은 그룹 좌표라 앵커 안 상대 위치로만 맞춘다.
        """
        shapes = []
        for sp in anchor.iter(f"{XDR}sp"):
            body = sp.find(f"{XDR}txBody")
            paras = []
            for p in body.iter(f"{A}p") if body is not None else []:
                text = "".join(t.text or "" for t in p.iter(f"{A}t"))
                if text.strip():
                    paras.append(Paragraph(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n),
                                           inlines=[Text(text=text)]))
            if not paras:
                continue
            off, ext = sp.find(f".//{A}xfrm/{A}off"), sp.find(f".//{A}xfrm/{A}ext")
            x = int(off.get("x", "0")) / 12700 if off is not None else 0.0
            y = int(off.get("y", "0")) / 12700 if off is not None else 0.0
            w = int(ext.get("cx", "0")) / 12700 if ext is not None else 200.0
            h = int(ext.get("cy", "0")) / 12700 if ext is not None else 60.0
            geom = sp.find(f".//{A}prstGeom")
            fill = sp.find(f"{XDR}spPr/{A}solidFill/{A}srgbClr")
            line = sp.find(f"{XDR}spPr/{A}ln")
            stroke = None if line is not None and line.find(f"{A}noFill") is not None else "#000000"
            shapes.append(Shape(x=x, y=y, width=w or 200.0, height=h or 60.0,
                                geometry=geom.get("prst", "rect") if geom is not None else "rect",
                                fill=f"#{fill.get('val').upper()}" if fill is not None and fill.get("val") else None,
                                stroke=stroke, text_blocks=paras, text_v_align="top"))
        if not shapes:
            return None
        x0, y0 = min(s.x for s in shapes), min(s.y for s in shapes)
        for s in shapes:
            s.x, s.y = s.x - x0, s.y - y0
        return Drawing(id=self._new_id(), prov=Provenance(parser=PARSER_NAME, page=n),
                       width_pt=max(s.x + s.width for s in shapes), height_pt=max(s.y + s.height for s in shapes),
                       shapes=shapes)

    def _fill_caches(self, chart_root, default_sheet: str) -> None:
        """값 캐시가 없는 계열 이름·범주·값(strRef·numRef)을 가리키는 칸의 값으로 채운다."""
        for ref in list(chart_root.iter(f"{C}strRef")) + list(chart_root.iter(f"{C}numRef")):
            if ref.find(f"{C}strCache") is not None or ref.find(f"{C}numCache") is not None:
                continue
            f = ref.find(f"{C}f")
            cells = self._range(f.text if f is not None else "", default_sheet)
            if cells is None:
                continue
            numeric = ln(ref) == "numRef" and ln(ref.getparent()) != "cat" and all(num is not None for _, num in cells)
            cache = etree.SubElement(ref, f"{C}{'numCache' if numeric else 'strCache'}")
            etree.SubElement(cache, f"{C}ptCount").set("val", str(len(cells)))
            for i, (text, num) in enumerate(cells):
                pt = etree.SubElement(cache, f"{C}pt")
                pt.set("idx", str(i))
                etree.SubElement(pt, f"{C}v").text = repr(num) if numeric else text

    def _range(self, formula: str, default_sheet: str) -> list[tuple[str, float | None]] | None:
        """'시트'!$A$2:$A$12 → 그 칸들의 (보이는 글, 숫자). 한 줄(가로나 세로)만."""
        m = _RANGE.match((formula or "").strip())
        if not m:
            return None
        sheet = (m.group(1) or default_sheet).replace("''", "'")
        values = self.values.get(sheet, {})
        r0, c0 = int(m.group(3)) - 1, col_index(m.group(2))
        r1, c1 = (int(m.group(5)) - 1, col_index(m.group(4))) if m.group(4) else (r0, c0)
        return [values.get((r, c), ("", None)) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

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
