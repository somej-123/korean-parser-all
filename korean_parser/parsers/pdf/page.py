"""PDF 페이지 분석 (pdfium): 글자 서식, 그래픽 개체 위치, 색 샘플링, 영역 잘라내기.

Docling은 레이아웃(무엇이 제목·표·그림인지)은 잘 찾지만 글자 크기·굵기·색은 주지 않는다.
PDF 텍스트 레이어에는 이 정보가 글자마다 있으므로, Docling이 찾은 영역 안의 글자를 읽어 서식을 되살린다.

좌표는 모두 '왼쪽 위 원점, pt' 로 통일한다. (PDF 자체는 왼쪽 아래 원점)
"""

from __future__ import annotations

import ctypes
import functools
import io
import statistics
import threading
from collections import Counter
from dataclasses import dataclass, field

from .rules import read_rules

try:  # Docling도 pdfium을 쓴다 (쪽 읽기 스레드). 같은 잠금을 써야 두 쪽이 동시에 pdfium을 부르지 않는다
    from docling.utils.locks import pypdfium2_lock as _PDFIUM_LOCK
except ImportError:  # pragma: no cover - Docling 없이 쓰는 경우
    _PDFIUM_LOCK = threading.Lock()
_tls = threading.local()


def _pdfium(fn):
    """pdfium을 부르는 함수를 잠금 안에서 돌린다.

    pdfium은 여러 스레드에서 동시에 쓰면 메모리가 깨진다 (Windows 0xc0000374로 프로그램이 멈췄다). Docling의 잠금은
    다시 잡을 수 없어서(재진입 불가) 같은 스레드의 바깥 호출에서만 잡는다.
    """
    @functools.wraps(fn)
    def wrap(*args, **kwargs):
        if getattr(_tls, "depth", 0):
            return fn(*args, **kwargs)
        with _PDFIUM_LOCK:
            _tls.depth = 1
            try:
                return fn(*args, **kwargs)
            finally:
                _tls.depth = 0
    return wrap

BBox = tuple[float, float, float, float]  # x0, y0(위), x1, y1(아래)

_BOLD_NAME = ("bold", "black", "heavy", "semibold", "demibold", "extrabold", "gothic-b", "-b", "견고딕", "헤드라인",
              # 한글(HWP)이 PDF에 넣는 HY 글꼴 이름: 헤드라인M, 견고딕, 견명조
              "h2hdrm", "h2gtre", "h2mjre", "hyheadline", "hygothic-extra", "hymyeongjo-extra")


@dataclass
class Char:
    text: str
    box: BBox
    size: float
    bold: bool
    italic: bool
    color: str
    font: str
    origin: tuple[float, float] = (0.0, 0.0)  # 글자 기준점 (x, 기준선 y)
    advance: float = 0.0  # 글꼴이 말하는 글자 폭 (loose box). 폭 정보가 깨진 글꼴은 글자 크기만큼 나온다
    generated: bool = False  # PDF에 없는 글자: pdfium이 위치 간격을 보고 끼워 넣은 공백


@dataclass
class Run:
    """같은 줄, 같은 서식으로 이어진 글자 묶음. 고정 레이아웃에서 원래 위치에 그대로 그린다."""

    text: str
    x: float
    baseline: float
    width: float
    size: float
    bold: bool
    italic: bool
    color: str
    font: str
    box: BBox


@dataclass
class Graphic:
    kind: str  # image, path, shading
    box: BBox
    fill: str | None = None  # 채운 도형의 색 (표 칸 배경 등)


@dataclass
class TextStyleGuess:
    size: float | None = None
    bold: bool = False
    italic: bool = False
    color: str | None = None
    font: str | None = None


