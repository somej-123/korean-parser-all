import unicodedata

from korean_parser import inspect_text, normalize_text


def test_nfd_hangul_is_composed():
    nfd = unicodedata.normalize("NFD", "한글 문서")
    assert len(nfd) > len("한글 문서")
    assert normalize_text(nfd) == "한글 문서"


def test_invisible_and_special_spaces():
    assert normalize_text("문서​파싱") == "문서파싱"
    assert normalize_text("본 문서　요약") == "본 문서 요약"
    assert normalize_text("본   문서") == "본 문서"


def test_middle_dot_variants_unified():
    assert normalize_text("설계ㆍ구현∙검증") == "설계·구현·검증"


def test_bullet_is_not_a_middle_dot():
    assert normalize_text("• 항목") == "• 항목"


def test_fullwidth_ascii_but_not_other_compat_chars():
    assert normalize_text("ＡＢＣ１２３！") == "ABC123!"
    assert normalize_text("① 면적 30㎡") == "① 면적 30㎡"


def test_pua_mapping():
    assert normalize_text("옛글", pua_map={"": "ᄒᆞᆫ"}) == unicodedata.normalize("NFC", "옛ᄒᆞᆫ글")


def test_garbled_detection():
    assert not inspect_text("정상적인 한국어 문서입니다.").garbled
    assert inspect_text(" 문서 ��").garbled
