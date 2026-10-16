"""PPTX·XLSX 시험 문서(sample.pptx, sample.xlsx) 생성.

라이선스가 분명한 한국어 PPTX·XLSX가 드물어 직접 만든다 (python-pptx, openpyxl).

PPTX: 제목 슬라이드, 단계가 있는 글머리 목록, 병합 칸이 있는 표, 막대 차트, 그림, 묶음 안 글상자, 발표자 메모, 바닥글
PPTX(plain): 자리 표시자 없이 글상자로만 만든 슬라이드 4장 (큰 글씨 제목, 두 단 비교, 슬라이드마다 같은 바닥글과 쪽 번호,
             '방법 N' 이름표, 파일 경로가 대체 텍스트인 그림) — 실제 PptxGenJS 문서를 흉내 낸다
PPTX(diagram): 연결선으로 이은 조직도(위 상자 하나, 아래 상자 둘, 연결선 없이 같은 너비로 쌓은 상자 하나),
               연결 정보 없이 끝점만 닿은 화살표, 글 없는 장식 사각형 — 실제 화면설계서의 '메뉴구조도'를 흉내 낸다
XLSX: 시트 두 개, 가로·세로 병합, 숫자·날짜·백분율·천 단위 표시 형식, 수식(저장된 값), 굵은 머리 행과 칸 배경, 숨긴 시트,
      값 캐시 없는 막대 차트(openpyxl은 캐시를 쓰지 않는다)

    python tests/fixtures/make_office.py   # → tests/fixtures/sample.pptx, sample_plain.pptx, sample_diagram.pptx, sample.xlsx
"""

from __future__ import annotations

import datetime
import io
from pathlib import Path

HERE = Path(__file__).parent


def make_pptx() -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Pt
    from pptx.util import Emu

    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[0])  # 제목 슬라이드
    s1.shapes.title.text = "2026년 문서 파싱 고도화 계획"
    s1.placeholders[1].text = "디지털혁신팀"

    s2 = prs.slides.add_slide(prs.slide_layouts[1])  # 제목 + 내용
    s2.shapes.title.text = "추진 배경"
    body = s2.placeholders[1].text_frame
    body.text = "문서 파싱 품질 문제"
    for text, level in [("표 구조 손실", 1), ("띄어쓰기 소실", 1), ("처리 속도 문제", 0)]:
        p = body.add_paragraph()
        p.text, p.level = text, level
    s2.notes_slide.notes_text_frame.text = "발표 시 띄어쓰기 사례를 함께 보여 줄 것"

    s3 = prs.slides.add_slide(prs.slide_layouts[5])  # 제목만
    s3.shapes.title.text = "예산 계획"
    table = s3.shapes.add_table(4, 3, Emu(457200), Emu(1600200), Emu(6000000), Emu(1600000)).table
    cells = [["구분", "2026년", ""], ["", "상반기", "하반기"], ["인건비", "10", "12"], ["장비비", "5", "7"]]
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            if text:
                table.cell(r, c).text = text
    table.cell(0, 1).merge(table.cell(0, 2))  # 가로 병합
    table.cell(0, 0).merge(table.cell(1, 0))  # 세로 병합

    s4 = prs.slides.add_slide(prs.slide_layouts[5])
    s4.shapes.title.text = "분기별 처리량"
    data = CategoryChartData()
    data.categories = ["1분기", "2분기", "3분기"]
    data.add_series("처리 문서 수", (120, 180, 240))
    s4.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Emu(457200), Emu(1600200), Emu(6000000), Emu(3000000), data)
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (30, 120, 200)).save(buf, "PNG")
    pic = s4.shapes.add_picture(io.BytesIO(buf.getvalue()), Emu(7000000), Emu(1600200), Emu(1200000), Emu(900000))
    pic._element.nvPicPr.cNvPr.set("descr", "처리 흐름 그림")
    group = s4.shapes.add_group_shape()
    box = group.shapes.add_textbox(Emu(457200), Emu(4800000), Emu(4000000), Emu(500000))
    box.text_frame.text = "※ 자료: 내부 집계"
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
    prs.save(HERE / "sample.pptx")