@dataclass
class PageInfo:
    number: int  # 1부터
    width: float
    height: float
    chars: list[Char] = field(default_factory=list)
    graphics: list[Graphic] = field(default_factory=list)
    rules: list = field(default_factory=list)  # 수평·수직 선 (rules.Rule). 괘선 표 구조를 찾는 데 쓴다
    _bitmap: object = None  # 색 샘플링용 저해상도 렌더링 (PIL)
    _page: object = None

    # ------------------------------------------------------------------

    def chars_in(self, box: BBox, pad: float = 1.0) -> list[Char]:
        x0, y0, x1, y1 = box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad
        out = []
        for c in self.chars:
            cx, cy = (c.box[0] + c.box[2]) / 2, (c.box[1] + c.box[3]) / 2
            # 세로 위치는 기준선에서 글자 크기만큼 위인 글자 칸의 가운데로 본다. 잉크 상자 가운데로 보면 줄 위쪽에 작게
            # 붙은 따옴표(’25년)의 가운데가 문단 상자 위로 벗어나 첫 줄의 따옴표가 빠졌다
            ey = c.origin[1] - c.size * 0.35
            if c.size > 0 and abs(ey - cy) < c.size:
                cy = ey
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                out.append(c)
        return out

    def style_in(self, box: BBox) -> TextStyleGuess:
        """영역 안 글자들의 대표 서식 (공백 제외, 가장 흔한 값)."""
        chars = [c for c in self.chars_in(box) if c.text.strip()]
        if not chars:
            return TextStyleGuess()
        size = statistics.median(c.size for c in chars)
        bold = sum(c.bold for c in chars) > len(chars) / 2
        italic = sum(c.italic for c in chars) > len(chars) / 2
        color = Counter(c.color for c in chars).most_common(1)[0][0]
        font = Counter(c.font for c in chars).most_common(1)[0][0]
        return TextStyleGuess(size=round(size * 2) / 2, bold=bold, italic=italic, color=color, font=font or None)

    def fill_at(self, x: float, y: float) -> str | None:
        """그 점을 덮는 가장 작은 '채운 도형'의 색 (흰색 제외). 표 칸 배경을 찾는 데 쓴다."""
        best, best_area = None, None
        for gr in self.graphics:
            if gr.fill is None:
                continue
            x0, y0, x1, y1 = gr.box
            if x0 <= x <= x1 and y0 <= y <= y1:
                a = (x1 - x0) * (y1 - y0)
                if best_area is None or a < best_area:
                    best, best_area = gr.fill, a
        if best is None or best in ("#FFFFFF",):
            return None
        return best

    def runs(self) -> list[Run]:
        """글자를 같은 줄·같은 서식·가까운 간격 단위로 묶는다 (내용 순서 기준).

        간격은 잉크 상자가 아니라 글자 기준점(origin)으로 잰다. 잉크 상자로 재면 좁은 글자('(', '.')
        옆이 띄어 쓴 것처럼 보이고, 폭 정보가 깨진 글꼴(모든 글자 상자가 글자 크기만큼)은 글자끼리
        겹쳐 보여 한 글자씩 쪼개진다.
        """
        chars = [c for c in _dedupe_overlaps(self.chars) if not _is_symbol(c.text)]  # 기호는 바탕 그림에 남김
        right = _right_edges(chars)
        runs: list[Run] = []
        cur: list[int] = []

        def flush():
            if not cur:
                return
            while cur and chars[cur[-1]].text.isspace():
                cur.pop()
            if cur:
                first = chars[cur[0]]
                x0, x1 = first.origin[0], right[cur[-1]]
                box = (x0, min(chars[i].box[1] for i in cur), x1, max(chars[i].box[3] for i in cur))
                runs.append(Run(text="".join(chars[i].text for i in cur), x=x0, baseline=first.origin[1],
                                width=max(x1 - x0, 0.5), size=first.size, bold=first.bold, italic=first.italic,
                                color=first.color, font=first.font, box=box))
            cur.clear()

        for i, c in enumerate(chars):
            if (c.generated or c.size <= 1.5) and c.text.isspace():
                flush()  # pdfium이 위치 간격을 보고 끼워 넣은 공백 = 단어 사이
                continue
            if cur:
                p = chars[cur[-1]]
                same_line = abs(c.origin[1] - p.origin[1]) < max(p.size * 0.25, 0.8)
                same_style = (abs(c.size - p.size) < 0.6 and c.bold == p.bold and c.color == p.color)
                gap = c.origin[0] - right[cur[-1]]
                # 공백 문자 없이 위치로만 띄운 단어 사이(간격이 글자 크기의 1/4 이상)에서도 끊어 제자리에 놓는다
                spaced = gap > p.size * 0.25 and not (c.text.isspace() or p.text.isspace())
                if not (same_line and same_style and -p.size * 0.3 < gap < p.size * 1.2) or spaced:
                    flush()
            if c.text.isspace() and not cur:
                continue
            cur.append(i)
        flush()
        return runs

    def text_lines(self, box: BBox, pad: float = 1.0, exclude: list[BBox] | None = None) -> list[str]:
        """영역 안 글을 텍스트 레이어에서 줄 단위로 읽는다 (내용 순서).

        PDF에 실제로 있는 공백 글자는 그대로 두고, pdfium이 끼워 넣은 공백은 글자 사이에 실제 간격이 있을 때만
        쓴다. 글꼴이 바뀌는 자리("2026" + "년")에 끼워 넣은 폭 0짜리 공백은 버린다.
        줄 끝의 공백은 남겨 둔다 (줄을 합칠 때 어절 경계 표시로 쓴다).
        한글과 영문·숫자 사이의 좁은 간격, 좁은 글꼴의 좁은 낱말 사이(`_narrow_spaces`)는 AMBIGUOUS_GAP으로 표시한다.
        """
        chars = [c for c in _dedupe_overlaps(self.chars_in(box, pad)) if not (c.text.isspace() and c.generated)]
        if exclude:
            # 다른 블록이 이미 담은 글자 (그림 영역과 겹친 문단·캡션)
            chars = [c for c in chars if not any(x[0] <= (c.box[0] + c.box[2]) / 2 <= x[2] and x[1] <= (c.box[1] + c.box[3]) / 2 <= x[3]
                                                 for x in exclude)]
        if not chars:
            return []
        lines_of = _group_lines(chars)
        lines: list[str] = []
        for line in lines_of:
            line.sort(key=lambda c: c.origin[0])
            right = _right_edges(line)
            narrow = _narrow_spaces(line, right)
            cur = ""
            for i, c in enumerate(line):
                if i:
                    p = line[i - 1]
                    size = max(p.size, c.size, 1.0)
                    if i in narrow:
                        cur += AMBIGUOUS_GAP
                    elif not c.text.isspace() and not p.text.isspace() and c.origin[0] - right[i - 1] > size * 0.15:
                        # 공백 글자 없이 위치로 띄운 단어 사이. 한글과 영문·숫자 사이의 좁은 간격은 조판 프로그램이
                        # 자동으로 넣는 '한글-영문 간격'일 수 있어(2026년, OCR이) 판단을 미룬다 (AMBIGUOUS_GAP)
                        mixed = _is_hangul(p.text) != _is_hangul(c.text) and p.text.isalnum() and c.text.isalnum()
                        cur += AMBIGUOUS_GAP if mixed and c.origin[0] - right[i - 1] < size * 0.4 else " "
                if c.text.isspace():
                    if cur and not cur.endswith(" "):
                        cur += " "
                else:
                    cur += c.text
            lines.append(cur)
        return [ln.lstrip() for ln in lines if ln.strip()]

    def superscripts(self) -> list[tuple[BBox, str, str, str]]:
        """쪽의 위 첨자 묶음 (상자, 글, 앞 두 글자, 뒤 두 글자). 각주 번호를 본문과 잇는 데 쓴다.

        줄 글자 크기 중앙값의 0.8배 이하이고 기준선이 글자 크기의 1/4 넘게 올라간 글자가 이어진 묶음이다.
        """
        out: list[tuple[BBox, str, str, str]] = []
        chars = [c for c in _dedupe_overlaps(self.chars) if not (c.text.isspace() and c.generated)]
        for line in _group_lines(chars):
            line.sort(key=lambda c: c.origin[0])
            solid = [c for c in line if c.text.strip()]
            if len(solid) < 2:
                continue
            med = statistics.median(c.size for c in solid)
            body = [c.origin[1] for c in solid if c.size >= med * 0.9]
            if not body:
                continue
            base = statistics.median(body)

            def raised(c) -> bool:
                return bool(c.text.strip()) and c.size <= med * 0.8 and c.origin[1] < base - c.size * 0.25

            i = 0
            while i < len(line):
                if not raised(line[i]):
                    i += 1
                    continue
                j = i
                while j < len(line) and raised(line[j]):
                    j += 1
                run = line[i:j]
                box = (min(c.box[0] for c in run), min(c.box[1] for c in run),
                       max(c.box[2] for c in run), max(c.box[3] for c in run))
                before = "".join(c.text for c in line[:i] if c.text.strip())[-2:]
                after = "".join(c.text for c in line[j:] if c.text.strip())[:2]
                out.append((box, "".join(c.text for c in run), before, after))
                i = j
        return out

    def sample_color(self, x: float, y: float) -> str | None:
        """그 위치의 화면 색 (표 칸 배경 등). 흰색에 가까우면 None."""
        if self._bitmap is None:
            return None
        img = self._bitmap
        px, py = int(x * img.width / self.width), int(y * img.height / self.height)
        if not (0 <= px < img.width and 0 <= py < img.height):
            return None
        r, g, b = img.getpixel((px, py))[:3]
        if min(r, g, b) > 245:
            return None
        return f"#{r:02X}{g:02X}{b:02X}"

    @_pdfium
    def crop_png(self, box: BBox, scale: float = 2.0) -> bytes | None:
        """영역을 PNG로 잘라 낸다 (그림·도식)."""
        if self._page is None:
            return None
        x0, y0, x1, y1 = box
        crop = (max(x0, 0), max(self.height - y1, 0), max(self.width - x1, 0), max(y0, 0))  # 좌, 하, 우, 상
        try:
            bitmap = self._page.render(scale=scale, crop=crop)
            img = bitmap.to_pil()
        except Exception:  # noqa: BLE001 - 잘라내기 실패는 그림 누락으로만 처리
            return None
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()


