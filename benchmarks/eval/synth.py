#!/usr/bin/env python3
"""정답이 있는 평가용 문서 만들기.

정답(구조화된 명세)을 먼저 만들고, 그 명세로 DOCX를 쓴 뒤 LibreOffice로 PDF를, 쪽 그림으로 스캔 PDF를 만든다.
같은 정답으로 DOCX·PDF·스캔 PDF 파싱 결과를 모두 잴 수 있다. 정답은 파서와 상관없이 명세에서 나오므로
파서를 고쳐도 기준이 바뀌지 않는다.

    python synth.py --out data --docs 12 --scans 4

만드는 것 (문서마다): <이름>.docx, <이름>.pdf, (일부) <이름>.scan.pdf, <이름>.truth.json

문서 구성은 한국 제안요청서를 흉내 낸다: 로마 숫자·"1."·"가." 장절 제목(제목 스타일 또는 굵고 큰 문단),
□ ○ - ※ 개조식, 숫자·영문 약어가 섞인 긴 문단(줄·쪽이 넘어간다), 세로·가로 병합 칸이 있는 표
(칸마다 선이 있는 표와 위아래 선만 있는 표). 글은 고정 문장 목록에서 시드에 따라 고른다.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Pt

ROMAN = "ⅠⅡⅢⅣⅤⅥⅦⅧ"
GANADA = "가나다라마바사아"

CHAPTERS = ["사업 개요", "추진 배경 및 필요성", "사업 범위", "제안요청 내용", "제안서 작성요령", "평가 방법", "계약 조건",
            "보안 요구사항", "품질 관리", "유지관리 방안"]
SECTIONS = ["추진 목표", "현황 및 문제점", "기능 요구사항", "성능 요구사항", "데이터 요구사항", "테스트 요구사항",
            "교육 및 기술지원", "산출물 제출", "일정 관리", "위험 관리", "인력 운영", "하자보수"]
ITEMS = ["사업명: 차세대 문서관리시스템 구축 용역", "사업기간: 계약일로부터 8개월", "사업예산: 1,250,000,000원(VAT 포함)",
         "계약방법: 협상에 의한 계약", "평가 비율은 기술평가 90%, 가격평가 10%로 함", "제안서 제출 부수는 원본 1부, 사본 5부",
         "보유인력 평가 점수는 5.0점 만점으로 산정", "OCR 인식률 98% 이상을 목표로 함", "API 응답시간은 3초 이내로 함",
         "하자보수 기간은 검수 완료일로부터 1년", "주간보고는 매주 금요일 PM이 제출", "산출물은 HWPX와 PDF 형식으로 제출",
         "개인정보는 암호화하여 저장하고 접근 기록을 6개월 이상 보관", "시스템 가용성 99.5% 이상 유지",
         "장애 발생 시 30분 이내 1차 대응", "교육은 관리자 2회, 사용자 3회 실시"]
SENTENCES = [
    "본 사업은 기관 내부 문서를 통합 관리하고 검색 품질을 높이기 위하여 추진한다.",
    "제안사는 현행 시스템의 문제점을 분석하고 이를 개선할 수 있는 구체적인 방안을 제시하여야 한다.",
    "모든 산출물은 발주기관의 표준 서식을 따라야 하며, 영문 약어는 처음 나올 때 우리말 설명을 함께 적는다.",
    "기술평가 점수가 배점 한도의 85% 미만인 경우에는 협상 대상에서 제외한다.",
    "수행사는 착수일로부터 14일 이내에 사업수행계획서를 제출하고 발주기관의 승인을 받아야 한다.",
    "데이터 이관 과정에서 원본 자료가 훼손되지 않도록 사전 검증 절차를 마련하여야 한다.",
    "보안 취약점 점검 결과 고위험 항목은 검수 이전까지 모두 조치하여야 한다.",
    "공동수급체를 구성하는 경우 대표사는 전체 지분의 50% 이상을 보유하여야 한다.",
    "제안서에 기재한 투입 인력은 정당한 사유 없이 교체할 수 없으며, 교체 시 동등 이상의 인력을 투입하여야 한다.",
    "시스템은 HWP, HWPX, DOCX, PDF 등 공공기관에서 주로 쓰는 문서 형식을 모두 처리할 수 있어야 한다.",
    "검색 결과에는 원문 위치와 쪽 번호가 함께 표시되어야 하며, 표와 그림도 검색 대상에 포함한다.",
    "성능 시험은 동시 사용자 200명 환경에서 실시하며 평균 응답시간이 2.5초를 넘지 않아야 한다.",
    "수행사는 매월 진척 현황과 위험 요소를 정리하여 월간보고서로 제출한다.",
    "제출된 서류는 반환하지 않으며 제안서 작성에 드는 비용은 제안사가 부담한다.",
    "납품하는 소프트웨어의 저작권 귀속은 계약 일반조건에 따른다.",
    "운영 환경은 클라우드(CSAP 인증) 기반으로 구성하고 재해 복구 계획을 함께 제시한다.",
]
NOTES = ["※ 세부 내용은 별첨 서식을 참조", "※ 공동수급 시 구성원 실적을 합산하여 평가",
         "※ 증빙 자료를 제출하지 않으면 해당 항목은 0점 처리", "※ 금액 단위: 천원"]
TABLE_HEAD = [["구분", "평가항목", "세부 내용", "배점"], ["항목", "내용", "비고"],
              ["단계", "주요 활동", "산출물", "기간"], ["품목", "규격", "수량", "단가", "금액"]]
CELL_WORDS = ["보유인력", "경영상태", "수행실적", "사업 이해도", "추진 전략", "기술 지원", "품질 보증", "보안 관리",
              "착수", "분석", "설계", "구현", "시험", "이행", "웹서버", "DB서버", "스토리지", "백업장비",
              "요구사항 정의서", "설계서", "시험 결과서", "완료 보고서"]


# ---------------------------------------------------------------------------
# 명세(정답)
# ---------------------------------------------------------------------------


def make_spec(seed: int) -> dict:
    rnd = random.Random(seed)
    style = "styled" if seed % 2 == 0 else "manual"  # 제목 스타일 / 굵고 큰 번호 문단
    blocks: list[dict] = []
    blocks.append({"type": "heading", "level": 1, "text": rnd.choice(["제 안 요 청 서", "과업지시서", "사업 제안요청서"])})
    for ci, chapter in enumerate(rnd.sample(CHAPTERS, rnd.randint(3, 4))):
        blocks.append({"type": "heading", "level": 1, "text": f"{ROMAN[ci]}. {chapter}"})
        for si, section in enumerate(rnd.sample(SECTIONS, rnd.randint(2, 3))):
            blocks.append({"type": "heading", "level": 2, "text": f"{si + 1}. {section}"})
            for _ in range(rnd.randint(1, 3)):
                kind = rnd.random()
                if kind < 0.35:
                    blocks.append({"type": "paragraph", "text": " ".join(rnd.sample(SENTENCES, rnd.randint(2, 4)))})
                elif kind < 0.75:
                    blocks.append({"type": "paragraph", "text": "□ " + rnd.choice(SECTIONS) + " 관련 사항"})
                    for item in rnd.sample(ITEMS, rnd.randint(2, 4)):
                        blocks.append({"type": "paragraph", "text": "○ " + item})
                    if rnd.random() < 0.5:
                        blocks.append({"type": "paragraph", "text": "- " + rnd.choice(SENTENCES)})
                    if rnd.random() < 0.4:
                        blocks.append({"type": "paragraph", "text": rnd.choice(NOTES)})
                else:
                    blocks.append(make_table(rnd))
    return {"name": f"synth_{seed:02d}", "seed": seed, "heading_style": style, "blocks": blocks}


def make_table(rnd: random.Random) -> dict:
    head = rnd.choice(TABLE_HEAD)
    ncol = len(head)
    nrow = rnd.randint(3, 7)
    rows = [[{"text": h, "rowspan": 1, "colspan": 1} for h in head]]
    covered: set[tuple[int, int]] = set()
    body = [[None] * ncol for _ in range(nrow)]
    r = 0
    while r < nrow:  # 첫 열 세로 병합
        span = min(rnd.choice([1, 1, 2, 3]), nrow - r)
        body[r][0] = {"text": rnd.choice(CELL_WORDS), "rowspan": span, "colspan": 1}
        for k in range(1, span):
            covered.add((r + k, 0))
        r += span
    for r in range(nrow):
        c = 1
        while c < ncol:
            if c < ncol - 2 and rnd.random() < 0.12:  # 가로 병합
                body[r][c] = {"text": rnd.choice(ITEMS), "rowspan": 1, "colspan": 2}
                covered.add((r, c + 1))
                c += 2
                continue
            if c == ncol - 1:
                text = rnd.choice(["5", "10", "15", "2.5", "1,200", "(20)", "-", "3개월"])
            else:
                text = rnd.choice(CELL_WORDS + ITEMS[:6])
            body[r][c] = {"text": text, "rowspan": 1, "colspan": 1}
            c += 1
    for r in range(nrow):
        rows.append([body[r][c] for c in range(ncol) if body[r][c] is not None and (r, c) not in covered])
    return {"type": "table", "ruled": rnd.random() < 0.8, "n_cols": ncol, "rows": rows}


# ---------------------------------------------------------------------------
# DOCX 쓰기
# ---------------------------------------------------------------------------


def write_docx(spec: dict, path: Path) -> None:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11)
    for b in spec["blocks"]:
        if b["type"] == "heading":
            if spec["heading_style"] == "styled":
                doc.add_heading(b["text"], level=b["level"])
            else:
                p = doc.add_paragraph()
                run = p.add_run(b["text"])
                run.bold = True
                run.font.size = Pt(18 if b["level"] == 1 else 14)
        elif b["type"] == "paragraph":
            doc.add_paragraph(b["text"])
        elif b["type"] == "table":
            write_table(doc, b)
            doc.add_paragraph("")
    doc.save(path)


def write_table(doc, spec: dict) -> None:
    rows, ncol = spec["rows"], spec["n_cols"]
    table = doc.add_table(rows=len(rows), cols=ncol)
    if spec["ruled"]:
        table.style = doc.styles["Table Grid"]
    else:  # 위아래 바깥선과 머리 행 아래 선만 있는 표
        borders = ('<w:tblBorders %s><w:top w:val="single" w:sz="8"/><w:bottom w:val="single" w:sz="8"/>'
                   '<w:left w:val="nil"/><w:right w:val="nil"/><w:insideH w:val="nil"/><w:insideV w:val="nil"/>'
                   '</w:tblBorders>' % nsdecls("w"))
        table._tbl.tblPr.append(parse_xml(borders))
    occupied: set[tuple[int, int]] = set()
    for r, row in enumerate(rows):
        c = 0
        for cell in row:
            while (r, c) in occupied:
                c += 1
            rs, cs = cell["rowspan"], cell["colspan"]
            target = table.cell(r, c)
            if rs > 1 or cs > 1:
                target = target.merge(table.cell(r + rs - 1, c + cs - 1))
            target.text = cell["text"]
            for rr in range(r, r + rs):
                for cc in range(c, c + cs):
                    occupied.add((rr, cc))
            c += cs
    if not spec["ruled"]:
        for cell in table.rows[0].cells:  # 머리 행 아래 선
            tcpr = cell._tc.get_or_add_tcPr()
            tcpr.append(parse_xml('<w:tcBorders %s><w:bottom w:val="single" w:sz="6"/></w:tcBorders>' % nsdecls("w")))


# ---------------------------------------------------------------------------
# PDF, 스캔 PDF
# ---------------------------------------------------------------------------


def _soffice() -> str:
    """LibreOffice 실행 파일. Windows는 설치해도 PATH에 넣지 않으므로 기본 설치 폴더도 본다."""
    found = shutil.which("soffice")
    if found:
        return found
    for base in ("C:/Program Files/LibreOffice", "C:/Program Files (x86)/LibreOffice"):
        exe = Path(base) / "program" / "soffice.exe"
        if exe.exists():
            return str(exe)
    raise SystemExit("LibreOffice(soffice)가 필요합니다")


def to_pdf(docx_files: list[Path], out: Path) -> None:
    soffice = _soffice()
    with tempfile.TemporaryDirectory() as profile:
        # 프로필 위치는 file URL로 넘긴다. Windows 경로를 'file://'에 그대로 붙이면 잘못된 주소가 되어
        # LibreOffice가 'bootstrap.ini가 손상되었다'는 창을 띄우고 멈춘다
        subprocess.run([soffice, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--convert-to", "pdf",
                        "--outdir", str(out), *map(str, docx_files)], check=True, capture_output=True)


def to_scan(pdf: Path, out: Path, dpi: int = 150, max_pages: int = 3) -> None:
    """쪽을 그림으로 그려 텍스트 레이어가 없는 PDF를 만든다 (스캔본 흉내, OCR 경로 시험용)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    images = [doc[i].render(scale=dpi / 72).to_pil().convert("RGB") for i in range(min(len(doc), max_pages))]
    images[0].save(out, "PDF", resolution=dpi, save_all=True, append_images=images[1:])


def main() -> int:
    p = argparse.ArgumentParser(description="정답이 있는 평가용 문서 만들기")
    p.add_argument("--out", default="data")
    p.add_argument("--docs", type=int, default=12)
    p.add_argument("--scans", type=int, default=4, help="스캔 PDF도 만들 문서 수 (앞에서부터)")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    docx_files = []
    for seed in range(args.docs):
        spec = make_spec(seed)
        (out / f"{spec['name']}.truth.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        path = out / f"{spec['name']}.docx"
        write_docx(spec, path)
        docx_files.append(path)
    to_pdf(docx_files, out)
    for path in docx_files[: args.scans]:
        pdf = path.with_suffix(".pdf")
        # 스캔본은 앞 3쪽만 그리므로 정답도 그 범위만 본다 (evaluate.py가 truth의 scan_pages를 쓴다)
        to_scan(pdf, path.with_suffix(".scan.pdf"))
    print(f"{len(docx_files)}개 문서 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
