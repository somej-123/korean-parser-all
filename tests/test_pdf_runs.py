"""PDF 고정 배치의 글자 묶기 (pdfium 없이 글자 목록만으로 시험)."""

from korean_parser.parsers.pdf.page import Char, PageInfo


def ch(text, x, size=11.0, advance=None, y=100.0):
    adv = size if advance is None else advance
    return Char(text, (x, y - size, x + adv, y), size, False, False, "#000000", "", (x, y), adv)


def run_texts(chars):
    page = PageInfo(number=1, width=600, height=800, chars=chars)
    return [r.text for r in page.runs()]


def test_broken_font_widths_do_not_split_glyphs():
    # 모든 글자 폭이 글자 크기(11pt)로 나오는 글꼴: '(20)'이 글자마다 쪼개지면 안 된다
    chars = [ch("(", 111.4), ch("2", 114.5), ch("0", 120.0), ch(")", 125.5)]
    runs = run_texts(chars)
    assert runs == ["(20)"]
    page = PageInfo(number=1, width=600, height=800, chars=chars)
    assert page.runs()[0].width < 20  # 마지막 글자도 반각 폭으로 본다


def test_condensed_text_word_gaps_from_neighbor_spacing():
    # 장평을 줄여 글자 간격 8pt, 단어 사이 12pt. 글꼴 폭(11pt)만 보면 단어 사이를 못 찾는다
    xs, x = [], 0.0
    for word in ("보고", "및", "일정계획"):
        for c in word:
            xs.append(ch(c, x))
            x += 8.1
        x += 4.1
    assert run_texts(xs) == ["보고", "및", "일정계획"]


def test_generated_space_splits_and_symbols_are_skipped():
    chars = [ch("", 50, advance=13), ch("항", 70), ch("목", 81),
             Char(" ", (200, 100, 200, 100), 1.0, False, False, "#000000", "", (200, 100), 0.0),
             ch("중", 96)]
    assert run_texts(chars) == ["항목", "중"]


def test_page_order_and_table_continuation_helpers():
    from korean_parser.ir import Provenance, Row, Table
    from korean_parser.parsers.pdf import _same_table, _side_by_side

    def blk(page, box, widths=None):
        return Table(id="t", prov=Provenance(parser="t", page=page, bbox=box), col_widths_pt=widths or [])

    # 한 단 쪽: 위아래로 놓인 블록 / 다단 쪽: 같은 높이에 나란히 놓인 블록
    assert not _side_by_side([blk(1, (90, 100, 500, 120)), blk(1, (90, 130, 500, 150))])
    assert _side_by_side([blk(1, (50, 100, 280, 300)), blk(1, (300, 100, 540, 300))])
    # 쪽을 넘어 이어지는 표: 다음 쪽이고 열 너비가 같다
    a = blk(3, (84, 600, 517, 780), [108, 108, 108, 108])
    assert _same_table(a, blk(4, (84, 72, 517, 120), [108, 108, 108.5, 108]))
    assert not _same_table(a, blk(4, (84, 72, 517, 120), [80, 136, 108, 108]))
    assert not _same_table(a, blk(5, (84, 72, 517, 120), [108, 108, 108, 108]))
    assert Row  # noqa: B018


def test_ink_box_keeps_wide_letter_together():
    # 실제 제안요청서 그림 설명의 '(MVP)' (8pt). 글꼴 폭이 깨져 'M'의 폭을 이웃 간격(약 4.8pt)으로 줄이면 잉크 끝(240.5)보다
    # 짧아져 'M VP'로 띄어졌다. 상자가 글꼴 폭과 다르면(실제 잉크) 잉크 끝까지를 글자로 본다
    def c(t, box, x, adv):
        return Char(t, box, 8.0, False, False, "#000000", "", (x, 646.0), adv)

    chars = [c("트", (223.16, 640.37, 231.66, 646.26), 222.96, 9.15), c("(", (229.96, 639.69, 234.68, 647.49), 229.92, 4.76),
             c("M", (232.99, 640.4, 240.53, 646.49), 232.32, 9.15), c("V", (239.22, 640.4, 246.16, 646.49), 239.28, 7.13),
             c("P", (245.0, 640.4, 250.89, 646.49), 244.32, 7.04), c(")", (248.69, 639.69, 253.4, 647.49), 249.12, 4.63)]
    page = PageInfo(number=1, width=600, height=800, chars=chars)
    assert page.text_lines((200, 630, 300, 650)) == ["트(MVP)"]