def _group_lines(chars: list) -> list[list]:
    """글자를 줄로 묶는다. 줄은 기준선 위치로 묶고, 줄 안 순서는 부르는 쪽이 왼쪽부터 정한다.

    그리는 순서(내용 순서)를 따르면 괄호·숫자를 따로 그리는 PDF에서 "( ) 소기업 확인서"처럼 글자 순서가 뒤섞인다.
    위 첨자(각주 번호)는 기준선이 높아 따로 한 줄이 되어 문단 맨 앞으로 갔다 ('1 이 사업의 … 공공기관 이다').
    작은 글자만 있는 줄이 이웃 줄의 높이 범위 안에 들고 가로로도 그 줄 범위 안이면 그 줄에 합친다.
    """
    lines_of: list[list] = []
    for c in sorted(chars, key=lambda c: c.origin[1]):
        if lines_of:
            ref = lines_of[-1][0]
            if abs(c.origin[1] - ref.origin[1]) <= max(ref.size, c.size, 1.0) * 0.4:
                lines_of[-1].append(c)
                continue
        lines_of.append([c])

    def size(line) -> float:
        solid = [c.size for c in line if c.text.strip()]
        return max(solid) if solid else 0.0

    k = 0
    while k < len(lines_of):
        small = lines_of[k]
        s = size(small)
        cy = statistics.mean((c.box[1] + c.box[3]) / 2 for c in small)
        x0, x1 = min(c.box[0] for c in small), max(c.box[2] for c in small)
        home = None
        for m in (k - 1, k + 1):
            if 0 <= m < len(lines_of) and s and s <= size(lines_of[m]) * 0.8:
                other = [c for c in lines_of[m] if c.text.strip()]
                if other and min(c.box[1] for c in other) <= cy <= max(c.box[3] for c in other) \
                        and min(c.box[0] for c in other) - 2 <= x0 and x1 <= max(c.box[2] for c in other) + 12:
                    home = m
                    break
        if home is None:
            k += 1
            continue
        lines_of[home].extend(small)
        del lines_of[k]
        k = max(k - 1, 0)
    return lines_of


