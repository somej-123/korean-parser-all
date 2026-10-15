#!/usr/bin/env python3
"""원본 문서 렌더링과 우리 HTML 렌더링을 나란히 놓은 비교 이미지를 만든다.

- 원본: LibreOffice로 PDF 변환 → 페이지 이미지 (Word와 완전히 같지는 않지만 기준으로 충분)
- 우리: korean_parser로 HTML 변환 → Chromium(Playwright)으로 전체 페이지 캡처

    python compare.py ../../tests/fixtures/korean_report.docx -o out/
    python compare.py 문서폴더/*.docx -o out/ --pages 2

필요: LibreOffice(soffice), playwright (pip install playwright, 브라우저는 별도 설치)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from korean_parser.parsers import parse  # noqa: E402
from korean_parser.render.html import render_html  # noqa: E402

SCALE = 96 / 72  # pt → px


def render_original(src: Path, pages: int, work: Path) -> Image.Image:
    import pypdfium2 as pdfium

    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(work), str(src)],
        check=True, capture_output=True, timeout=180,
    )
    pdf = pdfium.PdfDocument(str(work / (src.stem + ".pdf")))
    imgs = [pdf[i].render(scale=SCALE).to_pil() for i in range(min(pages, len(pdf)))]
    return _stack(imgs)


def render_ours(src: Path, work: Path, max_height: int) -> Image.Image:
    from playwright.sync_api import sync_playwright

    doc = parse(src)
    html_path = work / (src.stem + ".html")
    html_path.write_text(render_html(doc), encoding="utf-8")
    width = int(doc.page.width_pt * SCALE) + 40
    launch = {}
    if os.path.exists("/opt/pw-browsers/chromium"):
        launch["executable_path"] = "/opt/pw-browsers/chromium"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": width, "height": 800})
        page.goto(html_path.as_uri())
        page.wait_for_timeout(300)
        shot = work / (src.stem + "_ours.png")
        page.screenshot(path=str(shot), full_page=True)
        browser.close()
    img = Image.open(shot)
    if img.height > max_height:
        img = img.crop((0, 0, img.width, max_height))
    return img


def _stack(imgs: list[Image.Image]) -> Image.Image:
    w = max(i.width for i in imgs)
    h = sum(i.height for i in imgs) + 8 * (len(imgs) - 1)
    out = Image.new("RGB", (w, h), (200, 200, 200))
    y = 0
    for i in imgs:
        out.paste(i, (0, y))
        y += i.height + 8
    return out


def _font(size: int):
    for path in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                 "/System/Library/Fonts/AppleSDGothicNeo.ttc", "C:/Windows/Fonts/malgun.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def side_by_side(left: Image.Image, right: Image.Image, title: str) -> Image.Image:
    pad, head = 16, 44
    w = left.width + right.width + pad * 3
    h = max(left.height, right.height) + head + pad
    out = Image.new("RGB", (w, h), (233, 233, 236))
    d = ImageDraw.Draw(out)
    f = _font(18)
    d.text((pad, 12), f"원본 (LibreOffice) — {title}", fill=(30, 30, 30), font=f)
    d.text((left.width + pad * 2, 12), "korean-parser HTML (Chromium)", fill=(30, 30, 30), font=f)
    out.paste(left, (pad, head))
    out.paste(right, (left.width + pad * 2, head))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="원본 vs HTML 시각 비교")
    p.add_argument("inputs", nargs="+")
    p.add_argument("-o", "--out", default="compare_out")
    p.add_argument("--pages", type=int, default=2, help="원본에서 비교할 페이지 수")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for src in map(Path, args.inputs):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            try:
                orig = render_original(src, args.pages, work)
                ours = render_ours(src, work, max_height=orig.height + 200)
            except Exception as exc:  # noqa: BLE001
                print(f"실패  {src.name}: {exc}", file=sys.stderr)
                continue
            target = out / f"{src.stem}_compare.png"
            side_by_side(orig, ours, src.name).save(target)
            print(f"완료  {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
