"""OOXML 꾸러미(PPTX·XLSX) 공통: zip 안의 XML, 관계(rels), 문서 정보(core.xml).

DOCX 파서는 먼저 만들어져 자체 코드를 쓴다. PPTX·XLSX는 이 도우미를 함께 쓴다.
"""

from __future__ import annotations

import posixpath
import zipfile

from lxml import etree

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, recover=True)
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
CORE = {"dc": "http://purl.org/dc/elements/1.1/", "dcterms": "http://purl.org/dc/terms/",
        "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"}


def ln(el) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


class Package:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())
        self._rels: dict[str, dict[str, tuple[str, str]]] = {}

    def xml(self, name: str):
        if name not in self.names:
            return None
        return etree.fromstring(self.zip.read(name), _XML_PARSER)

    def read(self, name: str) -> bytes | None:
        return self.zip.read(name) if name in self.names else None

    def rels(self, part: str) -> dict[str, tuple[str, str]]:
        """part의 관계: id → (대상 경로, 관계 종류 끝 이름). 대상은 꾸러미 안 절대 경로로 바꾼다."""
        if part in self._rels:
            return self._rels[part]
        base = posixpath.dirname(part)
        rels_path = posixpath.join(base, "_rels", posixpath.basename(part) + ".rels")
        out: dict[str, tuple[str, str]] = {}
        root = self.xml(rels_path)
        if root is not None:
            for r in root:
                if r.get("TargetMode") == "External":
                    continue
                target = r.get("Target", "")
                path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(base, target))
                out[r.get("Id", "")] = (path, r.get("Type", "").rsplit("/", 1)[-1])
        self._rels[part] = out
        return out

    def rel_of_type(self, part: str, kind: str) -> str | None:
        return next((p for p, k in self.rels(part).values() if k == kind), None)

    def core(self) -> dict[str, str]:
        root = self.xml("docProps/core.xml")
        out: dict[str, str] = {}
        if root is None:
            return out
        for key, tag in (("title", "dc:title"), ("author", "dc:creator"), ("created", "dcterms:created"),
                         ("modified", "dcterms:modified"), ("language", "dc:language")):
            prefix, local = tag.split(":")
            el = root.find(f"{{{CORE[prefix]}}}{local}")
            if el is not None and el.text and el.text.strip():
                out[key] = el.text.strip()
        return out

    def close(self) -> None:
        self.zip.close()
