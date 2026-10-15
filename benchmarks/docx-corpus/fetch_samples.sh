#!/usr/bin/env bash
# 웹에서 공개 라이선스 DOCX 샘플을 받는다. 저장소에는 넣지 않는다 (용량, 출처 관리).
#  - Apache POI 테스트 문서 (Apache-2.0): 표, 병합, 번호, 도형, 각주 등 구조 검증용 130여 개
#  - OpenOffice 개조식 보고서 템플릿 (BSD): 다단계 번호 체계 (LibreOffice로 DOCX 변환)
set -euo pipefail
DEST="${1:-samples_web}"
mkdir -p "$DEST/poi" "$DEST/openoffice"

if [ ! -d "$DEST/.poi" ]; then
  git clone -q --filter=blob:none --no-checkout --depth 1 https://github.com/apache/poi "$DEST/.poi"
  git -C "$DEST/.poi" sparse-checkout set --no-cone 'test-data/document/*.docx'
  git -C "$DEST/.poi" checkout -q
fi
cp "$DEST"/.poi/test-data/document/*.docx "$DEST/poi/"

curl -sSL -o "$DEST/openoffice/itemized.ott" https://templates.openoffice.org/template/download/1791
if command -v soffice >/dev/null; then
  soffice --headless --convert-to docx:"MS Word 2007 XML" --outdir "$DEST/openoffice" "$DEST/openoffice/itemized.ott" >/dev/null
fi
echo "받은 DOCX: $(ls "$DEST"/poi/*.docx "$DEST"/openoffice/*.docx 2>/dev/null | wc -l)개 → $DEST"
