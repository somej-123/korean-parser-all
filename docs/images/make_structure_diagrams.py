"""README·가이드의 구조도(SVG)를 만든다.

    python docs/images/make_structure_diagrams.py

docs/images/structure_overall.svg (전체 흐름)와 structure_pdf.svg (PDF 처리 흐름)를 다시 쓴다. 상자 위치를 손으로 정했으므로
글을 길게 바꾸면 상자 너비를 함께 늘린다.
"""

from pathlib import Path

COL = {
    "gray": ("#F1EFE8", "#888780", "#2C2C2A", "#5F5E5A"),
    "purple": ("#EEEDFE", "#534AB7", "#3C3489", "#534AB7"),
    "teal": ("#E1F5EE", "#0F6E56", "#085041", "#0F6E56"),
    "coral": ("#FAECE7", "#993C1D", "#712B13", "#993C1D"),
}
LINE = "#73726C"


def box(x, y, w, h, title, sub=None, c="gray"):
    fill, stroke, tc, sc = COL[c]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>']
    cx = x + w / 2
    if sub:
        out.append(f'<text x="{cx}" y="{y + 18}" text-anchor="middle" dominant-baseline="central" font-size="14" font-weight="600" fill="{tc}">{title}</text>')
        out.append(f'<text x="{cx}" y="{y + 38}" text-anchor="middle" dominant-baseline="central" font-size="12" fill="{sc}">{sub}</text>')
    else:
        out.append(f'<text x="{cx}" y="{y + h / 2}" text-anchor="middle" dominant-baseline="central" font-size="14" font-weight="600" fill="{tc}">{title}</text>')
    return "".join(out)


def line(x1, y1, x2, y2):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{LINE}" stroke-width="1.2" marker-end="url(#a)"/>'


def path(d):
    return f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="1.2" marker-end="url(#a)"/>'


def label(x, y, t, anchor="start"):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="12" fill="#5F5E5A">{t}</text>'


DEFS = (f'<defs><marker id="a" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        f'<path d="M2 1L8 5L2 9" fill="none" stroke="{LINE}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>')


def svg(h, body):
    # 흰 바탕을 깐다: GitHub 어두운 화면에서도 글이 보이게
    return (f'<svg viewBox="0 0 680 {h}" width="680" xmlns="http://www.w3.org/2000/svg" '
            f'font-family="Malgun Gothic, Apple SD Gothic Neo, Noto Sans KR, sans-serif">{DEFS}'
            f'<rect width="680" height="{h}" fill="#FFFFFF"/>{body}</svg>')


d1 = [
    box(200, 30, 280, 56, "문서 입력", "HWP·HWPX·DOCX·PPTX·XLSX·PDF"),
    line(340, 86, 340, 124),
    box(210, 126, 260, 56, "형식 판별", "확장자가 아니라 파일 내용으로"),
    line(300, 182, 120, 220), line(325, 182, 268, 220), line(355, 182, 412, 220), line(380, 182, 560, 220),
    box(50, 222, 130, 56, "HWP·HWPX", "같은 코드로 읽기", "purple"),
    box(200, 222, 130, 56, "DOCX", "Word XML 직접", "purple"),
    box(350, 222, 130, 56, "PPTX·XLSX", "슬라이드·시트", "purple"),
    box(500, 222, 130, 56, "PDF·스캔", "Docling+글자 층", "purple"),
    line(120, 278, 290, 316), line(268, 278, 320, 316), line(412, 278, 360, 316), line(560, 278, 390, 316),
    box(200, 318, 280, 56, "통합 문서 모델 (IR)", "블록·출처(쪽·좌표)·경고", "teal"),
    line(340, 374, 340, 412),
    box(200, 414, 280, 56, "한국어 처리 레이어", "정규화·개조식 계층·제목 추정", "teal"),
    line(300, 470, 120, 508), line(325, 470, 268, 508), line(355, 470, 412, 508), line(380, 470, 560, 508),
    box(50, 510, 130, 56, "HTML", "원본 모양 화면"),
    box(200, 510, 130, 56, "LLM 글", "마크다운+HTML 표"),
    box(350, 510, 130, 56, "JSON", "전체 결과 저장"),
    box(500, 510, 130, 56, "검색용 조각", "제목 경로·출처"),
]
d2 = [
    box(260, 30, 160, 44, "PDF 파일"),
    line(340, 74, 340, 108),
    box(230, 110, 220, 44, "글자 층이 있나?"),
    path("M300 154 V176 H185 V198"), path("M380 154 V176 H495 V198"),
    label(176, 190, "있음", "end"), label(504, 190, "없음 (스캔)"),
    box(60, 200, 250, 56, "Docling 레이아웃", "문단·제목·표 영역 찾기", "purple"),
    line(185, 256, 185, 288),
    box(60, 290, 250, 56, "글자 층으로 다시 읽기", "띄어쓰기·서식 복원", "purple"),
    line(185, 346, 185, 378),
    box(60, 380, 250, 56, "괘선으로 표 만들기", "병합 칸·칸 안의 표", "purple"),
    box(370, 200, 250, 56, "OCR (RapidOCR)", "CPU, 한국어 모델", "coral"),
    line(495, 256, 495, 288),
    box(370, 290, 250, 56, "OCR 띄어쓰기", "쪽 그림의 글자 간격", "coral"),
    line(495, 346, 495, 378),
    box(370, 380, 250, 56, "빠짐 검사", "20% 넘게 빠지면 경고", "coral"),
    line(185, 436, 268, 474), line(495, 436, 412, 474),
    box(190, 476, 300, 56, "정리", "쪽 넘는 표·문단 잇기, 제목 아닌 줄"),
    line(340, 532, 340, 564),
    box(230, 566, 220, 44, "통합 문서 모델 (IR)", None, "teal"),
]

OUT = Path(__file__).resolve().parent
(OUT / "structure_overall.svg").write_text(svg(600, "".join(d1)), encoding="utf-8")
(OUT / "structure_pdf.svg").write_text(svg(630, "".join(d2)), encoding="utf-8")
print("docs/images/structure_overall.svg, structure_pdf.svg")
