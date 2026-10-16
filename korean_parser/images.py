"""브라우저가 그리지 못하는 그림(WMF, EMF, TIFF)을 PNG로 바꾼다.

한글·워드 문서의 로고와 도식은 WMF·EMF(윈도 메타파일)로 들어 있는 경우가 많다. 브라우저는 이 형식을 그리지 못해
HTML에서 그림이 빠졌다. 화면용으로 PNG를 만들어 바꿔 넣고, 원래 형식은 `Image.converted_from`에 남긴다.

바꾸는 방법 (앞의 것이 되면 뒤는 쓰지 않는다)
1. Pillow: TIFF는 어디서나, WMF·EMF는 Windows에서만 (운영체제의 그래픽 엔진으로 그린다)
2. LibreOffice(soffice): 설치되어 있으면 어느 운영체제에서나. 문서 하나의 그림을 한 번에 바꾼다
둘 다 안 되면 원래 그림과 경고를 그대로 둔다.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .ir import Document

UNRENDERABLE = {"image/x-emf", "image/emf", "image/x-wmf", "image/wmf", "image/tiff"}
_EXT = {"image/x-emf": ".emf", "image/emf": ".emf", "image/x-wmf": ".wmf", "image/wmf": ".wmf", "image/tiff": ".tif"}
_WARNING = "브라우저에서 표시할 수 없는 이미지 형식"
DPI = 150  # 메타파일을 그릴 해상도 (화면에서 흐려 보이지 않을 만큼)


def _png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _with_pillow(data: bytes, mime: str) -> bytes | None:
    from PIL import Image

    meta = mime in ("image/x-emf", "image/emf", "image/x-wmf", "image/wmf")
    if meta and sys.platform != "win32":
        return None  # Pillow는 Windows에서만 메타파일을 그린다
    try:
        img = Image.open(io.BytesIO(data))
        if meta:
            img.load(dpi=DPI)
        else:
            img.load()
        if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
            img = img.convert("RGBA")
        return _png(img)
    except Exception:  # noqa: BLE001 - 깨진 그림은 다음 방법(또는 원래 그림)으로
        return None


def _soffice() -> str | None:
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    for base in ("C:/Program Files/LibreOffice", "C:/Program Files (x86)/LibreOffice"):
        exe = Path(base) / "program" / "soffice.exe"
        if exe.exists():
            return str(exe)
    return None


def _with_libreoffice(items: dict[str, tuple[bytes, str]]) -> dict[str, bytes]:
    """{그림 id: (바이트, mime)} → {그림 id: PNG}. LibreOffice를 한 번만 띄운다."""
    soffice = _soffice()
    if not soffice or not items:
        return {}
    out: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as profile:
        src = []
        for key, (data, mime) in items.items():
            path = Path(work) / f"{key}{_EXT.get(mime, '.bin')}"
            path.write_bytes(data)
            src.append(str(path))
        try:
            subprocess.run([soffice, f"-env:UserInstallation={Path(profile).as_uri()}", "--headless",
                            "--convert-to", "png", "--outdir", str(Path(work) / "png"), *src],
                           check=True, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError):
            return {}
        for key in items:
            png = Path(work) / "png" / f"{key}.png"
            if png.exists() and png.stat().st_size:
                out[key] = _trim(png.read_bytes())
    return out


def _trim(png: bytes) -> bytes:
    """LibreOffice는 메타파일을 A4 쪽 가운데에 그린다. 둘레의 흰 여백을 잘라 낸다."""
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGBA")
    ink = img.convert("L").point(lambda v: 255 if v < 250 else 0)
    alpha = img.getchannel("A").point(lambda v: 255 if v > 5 else 0)
    from PIL import ImageChops

    box = ImageChops.multiply(ink, alpha).getbbox()
    return _png(img.crop(box)) if box else png


def convert_unrenderable(doc: Document) -> None:
    """문서의 WMF·EMF·TIFF 그림을 PNG로 바꿔 넣는다. 바꾸지 못한 그림만 경고를 남긴다."""
    todo = {k: im for k, im in doc.images.items() if im.mime in UNRENDERABLE}
    if not todo:
        return
    done: dict[str, bytes] = {}
    rest: dict[str, tuple[bytes, str]] = {}
    for key, im in todo.items():
        data = base64.b64decode(im.data_base64)
        png = _with_pillow(data, im.mime)
        if png:
            done[key] = png
        else:
            rest[key] = (data, im.mime)
    done.update(_with_libreoffice(rest))
    for key, png in done.items():
        im = doc.images[key]
        im.converted_from = im.mime
        im.mime = "image/png"
        im.data_base64 = base64.b64encode(png).decode("ascii")
    # 파서가 남긴 '표시할 수 없음' 경고는 바꾸지 못한 그림 것만 남긴다
    converted = {doc.images[k].name for k in done if doc.images[k].name}
    doc.warnings = [w for w in doc.warnings if not (w.startswith(_WARNING) and any(n in w for n in converted))]
