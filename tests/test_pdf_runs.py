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