def make_plain_pptx() -> None:
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Emu, Pt

    prs = Presentation()
    blank = prs.slide_layouts[6]
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (200, 160, 20)).save(buf, "PNG")

    def box(slide, text, x, y, w, h, size):
        tb = slide.shapes.add_textbox(Emu(int(x * 12700)), Emu(int(y * 12700)), Emu(int(w * 12700)), Emu(int(h * 12700)))
        tb.text_frame.text = text
        for run in tb.text_frame.paragraphs[0].runs:
            run.font.size = Pt(size)
        return tb

    for n in range(1, 5):
        s = prs.slides.add_slide(blank)
        box(s, f"방법 {5 - n}", 30, 20, 120, 20, 12)  # 위쪽 가장자리의 이름표: 숫자가 슬라이드 번호와 달라 내용으로 남는다
        box(s, f"검토 항목 {n}", 30, 80, 600, 40, 32)  # 제목처럼 쓴 큰 글씨
        box(s, "장점", 40, 150, 250, 24, 14)
        box(s, "+ 처리가 빠르다", 40, 180, 250, 24, 14)
        box(s, "단점", 360, 150, 250, 24, 14)
        box(s, "- 값이 비싸다", 360, 180, 250, 24, 14)
        box(s, "내부 검토용 자료", 30, 510, 300, 20, 10)  # 슬라이드마다 같은 바닥글
        box(s, f"{n} / 4", 640, 510, 60, 20, 10)  # 쪽 번호
        if n == 2:
            pic = s.shapes.add_picture(io.BytesIO(buf.getvalue()), Emu(500 * 12700), Emu(250 * 12700))
            pic._element.nvPicPr.cNvPr.set("descr", "/home/user/ppt/icon_check.png")
    prs.save(HERE / "sample_plain.pptx")


def make_diagram_pptx() -> None:
    from lxml import etree
    from pptx import Presentation
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
    from pptx.util import Emu, Pt

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])

    def emu(v):
        return Emu(int(v * 12700))

    def rect(text, x, y, w=100, h=30):
        sh = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, emu(x), emu(y), emu(w), emu(h))
        if text:
            sh.text_frame.text = text
            sh.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        return sh

    title = s.shapes.add_textbox(emu(30), emu(20), emu(300), emu(30))
    title.text_frame.text = "메뉴구조도"
    root = rect("산업안전", 260, 80)
    left, right = rect("안전교육", 140, 160), rect("안전점검", 380, 160)
    rect("교육현황", 140, 194)  # 연결선 없이 '안전교육' 바로 아래에 같은 너비로 쌓은 상자
    for child in (left, right):
        c = s.shapes.add_connector(MSO_CONNECTOR.ELBOW, 0, 0, 0, 0)
        c.begin_connect(root, 2)
        c.end_connect(child, 0)
    # 연결 정보 없이 끝점만 두 상자에 닿은 화살표 (접수 → 처리)
    a, b = rect("접수", 140, 330), rect("처리", 380, 330)
    arrow = s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, emu(240), emu(345), emu(380), emu(345))
    ln = arrow.line._get_or_add_ln()
    etree.SubElement(ln, "{http://schemas.openxmlformats.org/drawingml/2006/main}tailEnd", type="triangle")
    assert a and b
    rect(None, 560, 300, 60, 60)  # 글 없는 장식 사각형
    prs.save(HERE / "sample_diagram.pptx")


def make_xlsx() -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "예산"
    ws.append(["구분", "2026년", None, "비고"])
    ws.append([None, "상반기", "하반기", None])
    ws.append(["인건비", 1000000, 1200000, "단위: 원"])
    ws.append(["장비비", 50000.5, 0.25, None])
    ws.append(["착수일", datetime.date(2026, 3, 2), None, None])
    ws.append(["합계", "=B3+B4", None, None])
    ws.merge_cells("B1:C1")
    ws.merge_cells("A1:A2")
    ws.merge_cells("D1:D2")
    for cell in ws[1] + ws[2]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    ws["B3"].number_format = "#,##0"
    ws["C3"].number_format = "#,##0"
    ws["B4"].number_format = "#,##0.00"
    ws["C4"].number_format = "0%"
    ws["B5"].number_format = "yyyy-mm-dd"
    ws2 = wb.create_sheet("일정")
    ws2.append(["단계", "기간", "진행률"])
    ws2.append(["분석", "2개월", 1.0])
    ws2.append(["설계", "1개월", 0.45])
    from openpyxl.chart import BarChart, Reference

    chart = BarChart()
    chart.title = "단계별 진행률"
    chart.add_data(Reference(ws2, min_col=3, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(ws2, min_col=1, min_row=2, max_row=3))
    ws2.add_chart(chart, "E2")
    hidden = wb.create_sheet("숨김")
    hidden.append(["내부 메모"])
    hidden.sheet_state = "hidden"
    wb.save(HERE / "sample.xlsx")


if __name__ == "__main__":
    make_pptx()
    make_plain_pptx()
    make_diagram_pptx()
    make_xlsx()