def heavy_by_density(dens: dict[str, list[float]], count: Counter) -> set[str]:
    """글꼴별 잉크 비율 표본 → 굵게 볼 글꼴. 본문 글꼴(글자 수가 가장 많은 것)의 1.35배 이상이면서 0.42 이상."""
    med = {f: statistics.median(v) for f, v in dens.items() if len(v) >= 10}
    if len(med) < 2:
        return set()
    body = max(med, key=lambda f: count[f])
    return {f for f, d in med.items() if d >= max(med[body] * 1.35, 0.42)}


class PdfPages:
    """PDF 전체 페이지의 PageInfo를 필요할 때 만든다."""

    @_pdfium
    def __init__(self, path):
        import pypdfium2 as pdfium

        self._path = str(path)
        self.pdf = pdfium.PdfDocument(self._path)
        self._bg_pdf = None
        self._cache: dict[int, PageInfo] = {}
        self._heavy: set[str] | None = None

    def __len__(self):
        return len(self.pdf)

    @_pdfium
    def close(self):
        self._cache.clear()
        if self._bg_pdf is not None:
            self._bg_pdf.close()
        self.pdf.close()

    @_pdfium
    def links(self, number: int) -> list[tuple[BBox, str]]:
        """쪽의 바깥 링크 주석 (영역, 주소). 문서 안으로 가는 링크(목차 등)는 뺀다."""
        import pypdfium2.raw as raw

        page = self.pdf[number - 1]
        h = page.get_height()
        out: list[tuple[BBox, str]] = []
        for k in range(raw.FPDFPage_GetAnnotCount(page.raw)):
            annot = raw.FPDFPage_GetAnnot(page.raw, k)
            try:
                if raw.FPDFAnnot_GetSubtype(annot) != raw.FPDF_ANNOT_LINK:
                    continue
                link = raw.FPDFAnnot_GetLink(annot)
                action = raw.FPDFLink_GetAction(link) if link else None
                if not action or raw.FPDFAction_GetType(action) != raw.PDFACTION_URI:
                    continue
                n = raw.FPDFAction_GetURIPath(self.pdf.raw, action, None, 0)
                buf = ctypes.create_string_buffer(n)
                raw.FPDFAction_GetURIPath(self.pdf.raw, action, buf, n)
                uri = buf.value.decode("utf-8", "replace").strip()
                rect = raw.FS_RECTF()
                if uri and raw.FPDFAnnot_GetRect(annot, rect):
                    # 위·아래(왼쪽·오른쪽)가 뒤집혀 저장된 PDF가 있다 (실제 제안요청서, 입찰공고)
                    ys = sorted((h - rect.top, h - rect.bottom))
                    xs = sorted((rect.left, rect.right))
                    out.append(((xs[0], ys[0], xs[1], ys[1]), uri))
            finally:
                raw.FPDFPage_CloseAnnot(annot)
        return out

    @_pdfium
    def heavy_fonts(self, pages: int = 6, per_font: int = 60) -> set[str]:
        """굵기 정보 없이 굵게 그려지는 글꼴 이름들 (문서마다 한 번 계산).

        굵기를 이름의 숫자로만 나누는 글꼴 묶음이 있다 (윤고딕 'YDVYGOStd11'·'YDVYGOStd14'). 이름에 Bold가 없고 pdfium
        굵기 값도 모두 400이라 굵게로 보지 못했다. 앞 몇 쪽을 그려 글꼴마다 한글 글자 상자 안 잉크 비율(획 두께)을 재고,
        본문 글꼴(글자가 가장 많은 글꼴)의 1.35배 이상이면서 0.42 이상인 글꼴을 굵게 본다. 실제 문서에서 잰 값:
        윤고딕 11(본문) 0.31 · 14 0.49, KoPub 돋움 Medium 0.39 · Bold 0.49.
        """
        if self._heavy is not None:
            return self._heavy
        self._heavy = set()
        import numpy as np
        import pypdfium2.raw as raw

        dens: dict[str, list[float]] = {}
        count: Counter = Counter()
        buf, flags = ctypes.create_string_buffer(256), ctypes.c_int()
        sc = 2.0
        for n in range(min(len(self.pdf), pages)):
            page = self.pdf[n]
            h = page.get_height()
            tp = page.get_textpage()
            img = None
            for i in range(tp.count_chars()):
                ch = tp.get_text_range(i, 1)
                if not ("가" <= ch <= "힣"):
                    continue
                k = raw.FPDFText_GetFontInfo(tp.raw, i, buf, 256, ctypes.byref(flags))
                name = buf.value[:k].decode("utf-8", "ignore").split("+")[-1] if k else ""
                if raw.FPDFText_GetFontWeight(tp.raw, i) >= 600 or any(b in name.lower() for b in _BOLD_NAME):
                    continue
                count[name] += 1
                if len(dens.setdefault(name, [])) >= per_font:
                    continue
                if img is None:
                    img = np.asarray(page.render(scale=sc, grayscale=True).to_pil().convert("L"))
                left, bottom, right, top = tp.get_charbox(i)
                crop = img[int((h - top) * sc):int((h - bottom) * sc), int(left * sc):int(right * sc)]
                if crop.size >= 20:
                    dens[name].append(float((crop < 200).mean()))
        self._heavy = heavy_by_density(dens, count)
        return self._heavy

    @_pdfium
    def background_png(self, number: int, scale: float = 1.5) -> bytes | None:
        """글자를 지운 페이지 그림 (선·상자·색·이미지만). 고정 레이아웃의 바탕으로 쓴다.

        원본을 건드리지 않도록 PDF를 한 번 더 열어 그 사본에서 글자 개체를 지운다.
        양식(Form XObject) 안에 든 글자는 지울 수 없어 남는다.
        사용자 정의 영역(PUA) 기호만 든 글자 개체(글머리 □ 등)는 브라우저 글꼴로 그릴 수 없으므로 바탕에 남긴다.
        """
        import pypdfium2 as pdfium
        import pypdfium2.raw as raw

        if self._bg_pdf is None:
            self._bg_pdf = pdfium.PdfDocument(self._path)
        page = self._bg_pdf[number - 1]
        try:
            tp = page.get_textpage()
            remove = [obj for obj in page.get_objects(max_depth=1)
                      if obj.type == raw.FPDF_PAGEOBJ_TEXT and not _symbol_only(_obj_text(obj, tp))]
            tp.close()
            for obj in remove:
                page.remove_obj(obj)
            page.gen_content()
            img = page.render(scale=scale).to_pil().convert("RGB")
        except Exception:  # noqa: BLE001
            return None
        buf = io.BytesIO()
        img.save(buf, "PNG", compress_level=6)
        return buf.getvalue()

    @_pdfium
    def has_text_layer(self, sample: int = 5, min_chars: int = 30) -> bool:
        n = min(len(self.pdf), sample)
        if n == 0:
            return False
        total = 0
        for i in range(n):
            tp = self.pdf[i].get_textpage()
            total += len(tp.get_text_range().strip())
            tp.close()
        return total / n >= min_chars

    @_pdfium
    def page(self, number: int) -> PageInfo:
        if number not in self._cache:
            self._cache[number] = self._load(number)
        return self._cache[number]

    def _load(self, number: int) -> PageInfo:
        import pypdfium2.raw as raw

        page = self.pdf[number - 1]
        w, h = page.get_size()
        info = PageInfo(number=number, width=w, height=h, _page=page)

        # 그리는 순서(z-순서): 글자 위에 나중에 그려진 불투명 도형이 덮으면 원본에서는 안 보이는 글이다
        order: dict[int, int] = {}
        occluders: list[tuple[int, BBox, object, tuple | None]] = []  # (그린 순서, 범위, 그림이면 그 개체, 도형 채우기 색)
        r, g, b, a = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        for idx, obj in enumerate(page.get_objects(max_depth=1)):
            addr = ctypes.cast(obj.raw, ctypes.c_void_p).value
            if obj.type == raw.FPDF_PAGEOBJ_TEXT:
                order[addr] = idx
                continue
            opaque = obj.type == raw.FPDF_PAGEOBJ_IMAGE
            fill = None
            if obj.type == raw.FPDF_PAGEOBJ_PATH:
                mode, stroke = ctypes.c_int(), ctypes.c_int()
                if raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(mode), ctypes.byref(stroke)) and mode.value:
                    opaque = bool(raw.FPDFPageObj_GetFillColor(obj.raw, r, g, b, a)) and a.value >= 200
                    fill = (r.value, g.value, b.value)
            if opaque:
                vb = _visible_bounds(obj)
                if vb is not None:
                    left, bottom, right, top = vb
                    image = obj if obj.type == raw.FPDF_PAGEOBJ_IMAGE else None
                    occluders.append((idx, (left, h - top, right, h - bottom), image, fill))

        # 글자
        opaque_cache: dict[int, bool] = {}
        tp = page.get_textpage()
        buf = ctypes.create_string_buffer(256)
        flags = ctypes.c_int()
        font_cache: dict[int, tuple[str, bool, bool]] = {}
        for i in range(tp.count_chars()):
            ch = tp.get_text_range(i, 1)
            if not ch or ch in "\r\n":
                continue
            left, bottom, right, top = tp.get_charbox(i)
            loose = raw.FS_RECTF()
            raw.FPDFText_GetLooseCharBox(tp.raw, i, loose)
            # 실제 보이는 크기 = 글꼴 크기 × 글자 행렬의 배율 (행렬로 축소해 그리는 PDF가 많다)
            m = raw.FS_MATRIX()
            scale = 1.0
            if raw.FPDFText_GetMatrix(tp.raw, i, ctypes.byref(m)):
                scale = abs(m.a * m.d - m.b * m.c) ** 0.5 or 1.0
            size = raw.FPDFText_GetFontSize(tp.raw, i) * scale
            ox, oy = ctypes.c_double(), ctypes.c_double()
            raw.FPDFText_GetCharOrigin(tp.raw, i, ox, oy)
            weight = raw.FPDFText_GetFontWeight(tp.raw, i)
            n = raw.FPDFText_GetFontInfo(tp.raw, i, buf, 256, ctypes.byref(flags))
            name = buf.value[:n].decode("utf-8", "ignore") if n else ""
            key = hash(name) ^ flags.value
            if key not in font_cache:
                lname = name.lower()
                bold = any(k in lname for k in _BOLD_NAME)
                italic = "italic" in lname or "oblique" in lname or bool(flags.value & 0x40)
                font_cache[key] = (name.split("+")[-1], bold, italic)
            fname, bold, italic = font_cache[key]
            obj = raw.FPDFText_GetTextObject(tp.raw, i)
            mode = raw.FPDFTextObj_GetTextRenderMode(obj) if obj else raw.FPDF_TEXTRENDERMODE_FILL
            if mode == raw.FPDF_TEXTRENDERMODE_INVISIBLE:
                continue  # 화면에 보이지 않는 글 (스캔본의 OCR 글자층, 템플릿의 숨은 글 등)
            z = order.get(ctypes.cast(obj, ctypes.c_void_p).value) if obj else None
            text_fill = (r.value, g.value, b.value) if raw.FPDFText_GetFillColor(tp.raw, i, r, g, b, a) else None
            if z is not None:
                cx, cy = (left + right) / 2, h - (bottom + top) / 2
                box = (left, h - top, right, h - bottom)
                if any(oz > z and ob[0] <= cx <= ob[2] and ob[1] <= cy <= ob[3]
                       and (img is None or _opaque_image(img, opaque_cache))
                       and not (img is None and _glyph_outline(box, text_fill, ob, fill))
                       for oz, ob, img, fill in occluders):
                    continue  # 나중에 그려진 도형·불투명한 그림에 가려진 글
            if weight >= 600 or fname in self.heavy_fonts():
                bold = True
            elif not bold and mode == raw.FPDF_TEXTRENDERMODE_FILL_STROKE:
                bold = True  # 채우기+윤곽선 = 굵은 글꼴이 없을 때 쓰는 '가짜 굵게'
            color = "#000000"
            if raw.FPDFText_GetFillColor(tp.raw, i, r, g, b, a):
                color = f"#{r.value:02X}{g.value:02X}{b.value:02X}"
            info.chars.append(Char(ch, (left, h - top, right, h - bottom), size, bold, italic, color, fname,
                                   (ox.value, h - oy.value), max(loose.right - ox.value, 0.0),
                                   bool(raw.FPDFText_IsGenerated(tp.raw, i) == 1)))
        tp.close()

        # 그래픽 개체 (이미지, 선·도형). 쪽 전체를 덮는 배경은 뺀다.
        page_area = w * h
        for obj in page.get_objects(max_depth=4):
            kind = {raw.FPDF_PAGEOBJ_IMAGE: "image", raw.FPDF_PAGEOBJ_PATH: "path",
                    raw.FPDF_PAGEOBJ_SHADING: "shading"}.get(obj.type)
            if kind is None:
                continue
            vb = _visible_bounds(obj)
            if vb is None:
                continue
            left, bottom, right, top = vb
            box = (left, h - top, right, h - bottom)
            if (box[2] - box[0]) * (box[3] - box[1]) > 0.9 * page_area:
                continue
            fill = None
            if kind == "path":
                info.rules += read_rules(obj, h)
                mode, stroke = ctypes.c_int(), ctypes.c_int()
                if raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(mode), ctypes.byref(stroke)) and mode.value:
                    if raw.FPDFPageObj_GetFillColor(obj.raw, r, g, b, a) and a.value > 0:
                        fill = f"#{r.value:02X}{g.value:02X}{b.value:02X}"
            info.graphics.append(Graphic(kind, box, fill))

        # 색 샘플링용 저해상도 이미지 (36 dpi)
        try:
            info._bitmap = page.render(scale=0.5).to_pil().convert("RGB")
        except Exception:  # noqa: BLE001
            info._bitmap = None
        return info


