"""v1.0 기준 3(필수 요소) 확인용 DOCX 표본과 '들어 있어야 할 요소 목록'(manifest)을 만든다.

우리 파서와 무관한 프로그램이 만든 문서여야 하므로, LibreOffice(평문 ODT → DOCX)와 python-docx로 만든다.
목록(같은 이름의 .json)은 문서를 만든 내용 그대로 이 스크립트가 함께 쓴다 (benchmarks/goal/elements.py가 확인한다).

    python benchmarks/goal/samples/make_docx.py   # → benchmarks/goal/samples/*.docx, *.json (LibreOffice 필요)
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
      'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
      'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
      'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
      'xmlns:xlink="http://www.w3.org/1999/xlink" '
      'xmlns:math="http://www.w3.org/1998/Math/MathML"')


def _png() -> str:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (160, 100), (240, 244, 250))
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 150, 90), outline=(40, 80, 160), width=3)
    d.line((10, 90, 80, 30, 150, 60), fill=(200, 60, 60), width=3)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _soffice() -> str:
    return shutil.which("soffice") or "C:/Program Files/LibreOffice/program/soffice.exe"


def _fodt_to_docx(fodt: str, name: str) -> None:
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as profile:
        src = Path(work) / f"{name}.fodt"
        src.write_text(fodt, encoding="utf-8")
        subprocess.run([_soffice(), f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--convert-to",
                        "docx:MS Word 2007 XML", "--outdir", work, str(src)], check=True, capture_output=True, timeout=180)
        shutil.copy(Path(work) / f"{name}.docx", HERE / f"{name}.docx")


def _write_manifest(name: str, m: dict) -> None:
    (HERE / f"{name}.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def _note(n: int, text: str, kind: str = "footnote") -> str:
    return (f'<text:note text:id="n{kind}{n}" text:note-class="{kind}"><text:note-citation>{n}</text:note-citation>'
            f"<text:note-body><text:p>{text}</text:p></text:note-body></text:note>")


def _list(style: str, items: list) -> str:
    """items: 글 또는 (글, 하위 목록) — 하위 목록은 같은 목록 안 한 단계 아래."""
    out = [f'<text:list text:style-name="{style}">']
    for it in items:
        text, sub = (it, None) if isinstance(it, str) else it
        out.append(f"<text:list-item><text:p>{text}</text:p>")
        if sub:
            out.append("<text:list>" + "".join(f"<text:list-item><text:p>{s}</text:p></text:list-item>" for s in sub)
                       + "</text:list>")
        out.append("</text:list-item>")
    out.append("</text:list>")
    return "".join(out)


def _doc(body: str, header: str, footer: str, outline: str = "") -> str:
    bullets = "".join(
        f'<text:list-level-style-bullet text:level="{i + 1}" text:bullet-char="{ch}">'
        f'<style:list-level-properties text:space-before="{0.6 * i:.1f}cm" text:min-label-width="0.6cm"/>'
        f"</text:list-level-style-bullet>" for i, ch in enumerate("□○-"))
    numbers = ('<text:list-level-style-number text:level="1" style:num-suffix="." style:num-format="1">'
               '<style:list-level-properties text:min-label-width="0.6cm"/></text:list-level-style-number>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<office:document {NS} office:version="1.3" office:mimetype="application/vnd.oasis.opendocument.text">
 <office:styles>
  <style:style style:name="Standard" style:family="paragraph"><style:text-properties fo:font-size="11pt"/></style:style>
  <style:style style:name="Title" style:family="paragraph"><style:text-properties fo:font-size="20pt" fo:font-weight="bold"/></style:style>
  <style:style style:name="Heading_20_1" style:display-name="Heading 1" style:family="paragraph" style:default-outline-level="1"><style:text-properties fo:font-size="16pt" fo:font-weight="bold"/></style:style>
  <style:style style:name="Heading_20_2" style:display-name="Heading 2" style:family="paragraph" style:default-outline-level="2"><style:text-properties fo:font-size="14pt" fo:font-weight="bold"/></style:style>
  <style:style style:name="Heading_20_3" style:display-name="Heading 3" style:family="paragraph" style:default-outline-level="3"><style:text-properties fo:font-size="12pt" fo:font-weight="bold"/></style:style>
  <text:list-style style:name="Bul">{bullets}</text:list-style>
  <text:list-style style:name="Num">{numbers}</text:list-style>
  {outline}
 </office:styles>
 <office:automatic-styles>
  <style:style style:name="Head" style:family="table-cell"><style:table-cell-properties fo:background-color="#dde7f3" fo:border="0.5pt solid #000000"/></style:style>
  <style:style style:name="Cell" style:family="table-cell"><style:table-cell-properties fo:border="0.5pt solid #000000"/></style:style>
  <style:style style:name="Break" style:family="paragraph" style:parent-style-name="Standard"><style:paragraph-properties fo:break-before="page"/></style:style>
  <style:page-layout style:name="PL"><style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" fo:margin="2cm"/></style:page-layout>
 </office:automatic-styles>
 <office:master-styles>
  <style:master-page style:name="Standard" style:page-layout-name="PL">
   <style:header><text:p>{header}</text:p></style:header>
   <style:footer><text:p>{footer}</text:p></style:footer>
  </style:master-page>
 </office:master-styles>
 <office:body><office:text>{body}</office:text></office:body>
</office:document>"""


