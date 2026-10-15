# korean-parser-all

한국어와 한글 문서에 특화된 문서 파서입니다.

- **하나의 입구**: 문서 형식에 상관없이 `parse(파일)` 하나로 같은 결과 형식(통합 문서 모델)을 얻습니다.
- **후처리 없는 HTML**: Markdown을 거치지 않고 문서 모델에서 바로 HTML을 만들어, 표 병합·서식·도식·차트가 원본처럼 보입니다.
- **LLM용 텍스트**: 같은 모델에서 LLM이 읽기 좋은 텍스트(병합 표는 HTML, 도식은 연결 관계)를 만듭니다.
- **한국어 특화**: 개조식(□ ○ -)·한글 번호(가. 나.) 계층, 문자 정규화, PDF 띄어쓰기 복원.

![한국어 보고서: 원본(왼쪽)과 HTML 결과(오른쪽)](docs/images/korean_report_compare.png)

## 지원 형식

| 형식 | 상태 | 방식 |
|---|---|---|
| DOCX | ✅ v0.2 | XML 규칙 파싱 (AI 없음) |
| HWPX | 다음 단계 | XML 규칙 파싱 |
| PDF | 후처리만 지원 | Docling + 한국어 후처리 (`docling_adapter`) |
| HWP, PPTX, XLSX | 예정 | |

DOCX에서 읽는 것: 제목 계층, 글자 서식(굵게·색·크기·밑줄·대문자), 문단 서식(정렬·들여쓰기·간격·테두리),
자동 번호(가나다·원문자 등 한국어 형식 포함), 표(가로·세로 병합, 배경, 테두리, 중첩 표),
이미지, 도형·순서도(SVG로 렌더링), 차트(막대·꺾은선·원형 등), 떠 있는 개체 배치,
각주·미주, 머리말·꼬리말, 하이퍼링크, 양식 체크박스, 기호 글꼴.

## 사용법

```bash
pip install -e ".[dev]"
python -m korean_parser convert 문서.docx -o out/      # out/문서.html, 문서.llm.md, 문서.json
```

```python
from korean_parser.parsers import parse
from korean_parser.render.html import render_html
from korean_parser.render.llm import render_llm

doc = parse("보고서.docx")          # 통합 문서 모델 (pydantic, JSON 저장 가능)
html = render_html(doc)             # 한 파일로 완결된 HTML (CSS·이미지 포함)
text = render_llm(doc)              # LLM 입력용 텍스트
```

LLM 출력 예 ([`tests/fixtures/korean_report.docx`](tests/fixtures/)):

```markdown
# 2. 추진 계획  <!-- b12 -->
1. 통합 문서 모델 구축  <!-- b13 -->
  가. DOCX 파서 개발  <!-- b14 -->
    1) 문단과 서식  <!-- b15 -->

<table><tr><td rowspan="2">구분</td><td colspan="3">2026년</td></tr>...</table>  <!-- b22 -->

[도식: 결재 처리 순서도]  <!-- b50 -->
- 신청
- 부서 검토
- 적합?
연결:
- 신청 → 부서 검토
- 적합? → 승인
```

`<!-- b12 -->`는 블록 id입니다. HTML의 `data-kp-id`와 같아서, LLM 답변의 근거를 화면에서 바로 강조할 수 있습니다.

## 구성

| 경로 | 내용 |
|---|---|
| [`korean_parser/ir.py`](korean_parser/ir.py) | 통합 문서 모델 |
| [`korean_parser/parsers/`](korean_parser/parsers/) | 단일 입구 `parse()`와 형식별 파서 (DOCX) |
| [`korean_parser/render/`](korean_parser/render/) | HTML, LLM 텍스트, SVG(도형·차트) 렌더러 |
| [`korean_parser/enrich.py`](korean_parser/enrich.py) | 한국어 처리 레이어를 문서 모델에 적용 |
| `korean_parser/normalize.py`, `spacing.py`, `outline.py` | 문자 정규화, 띄어쓰기 복원, 개조식 계층 인식 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 설계와 단계별 계획 |
| [`benchmarks/visual-compare/`](benchmarks/visual-compare/) | 원본 vs HTML 시각 비교 |
| [`benchmarks/docx-corpus/`](benchmarks/docx-corpus/) | 웹 DOCX 131개 일괄 실행 (정상 문서 115개 전부 성공) |
| [`benchmarks/korean-postprocess/`](benchmarks/korean-postprocess/) | PDF 띄어쓰기 복원 평가 (F1 0.459 → 0.991) |
| [`benchmarks/docling-cpu/`](benchmarks/docling-cpu/) | Docling CPU 속도 벤치마크 |

## 한국어 처리

```python
from korean_parser import SpacingFixer, detect_outline, outline_to_markdown

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

PDF는 Docling으로 변환한 뒤 후처리를 적용할 수 있습니다 (`pip install -e ".[docling]"`).

```python
from docling.document_converter import DocumentConverter
from korean_parser.docling_adapter import postprocess_docling

doc = DocumentConverter().convert("문서.pdf").document
postprocess_docling(doc)
```

형태소 분석기 [Kiwi](https://github.com/bab2min/Kiwi)(kiwipiepy, Apache-2.0)를 사용합니다.

## 알려진 한계 (DOCX)

- 쪽 나눔 없이 한 화면으로 이어서 보여줍니다. 머리말·꼬리말은 첫 페이지 것만 표시합니다.
- EMF/WMF 이미지는 브라우저가 표시하지 못해 회색 상자로 대체합니다.
- 다단(단 나누기), 탭 위치 정렬, 글꼴이 없는 환경에서의 글자 폭 차이는 원본과 다를 수 있습니다.
- Office 2016 신형 차트(chartex)는 아직 읽지 않습니다.
- SmartArt는 Word가 저장해 둔 그리기 결과를 도형으로 읽도록 구현했지만, 테스트 문서가 없어 아직 검증하지 못했습니다.

## 라이선스

[MIT](LICENSE)
