"""추정 제목(heading_guess): 굵고 큰 번호 문단에 제목 추정만 붙이고 문단은 그대로 둔다."""

from korean_parser.enrich import enrich_korean
from korean_parser.ir import Document, Heading, Paragraph, Text, TextStyle


def para(text: str, size: float = 11, bold: bool = False) -> Paragraph:
    return Paragraph(inlines=[Text(text=text, style=TextStyle(size_pt=size, bold=bold or None))])


BODY = "본 사업은 기관 내부 문서를 통합 관리하고 검색 품질을 높이기 위하여 추진한다."


def guesses(blocks) -> list[tuple[str, int | None]]:
    doc = Document(blocks=blocks)
    enrich_korean(doc)
    return [(b.text, b.heading_guess.level if b.heading_guess else None) for b in doc.blocks if isinstance(b, Paragraph)]


def test_bold_big_numbered_paragraphs_get_levels():
    out = guesses([para("Ⅰ. 사업 개요", 18, True), para("1. 추진 목표", 14, True), para(BODY), para(BODY),
                   para("2. 사업 범위", 14, True), para(BODY), para("Ⅱ. 제안 요청 내용", 18, True)])
    assert out == [("Ⅰ. 사업 개요", 1), ("1. 추진 목표", 2), (BODY, None), (BODY, None), ("2. 사업 범위", 2),
                   (BODY, None), ("Ⅱ. 제안 요청 내용", 1)]


def test_paragraph_stays_paragraph_with_reason():
    doc = Document(blocks=[para("Ⅰ. 사업 개요", 18, True), para(BODY), para(BODY)])
    enrich_korean(doc)
    first = doc.blocks[0]
    assert isinstance(first, Paragraph) and first.text == "Ⅰ. 사업 개요"
    assert first.heading_guess.reason == "번호 'Ⅰ.' + 굵게 + 18pt (본문 11pt)"


def test_not_titles():
    out = dict(guesses([
        para(BODY), para(BODY), para(BODY),
        para("□ 굵게 강조한 항목", 11, True),  # 기호는 굵기만으로는 제목이 아니다 (강조)
        para("※ 참고 사항", 16, True),  # ※는 제목이 아니다
        para("1. 제안서는 5부를 제출한다.", 14, True),  # 문장으로 끝난다
        para("2. 사업기간 : 계약일로부터 8개월", 14, True),  # 항목 : 값
        para("3. 사업 개요 ··········· 3", 14, True),  # 목차 줄
        para("4. 본문과 같은 크기의 번호 문단", 11),  # 굵지도 크지도 않다
    ]))
    assert all(level is None for level in out.values()), out


def test_big_bullet_is_title_and_small_step_counts_for_numbers():
    # 기호는 1.1배 이상 커야 제목, 번호는 1.05배(그리고 0.9pt 넘게) 커도 제목 (본문 13pt에 14pt 절 제목)
    out = dict(guesses([para(BODY, 13), para(BODY, 13), para("□ 추진 배경", 15), para("○ 조금 큰 기호", 14),
                        para("1. 과업 개요", 14)]))
    assert out["□ 추진 배경"] is not None and out["○ 조금 큰 기호"] is None and out["1. 과업 개요"] is not None


def test_level_continues_below_real_heading():
    doc = Document(blocks=[Heading(level=2, inlines=[Text(text="사업 개요")]), para("1. 추진 목표", 14, True),
                           para(BODY), para(BODY)])
    enrich_korean(doc)
    assert doc.blocks[1].heading_guess.level == 3
