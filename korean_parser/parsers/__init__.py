"""형식별 파서와 단일 입구(parse).

사용하는 쪽은 형식을 몰라도 된다. 새 형식은 _PARSERS에 등록만 하면 된다.
모든 파서는 같은 IR(Document)을 돌려준다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..ir import Document


class DocumentError(ValueError):
    """문서를 열 수 없을 때 (손상, 암호화, 지원하지 않는 형식). 메시지는 사용자에게 그대로 보여줄 수 있다."""


def _docx(path: Path) -> Document:
    from .docx import parse_docx

    return parse_docx(path)


_PARSERS = {
    "docx": _docx,
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


def parse(path: str | Path, korean: bool = True) -> Document:
    """문서를 IR로 변환한다. korean=True면 한국어 처리 레이어(정규화, 개조식 계층)를 적용한다."""
    path = Path(path)
    if _is_ole(path):
        # 암호화된 DOCX도 확장자는 .docx지만 실제로는 OLE 파일이다
        raise DocumentError(f"{path.name}: 암호화된 문서이거나 구형 바이너리 형식(.doc/.hwp)입니다")
    fmt = detect_format(path)
    if fmt not in _PARSERS:
        raise DocumentError(
            f"{path.name}: 지원하지 않는 형식입니다 (감지: {fmt or '알 수 없음'}, 지원: {', '.join(supported_formats())})"
        )
    try:
        doc = _PARSERS[fmt](path)
    except zipfile.BadZipFile as exc:
        raise DocumentError(f"{path.name}: 손상된 파일입니다 ({exc})") from exc
    if korean:
        from ..enrich import enrich_korean

        enrich_korean(doc)
    return doc