def _glyph_outline(char_box: BBox, text_fill, path_box: BBox, path_fill) -> bool:
    """글자 위에 그려진 도형이 덮개가 아니라 그 글자를 곡선으로 바꾼 사본인가.

    디자인 프로그램은 글자 층을 남긴 채 같은 글자를 곡선으로 바꾼 도형을 바로 위에 겹쳐 그리기도 한다 (쇠고기 부위
    안내도의 ❶ 39개 중 35개가 가려진 글로 버려졌다). 도형이 글자 상자와 거의 같은 자리·크기(겹침 비율 0.5 이상)이고
    색이 같으면 그 글자의 모양이다. 글자를 가리는 흰 상자·지운 자리는 색이 다르거나 여러 글자를 덮어 크기가 다르다.
    """
    if text_fill is None or path_fill is None or max(abs(x - y) for x, y in zip(text_fill, path_fill)) > 24:
        return False
    inter = intersect(char_box, path_box)
    if inter is None:
        return False
    union_area = area(char_box) + area(path_box) - area(inter)
    return union_area > 0 and area(inter) / union_area >= 0.5


def _opaque_image(obj, cache: dict[int, bool]) -> bool:
    """그림이 실제로 글을 덮는가. Word가 넣는 투명한 그림(알파 0)은 덮지 않는다. 그림마다 한 번만 확인한다."""
    key = id(obj)
    if key not in cache:
        opaque = True
        try:
            img = obj.get_bitmap(render=True).to_pil()
            if img.mode in ("RGBA", "LA"):
                alpha = img.getchannel("A").resize((16, 16))
                opaque = sum(alpha.tobytes()) / 256 >= 128
        except Exception:  # noqa: BLE001 - 그림을 못 읽으면 예전처럼 덮는 것으로 본다
            opaque = True
        cache[key] = opaque
    return cache[key]