def _table(rows: list[list], head_rows: int = 1) -> str:
    """rows: 칸 글, 또는 (글, 행 수, 열 수). None은 병합에 덮인 칸."""
    cols = max(sum((c[2] if isinstance(c, tuple) else 1) for c in r if c is not None) for r in rows)
    out = [f'<table:table table:name="T{abs(hash(str(rows))) % 1000}"><table:table-column table:number-columns-repeated="{cols}"/>']
    for i, row in enumerate(rows):
        out.append("<table:table-row>")
        for c in row:
            style = "Head" if i < head_rows else "Cell"
            if c is None:
                out.append("<table:covered-table-cell/>")
            elif isinstance(c, tuple):
                text, rs, cs = c
                out.append(f'<table:table-cell table:style-name="{style}" table:number-rows-spanned="{rs}" '
                           f'table:number-columns-spanned="{cs}"><text:p>{text}</text:p></table:table-cell>')
            else:
                out.append(f'<table:table-cell table:style-name="{style}"><text:p>{c}</text:p></table:table-cell>')
        out.append("</table:table-row>")
    out.append("</table:table>")
    return "".join(out)


def make_rfp() -> None:
    """제안요청서: 제목 단계, 개조식 목록, 번호 목록, 병합 표, 각주, 머리말·꼬리말, 링크, 그림, 글상자, 수식."""
    png = _png()
    eq = ('<draw:frame draw:name="수식1" text:anchor-type="as-char" svg:width="3cm" svg:height="0.6cm"><draw:object>'
          '<math:math><math:semantics><math:mrow><math:mi>E</math:mi><math:mo>=</math:mo><math:mi>m</math:mi>'
          '<math:msup><math:mi>c</math:mi><math:mn>2</math:mn></math:msup></math:mrow>'
          '<math:annotation math:encoding="StarMath 5.0">E = m c^2</math:annotation></math:semantics></math:math>'
          "</draw:object></draw:frame>")
    body = "".join([
        '<text:p text:style-name="Title">2026년 문서 관리 시스템 구축 제안요청서</text:p>',
        '<text:h text:style-name="Heading_20_1" text:outline-level="1">Ⅰ. 사업 개요</text:h>',
        '<text:h text:style-name="Heading_20_2" text:outline-level="2">1. 추진 배경</text:h>',
        f"<text:p>기관 내부 문서가 여러 시스템에 흩어져 있어 검색과 재사용이 어렵다{_note(1, '2025년 정보화 실태 조사 결과를 따른다.')}.</text:p>",
        _list("Bul", [("현행 시스템 노후화", ["서버 도입 후 9년 경과", "운영체제 지원 종료"]), "문서 형식 다양화"]),
        '<text:h text:style-name="Heading_20_2" text:outline-level="2">2. 사업 범위</text:h>',
        _list("Num", ["문서 수집·변환 기능 구축", "통합 검색 기능 구축", "운영 및 하자보수"]),
        '<text:h text:style-name="Heading_20_3" text:outline-level="3">가. 예산</text:h>',
        "<text:p>(단위: 백만원)</text:p>",
        _table([[("구분", 2, 1), ("2026년", 1, 3), None, None],
                [None, "상반기", "하반기", "합계"],
                ["인건비", "120", "180", "300"],
                ["장비비", "80", "40", "120"]], head_rows=2),
        f"<text:p>총 사업비는 4억 2천만원{_note(2, '부가가치세를 포함한 금액이다.')}이다.</text:p>",
        '<text:h text:style-name="Heading_20_1" text:outline-level="1">Ⅱ. 제안 안내</text:h>',
        '<text:p>입찰 공고는 <text:a xlink:type="simple" xlink:href="https://www.g2b.go.kr">국가종합전자조달시스템</text:a>에서 확인한다.</text:p>',
        '<text:p>시스템 구성도: <draw:frame draw:name="그림1" text:anchor-type="as-char" svg:width="4cm" svg:height="2.5cm">'
        f'<draw:image><office:binary-data>{png}</office:binary-data></draw:image><svg:title>시스템 구성도</svg:title></draw:frame></text:p>',
        '<text:p><draw:frame draw:name="글상자1" text:anchor-type="paragraph" svg:width="9cm" svg:height="1.5cm" svg:x="2cm" svg:y="0cm">'
        "<draw:text-box><text:p>※ 제안서는 기한 내 전자 제출한다.</text:p></draw:text-box></draw:frame>평가 산식은 다음과 같다.</text:p>",
        f"<text:p>성능 지표: {eq}</text:p>",
    ])
    _fodt_to_docx(_doc(body, "공공 사업 제안요청서", "- 대외비 -"), "rfp_lo")
    _write_manifest("rfp_lo", {
        "headings": [["2026년 문서 관리 시스템 구축 제안요청서", 1], ["Ⅰ. 사업 개요", 1], ["1. 추진 배경", 2],
                     ["2. 사업 범위", 2], ["가. 예산", 3], ["Ⅱ. 제안 안내", 1]],
        "list_items": [["□", "현행 시스템 노후화"], ["○", "서버 도입 후 9년 경과"], ["○", "운영체제 지원 종료"],
                       ["□", "문서 형식 다양화"], ["1.", "문서 수집·변환 기능 구축"], ["2.", "통합 검색 기능 구축"],
                       ["3.", "운영 및 하자보수"]],
        "tables": [{"rows": 4, "cols": 4, "spans": [[0, 0, 2, 1], [0, 1, 1, 3]]}],
        "footnotes": ["2025년 정보화 실태 조사 결과를 따른다.", "부가가치세를 포함한 금액이다."],
        "header": ["공공 사업 제안요청서"], "footer": ["- 대외비 -"],
        "links": [["국가종합전자조달시스템", "https://www.g2b.go.kr"]],
        "images": 1,
        "equations": ["E"],
        "texts": ["※ 제안서는 기한 내 전자 제출한다.", "평가 산식은 다음과 같다.", "(단위: 백만원)", "인건비", "장비비"],
    })