def test_narrow_word_gaps_of_condensed_label():
    # 신청서 양식의 장평 줄인 이름표 '해지정산 내역 및 동의내역': 낱말 안 빈칸은 0, 낱말 사이는 글자 크기의 0.11배.
    # 그 자리만 '애매한 간격'으로 고른다. 글자 사이를 고르게 벌린 줄(모든 빈칸이 0.1배)은 고르지 않는다
    from korean_parser.parsers.pdf.page import _narrow_spaces, _right_edges

    def line(words, inner, between, size=8.0):
        out, x = [], 0.0
        for w in words:
            for c in w:
                out.append(ch(c, x, size=size, advance=6.0))
                x += 6.0 + inner * size
            x += (between - inner) * size
        return out

    label = line(["해지정산", "내역", "및", "동의내역"], 0.0, 0.11)
    assert _narrow_spaces(label, _right_edges(label)) == {4, 6, 7}
    tracked = line(["반드시", "열람하고", "숙지하여야"], 0.11, 0.87)
    assert _narrow_spaces(tracked, _right_edges(tracked)) == set()


def test_band_order_for_form_rows():
    # 왼쪽 구역 이름 + 오른쪽 표가 줄줄이 놓인 양식: 띠마다 이름 → 표. 진짜 2단 쪽(띠가 쪽 높이의 1/4보다 큼)은 그대로
    from korean_parser.ir import Paragraph, Provenance, Text
    from korean_parser.parsers.pdf import _band_order

    def blk(name, box):
        return Paragraph(id=name, prov=Provenance(parser="t", page=1, bbox=box), inlines=[Text(text=name)])

    docling = [blk("신청고객정보", (40, 93, 82, 98)), blk("해지사유", (40, 175, 68, 180)),
               blk("안내문", (139, 74, 324, 80)), blk("표1", (130, 87, 560, 159)), blk("표2", (130, 169, 560, 234))]
    assert [b.id for b in _band_order(docling, 842)] == ["안내문", "신청고객정보", "표1", "해지사유", "표2"]
    columns = [blk("왼쪽", (50, 100, 280, 700)), blk("오른쪽", (300, 100, 540, 700))]
    assert _band_order(columns, 842) is None


def test_heavy_fonts_by_ink_density():
    # 굵기를 이름의 숫자로만 나누는 글꼴(윤고딕 11·14): 잉크 비율로 굵게를 정한다. 실제 양식에서 잰 값 그대로.
    # KoPub Medium(0.39)은 본문보다 진해도 0.42에 못 미쳐 굵게가 아니다
    from collections import Counter

    from korean_parser.parsers.pdf.page import heavy_by_density

    form = {"YDVYGOStd11": [0.31] * 20, "YDVYGOStd14": [0.49] * 20, "YDVYGOStd12": [0.31] * 20}
    assert heavy_by_density(form, Counter(YDVYGOStd11=1289, YDVYGOStd14=392, YDVYGOStd12=40)) == {"YDVYGOStd14"}
    light = {"KoPubDotumLight": [0.28] * 20, "KoPubDotumMedium": [0.39] * 20}
    assert heavy_by_density(light, Counter(KoPubDotumLight=900, KoPubDotumMedium=300)) == set()
    assert heavy_by_density({"A": [0.5] * 20}, Counter(A=5)) == set()  # 글꼴 하나뿐이면 비교할 본문이 없다


def test_superscript_joins_its_line():
    # 기준선이 높은 작은 글자(각주 번호)는 따로 한 줄이 되지 않고 제 줄의 그 자리에 들어간다
    from korean_parser.parsers.pdf.page import PageInfo

    body = [ch(c, x, size=10.0, advance=10.0, y=100.0) for c, x in zip("기관이다", (10, 20, 34, 44))]
    sup = ch("1", 30, size=6.0, advance=3.0, y=96.0)  # '관'과 '이' 사이, 기준선 4pt 위
    page = PageInfo(number=1, width=600, height=800, chars=body[:2] + [sup] + body[2:])
    assert page.text_lines((0, 80, 200, 110)) == ["기관1이다"]
    assert [(t, b, a) for _, t, b, a in page.superscripts()] == [("1", "기관", "이다")]
