"""브라우저가 못 그리는 그림(WMF·EMF·TIFF)을 PNG로 바꾸기 (images.py)."""

import base64
import io
import struct
import sys

import pytest
from PIL import Image as PILImage

import korean_parser.images as images
from korean_parser.ir import Document, Image


def wmf_red_square() -> bytes:
    """빨간 사각형 하나를 그리는 가장 작은 WMF (위치 지정 머리 + 표준 머리 + 붓·선택·사각형·끝)."""
    def rec(func: int, *params: int) -> bytes:
        return struct.pack("<IH", 3 + len(params), func) + b"".join(struct.pack("<h", p) for p in params)

    body = struct.pack("<IH", 7, 0x02FC) + struct.pack("<HIH", 0, 0x000000FF, 0)  # 단색 붓, 빨강(COLORREF 0x0000FF)
    body += rec(0x012D, 0) + rec(0x041B, 90, 90, 10, 10) + rec(0x0000)  # 선택, 사각형(아래·오른쪽·위·왼쪽), 끝
    std = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(body)) // 2, 1, 10, 0)
    head = struct.pack("<IHhhhhHI", 0x9AC6CDD7, 0, 0, 0, 100, 100, 1440, 0)
    check = 0
    for (word,) in struct.iter_unpack("<H", head):
        check ^= word
    return head + struct.pack("<H", check) + std + body


def doc_with(data: bytes, mime: str, name: str) -> Document:
    doc = Document(images={"img1": Image(mime=mime, data_base64=base64.b64encode(data).decode(), name=name)})
    doc.warnings.append(f"브라우저에서 표시할 수 없는 이미지 형식: {name} ({mime})")
    return doc


def png_of(doc: Document) -> PILImage.Image:
    return PILImage.open(io.BytesIO(base64.b64decode(doc.images["img1"].data_base64)))


def test_tiff_becomes_png_everywhere():
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 6), (0, 128, 255)).save(buf, "TIFF")
    doc = doc_with(buf.getvalue(), "image/tiff", "scan.tif")
    images.convert_unrenderable(doc)
    im = doc.images["img1"]
    assert (im.mime, im.converted_from) == ("image/png", "image/tiff")
    assert png_of(doc).size == (8, 6) and not doc.warnings  # 바꿨으면 '표시할 수 없음' 경고를 지운다


@pytest.mark.skipif(sys.platform != "win32" and images._soffice() is None,
                    reason="메타파일을 그리려면 Windows(Pillow) 또는 LibreOffice가 필요하다")
def test_wmf_becomes_png():
    doc = doc_with(wmf_red_square(), "image/x-wmf", "logo.wmf")
    images.convert_unrenderable(doc)
    assert doc.images["img1"].mime == "image/png" and doc.images["img1"].converted_from == "image/x-wmf"
    img = png_of(doc).convert("RGB")
    assert img.getpixel((img.width // 2, img.height // 2)) == (255, 0, 0)
    assert not doc.warnings


def test_failed_conversion_keeps_original_and_warning(monkeypatch):
    monkeypatch.setattr(images, "_with_pillow", lambda data, mime: None)
    monkeypatch.setattr(images, "_with_libreoffice", lambda items: {})
    doc = doc_with(b"not really a metafile", "image/x-emf", "chart.emf")
    images.convert_unrenderable(doc)
    assert doc.images["img1"].mime == "image/x-emf" and doc.images["img1"].converted_from is None
    assert doc.warnings == ["브라우저에서 표시할 수 없는 이미지 형식: chart.emf (image/x-emf)"]


def test_libreoffice_output_is_trimmed():
    # LibreOffice는 그림을 A4 쪽 가운데에 그린다: 흰 여백을 잘라 그림만 남긴다
    page = PILImage.new("RGB", (200, 300), (255, 255, 255))
    page.paste((0, 0, 0), (90, 140, 110, 160))
    buf = io.BytesIO()
    page.save(buf, "PNG")
    trimmed = PILImage.open(io.BytesIO(images._trim(buf.getvalue())))
    assert trimmed.size == (20, 20)