def make_report() -> None:
    """보고서: 자동 개요 번호(1, 1.1), 미주, 쪽을 넘는 긴 표, 쪽 나누기."""
    outline = ('<text:outline-style style:name="Outline">'
               '<text:outline-level-style text:level="1" style:num-format="1" style:num-suffix="."/>'
               '<text:outline-level-style text:level="2" style:num-format="1" text:display-levels="2"/>'
               "</text:outline-style>")
    long_rows = [["번호", "점검 항목", "결과"]] + [[str(i), f"점검 항목 {i}", "적합" if i % 3 else "보완"] for i in range(1, 46)]
    body = "".join([
        '<text:h text:style-name="Heading_20_1" text:outline-level="1">점검 개요</text:h>',
        f"<text:p>본 보고서는 2026년 상반기 정보시스템 보안 점검 결과를 정리한 것이다{_note(1, '점검 기준은 내부 지침 제12조를 따른다.', 'endnote')}.</text:p>",
        '<text:h text:style-name="Heading_20_2" text:outline-level="2">점검 대상</text:h>',
        "<text:p>대상은 업무 서버 45대이며 결과는 아래 표와 같다.</text:p>",
        _table(long_rows),
        '<text:h text:style-name="Heading_20_1" text:outline-level="1">조치 계획</text:h>',
        "<text:p>보완 판정 항목은 다음 분기까지 조치한다.</text:p>",
    ])
    _fodt_to_docx(_doc(body, "정보보안 점검 보고서", "정보화담당관", outline), "report_lo")
    _write_manifest("report_lo", {
        "headings": [["점검 개요", 1], ["점검 대상", 2], ["조치 계획", 1]],
        "tables": [{"rows": 46, "cols": 3, "spans": []}],
        "footnotes": ["점검 기준은 내부 지침 제12조를 따른다."],
        "header": ["정보보안 점검 보고서"], "footer": ["정보화담당관"],
        "texts": ["점검 항목 45", "보완 판정 항목은 다음 분기까지 조치한다."],
    })