def _visible_bounds(obj) -> tuple[float, float, float, float] | None:
    """개체 범위를 잘라내기 경로(clip path)로 좁힌 실제로 보이는 범위 (PDF 좌표: left, bottom, right, top).

    그라데이션 띠처럼 긴 조각을 잘라내기 경로로 일부만 보이게 그린 PDF가 많다. 잘라내기를 무시하면
    보이지 않는 부분이 옆의 글을 '덮는' 것으로 오판한다. 전부 잘려 보이지 않으면 None.
    """
    import pypdfium2.raw as raw

    left, bottom, right, top = obj.get_bounds()
    cp = raw.FPDFPageObj_GetClipPath(obj.raw)
    if not cp:
        return left, bottom, right, top
    x, y = ctypes.c_float(), ctypes.c_float()
    for pi in range(raw.FPDFClipPath_CountPaths(cp)):
        n = raw.FPDFClipPath_CountPathSegments(cp, pi)
        if n <= 0:
            continue
        xs, ys = [], []
        for si in range(n):
            seg = raw.FPDFClipPath_GetPathSegment(cp, pi, si)
            if seg and raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y)):
                xs.append(x.value)
                ys.append(y.value)
        if xs:
            left, right = max(left, min(xs)), min(right, max(xs))
            bottom, top = max(bottom, min(ys)), min(top, max(ys))
    if right < left or top < bottom:
        return None
    return left, bottom, right, top


