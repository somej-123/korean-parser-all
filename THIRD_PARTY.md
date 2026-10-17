# 가져다 쓴 것들의 출처

korean-parser-all이 쓰는 다른 사람의 소프트웨어·모델·자료와 그 라이선스입니다.
이 저장소에는 아래 소프트웨어와 모델이 들어 있지 않습니다. 소프트웨어는 설치할 때 pip이 받아 오고, 모델은 처음 실행할 때
Docling·RapidOCR이 내려받습니다. 함께 묶어 배포한다면 각 라이선스의 조건(저작권 표시, 라이선스 사본 등)을 따라야 합니다.

버전은 2026-10-16에 시험을 통과한 환경 기준입니다. 라이선스는 설치된 패키지 정보(`pip show`)와 모델 설명서에서 확인했습니다.

## 실행에 필요한 것

| 이름 | 쓰는 곳 | 만든 곳 | 라이선스 |
|---|---|---|---|
| [Kiwi / kiwipiepy](https://github.com/bab2min/kiwipiepy) 0.24 (모델 kiwipiepy_model 포함) | 띄어쓰기 복원, 형태소 분석 (`spacing.py`) | bab2min | Apache-2.0 |
| [pydantic](https://github.com/pydantic/pydantic) 2.14 | 문서 모델(IR) 정의·JSON 저장 (`ir.py`) | Pydantic | MIT |
| [lxml](https://lxml.de/) 6.1 | DOCX·HWPX XML 읽기, HWP를 HWPX 모양 XML로 옮기기 | lxml 개발자 | BSD-3-Clause |
| [olefile](https://github.com/decalage2/olefile) 0.47 | HWP 5.0 파일(OLE 복합 파일) 열기 | Philippe Lagadec | BSD (2-Clause) |

## PDF·스캔 PDF (`pip install "korean-parser[pdf]"`)

| 이름 | 쓰는 곳 | 만든 곳 | 라이선스 |
|---|---|---|---|
| [Docling](https://github.com/docling-project/docling) 2.139 (docling-core, docling-parse, docling-ibm-models 포함) | PDF 레이아웃·읽기 순서·표 구조, OCR 실행 | Docling 프로젝트 (IBM Research에서 시작) | MIT |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) 5.14 (안에 Google [PDFium](https://pdfium.googlesource.com/pdfium/) 포함) | PDF 텍스트 레이어·글자 서식·선·쪽 그림 | pypdfium2 팀 / Google | BSD-3-Clause, Apache-2.0 (PDFium: BSD-3-Clause) |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) 3.10 | 스캔 PDF 글자 인식 | RapidAI | Apache-2.0 |
| [ONNX Runtime](https://onnxruntime.ai) 1.31 | RapidOCR 모델 실행 | Microsoft | MIT |
| [PyTorch](https://pytorch.org) 2.14 (CPU) | Docling 레이아웃·표 모델 실행 | PyTorch 재단 | BSD-3-Clause 등 (패키지 정보: Apache-2.0 AND BSD-3-Clause AND MIT …) |
| [Pillow](https://github.com/python-pillow/Pillow) 12.3 | 그림 자르기·PNG 저장, WMF·EMF·TIFF → PNG | Pillow 개발자 | MIT-CMU |
| [NumPy](https://numpy.org) 2.5 | 스캔 쪽 그림의 빈칸 계산 (`ocr_spacing.py`) | NumPy 개발자 | BSD-3-Clause |

Docling은 이 밖에도 Hugging Face transformers·huggingface-hub(Apache-2.0), OpenCV(Apache-2.0) 등을 함께 설치합니다.

### 실행할 때 내려받는 모델

| 모델 | 쓰는 곳 | 받는 곳 | 라이선스 |
|---|---|---|---|
| docling-layout-heron | 쪽 레이아웃 (제목·문단·표·그림 영역) | [huggingface.co/docling-project/docling-layout-heron](https://huggingface.co/docling-project/docling-layout-heron) | Apache-2.0 |
| docling-models (TableFormer) | 선으로 읽지 못한 표의 구조 | [huggingface.co/docling-project/docling-models](https://huggingface.co/docling-project/docling-models) | CDLA-Permissive-2.0 |
| PP-OCRv5 글자 영역 찾기 (ch_PP-OCRv5_det_mobile) | 스캔 쪽에서 글 줄 찾기 | 원 모델 [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), ONNX 변환·배포 RapidAI ([ModelScope RapidAI/RapidOCR](https://www.modelscope.cn/models/RapidAI/RapidOCR)) | Apache-2.0 |
| PP-OCRv5 한국어 글자 인식 (korean_PP-OCRv5_rec_mobile) | 스캔 쪽 한국어 글자 읽기 | 위와 같음 | Apache-2.0 |

## 평가·시험·화면 비교 도구 (`dev`, `eval`, `visual`)

| 이름 | 쓰는 곳 | 라이선스 |
|---|---|---|
| [pytest](https://pytest.org) 9.1 | 시험 | MIT |
| [python-docx](https://github.com/python-openxml/python-docx) 1.2 | 시험용 DOCX·평가용 합성 문서 만들기 | MIT |
| [python-pptx](https://github.com/scanny/python-pptx) 1.0 | 시험용 PPTX 만들기 (`make_office.py`) | MIT |
| [openpyxl](https://foss.heptapod.net/openpyxl/openpyxl) 3.1 | 시험용 XLSX 만들기 (`make_office.py`) | MIT |
| [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) 3.14 | 평가: 글자 오류율·블록 짝짓기 | MIT |
| [APTED](https://github.com/JoaoFelipe/apted) 1.0 | 평가: 표 TEDS의 트리 편집 거리 | MIT |
| [Playwright](https://github.com/microsoft/playwright-python) 1.64 (Chromium) | 화면 비교: HTML을 그림으로 | Apache-2.0 |
| [LibreOffice](https://www.libreoffice.org) (설치 프로그램, pip 아님) | 평가용 DOCX → PDF 변환, DOCX 화면 비교, (있으면) WMF·EMF → PNG | MPL-2.0 |

## 시험 파일

| 파일 | 출처 | 라이선스 |
|---|---|---|
| `tests/fixtures/hwpx/*.hwpx` (rfp_example 제외) | [hwpxlib](https://github.com/neolord0/hwpxlib) `testFile/` | Apache-2.0 |
| `tests/fixtures/hwpx/rfp_example.hwpx` | 과학기술정보통신부·정보통신산업진흥원 「공공SW사업 제안요청서 작성 예시」 | 공공누리 제1유형 (출처표시) |
| `tests/fixtures/korean_report.docx`, `.pdf` | 이 프로젝트가 만듦 (`make_korean_report.py`) | Apache-2.0 (이 프로젝트) |
| `tests/fixtures/footnotes.pdf`, `benchmarks/goal/samples/*` | 이 프로젝트가 만듦 (`make_footnotes.py`, `benchmarks/goal/samples/make_*.py`, LibreOffice·python-docx·한글로 생성) | Apache-2.0 (이 프로젝트) |

자세한 원본 경로는 [tests/fixtures/hwpx/NOTICE.md](tests/fixtures/hwpx/NOTICE.md)에 있습니다.

## 참고한 형식 문서

| 문서 | 쓰는 곳 | 출처 |
|---|---|---|
| 한글 문서 파일 형식 5.0 (revision 1.3) | HWP 파서 (`korean_parser/parsers/hwp/`) | 한글과컴퓨터 공개 문서 ([cdn.hancom.com](https://cdn.hancom.com/link/docs/한글문서파일형식_5.0_revision1.3.pdf)) |
| OWPML (KS X 6101, HWPX) | HWPX 파서 | 한글과컴퓨터 / 국가표준 |
| [LibreOffice](https://github.com/LibreOffice/core) 글꼴 변환 표 (`unotools/source/misc/fontcvt.cxx`) | Wingdings 2·3, MT Extra 글꼴 문자 → 유니코드 대응 (`korean_parser/parsers/docx/symbols.py`). 실제 유니코드 문자로 대응되는 항목만 옮겼다 | The Document Foundation, MPL-2.0 |
| [hwplib](https://github.com/neolord0/hwplib) 읽기 코드 | HWP 그리기 개체 레코드 배치 확인 (공개 문서에 없는 연결선, 문서와 다른 그러데이션 값 크기). 코드는 가져오지 않았다 | neolord0, Apache-2.0 |

본 제품은 한글과컴퓨터의 한글 문서 파일(.hwp) 공개 문서를 참고하여 개발하였습니다.

## 가져다 쓴 방법·지표

코드를 가져온 것은 아니고, 아래 논문·방법을 보고 직접 구현했습니다.

| 방법 | 쓰는 곳 | 출처 |
|---|---|---|
| TEDS (표 트리 편집 거리 유사도) | 평가 지표 (`benchmarks/eval/metrics.py`) | Zhong et al., "Image-based table recognition: data, model, and evaluation", ECCV 2020 (PubTabNet) |
| APTED (트리 편집 거리) | TEDS 계산 (위 apted 패키지) | Pawlik & Augsten, "Tree edit distance: Robust and memory-efficient", Information Systems 2016 |
| 오츠 방법 (두 무리로 가르는 문턱값) | 스캔 쪽 빈칸 폭으로 공백 기준 정하기 (`ocr_spacing.py`) | Otsu, "A Threshold Selection Method from Gray-Level Histograms", IEEE TSMC 1979 |
| 켄달 타우 (순위 상관) | 평가: 읽기 순서 | Kendall, "A New Measure of Rank Correlation", Biometrika 1938 |