def make_minutes() -> None:
    """회의록 (python-docx): 제목 스타일, 글머리·번호 목록 스타일, 병합 표, 머리말·꼬리말, 그림과 대체 글, 링크."""
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    doc = Document()
    doc.core_properties.title = "정보화 추진위원회 회의록"
    doc.core_properties.author = "정보화담당관"
    sec = doc.sections[0]
    sec.header.paragraphs[0].text = "정보화 추진위원회"
    sec.footer.paragraphs[0].text = "내부 회의 자료"
    doc.add_heading("정보화 추진위원회 회의록", level=1)
    doc.add_heading("1. 회의 개요", level=2)
    t = doc.add_table(rows=3, cols=4)
    t.style = "Table Grid"
    vals = [["일시", "2026. 3. 5.(목) 14:00", "장소", "본관 3층 회의실"],
            ["참석", "위원장 외 7명", "", ""],
            ["안건", "문서 관리 시스템 구축 계획 심의", "", ""]]
    for r, row in enumerate(vals):
        for c, v in enumerate(row):
            if v:
                t.cell(r, c).text = v
    t.cell(1, 1).merge(t.cell(1, 3))
    t.cell(2, 1).merge(t.cell(2, 3))
    doc.add_heading("2. 논의 내용", level=2)
    for s in ("사업 범위를 1단계와 2단계로 나눈다", "1단계는 문서 수집과 변환에 집중한다"):
        doc.add_paragraph(s, style="List Bullet")
    for s in ("예산안은 원안대로 의결", "세부 일정은 다음 회의에서 확정"):
        doc.add_paragraph(s, style="List Number")
    p = doc.add_paragraph("관련 자료: ")
    rid = p.part.relate_to("https://www.example.go.kr/meeting", RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    t_el = OxmlElement("w:t")
    t_el.text = "회의 자료실"
    run.append(t_el)
    link.append(run)
    p._p.append(link)
    buf = io.BytesIO(base64.b64decode(_png()))
    doc.add_picture(buf, width=Cm(4))
    doc.inline_shapes[0]._inline.docPr.set("descr", "회의 진행 사진")
    doc.save(HERE / "minutes_pydocx.docx")
    _write_manifest("minutes_pydocx", {
        "headings": [["정보화 추진위원회 회의록", 1], ["1. 회의 개요", 2], ["2. 논의 내용", 2]],
        "list_items": [["•", "사업 범위를 1단계와 2단계로 나눈다"], ["•", "1단계는 문서 수집과 변환에 집중한다"],
                       ["1.", "예산안은 원안대로 의결"], ["2.", "세부 일정은 다음 회의에서 확정"]],
        "tables": [{"rows": 3, "cols": 4, "spans": [[1, 1, 1, 3], [2, 1, 1, 3]]}],
        "header": ["정보화 추진위원회"], "footer": ["내부 회의 자료"],
        "links": [["회의 자료실", "https://www.example.go.kr/meeting"]],
        "images": 1,
        "texts": ["본관 3층 회의실", "문서 관리 시스템 구축 계획 심의"],
    })


if __name__ == "__main__":
    make_rfp()
    make_report()
    make_minutes()
    print("만든 표본:", sorted(p.name for p in HERE.glob("*.docx")))
