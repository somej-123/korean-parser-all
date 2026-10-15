"""한국어 테스트 문서(korean_report.docx) 생성.

공개된 한국어 DOCX 중 라이선스가 명확한 것이 드물어서, 한국 보고서에 자주 나오는 요소를
모두 넣은 문서를 직접 만든다.

- 제목 스타일(제목 1/2), 머리말, 하이퍼링크
- 개조식(□ ○ -)과 ※ 참고
- Word 자동 번호: 1. → 가. → 1) → 가)
- 셀 병합(가로/세로), 배경색, 굵은 글씨가 있는 표
- 그림(PNG)
- 순서도: 도형 그룹 + 화살표 연결선 (DrawingML wpg/wps)

    python tests/fixtures/make_korean_report.py   # → tests/fixtures/korean_report.docx
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Pt, RGBColor
from PIL import Image, ImageDraw

OUT = Path(__file__).with_name("korean_report.docx")

NS_DECL = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)
EMU = 12700  # 1pt


def add_korean_numbering(doc) -> int:
    """1. → 가. → 1) → 가) 4단계 번호 정의를 numbering.xml에 추가하고 numId를 돌려준다."""
    numbering = doc.part.numbering_part.element
    levels = [("decimal", "%1."), ("ganada", "%2."), ("decimal", "%3)"), ("ganada", "%4)")]
    lvls = "".join(
        f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
        f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{400 + i * 400}" w:hanging="400"/></w:pPr></w:lvl>'
        for i, (fmt, text) in enumerate(levels)
    )
    abstract = parse_xml(f'<w:abstractNum {nsdecls("w")} w:abstractNumId="90">'
                         f'<w:multiLevelType w:val="multilevel"/>{lvls}</w:abstractNum>')
    first_num = numbering.find(qn("w:num"))
    if first_num is not None:
        first_num.addprevious(abstract)
    else:
        numbering.append(abstract)
    numbering.append(parse_xml(f'<w:num {nsdecls("w")} w:numId="90"><w:abstractNumId w:val="90"/></w:num>'))
    return 90


def numbered(doc, text: str, level: int, num_id: int):
    p = doc.add_paragraph(text)
    ppr = p._p.get_or_add_pPr()
    ppr.append(parse_xml(f'<w:numPr {nsdecls("w")}><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'))
    return p


def shape_xml(sid: int, name: str, geom: str, x, y, w, h, fill: str, text: str = "") -> str:
    txbx = ""
    if text:
        txbx = (
            '<wps:txbx><w:txbxContent><w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
            f'<w:r><w:rPr><w:b/><w:color w:val="FFFFFF"/><w:sz w:val="20"/></w:rPr><w:t>{text}</w:t></w:r>'
            "</w:p></w:txbxContent></wps:txbx>"
        )
    return (
        f'<wps:wsp><wps:cNvPr id="{sid}" name="{name}"/><wps:cNvSpPr/>'
        f'<wps:spPr><a:xfrm><a:off x="{x * EMU}" y="{y * EMU}"/><a:ext cx="{w * EMU}" cy="{h * EMU}"/></a:xfrm>'
        f'<a:prstGeom prst="{geom}"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
        f'<a:ln w="12700"><a:solidFill><a:srgbClr val="1F3864"/></a:solidFill></a:ln></wps:spPr>'
        f'{txbx}<wps:bodyPr anchor="ctr" lIns="45720" rIns="45720"/></wps:wsp>'
    )


def connector_xml(sid: int, start: int, end: int, x, y, w, h, flip_v: bool = False) -> str:
    flip = ' flipV="1"' if flip_v else ""
    return (
        f'<wps:wsp><wps:cNvPr id="{sid}" name="연결선 {sid}"/>'
        f'<wps:cNvCnPr><a:stCxn id="{start}" idx="3"/><a:endCxn id="{end}" idx="1"/></wps:cNvCnPr>'
        f'<wps:spPr><a:xfrm{flip}><a:off x="{x * EMU}" y="{y * EMU}"/><a:ext cx="{w * EMU}" cy="{h * EMU}"/></a:xfrm>'
        f'<a:prstGeom prst="straightConnector1"><a:avLst/></a:prstGeom>'
        f'<a:ln w="19050"><a:solidFill><a:srgbClr val="404040"/></a:solidFill><a:tailEnd type="triangle"/></a:ln>'
        f"</wps:spPr><wps:bodyPr/></wps:wsp>"
    )


def add_flowchart(doc) -> None:
    """신청 → 검토 → (승인 / 반려) 순서도."""
    W, H = 430, 150
    children = [
        shape_xml(11, "신청", "flowChartTerminator", 0, 50, 90, 40, "2E75B6", "신청"),
        shape_xml(12, "검토", "flowChartProcess", 140, 50, 100, 40, "2E75B6", "부서 검토"),
        shape_xml(13, "판단", "flowChartDecision", 290, 35, 110, 70, "C55A11", "적합?"),
        shape_xml(14, "승인", "roundRect", 300, 0, 90, 28, "548235", "승인"),
        shape_xml(15, "반려", "roundRect", 300, 118, 90, 28, "A5A5A5", "반려"),
        connector_xml(21, 11, 12, 90, 70, 50, 0),
        connector_xml(22, 12, 13, 240, 70, 50, 0),
        connector_xml(23, 13, 14, 345, 28, 0, 7, flip_v=True),
        connector_xml(24, 13, 15, 345, 105, 0, 13),
    ]
    group = (
        f'<wpg:wgp><wpg:cNvGrpSpPr/><wpg:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{W * EMU}" cy="{H * EMU}"/>'
        f'<a:chOff x="0" y="0"/><a:chExt cx="{W * EMU}" cy="{H * EMU}"/></a:xfrm></wpg:grpSpPr>'
        f"{''.join(children)}</wpg:wgp>"
    )
    drawing = (
        f'<w:r {NS_DECL}><mc:AlternateContent><mc:Choice Requires="wpg"><w:drawing>'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{W * EMU}" cy="{H * EMU}"/>'
        f'<wp:docPr id="100" name="순서도" descr="결재 처리 순서도"/><wp:cNvGraphicFramePr/>'
        f'<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup">'
        f"{group}</a:graphicData></a:graphic></wp:inline></w:drawing></mc:Choice><mc:Fallback/></mc:AlternateContent></w:r>"
    )
    p = doc.add_paragraph()
    p._p.append(parse_xml(drawing))


def chart_png() -> io.BytesIO:
    img = Image.new("RGB", (480, 240), "white")
    d = ImageDraw.Draw(img)
    for i, v in enumerate([120, 150, 170, 210]):
        x = 60 + i * 100
        d.rectangle([x, 220 - v, x + 50, 220], fill=(46, 117, 182))
    d.line([40, 220, 460, 220], fill="black", width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


def add_hyperlink(paragraph, text: str, url: str) -> None:
    rid = paragraph.part.relate_to(
        url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True
    )
    paragraph._p.append(parse_xml(
        f'<w:hyperlink {nsdecls("w", "r")} r:id="{rid}"><w:r><w:rPr><w:color w:val="0563C1"/><w:u w:val="single"/>'
        f"</w:rPr><w:t>{text}</w:t></w:r></w:hyperlink>"
    ))


def shade(cell, color: str) -> None:
    cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="{color}"/>'))


def build() -> Path:
    doc = Document()
    doc.core_properties.title = "2026년 문서 파싱 고도화 계획"
    doc.core_properties.author = "korean-parser"
    doc.core_properties.created = datetime(2026, 10, 15, 9, 0)
    style = doc.styles["Normal"]
    style.font.size = Pt(10.5)
    style.font.name = "맑은 고딕"
    style.element.rPr.get_or_add_rFonts().set(qn("w:eastAsia"), "맑은 고딕")

    doc.sections[0].header.paragraphs[0].text = "대외비 · 문서 파싱 TF"
    doc.sections[0].header.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.add_heading("2026년 문서 파싱 고도화 계획", 0)
    p = doc.add_paragraph("작성: 문서 파싱 TF  |  참고: ")
    add_hyperlink(p, "Docling 저장소", "https://github.com/docling-project/docling")

    doc.add_heading("1. 추진 배경", 1)
    for text in [
        "□ 문서 파싱 품질 문제",
        "○ 표 구조 손실: 병합 셀이 Markdown 변환 과정에서 사라짐",
        "- 예산표, 결재란 등 업무 문서의 핵심 정보 누락",
        "- 화면 표시와 LLM 입력 모두 품질 저하",
        "○ 한국어 띄어쓰기 소실",
        "※ 디지털 PDF 기준 어절 일치율 11.4%",
        "□ 처리 속도 문제",
        "○ 스캔 문서 OCR이 전체 처리 시간의 대부분을 차지",
    ]:
        doc.add_paragraph(text)

    doc.add_heading("2. 추진 계획", 1)
    num = add_korean_numbering(doc)
    numbered(doc, "통합 문서 모델 구축", 0, num)
    numbered(doc, "DOCX 파서 개발", 1, num)
    numbered(doc, "문단과 서식", 2, num)
    numbered(doc, "자동 번호", 3, num)
    numbered(doc, "셀 병합", 3, num)
    numbered(doc, "도형과 순서도", 2, num)
    numbered(doc, "HWPX 파서 개발", 1, num)
    numbered(doc, "화면 표시 품질 검증", 0, num)

    doc.add_heading("3. 예산 계획", 1)
    t = doc.add_table(rows=5, cols=4)
    t.style = "Table Grid"
    data = [
        ["구분", "2026년", "", ""],
        ["", "상반기", "하반기", "합계"],
        ["인건비", "10", "12", "22"],
        ["장비비", "5", "7", "12"],
        ["합계", "15", "19", "34"],
    ]
    for r, row in enumerate(data):
        for c, v in enumerate(row):
            t.cell(r, c).text = v
    t.cell(0, 0).merge(t.cell(1, 0)).text = "구분"
    t.cell(0, 1).merge(t.cell(0, 3)).text = "2026년"
    for r in (0, 1):
        for c in range(4):
            shade(t.cell(r, c), "D9E2F3")
            for run in t.cell(r, c).paragraphs[0].runs:
                run.bold = True
    for c in range(4):
        shade(t.cell(4, c), "F2F2F2")
    doc.add_paragraph("(단위: 억원)").alignment = WD_ALIGN_PARAGRAPH.RIGHT

    doc.add_heading("4. 실적 추이", 1)
    doc.add_picture(chart_png(), width=Inches(4))
    cap = doc.add_paragraph("그림 1. 분기별 처리 건수")
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_heading("5. 결재 절차", 1)
    add_flowchart(doc)
    note = doc.add_paragraph()
    run = note.add_run("※ 반려 시 신청 단계로 돌아갑니다.")
    run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
    run.italic = True

    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    print(build())
