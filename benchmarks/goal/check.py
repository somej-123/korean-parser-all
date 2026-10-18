#!/usr/bin/env python3
"""v1.0 완료 기준(docs/GOAL.md)을 기준 문서 묶음으로 한 번에 잰다.

    python benchmarks/goal/check.py --out goal_report.md            # 전부 (PDF·스캔 평가 포함, 몇 분)
    python benchmarks/goal/check.py --skip-eval --out goal_report.md  # 기준 1만 (정답 세트 평가 생략)

기준 1(빠짐없이): 파일마다 원문 글자를 파서와 따로 읽어(PDF 텍스트 레이어, DOCX w:t, HWPX hp:t, HWP 문단 글 레코드,
PPTX a:t, XLSX 글 칸) 우리 결과에 남은 비율을 잰다. 메모(한글 메모, Word comments.xml, XLSX 칸 메모)는 v1.4.0부터
읽으므로 원문에 넣는다. 두 쪽 모두 같은 정규화(normalize_text, NFKC, 공백 제거)를 한다.
두 쪽(슬라이드) 이상, 절반 넘게 되풀이되는 줄은 한 번만 세고, 숫자만 있는 짧은 줄(쪽 번호)은 뺀다 (파서가 일부러 한 번만 남기거나 버린다).
보존율이 0.99 미만인데 경고가 없으면 '경고 없이 사라진 글'로 표시한다.

기준 2(구조 정확도): benchmarks/eval의 합성 정답 세트 평가와 HWPX 미리보기 비교를 그대로 돌려 GOAL.md의 수치와 비교한다.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
import time
import unicodedata
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "benchmarks" / "eval"))

from lxml import etree  # noqa: E402

from korean_parser.ir import walk_blocks  # noqa: E402
from korean_parser.normalize import normalize_text  # noqa: E402
from korean_parser.parsers import detect_format, parse  # noqa: E402
from korean_parser.parsers.pdf.page import _PDFIUM_LOCK  # noqa: E402  (Docling의 쪽 읽기 스레드와 pdfium을 나눠 쓴다)

# 기준 문서 묶음 (docs/GOAL.md). 없는 폴더는 건너뛴다
SETS = [
    ("실제 문서", ROOT / "samples_rfp"),
    ("공개 시험 문서", ROOT / "benchmarks" / "corpus" / "samples_web" / "hwpxlib"),
    ("공개 시험 문서", ROOT / "benchmarks" / "corpus" / "samples_web" / "hwplib"),
    ("공개 시험 문서", ROOT / "benchmarks" / "corpus" / "samples_web" / "poi"),
    ("표본 문서", ROOT / "benchmarks" / "goal" / "samples"),
    ("저장소 시험 파일", ROOT / "tests" / "fixtures"),
    ("저장소 시험 파일", ROOT / "benchmarks" / "docling-cpu" / "samples"),
]
FORMATS = {".pdf", ".docx", ".hwpx", ".hwp", ".pptx", ".xlsx"}
KEPT_MIN = 0.99
# 파서가 일부러 둔 크기 제한의 경고 (docs/GOAL.md 기준 1의 예외). 이 경고가 있는 파일은 0.99 미만이어도 미달이 아니다
LIMIT_WARNINGS = ("칸이 너무 많아",)
# 기준 2 (docs/GOAL.md): (대상, 지표, 하위 키, 비교, 기준값)
STRUCTURE = [
    ("pdf", "cer", None, "<=", 0.01), ("pdf", "eojeol_f1", None, ">=", 0.98), ("pdf", "table_teds", None, ">=", 0.95),
    ("pdf", "heading", "recall", ">=", 0.95), ("pdf", "reading_order", None, ">=", 0.99),
    ("scan", "eojeol_f1", None, ">=", 0.94), ("scan", "table_teds", None, ">=", 0.92), ("scan", "cer", None, "<=", 0.02),
    ("docx (추정 제목 포함)", "heading", "recall", ">=", 0.95), ("docx", "table_teds", None, ">=", 1.0),
]
HWPX_PREVIEW_MIN = 0.98

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_PAGE_NO = re.compile(r"^[\d\s\-–/|·.()]{1,12}$")


def norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", normalize_text(text)).split())


# ---------------------------------------------------------------------------
# 원문 글 (파서와 따로 읽는다). 쪽(슬라이드)마다 줄 목록
# ---------------------------------------------------------------------------


def _docx_dropped(t) -> bool:
    """Word에서 화면에 없는 글: 변경 추적에서 옮기기 전 자리(w:moveFrom), 숨김 서식(글자 단위 w:vanish, 파서가 경고한다)."""
    if any(a.tag == W + "moveFrom" for a in t.iterancestors()):
        return True
    run = t.getparent()
    vanish = run.find(f"{W}rPr/{W}vanish") if run is not None else None
    return vanish is not None and vanish.get(W + "val", "true") not in ("0", "false")


def _xml_texts(z: zipfile.ZipFile, name: str, tag: str, skip_fallback: bool = False) -> list[str]:
    root = etree.fromstring(z.read(name))
    out = []
    for t in root.iter(tag):
        if skip_fallback and any(a.tag == MC_FALLBACK for a in t.iterancestors()):
            continue  # 호환용 사본 (같은 글상자가 Choice와 Fallback에 두 번 들어 있다)
        if tag == W + "t" and _docx_dropped(t):
            continue
        if t.text:
            out.append(t.text)
    return out


def _pdf_pages(path: Path) -> list[list[str]] | None:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    # 쪽 안에 보이는 글자만 센다. 두 쪽에 걸친 표를 두 쪽 모두에 통째로 그려 두고 쪽마다 반쪽만 보이게 한 PDF가 있다
    # (교육훈련계획: 28쪽 밖으로 나간 글자 1,073개 = 29쪽에 보이는 글). 쪽 밖 글자까지 세면 같은 글을 두 번 센다
    pages = []
    for i in range(len(pdf)):
        left, bottom, right, top = pdf[i].get_cropbox()
        pages.append(pdf[i].get_textpage().get_text_bounded(left, bottom, right, top).splitlines())
    pdf.close()
    return pages if sum(len(norm("".join(p))) for p in pages) >= 20 else None


def _real_ext(path: Path) -> str:
    """내용으로 본 형식 (확장자가 틀린 파일: '.hwpx'인 PDF). 파서와 같은 판단을 쓴다."""
    fmt = detect_format(path)
    return "." + fmt if fmt else path.suffix.lower()


def source_pages(path: Path) -> list[list[str]] | None:
    """원문의 쪽마다 글 줄. 텍스트 레이어가 없는 PDF(스캔)는 None."""
    ext = _real_ext(path)
    if ext == ".pdf":
        with _PDFIUM_LOCK:
            return _pdf_pages(path)
    with zipfile.ZipFile(path) if ext in (".docx", ".hwpx", ".pptx", ".xlsx") else _NoZip() as z:
        if ext == ".docx":
            names = [n for n in z.namelist() if re.match(r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", n)]
            return [[t for n in names for t in _xml_texts(z, n, W + "t", skip_fallback=True)]]
        if ext == ".hwpx":
            names = sorted(n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n))
            hp = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
            return [[t for n in names for t in _xml_texts(z, n, hp + "t")]]
        if ext == ".pptx":
            slides = sorted((n for n in z.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
                            key=lambda n: (n.split("/")[1], int(re.findall(r"\d+", n.split("/")[-1])[0])))
            out, notes = [], []
            for n in slides:
                root = etree.fromstring(z.read(n))
                lines = ["".join(t.text or "" for t in p.iter(A + "t")) for p in root.iter(A + "p")]
                # 발표자 메모는 한 쪽으로 모은다 (슬라이드 수가 늘면 '절반 이상 되풀이' 판단이 어긋난다)
                if "notesSlide" in n:
                    notes.extend(lines)
                else:
                    out.append(lines)
            # 슬라이드 메모(옛 p:cm/p:text, 새 p188:cm 글과 답글)도 한 쪽으로 모은다
            for n in sorted(n for n in z.namelist() if re.match(r"ppt/comments/[^/]+\.xml$", n)):
                root = etree.fromstring(z.read(n))
                notes += [line for el in root.iter() if isinstance(el.tag, str) and etree.QName(el).localname == "text"
                          for line in (el.text or "").splitlines()]
                notes += ["".join(t.text or "" for t in p.iter(A + "t")) for p in root.iter(A + "p")]
            return out + ([notes] if notes else [])
        if ext == ".xlsx":
            shared = []
            if "xl/sharedStrings.xml" in z.namelist():
                root = etree.fromstring(z.read("xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in si.iter(S + "t") if etree.QName(t.getparent()).localname != "rPh")
                          for si in root.iter(S + "si")]
            out = []
            for n in sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
                lines = []
                for c in etree.fromstring(z.read(n)).iter(S + "c"):
                    v, t = c.find(S + "v"), c.get("t")
                    if t == "s" and v is not None and v.text and int(v.text) < len(shared):
                        lines.append(shared[int(v.text)])
                    elif t == "inlineStr":
                        lines.append("".join(x.text or "" for x in c.iter(S + "t")))
                    elif t == "str" and v is not None and v.text:
                        lines.append(v.text)
                out.append(lines)
            # 칸 밖의 글: 칸 메모(스레드 메모가 있으면 그것만: Excel이 옛 메모 자리에 안내 글을 함께 저장한다),
            # 글상자·도형 글, 인쇄 머리글·바닥글(&코드 빼고, 같은 글은 한 번)
            names = z.namelist()
            threaded = [n for n in names if n.startswith("xl/threadedComments/")]
            extra = []
            for n in threaded or [n for n in names if re.match(r"xl/comments\d*\.xml$", n)]:
                root = etree.fromstring(z.read(n))
                extra += ["".join(t.text or "" for t in el.iter() if isinstance(t.tag, str)
                                  and etree.QName(t).localname in ("t", "text") and etree.QName(t.getparent()).localname != "rPh")
                          for el in root.iter() if isinstance(el.tag, str)
                          and etree.QName(el).localname in ("comment", "threadedComment")]
            for n in names:
                if re.match(r"xl/drawings/drawing\d+\.xml$", n):
                    root = etree.fromstring(z.read(n))
                    extra += ["".join(t.text or "" for t in p.iter(A + "t")) for p in root.iter(A + "p")]
            hf = set()
            for n in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
                for el in etree.fromstring(z.read(n)).iter():
                    if isinstance(el.tag, str) and etree.QName(el).localname.endswith(("Header", "Footer")) and el.text:
                        hf.add(re.sub(r'&(?:"[^"]*"|K\w{6}|\d+|[A-Za-z&])', "", el.text))
            if extra or hf:
                out.append(extra + sorted(hf))
            return out
    if ext == ".hwp":
        return [_hwp_texts(path)]
    return None


class _NoZip:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


def _hwp_texts(path: Path) -> list[str]:
    """HWP 본문 레코드의 문단 글(PARA_TEXT)과 글자 겹침·덧말 컨트롤의 글."""
    from korean_parser.parsers.hwp import _CHAR_CTRL, HwpFile
    from korean_parser.parsers.hwp.records import Reader, Tag, ctrl_id

    f = HwpFile(path)
    out: list[str] = []

    def walk(records, parent=None):
        for r in records:
            # 경고만 남기는 곳은 뺀다: 바탕쪽(구역 정의 아래 문단), 숨은 설명, 문단 바로 아래 달린 글 목록
            if r.tag == Tag.PARA_HEADER and parent is not None and (
                    parent.tag == Tag.PARA_HEADER
                    or parent.tag == Tag.CTRL_HEADER and ctrl_id(parent.data) in ("secd", "tcmt")):
                continue
            if r.tag == Tag.PARA_TEXT:
                chars, i, buf = r.data.decode("utf-16-le", "replace"), 0, []
                while i < len(chars):
                    c = ord(chars[i])
                    if c >= 32:
                        buf.append(chars[i])
                        i += 1
                    else:
                        i += 1 if c in _CHAR_CTRL else 8
                out.append("".join(buf))
            elif r.tag == Tag.CTRL_HEADER and ctrl_id(r.data) in ("tcps", "tdut"):
                rd = Reader(r.data, 4)
                out.append(rd.text() + (rd.text() if ctrl_id(r.data) == "tdut" else ""))
            walk(r.children, r)

    for s in f.sections():
        walk(f.records(s))
    f.close() if hasattr(f, "close") else None
    return out


def edge_lines(path: Path) -> set[str]:
    """PDF 쪽의 위·아래 끝(높이의 12% 안)에 있는 줄 (norm). 파서는 두 쪽 이상에 나오는 이런 줄을 머리말·꼬리말로 보고
    글(숫자 빼고)마다 한 번만 남긴다."""
    if _real_ext(path) != ".pdf":
        return set()
    import pypdfium2 as pdfium

    out: set[str] = set()
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        for i in range(len(pdf)):
            page = pdf[i]
            w, h = page.get_size()
            tp = page.get_textpage()
            for top, bottom in ((h, h * 0.88), (h * 0.12, 0)):
                out.update(norm(ln) for ln in tp.get_text_bounded(0, bottom, w, top).splitlines() if norm(ln))
        pdf.close()
    return out


def source_counter(pages: list[list[str]], edges: set[str] | None = None) -> collections.Counter:
    """원문 글자 모음. 여러 쪽에 되풀이되는 줄은 한 번, 쪽 번호 같은 숫자 줄은 뺀다.

    edges(쪽 위·아래 끝의 줄)는 두 쪽 이상에 나오면 한 번만 센다 (장마다 바뀌는 머리말)."""
    lines = [[norm(ln) for ln in p] for p in pages]
    # 두 쪽 이상에 절반 넘게 되풀이되는 줄 (PDF는 두 쪽에 걸친 머리말도 한 번만 남긴다).
    # 숫자만 다른 줄('… 국어 책형 2쪽', '3쪽')도 같은 줄로 본다 (쪽 번호가 든 머리말)
    def key(ln: str) -> str:
        return re.sub(r"\d+", "#", ln)

    seen = collections.Counter(k for p in lines for k in {key(ln) for ln in p if ln})
    repeated = {k for k, c in seen.items() if c >= max(2, len(lines) / 2)} if len(lines) >= 2 else set()
    edge_keys = {key(ln) for ln in edges or ()}
    repeated |= {k for k, c in seen.items() if c >= 2 and k in edge_keys}
    out: collections.Counter = collections.Counter()
    counted: set[str] = set()
    for p in lines:
        for ln in p:
            if not ln or (_PAGE_NO.match(ln) and len(lines) > 1):
                continue
            if key(ln) in repeated:
                if key(ln) in counted:
                    continue
                counted.add(key(ln))
            out.update(ln)
    return out


# ---------------------------------------------------------------------------
# 우리 결과의 글
# ---------------------------------------------------------------------------


def our_text(doc) -> str:
    parts = []
    for blocks in [doc.header, doc.blocks, doc.footer] + [n.blocks for n in doc.notes.values()]:
        for b in walk_blocks(blocks):
            if hasattr(b, "inlines"):
                parts.append("".join(getattr(i, "text", "") or "" for i in b.inlines))
            for key in ("inner_text", "caption", "alt"):
                if isinstance(getattr(b, key, None), str):
                    parts.append(getattr(b, key))
    return "".join(parts)


def check_file(path: Path) -> dict:
    t0 = time.perf_counter()
    row = {"file": path, "format": path.suffix.lower().lstrip(".")}
    try:
        pages = source_pages(path)
        doc = parse(path)
    except Exception as exc:  # noqa: BLE001
        if "배포용" in str(exc) or "암호" in str(exc):
            return row | {"skip": "암호화된 문서 (안내 오류, 기준 4)"}
        return row | {"error": f"{type(exc).__name__}: {exc}"[:200]}
    row["seconds"] = time.perf_counter() - t0
    row["warnings"] = list(doc.warnings)
    if pages is None:
        row["skip"] = "텍스트 레이어 없음 (스캔: 기준 2에서 잰다)"
        return row
    src, ours = source_counter(pages, edge_lines(path)), collections.Counter(norm(our_text(doc)))
    total = sum(src.values())
    row["chars"] = total
    row["kept"] = sum((src & ours).values()) / total if total else 1.0
    row["missing"] = "".join(ch * n for ch, n in (src - ours).most_common(8))[:40]
    return row


# ---------------------------------------------------------------------------


def structure(skip_eval: bool) -> list[tuple]:
    if skip_eval:
        return []
    import statistics

    from evaluate import hwpx_preview, run

    data = ROOT / "benchmarks" / "eval" / "data"
    if not data.exists():
        return [("정답 세트", "-", "-", "benchmarks/eval/data 없음 (synth.py로 만든다)", "미확인")]
    results = run(data, baseline=False, formats={"pdf", "scan", "docx"}, guess=True)
    out = []
    for target, key, sub, op, limit in STRUCTURE:
        rows = results.get(f"korean-parser · {target}", [])
        vals = [(r[key][sub] if sub else r[key]) for r in rows if key in r]
        if not vals:
            out.append((target, key + (f".{sub}" if sub else ""), f"{op} {limit}", "-", "미확인"))
            continue
        v = statistics.mean(vals)
        ok = v <= limit if op == "<=" else v >= limit - 1e-9
        out.append((target, key + (f".{sub}" if sub else ""), f"{op} {limit}", f"{v:.3f}", "통과" if ok else "미달"))
    folder = ROOT / "benchmarks" / "corpus" / "samples_web"
    hw = hwpx_preview(folder) if folder.exists() else []
    if hw:
        v = statistics.mean(r["recall"] for r in hw)
        out.append(("hwpx", f"미리보기 재현율 ({len(hw)}개)", f">= {HWPX_PREVIEW_MIN}", f"{v:.3f}",
                    "통과" if v >= HWPX_PREVIEW_MIN else "미달"))
    else:
        out.append(("hwpx", "미리보기 재현율", f">= {HWPX_PREVIEW_MIN}", "-", "미확인 (samples_web 없음)"))
    return out


def is_limited(row: dict) -> bool:
    """의도한 크기 제한에 걸려 경고를 남긴 파일인가 (기준 1의 예외)."""
    return any(k in w for w in row.get("warnings", []) for k in LIMIT_WARNINGS)


def report(rows: list[dict], struct: list[tuple]) -> str:
    measured = [r for r in rows if "kept" in r]
    low = [r for r in measured if r["kept"] < KEPT_MIN]
    excepted = [r for r in low if is_limited(r)]
    below = [r for r in low if not is_limited(r)]
    silent = [r for r in below if not r["warnings"]]
    errors = [r for r in rows if "error" in r]
    lines = ["# v1.0 완료 기준 확인 (docs/GOAL.md)", "",
             "## 기준 1. 빠짐없이", "",
             f"- 파일 {len(rows)}개 (원문 글자를 잰 파일 {len(measured)}개, 건너뛴 파일(스캔·암호화) "
             f"{sum('skip' in r for r in rows)}개, 오류 {len(errors)}개)",
             f"- 보존율 {KEPT_MIN} 미만: {len(below)}개 (그중 경고 없음: {len(silent)}개)",
             f"- 예외 (의도한 크기 제한에 걸려 경고를 남김): {len(excepted)}개",
             f"- 판정: **{'통과' if not below and not errors else '미달'}**", "",
             "| 묶음 | 파일 | 형식 | 원문 글자 | 보존율 | 경고 | 빠진 글자(많은 순) |",
             "|---|---|---|---:|---:|---|---|"]
    for r in sorted(rows, key=lambda r: (r.get("kept", 2), r["file"].name)):
        name = r["file"].name[:40]
        if "error" in r:
            lines.append(f"| {r['set']} | {name} | {r['format']} | - | 오류 | {r['error']} | |")
        elif "skip" in r:
            lines.append(f"| {r['set']} | {name} | {r['format']} | - | - | {r['skip']} | |")
        else:
            mark = ("" if r["kept"] >= KEPT_MIN else " 예외(크기 제한)" if is_limited(r)
                    else " ⚠" if r["warnings"] else " ✗ 경고 없음")
            warn = "; ".join(w[:40] for w in r["warnings"][:3])
            lines.append(f"| {r['set']} | {name} | {r['format']} | {r['chars']} | {r['kept']:.3f}{mark} | {warn} "
                         f"| {r['missing']} |")
    lines += ["", "## 기준 2. 구조 정확도", ""]
    if struct:
        lines += ["| 대상 | 지표 | 기준 | 측정 | 판정 |", "|---|---|---|---:|---|"]
        lines += [f"| {' | '.join(map(str, s))} |" for s in struct]
    else:
        lines.append("(--skip-eval: 재지 않음)")
    lines += ["", "## 기준 3. 필수 요소", ""]
    lines += elements_report()
    lines += ["", "기준 4(약속)는 `tests/test_contract.py`로 확인한다."]
    return "\n".join(lines) + "\n"


def elements_report() -> list[str]:
    """표본 문서(benchmarks/goal/samples)마다 '들어 있어야 할 요소 목록'을 파서 결과와 맞춰 본다."""
    import json

    from elements import check

    folder = ROOT / "benchmarks" / "goal" / "samples"
    rows, total, passed = [], 0, 0
    for manifest in sorted(folder.glob("*.json")):
        doc_path = next((p for p in folder.glob(manifest.stem + ".*") if p.suffix != ".json"), None)
        if doc_path is None:
            continue
        results = check(parse(doc_path), json.loads(manifest.read_text(encoding="utf-8")))
        bad = [r for r in results if not r[1]]
        total += len(results)
        passed += len(results) - len(bad)
        rows.append(f"| {doc_path.name} | {len(results) - len(bad)} / {len(results)} | "
                    + "; ".join(f"{k}: {d}" for k, _, d in bad)[:300] + " |")
    if not rows:
        return ["(표본 문서 없음: benchmarks/goal/samples/make_*.py로 만든다)"]
    return [f"- 요소 {total}개 중 {passed}개 통과, 판정: **{'통과' if passed == total else '미달'}**", "",
            "| 표본 | 통과 | 못 찾은 요소 |", "|---|---:|---|", *rows]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="goal_report.md")
    p.add_argument("--skip-eval", action="store_true", help="기준 2(정답 세트 평가) 생략")
    p.add_argument("--no-pdf", action="store_true", help="텍스트 레이어 PDF 생략 (느림)")
    p.add_argument("--folder", type=Path, help="기준 문서 묶음 대신 이 폴더만 잰다 (새로 모은 문서 점검)")
    args = p.parse_args()
    rows = []
    for set_name, folder in ([("지정 폴더", args.folder.resolve())] if args.folder else SETS):
        if not folder.exists():
            continue
        for f in sorted(folder.rglob("*")):
            if f.suffix.lower() not in FORMATS or any(part.startswith(".") for part in f.relative_to(folder).parts):
                continue
            if args.no_pdf and f.suffix.lower() == ".pdf":
                continue
            row = check_file(f) | {"set": set_name}
            rows.append(row)
            print(f"{set_name:8s} {f.name[:40]:40s} {row.get('kept', row.get('error', row.get('skip', ''))) if 'kept' not in row else round(row['kept'], 3)}",
                  flush=True)
    text = report(rows, structure(args.skip_eval))
    Path(args.out).write_text(text, encoding="utf-8")
    print("보고서:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
