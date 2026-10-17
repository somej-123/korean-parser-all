#!/usr/bin/env bash
# 웹에서 공개 라이선스 문서 샘플을 받는다. 저장소에는 넣지 않는다 (용량, 출처 관리).
#  - Apache POI 테스트 문서 (Apache-2.0): DOCX 130여 개. 표, 병합, 번호, 도형, 각주 등
#  - OpenOffice 개조식 보고서 템플릿 (BSD): 다단계 번호 체계 (LibreOffice로 DOCX 변환)
#  - hwpxlib 테스트 문서 (Apache-2.0): HWPX 40여 개. 도형, 표, 수식, 개요 번호, 실제 공공기관 문서 포함
#  - hwplib 시험 문서 (Apache-2.0): HWP 40여 개. 차트, 덧말, 글자 겹침, 바탕쪽, 숨은 설명, 캡션 등
set -euo pipefail
DEST="${1:-samples_web}"
mkdir -p "$DEST/poi" "$DEST/openoffice" "$DEST/hwpxlib" "$DEST/hwplib"

if [ ! -d "$DEST/.poi" ]; then
  git clone -q --filter=blob:none --no-checkout --depth 1 https://github.com/apache/poi "$DEST/.poi"
  git -C "$DEST/.poi" sparse-checkout set --no-cone 'test-data/document/*.docx'
  git -C "$DEST/.poi" checkout -q
fi
cp "$DEST"/.poi/test-data/document/*.docx "$DEST/poi/"

if [ ! -d "$DEST/.hwpxlib" ]; then
  git clone -q --filter=blob:none --no-checkout --depth 1 https://github.com/neolord0/hwpxlib "$DEST/.hwpxlib"
  git -C "$DEST/.hwpxlib" sparse-checkout set --no-cone 'testFile/**/*.hwpx'
  git -C "$DEST/.hwpxlib" checkout -q
fi
# 하위 폴더마다 같은 이름(test.hwpx 등)이 있으므로 경로를 파일 이름에 넣어 덮어쓰지 않게 한다
(cd "$DEST/.hwpxlib/testFile" && find . -name '*.hwpx' | while read -r f; do
  cp "$f" "../../hwpxlib/$(echo "${f#./}" | tr '/' '_')"
done)

if [ ! -d "$DEST/.hwplib" ]; then
  git clone -q --filter=blob:none --no-checkout --depth 1 https://github.com/neolord0/hwplib "$DEST/.hwplib"
  git -C "$DEST/.hwplib" sparse-checkout set --no-cone 'sample_hwp/**/*.hwp'
  git -C "$DEST/.hwplib" checkout -q
fi
(cd "$DEST/.hwplib/sample_hwp" && find . -name '*.hwp' | while read -r f; do
  cp "$f" "../../hwplib/$(echo "${f#./}" | tr '/' '_')"
done)

curl -sSL -o "$DEST/openoffice/itemized.ott" https://templates.openoffice.org/template/download/1791
if command -v soffice >/dev/null; then
  soffice --headless --convert-to docx:"MS Word 2007 XML" --outdir "$DEST/openoffice" "$DEST/openoffice/itemized.ott" >/dev/null
fi
echo "받은 문서: DOCX $(ls "$DEST"/poi/*.docx "$DEST"/openoffice/*.docx 2>/dev/null | wc -l)개, HWPX $(ls "$DEST"/hwpxlib/*.hwpx 2>/dev/null | wc -l)개, HWP $(ls "$DEST"/hwplib/*.hwp 2>/dev/null | wc -l)개 → $DEST"