AMBIGUOUS_GAP = "\u200b"  # text_lines가 띄어 쓸지 판단하지 못한 자리 (읽는 쪽이 정한다)


def _is_hangul(ch: str) -> bool:
    return "\uac00" <= ch <= "\ud7a3"


def _is_symbol(ch: str) -> bool:
    return len(ch) == 1 and 0xE000 <= ord(ch) <= 0xF8FF


def _symbol_only(text: str) -> bool:
    t = [ch for ch in text if not ch.isspace()]
    return bool(t) and all(_is_symbol(ch) for ch in t)


def _obj_text(obj, textpage) -> str:
    import pypdfium2.raw as raw

    n = raw.FPDFTextObj_GetText(obj.raw, textpage.raw, None, 0)
    if n <= 2:
        return ""
    buf = ctypes.create_string_buffer(n)
    raw.FPDFTextObj_GetText(obj.raw, textpage.raw, ctypes.cast(buf, ctypes.POINTER(raw.FPDF_WCHAR)), n)
    return buf.raw[: n - 2].decode("utf-16-le", "ignore")


def _narrow_spaces(line: list, right: list[float]) -> set[int]:
    """좁은 글꼴의 낱말 사이로 보이는 한글-한글 자리 (그 뒤 글자의 위치 모음).

    장평을 줄인 글꼴은 공백 폭도 좁다: 양식의 '해지정산 내역 및 동의내역'은 낱말 사이가 글자 크기의 0.11배,
    낱말 안은 0 이하였다 (보통 기준 0.15배로는 공백이 아니다). 줄 안의 다른 한글-한글 빈칸이 모두 거의 0(0.03배 이하)이고
    이 빈칸만 0.08~0.15배로 뚜렷하게 넓을 때만 고르고, 띄울지는 형태소 분석이 정한다. 거의 0인 낱말 안 빈칸이 고른
    자리보다 두 배 이상 많아야 한다: 글자 사이를 고르게 벌린 줄(한남대 공고, 낱말 안 빈칸 0.10~0.11배)은 고르지 않는다.
    """
    cand, tight, loose = set(), 0, False
    for i in range(1, len(line)):
        p, c = line[i - 1], line[i]
        if not (_is_hangul(p.text) and _is_hangul(c.text)):
            continue
        size = max(p.size, c.size, 1.0)
        gap = c.origin[0] - right[i - 1]
        if gap <= size * 0.03:
            tight += 1
        elif gap < size * 0.08:
            loose = True
        elif gap <= size * 0.15:
            cand.add(i)
    return cand if cand and not loose and tight >= 2 * len(cand) else set()


