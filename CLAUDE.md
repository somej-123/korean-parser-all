# korean-parser-all — 작업 안내 (Claude Code용)

한국어·한글 문서 파서(엔진). 모든 형식을 `parse()` 하나로 받아 통합 문서 모델(IR)로 만들고, 그 모델에서 바로
HTML(원본 모양)·LLM 텍스트·JSON·검색용 조각을 만든다. 저작권자: 우정호(woojungho). 라이선스: Apache-2.0 (+ NOTICE).

먼저 읽을 것: `docs/HANDOFF.md`(현재 상태와 다음 작업), `docs/SCOPE.md`(범위), `docs/ARCHITECTURE.md`(설계와 형식별 주의점).

## 범위 (반드시 지킬 것)

- 이 프로젝트는 **파서까지만** 맡는다. 원문에 있는 것을 빠짐없이, 원문에 없는 것은 넣지 않고, 모든 블록의 출처(id·쪽·좌표)를 남긴다.
- 분류·임베딩·검색·정보 추출·요약·AI 그림 설명은 **후속 프로젝트** 몫이다. 여기에 넣지 않는다.
- 띄어쓰기 교정 같은 '원문 복원'은 원문이 손상된 경우(PDF·OCR)에만 한다. DOCX·HWPX 원문은 고치지 않는다.

## 쓰는 쪽과의 약속

- `ir.IR_VERSION`(`Document.schema_version`): 필드 추가는 두 번째 자리, 이름·의미 변경은 첫 자리를 올린다.
- 결정성: 같은 파일 → 블록 id·좌표까지 같은 결과 (`tests/test_contract.py`). 파싱 시각 같은 값은 넣지 않는다.
- 패키지 버전은 `korean_parser/__init__.py`의 `__version__` 하나 (pyproject가 읽어 간다).

## 개발 환경

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev,pdf,eval,visual]"
python -m pytest -q                                  # PDF 시험은 Docling이 없으면 건너뛴다
                                                     # samples_rfp/에 실제 문서(.hwp .pdf)가 있으면 그것도 시험한다 (test_real_samples.py)
```

- PDF는 Docling 모델을, 스캔 PDF는 RapidOCR 모델을 처음 실행 때 내려받는다 (인터넷 필요).
- `KP_THREADS`: Docling CPU 스레드 수 (기본 CPU 수).
- 평가 문서 만들기(`benchmarks/eval/synth.py`)와 DOCX 시각 비교는 LibreOffice(`soffice`)가 필요하다.
- 시각 비교 화면을 원본과 비슷하게 보려면 한글 글꼴(맑은 고딕, Noto Sans KR 등)이 설치되어 있어야 한다.

## 고친 뒤 확인

```bash
python -m pytest -q
cd benchmarks/eval && python synth.py --out data && python evaluate.py data --out report.md   # 정답 세트 지표
python benchmarks/corpus/run.py <샘플 폴더> --pdf --out report.md                              # 실제 문서 일괄 실행
python benchmarks/visual-compare/compare.py <파일> -o out/ --pages 2                          # 원본 vs HTML 그림
```

지표가 나빠지지 않았는지(`benchmarks/eval/README.md`의 결과표와 비교) 확인하고, 좋아졌으면 그 표를 갱신한다.

## 코드와 문서 관례

- 주석·문서·사용자 메시지는 **쉬운 한국어**. 코드 이름은 영어. 주석은 '왜'를 적는다 (기존 코드의 밀도와 말투를 따른다).
- 길이 단위 pt, 색 `#RRGGBB`. 새 IR 필드는 pydantic 모델(`korean_parser/ir.py`)에 추가하고 기본값을 둔다.
- 실제 문서에서 문제를 찾으면: 원인을 좌표·글자 단위로 확인 → 일반 규칙으로 고침 → 단위 시험 추가 → 문서(ARCHITECTURE의
  형식별 '알아 둘 점')에 한 줄.
- 재배포 조건이 불분명한 외부 문서(나라장터 PDF 등)는 저장소에 넣지 않는다. 넣는 시험 파일은 출처·라이선스를
  `tests/fixtures/**/NOTICE.md`에 적는다.
- 커밋 메시지: 영어 한 줄 요약 + 본문(무엇을 왜, 지표 변화). `main`에 바로 푸시해 왔다.
