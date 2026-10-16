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