def _right_edges(chars: list[Char]) -> list[float]:
    """글자마다 실제로 차지하는 오른쪽 끝.

    보통은 글꼴의 글자 폭(advance)을 쓰고, 다음 글자가 그보다 가까이 놓였으면 다음 글자의 시작점을 쓴다.
    폭 정보가 깨진 글꼴(모든 글자 폭이 글자 크기만큼)은 같은 줄 이웃 글자들의 보통 간격을 폭으로 본다.
    그래야 장평을 줄인 글에서 단어 사이 간격을 찾을 수 있다.
    """
    n = len(chars)
    steps: list[float | None] = []
    for i, c in enumerate(chars):
        nxt = chars[i + 1] if i + 1 < n else None
        step = None
        if nxt is not None and abs(nxt.origin[1] - c.origin[1]) < c.size * 0.25 and abs(nxt.size - c.size) < 0.6:
            d = nxt.origin[0] - c.origin[0]
            if 0 < d < c.size * 1.5:
                step = d
        steps.append(step)

    out = []
    for i, c in enumerate(chars):
        adv = c.advance if c.advance > 0 else c.box[2] - c.origin[0]
        if adv >= c.size * 0.9:
            # 폭 정보가 깨졌을 수 있다. 같은 줄 이웃 중 같은 종류(반각/전각) 글자들이 글꼴 폭보다 일관되게
            # 좁게 놓였으면(깨진 폭, 장평 축소) 그 보통 간격을 폭으로 본다. 'M', 'W'처럼 원래 넓은 글자는 그대로 둔다.
            near = [j for j in range(max(0, i - 8), min(n, i + 9))
                    if steps[j] is not None and chars[j].text.isascii() == c.text.isascii()
                    and abs(chars[j].origin[1] - c.origin[1]) < c.size * 0.25 and chars[j].advance > 0]
            if len(near) >= 3:
                tight = statistics.median(steps[j] / chars[j].advance for j in near)
                if tight < 0.85:
                    adv = min(adv, statistics.median(steps[j] for j in near))
            elif c.text.isascii():
                adv = c.size * 0.55
        step = steps[i]
        if step is not None and 0 < step <= adv:
            edge = c.origin[0] + step  # 다음 글자가 바로 이어 놓였으면 그 간격이 실제 폭
        else:
            edge = c.origin[0] + max(adv, 0.0)
        # 짐작한 폭이 글자의 실제 잉크보다 좁으면 잉크 끝까지 (이웃 간격으로 줄인 'M'이 잉크보다 짧아
        # 'MVP'가 'M VP'로 띄어졌다). 상자가 글꼴 폭을 그대로 옮긴 것(깨진 글꼴은 상자도 글자 크기만큼)이면 잉크가 아니므로 쓰지 않는다
        ink = c.box[2] - c.box[0]
        if c.text.strip() and 0 < ink <= c.size * 1.2 and abs(ink - c.advance) > 0.5:
            edge = max(edge, c.box[2] - 0.3)
        out.append(edge)
    return out


def _dedupe_overlaps(chars: list[Char]) -> list[Char]:
    """그림자·외곽선 효과로 같은 글자를 거의 같은 자리에 두 번 그린 경우 한 번만 남긴다."""
    out: list[Char] = []
    seen: dict[tuple, int] = {}
    for c in chars:
        key = (c.text, round(c.origin[0] / 2), round(c.origin[1] / 2))
        if c.text.strip() and key in seen:
            continue
        seen[key] = len(out)
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# 상자 계산
# ---------------------------------------------------------------------------


def area(b: BBox) -> float:
    return max(b[2] - b[0], 0) * max(b[3] - b[1], 0)


def intersect(a: BBox, b: BBox) -> BBox | None:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def inside_ratio(inner: BBox, outer: BBox) -> float:
    """inner 중 outer 안에 들어간 비율."""
    i = intersect(inner, outer)
    a = area(inner)
    if a == 0:  # 선처럼 면적이 없는 개체
        cx, cy = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
        return 1.0 if outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3] else 0.0
    return area(i) / a if i else 0.0


def union(a: BBox, b: BBox) -> BBox:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def expand(b: BBox, d: float) -> BBox:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def cluster(boxes: list[BBox], gap: float) -> list[BBox]:
    """서로 gap 이내로 가까운 상자들을 하나로 묶는다."""
    groups = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out: list[list[float]] = []
        for b in groups:
            for o in out:
                if intersect(expand(tuple(o), gap), tuple(b)):
                    o[:] = union(tuple(o), tuple(b))
                    changed = True
                    break
            else:
                out.append(list(b))
        groups = out
    return [tuple(g) for g in groups]
