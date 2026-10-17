"""각주가 있는 시험 문서(footnotes.pdf) 생성.

본문 문장 끝에 위 첨자 번호(1, 2, 3)가 붙고 쪽 아래에 같은 번호의 각주가 있는 PDF. python-docx는 각주를 만들지 못해
LibreOffice 평문 ODT(fodt)로 쓰고 PDF로 바꾼다. 두 번째 쪽에도 각주가 있어 번호가 쪽을 넘어 이어진다.

    python tests/fixtures/make_footnotes.py   # → tests/fixtures/footnotes.pdf (LibreOffice 필요)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

NOTES = ["「전자정부법」 제2조에 따른 행정기관을 말한다.", "2026년 1월 기준 금액이며 부가가치세를 포함한다.",
         "세부 기준은 별첨 2의 평가표를 따른다."]
BODY = "본 사업은 기관 내부 문서를 통합 관리하고 검색 품질을 높이기 위하여 추진한다. " * 6


def _note(n: int, text: str) -> str:
    return (f'<text:note text:id="ftn{n}" text:note-class="footnote"><text:note-citation>{n}</text:note-citation>'
            f'<text:note-body><text:p text:style-name="Footnote">{text}</text:p></text:note-body></text:note>')


def main() -> None:
    paras = [
        f"<text:h text:outline-level=\"1\">1. 사업 개요</text:h>",
        f"<text:p>이 사업의 발주 기관은 공공기관{_note(1, NOTES[0])}이다. {BODY}</text:p>",
        f"<text:p>사업 예산은 10억 원{_note(2, NOTES[1])}이다. {BODY}</text:p>",
        "<text:p><text:soft-page-break/></text:p>",
        "<text:p text:style-name=\"Break\"/>",
        f"<text:h text:outline-level=\"1\">2. 평가 방법</text:h>",
        f"<text:p>제안서는 기술 평가와 가격 평가로 나누어 평가한다{_note(3, NOTES[2])}. {BODY}</text:p>",
    ]
    fodt = f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0"
 xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
 office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
 <office:styles>
  <style:style style:name="Break" style:family="paragraph"><style:paragraph-properties fo:break-before="page"/></style:style>
 </office:styles>
 <office:body><office:text>{''.join(paras)}</office:text></office:body>
</office:document>"""
    soffice = shutil.which("soffice") or "C:/Program Files/LibreOffice/program/soffice.exe"
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as profile:
        src = Path(work) / "footnotes.fodt"
        src.write_text(fodt, encoding="utf-8")
        subprocess.run([soffice, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--convert-to", "pdf",
                        "--outdir", work, str(src)], check=True, capture_output=True, timeout=180)
        shutil.copy(Path(work) / "footnotes.pdf", HERE / "footnotes.pdf")


if __name__ == "__main__":
    main()
