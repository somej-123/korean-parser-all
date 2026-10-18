# 다른 파서와 비교

같은 문서를 여러 파서로 변환하고, 우리 파서를 재는 것과 같은 기준으로 잰다.

| 파서 | 버전 | 라이선스 | 형식 | 설치 |
|---|---|---|---|---|
| korean-parser (이 저장소) | | Apache-2.0 | 모두 | 프로젝트 환경 |
| [Docling](https://github.com/docling-project/docling) | 2.139 | MIT | PDF, DOCX, PPTX, XLSX | 프로젝트 환경 (기본 설정, 표 구조 인식 켬) |
| [kordoc](https://github.com/chrisryugj/kordoc) | 4.22.5 | MIT | HWP, HWPX, PDF, DOCX, PPTX, XLSX | `npm install kordoc` (이 폴더) |
| [MarkItDown](https://github.com/microsoft/markitdown) | 0.1.8 | MIT | PDF, DOCX, PPTX, XLSX | 비교용 환경 |
| [PyMuPDF4LLM](https://github.com/pymupdf/pymupdf4llm) | 1.28.2 | AGPL-3.0 | PDF | 비교용 환경 |
| [opendataloader-pdf](https://github.com/opendataloader-project/opendataloader-pdf) | 2.5.13 | Apache-2.0 | PDF | 비교용 환경 (Java 11 이상) |
| [hwp-hwpx-parser](https://pypi.org/project/hwp-hwpx-parser/) | 1.0.0 | Apache-2.0 | HWP, HWPX | 비교용 환경 |

다른 파서는 프로젝트 환경과 섞이지 않게 비교용 가상 환경(`.venv-compare`)과 이 폴더의 `node_modules`에 설치한다.
둘 다 저장소에 넣지 않는다. 다른 파서는 기본 설정으로 돌린다 (kordoc만 스캔 PDF에 `--ocr`).

```bash
python -m venv .venv-compare
.venv-compare/Scripts/python -m pip install "markitdown[all]" pymupdf4llm opendataloader-pdf hwp-hwpx-parser
(cd benchmarks/compare && npm install kordoc@4.22.5)

# 파일 목록 (정답 세트 + v1.0 기준 문서 묶음)은 score.py가 쓰는 out/files.txt. 스캔은 줄 끝에 '|ocr'
.venv-compare/Scripts/python benchmarks/compare/run_tools.py --list benchmarks/compare/out/files.txt --out benchmarks/compare/out
python benchmarks/compare/score.py --out benchmarks/compare/report.md      # 우리 파서·Docling을 돌리고 모두 채점
```

## 재는 것

1. **정답 세트** (`benchmarks/eval/data`: 합성 제안요청서 PDF·DOCX 12개씩, 스캔 PDF 4개): 글자 오류율, 어절 F1, 표 TEDS,
   제목 재현율, 읽기 순서 (`benchmarks/eval/metrics.py`). 다른 파서의 마크다운은 `#` 줄을 제목, `|` 표와 HTML `<table>`을
   표(HTML 표는 병합 칸까지), 나머지를 문단으로 나눈다. 번호 목록('1. 산출물 제출')으로 낸 제목은 제목으로 세지 않는다.
2. **기준 문서 묶음** (`benchmarks/goal/check.py`의 묶음, 264개): 원문 글자 보존율. 원문 글자는 v1.0 기준 1과 같은
   방법으로 파서와 따로 읽는다 (되풀이되는 머리말·꼬리말은 한 번). 다른 파서의 결과에서는 그림 주소(base64 포함),
   HTML 태그, 마크다운 기호를 빼고 센다. 보존율은 원문 글자가 결과에 남은 비율이라, 글이 빠진 것만 잡고 순서·구조는
   보지 않는다.

시간은 파일마다 변환에 걸린 시간이다 (kordoc·opendataloader-pdf는 파일마다 프로그램을 새로 띄우므로 그 시간이 들어간다).

## 주의

- 정답 세트는 이 저장소가 만든 합성 문서다. 우리 파서는 이 세트로 고쳐 왔으므로 우리에게 유리하다.
  기준 문서 묶음의 보존율이 더 공정한 비교다.
- 기준 문서 묶음의 실제 문서(`samples_rfp/`)는 저장소에 없다. 보고서는 숫자만 담는다.

## 결과 (2026-10-18, `report.md`)

- 정답 세트 PDF: 우리 파서가 모든 지표에서 가장 높다 (글자 오류율 0.0%, 표 TEDS 0.993). 다음은 kordoc(오류율 0.4%,
  표 0.858)과 Docling(표 0.922). 스캔 PDF는 OCR을 하는 파서가 우리, Docling, kordoc뿐이고, 어절 F1이 0.955 · 0.309 · 0.321로
  차이가 크다 (다른 둘은 낱말 사이 공백을 거의 살리지 못한다).
- 정답 세트 DOCX: 모든 파서가 글은 거의 그대로 읽는다. 제목 재현율 0.500은 정답 문서 제목의 절반이 제목 스타일 없이
  글자 크기·굵기로만 만든 제목이어서다 (우리 파서는 추정 제목까지 넣으면 0.961, `docs/GOAL.md` 기준 2).
- 기준 문서 보존율: HWP·HWPX·PDF·DOCX·PPTX는 우리 파서가 0.999~1.000으로 가장 높고 실패가 없다. 다른 파서가 크게
  잃은 곳은 그림 안 글(도식 PDF: Docling 0.06, kordoc·MarkItDown·PyMuPDF4LLM 0.02), 글자 겹침·덧말(HWP), 변환 실패였다.
  XLSX만 우리가 낮다 (0.938): 82만 칸 원자료를 칸 수 제한으로 앞부분만 넣었기 때문이다 (다른 파서는 모두 읽는다).
- 속도(중앙값): HWP·HWPX·DOCX는 우리가 가장 빠르다 (0.01초). PDF는 MarkItDown(0.11초)이 가장 빠르고 우리는 0.84초.

이 비교는 우리에게 유리한 쪽으로 치우쳐 있다. 정답 세트는 우리가 만들었고, 기준 문서 묶음의 문서로 우리 파서를 고쳐
왔으며, 보존율도 우리 v1.0 기준과 같은 방법으로 잰다. 다른 파서의 마크다운을 블록으로 나누는 방법(위 '재는 것')도
그 파서의 장점(예: kordoc의 번호 목록, 청크 출력)을 다 살리지 못한다. 처음 보는 문서 묶음으로 다시 재야 공정하다.
