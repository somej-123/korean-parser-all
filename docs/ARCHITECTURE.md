# 아키텍처

## 목표

1. **모든 형식을 하나의 입구로**: HWP, HWPX, DOCX, PPTX, XLSX, PDF, 스캔 이미지 → 하나의 결과 형식
2. **한국어 특화**: 한글 문서 형식, 개조식·번호 체계, 띄어쓰기, 한국어 OCR
3. **후처리 없는 HTML**: Markdown을 거치지 않고, 파싱 결과가 바로 원본처럼 보이는 화면
4. **빠짐없는 파싱**: 텍스트, 표(병합·서식), 도식(도형·연결선), 차트, 이미지, 각주, 머리말

## 구조

```
입력 (DOCX ✅, HWPX, HWP, PPTX, XLSX, PDF, 이미지)
  │
  ▼
parsers.parse(path)                 ← 단일 입구. 형식 판별 후 형식별 파서 호출
  │                                    손상·암호화 파일은 DocumentError(사용자용 메시지)
  ├─ parsers/docx/   XML 규칙 파싱 (AI 없음)
  ├─ parsers/hwpx/   (예정)
  └─ parsers/pdf/    (예정: Docling 레이아웃·표 + OCR → IR 어댑터)
  │
  ▼
ir.Document                         ← 통합 문서 모델. 모든 파서의 공통 계약
  │
  ▼
enrich.enrich_korean(doc)           ← 한국어 처리 레이어 (정규화, 개조식 계층)
  │                                    PDF/OCR이면 spacing(띄어쓰기 복원)도 적용
  ▼
render/
  ├─ html.py   화면용 HTML (서식, 병합, 테두리, 이미지, SVG 도식·차트, 떠 있는 개체)
  ├─ llm.py    LLM 입력용 텍스트 (Markdown + 병합 표는 HTML + 도식은 연결 관계)
  └─ JSON      Document.model_dump_json() 그대로
```

**밖에서는 하나의 입구, 안에서는 형식별 분리.** 사용하는 쪽은 형식을 몰라도 되고, 형식별 파서는
서로를 모른 채 "IR로 돌려준다"는 약속만 지킨다. PDF 엔진을 바꿔도 HTML·LLM 출력 코드는 그대로다.

## 통합 문서 모델 (IR)

`korean_parser/ir.py`. pydantic 모델이라 JSON으로 저장·복원할 수 있다.

| 요소 | 담는 정보 |
|---|---|
| `Paragraph`, `Heading` | 서식 있는 글 조각(`Text`), 문단 서식(정렬·들여쓰기·간격·테두리), 자동 번호(`ListInfo`, 화면 표시 그대로 계산한 "가." 등), 개조식 계층(`OutlineInfo`) |
| `Table` | 행·셀, `row_span`/`col_span`, 셀 배경·테두리·세로 정렬, 열 너비, 셀 안의 블록(중첩 표 포함) |
| `Drawing` | 도형(`Shape`: 모양, 좌표, 회전, 채우기, 선, 화살표, 연결 대상, 글상자), 떠 있는 배치(`Anchor`) |
| `Chart` | 종류, 방향, 묶음/누적, 범주, 계열 값과 색 |
| `Figure`, `ImageRef` | 이미지(원본 바이트를 base64로 보관), 크기, 대체 텍스트 |
| `Note` | 각주·미주 |
| `Provenance` | 어떤 파서가 원본의 어디(페이지, 좌표)에서 만들었는지, 신뢰도 |

길이는 모두 pt, 색은 `#RRGGBB`. 모든 블록에 `id`가 있어 LLM 답변의 근거를 화면 위치로 되짚을 수 있다
(HTML의 `data-kp-id`, LLM 텍스트의 `<!-- b12 -->`).

`Provenance`는 나중에 같은 문서를 여러 파서로 처리하고 요소별로 더 나은 결과를 고를 때(예: PDF 텍스트는
텍스트 레이어, 표는 표 인식 모델) 근거로 쓴다.

## HTML 렌더링 원칙

- 문서 모델에서 바로 만든다. Markdown을 거치지 않으므로 병합 셀, 테두리, 색, 도형이 사라지지 않는다.
- 한 파일로 완결: CSS와 이미지(data URI)를 모두 안에 넣는다. 외부 요청 없음.
- 원문 텍스트는 모두 이스케이프하고, 링크는 `http(s)`, `mailto`, `#`만 허용한다. 색과 글꼴 이름은
  정규식으로 검증한 값만 CSS에 넣는다.
- 한국어 줄바꿈은 어절 단위(`word-break: keep-all`).
- Word의 문단 간격은 앞뒤를 더하므로 CSS margin 대신 padding을 쓴다.
- 도형은 SVG, 도형 안 글은 `foreignObject`의 HTML(한국어 자동 줄바꿈 때문).
- 떠 있는 개체: 글이 옆으로 흐르면 `float`, 글과 겹치면 페이지 기준 `position:absolute`.

## 검증 방법

| 도구 | 확인하는 것 |
|---|---|
| `pytest` | 파서·렌더러 단위 동작 (`tests/`) |
| `benchmarks/docx-corpus/` | 웹에서 받은 DOCX 130여 개를 일괄 처리해 실패·경고·시간 집계 |
| `benchmarks/visual-compare/` | 원본(LibreOffice 렌더링)과 우리 HTML(Chromium 캡처)을 나란히 비교 |
| `benchmarks/korean-postprocess/` | PDF 파싱 결과의 띄어쓰기 정확도 (DOCX 원본을 정답으로) |

## 단계

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | CPU 벤치마크, 한국어 후처리 v0 | 완료 |
| 1 | IR + HTML 렌더러 + LLM 렌더러 + DOCX 파서 | 완료 (v0.2) |
| 2 | HWPX 파서 (개조식, 표 병합, 도형 → SVG) | 다음 |
| 3 | PDF: Docling 결과 → IR 어댑터, 벡터 도식 SVG 추출 | |
| 4 | 정답 세트와 요소별 평가 지표 (CER, TEDS, 읽기 순서) | |
| 5 | HWP 5.0, PPTX, XLSX | |
