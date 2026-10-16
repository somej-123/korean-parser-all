# 작업 인계 (2026-10-16, v0.7.0)

클라우드 세션에서 하던 작업을 로컬 PC로 옮기며 남기는 기록이다. 프로젝트 규칙은 루트의 `CLAUDE.md`에 있다.

## 1. 지금까지 한 일

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | Docling CPU 벤치마크, 한국어 후처리(정규화·띄어쓰기·개조식) | 완료 |
| 1 | 통합 문서 모델(IR), DOCX 파서, HTML·LLM 렌더러 | 완료 |
| 2 | HWPX 파서 (개요 번호, 표 병합, 도형·연결선 → SVG, 쪽 배경) | 완료 |
| 3 | PDF: Docling + 텍스트 레이어로 글·띄어쓰기·서식 복원, 괘선 표, 고정 배치 HTML, 끊긴 문단·표 잇기 | 완료 |
| 3.5 | 구조 기반 청크 도구 (`korean_parser/chunk.py`) | 완료 |
| 4 | 정답 세트와 요소별 평가 (`benchmarks/eval/`) | 완료 |
| — | 범위 정리(`docs/SCOPE.md`), 메타데이터(해시·파서 버전·쪽 수), Apache-2.0 + NOTICE | 완료 |

## 2. 현재 품질 (benchmarks/eval, 합성 제안요청서 12개)

| 대상 | 글자 오류율 | 어절 F1 | 읽기 순서 | 제목 재현율 | 표 TEDS |
|---|---:|---:|---:|---:|---:|
| DOCX | 0.0% | 1.000 | 1.000 | 0.500 | 1.000 |
| PDF | 1.2% | 0.989 | 1.000 | 0.985 | 0.966 |
| 스캔 PDF (OCR) | 14.3% | 0.581 | 1.000 | 0.891 | 0.802 |
| (비교) Docling 기본 PDF | 3.4% | 0.367 | 0.999 | 0.985 | 0.903 |

HWPX 22개: 한컴오피스 미리보기 글 재현율 0.980. 실제 제안요청서 7종(나라장터 PDF 등) 모두 처리, 글자 보존 0.998~1.000.

## 3. 다음 작업 (우선순위 순)

### 3-1. 스캔 PDF 띄어쓰기 복원 (어절 F1 0.58 → 목표 0.85 이상)

조사한 것:
- OCR 결과는 공백이 대부분 빠진다: `1.산출물제출`, `계약일로 부터8개월`, `클라우드(CSAP인증)기반으로 구성하고재해`.
- 그런데 `PdfParser.spacing_reset`이 **False**로 나온다. `docling_adapter.postprocess_docling`이 `spacing_lost()`로
  '띄어쓰기 소실'을 판단하는데 OCR 글에서 소실로 판정하지 못해, 빠진 공백만 조금 채우고(reset=False) 끝난다.
- 글자 자체의 OCR 오류(`인식를`, `함목은`, `○`→`O`)도 섞여 있다. 이것은 띄어쓰기와 별개다.

해 볼 것:
1. OCR 경로(`self.ocr`)에서는 판단 없이 항상 `reset=True`로 다시 띄우기 (OCR 공백은 믿을 수 없다). 또는 `spacing_lost` 기준을
   OCR에 맞게 조정.
2. OCR이 줄 단위로 준 글을 합칠 때 `SpacingFixer.join_lines`를 쓰는지 확인.
3. `○`를 `O`로 읽는 경우: 줄 맨 앞의 단독 `O`/`0` + 공백을 `○`로 바꾸는 규칙 (개조식 문서에 흔함). 단, 영문 문장 앞의 O는 제외.
4. 확인: `cd benchmarks/eval && python evaluate.py data --formats scan` (스캔 4개, 몇 분)

### 3-2. 위아래 선만 있는 표 (열린 표)

- 칸마다 선이 있는 표는 선으로 읽어 정확하다(TEDS 1.0 수준). 위아래 선만 있는 표는 Docling 표 구조 모델(TableFormer)에
  맡기는데, **줄이 넘어간 칸을 다른 행으로 나눈다** (`'가 90%, 가격평'`, `'10%로 함'`이 별도 행).
- 이런 표가 쪽을 넘어가면 다음 쪽의 나머지는 선이 전혀 없어 표로 찾지 못한다 (synth_11의 8행 표, 글자 오류율 10%의 원인).
- 해 볼 것: 표 영역의 글자 위치로 열(세로 빈 띠)과 행(첫 열 글 시작, 줄 간격)을 정하는 '글 정렬 기반' 격자.
  `korean_parser/parsers/pdf/rules.py`의 격자 구조(`Grid`, `GridCell`)를 그대로 쓰면 `_ruled_table`로 IR을 만들 수 있다.
- 확인: `synth.py`가 표의 20%를 열린 표로 만든다 (`truth.json`의 `"ruled": false`). 표별 TEDS를 따로 보려면
  `metrics.teds()`를 열린 표만 골라 돌린다.

### 3-3. 그 밖의 후보

- DOCX·HWPX에서 '굵고 큰 번호 문단'으로 쓴 제목: 지금은 문단으로 둔다(원본에 제목 정보가 없음). 청크 도구는 이미 제목
  경로로 인식한다. IR에서 제목으로 바꿀지는 범위 판단이 필요하다 (원문에 없는 해석을 넣는 것인지).
- HWPX: 문단 중간에 놓인 글상자 글이 문단 앞에 나온다 (미리보기와 순서 차이).
- PDF 표: 점선·선 굵기를 실선으로 그림. 굵은 글꼴 정보가 없는 PDF의 머리 행 굵게.
- 5단계: HWP 5.0(바이너리), PPTX, XLSX 파서. EMF/WMF 그림 → PNG 변환. Office 2016 신형 차트(chartex).

## 4. 저장소 밖에 있던 것

- 실제 제안요청서 샘플(나라장터 첨부 PDF, 병원·KIND·국토교통 PDF): 재배포 조건이 불분명해 저장소에 넣지 않았다.
  로컬에서 다시 모을 때는 공개된 공고 첨부파일을 받아 `samples_rfp/` 폴더에 둔다 (.gitignore에 들어 있어 커밋되지 않는다).
- 웹 샘플 코퍼스: `benchmarks/corpus/fetch_samples.sh samples_web` 로 다시 받는다 (POI·hwpxlib·OpenOffice).
- 나라장터 API 수집(`benchmarks/corpus/fetch_g2b.py`): 공공데이터포털 서비스 키 필요(`G2B_SERVICE_KEY` 환경 변수).
  키는 채팅·저장소에 넣지 말 것. 아직 실제 호출로 검증하지 못했다.
- 평가용 합성 문서(`benchmarks/eval/data/`)는 `synth.py`로 언제든 다시 만든다 (저장소에 넣지 않음).

## 5. 검증된 개발 환경 (클라우드)

Python 3.13, docling 2.139.0, docling-core 2.101.1, docling-ibm-models 4.0.3, pypdfium2 5.14.0, kiwipiepy 0.24.0,
rapidocr 3.10.0, onnxruntime 1.31.0, torch 2.14.1(CPU), pydantic 2.14.1, lxml 6.1.3, rapidfuzz 3.14.6, apted 1.0.3,
python-docx 1.2.0, playwright 1.64.0, LibreOffice 24.2. 시험 103개 통과.

로컬에서 버전이 달라 결과가 다르면, 위 버전으로 맞춰 보고 차이를 확인한다 (특히 docling은 출력 형식이 자주 바뀐다).
