"""형식별 파서와 단일 입구(parse).

사용하는 쪽은 형식을 몰라도 된다. 새 형식은 _PARSERS에 등록만 하면 된다.
모든 파서는 같은 IR(Document)을 돌려준다.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from ..ir import Document


class DocumentError(ValueError):
    """문서를 열 수 없을 때 (손상, 암호화, 지원하지 않는 형식). 메시지는 사용자에게 그대로 보여줄 수 있다."""


def _docx(path: Path) -> Document:
    from .docx import parse_docx

    return parse_docx(path)


def _hwpx(path: Path) -> Document:
    from .hwpx import parse_hwpx

    return parse_hwpx(path)


def _hwp(path: Path) -> Document:
    from .hwp import HwpError, parse_hwp

    try:
        return parse_hwp(path)
    except HwpError as exc:
        raise DocumentError(str(exc)) from exc


def _pptx(path: Path) -> Document:
    from .pptx import parse_pptx

    return parse_pptx(path)


def _xlsx(path: Path) -> Document:
    from .xlsx import parse_xlsx

    return parse_xlsx(path)


def _pdf(path: Path) -> Document:
    from .pdf import PdfEngineMissing, parse_pdf

    try:
        return parse_pdf(path)
    except PdfEngineMissing as exc:
        raise DocumentError(str(exc)) from exc


_PARSERS = {
    "docx": _docx,
    "hwp": _hwp,
    "hwpx": _hwpx,
    "pdf": _pdf,
    "pptx": _pptx,
    "xlsx": _xlsx,
}


def detect_format(path: Path) -> str | None:
    """확장자를 우선하되, 확장자가 없거나 틀린 경우 내용으로 판별한다."""
    ext = path.suffix.lower().lstrip(".")
    if ext in _PARSERS:
        return ext
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return None
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "word/document.xml" in names:
            return "docx"
        if "Contents/content.hpf" in names:
            return "hwpx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "xl/workbook.xml" in names:
            return "xlsx"
    if head.startswith(b"%PDF"):
        return "pdf"
    return None


def _is_ole(path: Path) -> bool:
    """OLE 복합 파일 (암호화된 Office 문서, .doc, .hwp 5.0)."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    except OSError:
        return False


def supported_formats() -> list[str]:
    return sorted(_PARSERS)


def parse(path: str | Path, korean: bool = True, convert_images: bool = True) -> Document:
    """문서를 IR로 변환한다. korean=True면 한국어 처리 레이어(정규화, 개조식 계층)를 적용한다.

    convert_images=True면 브라우저가 못 그리는 그림(WMF·EMF·TIFF)을 PNG로 바꿔 넣는다 (images.py).
    """
    path = Path(path)
    if _is_ole(path):
        from .hwp import is_hwp

        if not is_hwp(path):
            # 암호화된 DOCX도 확장자는 .docx지만 실제로는 OLE 파일이다
            raise DocumentError(f"{path.name}: 암호화된 문서이거나 구형 바이너리 형식(.doc)입니다")
        fmt = "hwp"
    else:
        fmt = detect_format(path)
    if fmt not in _PARSERS:
        raise DocumentError(
            f"{path.name}: 지원하지 않는 형식입니다 (감지: {fmt or '알 수 없음'}, 지원: {', '.join(supported_formats())})"
        )
    try:
        doc = _PARSERS[fmt](path)
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{path.name}: 손상된 파일입니다 ({exc})") from exc
    _stamp_source(doc, path)
    if convert_images:
        from ..images import convert_unrenderable

        convert_unrenderable(doc)
    if korean:
        from ..enrich import enrich_korean

        enrich_korean(doc)
    return doc


def _stamp_source(doc: Document, path: Path) -> None:
    """원본 파일 해시·크기와 파서 버전을 문서 정보에 남긴다 (모든 형식 공통)."""
    from .. import __version__

    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    m = doc.meta
    m.source_sha256 = h.hexdigest()
    m.source_size_bytes = path.stat().st_size
    m.parser_version = __version__
