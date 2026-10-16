"""실제 문서(samples_rfp/)로 하는 시험. 폴더에 문서가 있을 때만 돈다.

나라장터·기관 공고의 첨부 파일은 재배포 조건이 불분명해 저장소에 넣지 않는다 (.gitignore). 로컬에서 공고 첨부
파일(.hwp, .pdf …)을 samples_rfp/에 두면 이 시험이 그 문서들로 돈다. PDF는 Docling을 돌려 문서당 10~40초 걸린다.

- 어떤 형식이든 오류 없이 열리고 블록이 나온다
- HWP: 한컴오피스 미리보기 글(PrvText)의 앞부분이 결과에 그대로 나온다
- PDF: 텍스트 레이어 글자의 97% 이상이 결과에 남는다 (쪽 번호는 일부러 빼므로 100%는 아니다).
  목차를 표로 만들지 않는다
"""

import collections
import unicodedata
from pathlib import Path

import pytest

from korean_parser.ir import Heading, Paragraph, Table, _walk
from korean_parser.parsers import parse

FOLDER = Path(__file__).resolve().parents[1] / "samples_rfp"
HWP = sorted(FOLDER.glob("*.hwp"))
PDF = sorted(FOLDER.glob("*.pdf"))


def _name(p: Path) -> str:
    return p.name[:24]


def _all_text(doc) -> str:
    parts = []
    for blocks in [doc.header, doc.blocks, doc.footer] + [n.blocks for n in doc.notes.values()]:
        for b in _walk(blocks):
            if hasattr(b, "inlines"):
                parts.append("".join(getattr(i, "text", "") for i in b.inlines))
            for key in ("inner_text", "caption"):
                if getattr(b, key, None):
                    parts.append(getattr(b, key))
    return "".join(parts)


def _norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFKC", s).split())


@pytest.mark.skipif(not HWP, reason="samples_rfp/*.hwp 없음")
@pytest.mark.parametrize("path", HWP, ids=_name)
def test_hwp_preview_text(path):
    import olefile

    with olefile.OleFileIO(str(path)) as ole:
        preview = ole.openstream("PrvText").read().decode("utf-16-le", "ignore")
    want = "".join(ch for ch in preview if ch.isalnum())[:100]
    doc = parse(path)
    got = "".join(ch for ch in " ".join(b.text for b in doc.iter_blocks() if isinstance(b, (Heading, Paragraph)))
                  if ch.isalnum())
    assert doc.blocks and want in got


@pytest.mark.skipif(not PDF, reason="samples_rfp/*.pdf 없음")
@pytest.mark.parametrize("path", PDF, ids=_name)
def test_pdf_keeps_text_layer(path):
    pytest.importorskip("docling")
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    layer = _norm("".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf))))
    pdf.close()
    doc = parse(path)
    ours = _norm(_all_text(doc))
    a, b = collections.Counter(layer), collections.Counter(ours)
    kept = sum((a & b).values()) / max(len(layer), 1)
    assert kept >= 0.97, (kept, (a - b).most_common(10))
    # 목차 줄('제목 ···· 쪽')이 표 칸에 들어가지 않는다
    for t in doc.iter_blocks():
        if isinstance(t, Table):
            cells = " ".join(p.text for r in t.rows for c in r.cells for p in c.blocks if hasattr(p, "text"))
            assert cells.count("·····") < 3, cells[:80]
