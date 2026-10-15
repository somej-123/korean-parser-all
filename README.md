# korean-parser-all

한국어와 한글(HWP/HWPX) 문서에 최적화된 문서 파싱 프로젝트입니다.

원본 문서의 구조(제목 계층, 셀 병합 표, 이미지, 위치 정보)를 보존한 **중간 문서 모델**을 만들고,
여기서 **화면 표시용 HTML**과 **LLM용 텍스트**를 각각 생성하는 것을 목표로 합니다.

## 구성

| 경로 | 내용 | 상태 |
|---|---|---|
| [`korean_parser/`](korean_parser/) | 한국어 후처리 모듈: 정규화, 띄어쓰기 복원, 줄 합치기, 개조식 계층 인식 | 프로토타입 |
| [`benchmarks/korean-postprocess/`](benchmarks/korean-postprocess/) | 후처리 전후 띄어쓰기 정확도 평가 | 사용 가능 |
| [`benchmarks/docling-cpu/`](benchmarks/docling-cpu/) | Docling을 CPU에서 설정별로 돌려 속도와 결과를 비교하는 벤치마크 | 사용 가능 |

## 한국어 후처리 모듈

```bash
pip install -e ".[dev]"          # 후처리 모듈만
pip install -e ".[docling,dev]"  # Docling 연동까지
pytest
```

| 모듈 | 기능 |
|---|---|
| `normalize` | NFD 자모 분리 → NFC, 보이지 않는 문자·특수 공백 정리, 가운뎃점 통일, 전각 영숫자 → 반각, PUA(사용자 정의 문자) 매핑, 글자 깨짐 감지 |
| `spacing` | 띄어쓰기 소실 감지, Kiwi 기반 띄어쓰기 복원(과잉 분리 방지 규칙 포함), 줄 합치기, 문장부호 뒤 공백 보정 |
| `outline` | `□ ○ -`, `1. 가. 1) 가)` 등 개조식·번호 체계 계층 인식 (문서에 나온 순서로 단계 결정) |
| `docling_adapter` | Docling 변환 결과의 본문과 표 셀에 후처리 적용 |

```python
from korean_parser import SpacingFixer, normalize_text, detect_outline, outline_to_markdown

fixer = SpacingFixer()
fixer.respace("본문서는 2026 년도사업추진방향과예산계획을설명합니다.", reset=True)
# '본 문서는 2026년도 사업 추진 방향과 예산 계획을 설명합니다.'

fixer.join_lines(["담당 부서는 진행 상", "황을 매주 보고합니다."])
# '담당 부서는 진행 상황을 매주 보고합니다.'

print(outline_to_markdown(detect_outline(["□ 추진 배경", "○ 품질 문제", "- 표 구조 손실"])))
# - □ 추진 배경
#   - ○ 품질 문제
#     - - 표 구조 손실
```

Docling과 함께 쓰기:

```python
from docling.document_converter import DocumentConverter
from korean_parser.docling_adapter import postprocess_docling

doc = DocumentConverter().convert("문서.pdf").document
report = postprocess_docling(doc)  # 본문과 표 셀을 직접 수정, 원문은 각 항목의 orig에 남음
```

샘플 PDF 기준 띄어쓰기 F1이 디지털 PDF는 0.459 → 0.991, 스캔 PDF(OCR)는 0.866 → 0.967로 개선됐습니다. 자세한 내용은 [`benchmarks/korean-postprocess/`](benchmarks/korean-postprocess/)를 참고하세요.

형태소 분석기 [Kiwi](https://github.com/bab2min/Kiwi)(kiwipiepy, Apache-2.0)를 사용합니다.

## 방향

```
각종 파서 (HWP, HWPX, DOCX, PDF/Docling)
        ↓  원문 + 좌표
한국어 처리 레이어
  - 유니코드 정규화(NFC), 기호 통일, HWP 고유 문자 변환
  - PDF 글자 깨짐 감지 → OCR 재처리
  - 줄 합치기, 띄어쓰기 복원
  - 개조식(□ ○ -) · 번호 체계(1. 가. 1) 가)) 계층 인식
  - 문장 분리
        ↓
중간 문서 모델 → 화면용 HTML / LLM용 텍스트 / 검색 인덱스
```

- DOCX/HWPX/PPTX/XLSX처럼 구조가 XML에 있는 형식은 AI 없이 규칙 기반으로 파싱합니다.
- AI 모델은 PDF의 레이아웃·표 인식과 스캔 문서 OCR에만 사용하며, CPU에서 동작하는 것을 기준으로 합니다.

## 라이선스

[MIT](LICENSE)
